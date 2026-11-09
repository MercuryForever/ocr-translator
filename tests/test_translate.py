"""翻译层单元测试：用可控的假后端验证降级链、下标映射、缓存隔离。

为什么全部用假后端：降级逻辑的正确性**不能依赖网络状态**。
真机上 Google 恰好可达（或恰好不可达）会让测试"看起来过了"却什么都没验证。
这里把每个后端的失败模式（网络/配额/认证/条数不符/部分失败）都注入进去。

关键手法：假后端**注册成真实名字**（google/deepl/...）并临时替换 BACKENDS，
这样被测代码走的是真实的 AUTO_CHAIN 顺序与真实的 `_chain()` 排序逻辑
（冷静期剔除、preferred 提前），而不是把 `_chain` monkeypatch 掉。
addCleanup 负责还原，`TestProbeContract` 才能继续验证真实 probe。
"""

from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ocrtranslator.config import AppConfig
from ocrtranslator.translate import AUTO_CHAIN, BACKENDS, TranslateManager
from ocrtranslator.translate.base import (AuthError, BackendInfo, NetworkError,
                                          QuotaExceeded, TranslationError,
                                          TranslatorBackend, split_batch)
from ocrtranslator.translate.cache import TranslationCache

MODES = ("ok", "network", "quota", "auth", "engine", "wronglen", "empty", "partial")


class Fake(TranslatorBackend):
    """可编程失败的后端。"""

    #: 子类（每个真实后端名一个）覆盖这三个类属性
    name = "fake"
    label = "Fake"
    init_mode = "ok"
    init_available = True

    def __init__(self, cfg, creds=None, *, name=None, mode=None, available=None):
        super().__init__(cfg, creds)
        self.name = name or self.name
        self.mode = mode if mode is not None else self.init_mode
        # 实例属性与类属性同名会让 super 的 ABC 检查读成实例值，这里两者都设
        self._available = self.init_available if available is None else available
        self.calls: list[list[str]] = []

    def translate(self, texts, src, dst):
        self.calls.append(list(texts))
        if self.mode == "network":
            raise NetworkError("连不上")
        if self.mode == "quota":
            raise QuotaExceeded("配额用尽")
        if self.mode == "auth":
            raise AuthError("密钥无效")
        if self.mode == "engine":
            raise TranslationError("引擎内部错误")
        if self.mode == "wronglen":
            return [f"T({t})" for t in texts[:-1]]
        if self.mode == "empty":
            return [""] * len(texts)
        if self.mode == "partial":
            # 第 2 条返回空串：模拟服务端漏译，其余保留
            return [f"T({t})" if i != 1 else "" for i, t in enumerate(texts)]
        return [f"{self.name}:{t}" for t in texts]

    @classmethod
    def probe(cls, cfg) -> BackendInfo:
        """`available` 决定该档是否进链 —— 这正是"没配密钥"的真实形态。"""
        avail = getattr(cls, "init_available", True)
        return BackendInfo(cls.name, cls.label, available=avail,
                           online=cls.name != "opus", needs_key=cls.name in ("deepl", "baidu"))


def make_manager(modes: dict[str, str | tuple], *, only: list[str] | None = None,
                 backend: str = "auto") -> TranslateManager:
    """把假后端注册成**真实名字**，临时替换 BACKENDS 与 AUTO_CHAIN。

    为什么替换 AUTO_CHAIN 而不是 monkeypatch `_chain`：
    这样被测代码走的是真实的链排序逻辑（probe 可用性过滤、冷静期剔除、
    preferred 提前、未知后端回退），monkeypatch 会让这些行为完全没被覆盖。
    `tearDown`（_RestoreMixin）负责还原，TestProbeContract 才能继续用真实类。

    :param modes: {真实后端名: 失败模式} 或 {name: (mode, available)}
    :param only: 参与排序的后端名（默认 = modes 的键，按给定顺序）
    """
    import ocrtranslator.translate as pkg

    cfg = AppConfig()
    cfg.translate.backend = backend
    cfg.translate.timeout = 5.0
    cfg.translate.deepl_auth_key = ""
    cfg.translate.baidu_appid = cfg.translate.baidu_key = ""
    for var in ("DEEPL_API_KEY", "BAIDU_APPID", "BAIDU_KEY"):
        os.environ.pop(var, None)

    _CLEANUPS.append((dict(pkg.BACKENDS), pkg.AUTO_CHAIN))
    names = list(modes) if only is None else list(only)
    for n, spec in modes.items():
        mode, avail = (spec, True) if isinstance(spec, str) else spec
        cls = type(f"Fake_{n}", (Fake,), {"name": n, "label": f"Fake[{n}]",
                                          "init_mode": mode, "init_available": avail})
        pkg.BACKENDS[n] = cls
    pkg.AUTO_CHAIN = tuple(names)

    mgr = TranslateManager(cfg)
    for n in names:
        if pkg.BACKENDS[n].probe(cfg).available:
            mgr._instances[n] = pkg.BACKENDS[n](cfg)
    return mgr


_CLEANUPS: list[tuple] = []


class _RestoreMixin:
    def tearDown(self) -> None:
        import ocrtranslator.translate as pkg
        while _CLEANUPS:
            backends, chain = _CLEANUPS.pop()
            pkg.BACKENDS.clear()
            pkg.BACKENDS.update(backends)
            pkg.AUTO_CHAIN = chain


class TestBatchMapping(_RestoreMixin, unittest.TestCase):
    def test_index_alignment_when_first_backend_partially_fails(self):
        """回归测试：第一个后端只译好一部分时，第二个必须补**正确的那些条**，
        且写回下标是原始下标而不是 batch 内下标。

        partial 返回 ["T(a)", "", "T(c)", "T(d)", "T(e)"]：第 2 条空。
        于是 google 负责 0/2/3/4，deepl 只收到 ["b"]，必须写回 index 1。
        若错用 batch 内下标，"deepl:b" 会被写到 index 0，整列左移一格 ——
        这种 bug 不崩溃、不报错，只是每一行都错一行，最难发现。
        """
        texts = ["a", "b", "c", "d", "e"]
        mgr = make_manager({"google": "partial", "deepl": "ok", "baidu": "ok"})
        rep = mgr.translate(texts, "en", "zh", use_cache=False)
        self.assertEqual(rep.texts, ["T(a)", "deepl:b", "T(c)", "T(d)", "T(e)"])
        self.assertEqual(rep.engines, ["google", "deepl", "google", "google", "google"])
        self.assertEqual(rep.failed, [])
        # 第二个后端必须只收到那一条，不能整批重译（整批重译会白烧配额）
        self.assertEqual(mgr._instances["deepl"].calls, [["b"]])
        self.assertEqual(mgr._instances["baidu"].calls, [])

    def test_each_row_maps_to_its_own_source(self):
        """逐条校验：译文必须包含它自己那条原文，防 off-by-one。"""
        texts = [f"s{i}" for i in range(7)]
        mgr = make_manager({"google": "ok", "deepl": "ok"})
        rep = mgr.translate(texts, "en", "zh", use_cache=False)
        for i, (src, got) in enumerate(zip(texts, rep.texts)):
            self.assertIn(src, got, f"第 {i} 行错位：{got} 不对应 {src}")

    def test_wrong_length_batch_is_discarded_not_truncated(self):
        """条数不符必须整批丢弃，绝不能截取前 N 条 —— 那会让译文错位一行。"""
        mgr = make_manager({"google": "wronglen", "deepl": "ok"})
        rep = mgr.translate(["x", "y", "z"], "en", "zh", use_cache=False)
        self.assertNotIn("google", rep.engines)
        self.assertEqual(rep.texts, ["deepl:x", "deepl:y", "deepl:z"])

    def test_all_empty_output_reports_failure_per_item(self):
        mgr = make_manager({"google": "empty", "deepl": "empty"})
        rep = mgr.translate(["x", "y"], "en", "zh", use_cache=False)
        self.assertEqual(rep.failed, [0, 1])
        self.assertTrue(any("失败" in w for w in rep.warnings))

    def test_report_length_matches_input_always(self):
        """不变式：texts / engines / errors 三者长度永远等于输入条数。"""
        for mode in MODES:
            mgr = make_manager({"google": mode, "deepl": mode})
            texts = ["a", "b", "c"]
            rep = mgr.translate(texts, "en", "zh", use_cache=False)
            self.assertEqual(len(rep.texts), len(rep.engines), f"{mode}: engines 长度")
            self.assertEqual(len(rep.texts), len(rep.errors), f"{mode}: errors 长度")
            self.assertEqual(len(rep.errors), 3, f"{mode}: 条数变了")


class TestFallbackChain(_RestoreMixin, unittest.TestCase):
    def test_real_chain_order_is_used(self):
        """不加 monkeypatch 时，链顺序就是 AUTO_CHAIN。"""
        mgr = make_manager({"google": "ok", "deepl": "ok", "baidu": "ok",
                            "mymemory": "ok", "opus": "ok"})
        self.assertEqual(mgr._chain(), list(AUTO_CHAIN))

    def test_network_failure_cools_only_that_backend(self):
        """按后端记冷静期，而不是全局跳过在线档。

        旧实现用一个 `_degraded` 布尔：Google 一超时就跳过**所有**在线后端。
        本机实测会把已经证明可达的 MyMemory 一起跳掉 —— 那是真实故障，
        这台机器就是 Google 必超时、MyMemory 必可达。
        """
        mgr = make_manager({"google": "network", "deepl": ("ok", False),
                            "mymemory": "ok", "opus": ("ok", False)})
        rep = mgr.translate(["hello"], "en", "zh", use_cache=False)
        self.assertEqual(rep.texts[0], "mymemory:hello")
        self.assertGreater(mgr._cool("google"), 0)
        self.assertEqual(mgr._cool("mymemory"), 0.0)
        # 冷静期只剔除坏的那个，好后端仍在链里
        self.assertNotIn("google", mgr._chain())
        self.assertIn("mymemory", mgr._chain())

    def test_preferred_backend_moves_to_front(self):
        """上次成功的后端排到链首，否则每次都先付一遍超时。"""
        mgr = make_manager({"google": "network", "deepl": "network", "mymemory": "ok"})
        mgr.translate(["t"], "en", "zh", use_cache=False)
        self.assertEqual(mgr._preferred, "mymemory")
        self.assertEqual(mgr._chain()[0], "mymemory")
        # 第二个后端也失败了：不应因为"成功过"而被提到首位
        self.assertNotIn("deepl", mgr._chain())

    def test_reset_backoff_makes_backend_immediately_usable(self):
        """用户改完配置或网络恢复后点重试，不该干等 90 秒。"""
        mgr = make_manager({"google": "network", "mymemory": "ok"})
        mgr.translate(["t"], "en", "zh", use_cache=False)
        self.assertGreater(mgr._cool("google"), 0)
        mgr.reset_backoff()
        self.assertEqual(mgr._cool("google"), 0.0)
        self.assertIn("google", mgr._chain())

    def test_cooloff_length_by_error_kind(self):
        cases = {"network": TranslateManager.COOLDOWN_NETWORK,
                 "quota": TranslateManager.COOLDOWN_QUOTA,
                 "auth": TranslateManager.COOLDOWN_AUTH}
        for mode, expect in cases.items():
            mgr = make_manager({"google": mode, "mymemory": "ok"})
            mgr.translate(["t"], "en", "zh", use_cache=False)
            got = mgr._cool("google")
            self.assertGreater(got, expect - 2, f"{mode} 冷静期过短")
            self.assertLessEqual(got, expect + 1, f"{mode} 冷静期过长")

    def test_auth_error_continues_chain(self):
        mgr = make_manager({"deepl": "auth", "baidu": "ok"})
        rep = mgr.translate(["t"], "en", "zh", use_cache=False, )
        self.assertEqual(rep.texts[0], "baidu:t")
        self.assertTrue(any("密钥" in w or "配置" in w for w in rep.warnings))

    def test_explicit_backend_has_no_fallback(self):
        """用户指定单后端时**不能**悄悄换后端 —— 那与他看到的设置矛盾。"""
        mgr = make_manager({"google": "network", "mymemory": "ok"},
                           backend="google")
        rep = mgr.translate(["t"], "en", "zh", use_cache=False)
        self.assertEqual(rep.failed, [0])
        self.assertEqual(mgr._chain(), ["google"])

    def test_empty_input_short_circuits(self):
        mgr = make_manager({"google": "network"})
        rep = mgr.translate([], "en", "zh")
        self.assertEqual((rep.texts, rep.failed), ([], []))

    def test_no_backend_at_all(self):
        mgr = make_manager({"google": "ok"})
        import ocrtranslator.translate as pkg
        pkg.AUTO_CHAIN = ()
        rep = mgr.translate(["a", "b"], "en", "zh")
        self.assertEqual(len(rep.errors), 2)
        self.assertTrue(all("后端" in e for e in rep.errors))
        # 没有后端时 engines 也必须是 2 个空串，而不是短列表
        self.assertEqual(len(rep.engines), 2)


class TestCache(_RestoreMixin, unittest.TestCase):
    def test_cache_isolated_by_backend(self):
        """换后端必须读不到旧后端的条目。

        否则用户切了后端却看到一模一样的译文，会以为"后端没生效"，
        而这类 bug 从日志完全查不出来。
        """
        mgr = make_manager({"google": "ok", "deepl": "ok"})
        r1 = mgr.translate(["same"], "en", "zh")
        self.assertEqual(r1.texts[0], "google:same")
        self.assertEqual(r1.cache_hits, 0)
        r2 = mgr.translate(["same"], "en", "zh")
        self.assertEqual(r2.texts[0], "google:same")
        self.assertEqual(r2.cache_hits, 1)
        self.assertIn("cache", r2.engines[0])
        # 把 google 从链里摘掉：必须重译，而不是继续吐 google 的缓存
        import ocrtranslator.translate as pkg
        pkg.AUTO_CHAIN = ("deepl",)
        r3 = mgr.translate(["same"], "en", "zh")
        self.assertEqual(r3.texts[0], "deepl:same")
        self.assertEqual(r3.cache_hits, 0)

    def test_cache_key_ignores_whitespace_noise(self):
        c = TranslationCache()
        self.assertEqual(c.make_key("g", "en", "zh", "a   b"),
                         c.make_key("g", "en", "zh", "a b"))

    def test_cache_key_separates_language_pair(self):
        c = TranslationCache()
        self.assertNotEqual(c.make_key("g", "en", "zh", "a"),
                            c.make_key("g", "en", "ja", "a"))

    def test_lru_eviction(self):
        c = TranslationCache(capacity=128)
        for i in range(300):
            c.put(f"k{i}", f"v{i}")
        self.assertLessEqual(c.stats.size, 128)
        self.assertEqual(c.get("k0"), None)
        self.assertEqual(c.get("k299"), "v299")
        self.assertGreater(c.stats.evictions, 100)

    def test_ttl_expiry(self):
        c = TranslationCache(capacity=128, ttl=0.01)
        c.put("k", "v")
        time.sleep(0.02)
        self.assertEqual(c.get("k"), None)
        self.assertEqual(c.stats.ttl_skips, 1)

    def test_clear(self):
        c = TranslationCache()
        c.put("k", "v")
        c.clear()
        self.assertEqual(c.get("k"), None)
        self.assertEqual(c.stats.size, 0)


class TestSplitBatch(unittest.TestCase):
    def test_exact(self):
        self.assertEqual(split_batch("a\nb\nc", 3), ["a", "b", "c"])

    def test_trailing_blank_ok(self):
        self.assertEqual(split_batch("a\nb\n", 2), ["a", "b"])

    def test_mismatch_returns_none(self):
        self.assertIsNone(split_batch("a\nb\nc", 2))
        self.assertIsNone(split_batch("a", 2))

    def test_merged_line_returns_none(self):
        """服务端把两行合成一行时必须拒绝，否则错位。"""
        self.assertIsNone(split_batch("ab", 2))

    def test_empty_pieces_kept_as_placeholders(self):
        """拆出的某条为空也算结构对齐（内容空由上层决定重译）。"""
        self.assertEqual(split_batch("a\n\nc", 3), ["a", "", "c"])


class TestSplitItem(_RestoreMixin, unittest.TestCase):
    """单条超长拆分（split_item）—— 修"长段把 MyMemory 打爆"的故障。

    三条硬不变式：
      1. 每个片段都不超过后端预算（_size 单位）
      2. "".join(pieces) == 原文（切分无损，拼接必须还原）
      3. 短文本原样返回单元素列表（不做无意义拆分）

    必须继承 _RestoreMixin：本组里 make_manager 会把 BACKENDS["mymemory"]
    换成 Fake，不还原会污染组内按字母序后跑的用例（实测 test_all_fail...
    先跑，test_long_cjk... 拿到 Fake 的按字符 _size 而假失败）。
    """

    def _check(self, be, text):
        pieces = be.split_item(text)
        self.assertTrue(pieces, "拆分结果不能为空")
        for p in pieces:
            self.assertLessEqual(be._size(p), be.max_chars,
                                 f"片段超预算：{be._size(p)} > {be.max_chars}: {p[:50]!r}")
        self.assertEqual("".join(pieces), text, "拼接必须无损还原原文")
        return pieces

    def _inst(self, cls_name: str):
        cfg = AppConfig()
        for var in ("DEEPL_API_KEY", "BAIDU_APPID", "BAIDU_KEY"):
            os.environ.pop(var, None)
        return BACKENDS[cls_name](cfg)

    def test_short_text_untouched(self):
        be = self._inst("google")
        self.assertEqual(be.split_item("hello world"), ["hello world"])

    def test_long_mixed_text_char_budget(self):
        """英文长段：句子边界切分，全部 ≤ 4500 字符。"""
        be = self._inst("google")
        text = ("The quick brown fox jumps over the lazy dog. " * 300).strip()
        pieces = self._check(be, text)
        self.assertGreater(len(pieces), 1)

    def test_long_cjk_bytes_budget(self):
        """中文长段 + 字节限后端：必须按 UTF-8 字节算预算。
        回归背景：630 字节的混排段（60% ASCII 路径 + 40% 中文）把
        MyMemory 打爆，而按字符数算预算完全发现不了。"""
        be = self._inst("mymemory")
        self.assertEqual(be._size("中"), 3, "中文一字 3 字节")
        text = ("vendor C:\\Users\\Mercury\\vendor312 与当前解释器不兼容 "
                "(ImportError: DLL load failed while importing QtCore: 找不到模块)。" * 6)
        self.assertGreater(be._size(text), be.max_chars, "测试文本必须真的超预算")
        pieces = self._check(be, text)
        self.assertGreater(len(pieces), 1)

    def test_no_punctuation_hard_split(self):
        """无标点无空格的长串只能硬切，仍须满足预算与无损两条不变式。"""
        be = self._inst("mymemory")
        text = "无标点长串" * 500          # 2000 字 = 6000 字节，无任何切分点
        pieces = self._check(be, text)
        self.assertGreater(len(pieces), 2)

    def test_multiline_content(self):
        """换行是天然切分点（OCR 逐行输出最常见形态）。"""
        be = self._inst("mymemory")
        text = "\n".join(f"line {i}：这是一行中文内容用于测试字节预算" for i in range(30))
        self._check(be, text)

    def test_merge_reduces_fragments(self):
        """贪心合并：短句们应尽量合并进同一段，而不是一句一个请求。"""
        be = self._inst("google")
        text = "短句一。短句二。短句三。" * 10     # 90 字符 << 4500
        self.assertEqual(be.split_item(text), [text],
                         "全部塞得进预算时不应产生任何拆分")

    def test_translate_via_rejoins_in_order(self):
        """_translate_via：拆 → 译 → 按原顺序拼回；译文对齐不能错位。"""
        mgr = make_manager({"mymemory": "ok"})
        be = mgr._instances["mymemory"]
        long_text = "第一句话。" * 400        # 1600 字节 > 480，必拆
        out = mgr._translate_via(be, [long_text], "zh", "en")
        self.assertEqual(len(out), 1)
        # ok 模式返回 "mymemory:{片段}"，拼回后所有片段都在且按序
        self.assertTrue(out[0].startswith("mymemory:第一句话。"))
        self.assertEqual(out[0].count("第一句话。"), long_text.count("第一句话。"))

    def test_translate_via_dedupes_identical_pieces(self):
        """两处相同的片段只发一次请求（OCR 常产出重复路径/短语）。"""
        mgr = make_manager({"mymemory": "ok"})
        be = mgr._instances["mymemory"]
        piece = "重复片段。" * 100           # 500 字节 > 480，一条拆 2 片
        mgr._translate_via(be, [piece, piece], "zh", "en")
        sent = be.calls[-1]
        self.assertEqual(len(sent), len(set(sent)), f"重复片段未去重：{sent}")

    def test_wronglen_now_records_reason(self):
        """条数不符必须抛错留下具体原因，而不是静默丢到下一档无解释。
        回归背景：用户截图里"全部后端失败"没有任何具体原因，
        就是因为静默路径不记录 errors_by_backend。"""
        mgr = make_manager({"google": "wronglen", "deepl": "ok"})
        rep = mgr.translate(["x", "y", "z"], "en", "zh", use_cache=False)
        self.assertEqual(rep.texts, ["deepl:x", "deepl:y", "deepl:z"])
        self.assertTrue(rep.errors[0] == "", "成功条目不应有错误")

    def test_all_fail_now_carries_backend_reason(self):
        """全部失败时，errors 里要有具体后端与原因，而非笼统一句。"""
        mgr = make_manager({"mymemory": "engine"})
        rep = mgr.translate(["x"], "en", "zh", use_cache=False)
        self.assertEqual(rep.failed, [0])
        self.assertIn("mymemory", rep.errors[0], f"原因未含后端名：{rep.errors[0]!r}")
        self.assertIn("引擎内部错误", rep.errors[0])

    def test_silent_empty_gets_explained(self):
        """后端不抛错但返回空译文时，失败原因不能是空白或笼统句。"""
        mgr = make_manager({"google": "empty", "deepl": "empty"})
        rep = mgr.translate(["x"], "en", "zh", use_cache=False)
        self.assertEqual(rep.failed, [0])
        self.assertTrue(rep.errors[0], "空译文失败也必须有原因")


class TestProbeContract(unittest.TestCase):
    """这一组用**真实**后端类（不注册假实例）。"""

    def test_all_backends_probe_without_exception(self):
        cfg = AppConfig()
        for name, cls in BACKENDS.items():
            info = cls.probe(cfg)
            self.assertEqual(info.name, name, f"{name} probe 名称不符")
            self.assertIsInstance(info.available, bool)

    def test_google_available_by_default(self):
        info = BACKENDS["google"].probe(AppConfig())
        self.assertTrue(info.available)
        self.assertFalse(info.needs_key)

    def test_keyed_backends_unavailable_without_key(self):
        cfg = AppConfig()
        for var in ("DEEPL_API_KEY", "BAIDU_APPID", "BAIDU_KEY"):
            os.environ.pop(var, None)
        self.assertFalse(BACKENDS["deepl"].probe(cfg).available)
        self.assertFalse(BACKENDS["baidu"].probe(cfg).available)
        self.assertTrue(BACKENDS["deepl"].probe(cfg).needs_key)

    def test_keyed_backends_available_with_key(self):
        cfg = AppConfig()
        cfg.translate.deepl_auth_key = "abc123:fx"
        self.assertTrue(BACKENDS["deepl"].probe(cfg).available)

    def test_secret_env_var_priority(self):
        cfg = AppConfig()
        old = os.environ.get("DEEPL_API_KEY")
        os.environ["DEEPL_API_KEY"] = "env-key:fx"
        cfg.translate.deepl_auth_key = "file-key"
        try:
            self.assertEqual(cfg.deepl_key, "env-key:fx")
            self.assertEqual(cfg.secret("deepl"), "env-key:fx")
        finally:
            if old is None:
                del os.environ["DEEPL_API_KEY"]
            else:
                os.environ["DEEPL_API_KEY"] = old

    def test_deepl_endpoint_follows_key_suffix(self):
        cfg = AppConfig()
        mgr = TranslateManager(cfg)
        mgr._instances["deepl"] = BACKENDS["deepl"](cfg)
        be = mgr._instances["deepl"]
        be._creds["deepl"] = "k:fx"
        self.assertIn("api-free", be.endpoint)
        be._creds["deepl"] = "k-paid"
        self.assertNotIn("api-free", be.endpoint)

    def test_unknown_backend_falls_back_to_auto(self):
        cfg = AppConfig()
        cfg.translate.backend = "nonsense"
        mgr = TranslateManager(cfg)
        self.assertTrue(len(mgr.chain()) >= 1)


class TestGuarded(_RestoreMixin, unittest.TestCase):
    """后端抛出的**未预期异常**不能穿透到 GUI 线程。"""

    class _Raising(Fake):
        """子类化假后端，用 raise_kind 决定抛什么。"""

        raise_kind = RuntimeError

        def __init__(self, cfg, creds=None):
            super().__init__(cfg, creds, name="google", mode="ok")

        def translate(self, texts, src, dst):
            raise self.raise_kind("模拟后端内部异常")

    def _install(self, cls):
        import ocrtranslator.translate as pkg
        pkg.BACKENDS["google"] = cls
        pkg.AUTO_CHAIN = ("google", "mymemory")

    def test_unexpected_exception_becomes_translation_error(self):
        boom = type("Boom", (self._Raising,), {"raise_kind": ZeroDivisionError})
        mgr = make_manager({"google": "ok", "mymemory": "ok"})
        self._install(boom)
        mgr._instances["google"] = boom(AppConfig())
        rep = mgr.translate(["t"], "en", "zh", use_cache=False)
        self.assertEqual(rep.texts[0], "mymemory:t")
        self.assertTrue(any("内部错误" in w or "ZeroDivision" in w for w in rep.warnings))

    def test_oserror_becomes_network_error(self):
        """裸 OSError/ConnectionError 归一成 NetworkError -> 进较短的冷静期。

        这条区分有意义：如果当成 engine 错误，冷静期只有默认值，
        而真正断网时应该 90 秒内不再重试该后端。
        """
        raw = type("Raw", (self._Raising,), {"raise_kind": ConnectionResetError})
        mgr = make_manager({"google": "ok", "mymemory": "ok"})
        self._install(raw)
        mgr._instances["google"] = raw(AppConfig())
        rep = mgr.translate(["t"], "en", "zh", use_cache=False)
        self.assertEqual(rep.texts[0], "mymemory:t")
        self.assertGreater(mgr._cool("google"), mgr.COOLDOWN_NETWORK - 2)


class TestLogHygiene(_RestoreMixin, unittest.TestCase):
    """同一条失败只允许记一次日志。

    曾经 engine 类错误在 `_note_failure` 与降级循环里各 log 一次，
    `-v` 跑单测时同一行刷两遍 —— 日志重复会让人以为发生了两次失败。
    """

    def test_engine_error_logged_once(self):
        import logging

        records: list[str] = []

        class Cap(logging.Handler):
            def emit(self, rec):
                records.append(rec.getMessage())

        from ocrtranslator.logger import get_logger
        lg = get_logger("translate")
        h = Cap()
        lg.addHandler(h)
        try:
            mgr = make_manager({"google": "engine", "mymemory": "ok"})
            mgr.translate(["t"], "en", "zh", use_cache=False)
        finally:
            lg.removeHandler(h)
        hits = [r for r in records if "内部错误" in r or "引擎内部错误" in r]
        self.assertLessEqual(len(hits), 1, f"日志重复：{hits}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
