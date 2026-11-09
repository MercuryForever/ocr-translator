"""翻译调度：缓存 → 主后端 → 降级链 → 报错。

这是异常处理最密集的一处，也是"网络不可用"这类需求被真正兑现的地方。

降级顺序（`backend="auto"`）：

    google（零配置、质量/覆盖最好）
      → deepl（有 key 才进；质量最好，国内可直连）
        → baidu（有 key 才进；国内可达性最佳）
          → mymemory（零配置，配额低，但**几乎一定能连上**）
            → opus（离线，需预装模型）

顺序设计依据（本机实测，不是猜的）：
    translate.googleapis.com  TCP 超时（被墙）
    api-free.deepl.com        TCP 可连
    fanyi-api.baidu.com       TCP 可连
    api.mymemory.translated.net TCP 可连
    pypi.org                  TCP 可连
也就是说"Google 排第一"在开发机上是理想情况，在这台机器上第一次尝试就必失败。
这正是必须有降级链的原因：**每个后端都当成会失败来设计**。

三条硬规则：
  1. **条数守恒**。降级后必须重译"整批失败的条目"，绝不能只补一部分，
     否则原文/译文行数错位，双语对照表整列歪。
  2. **错误分型决定后续行为**。按后端记"冷静期"（Network 90s / Quota 300s /
     Auth 600s），而不是用一个全局"已降级"布尔。
     后者在本机实测会把已经证明可达的 MyMemory 一起跳掉，
     而且下次调用还要重吃一遍 Google 的 6~10 秒 TCP 超时。
     同时把"上次成功的后端"提到链首，让超时只付一次。
  3. **降级要告诉用户**。静默换后端会让用户以为 DeepL 生效了但质量突然变差。
     结果元信息里记录 `engine` 实际用了谁，UI 状态栏显示。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from ..languages import AUTO, detect
from ..logger import get_logger
from .base import (AuthError, BackendInfo, NetworkError, QuotaExceeded,
                   TranslationError, TranslatorBackend)
from .baidu import BaiduBackend
from .deepl import DeepLBackend
from .google_web import GoogleBackend
from .mymemory import MyMemoryBackend
from .cache import TranslationCache
from .opus import OpusBackend

log = get_logger("translate")

AUTO_CHAIN = ("google", "deepl", "baidu", "mymemory", "opus")
BACKENDS: dict[str, type[TranslatorBackend]] = {
    cls.name: cls for cls in (GoogleBackend, DeepLBackend, BaiduBackend,
                              MyMemoryBackend, OpusBackend)
}


@dataclass
class BatchReport:
    """一次批量翻译的结果与过程记录。"""

    texts: list[str] = field(default_factory=list)
    engines: list[str] = field(default_factory=list)      # 与 texts 等长，标明每条由谁译出
    errors: list[str] = field(default_factory=list)       # 与 texts 等长，失败时非空
    warnings: list[str] = field(default_factory=list)
    detected_lang: str = ""
    ms: int = 0
    cache_hits: int = 0

    @property
    def failed(self) -> list[int]:
        return [i for i, e in enumerate(self.errors) if e]

    @property
    def primary_engine(self) -> str:
        return max(set(self.engines), key=self.engines.count) if self.engines else ""

    def __post_init__(self) -> None:
        n = len(self.texts)
        while len(self.engines) < n:
            self.engines.append("")
        while len(self.errors) < n:
            self.errors.append("")


class TranslateManager:
    """对外唯一入口。线程安全：OCR/翻译在 Worker 线程跑，主线程可能同时改设置。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.cache = TranslationCache(cfg.translate.cache_size)
        self._instances: dict[str, TranslatorBackend] = {}
        self._lock = threading.RLock()
        # 每个后端的"冷静期"截止时刻（monotonic 秒）。
        # 曾经用一个全局 _degraded 布尔：Google 一次超时就跳过**所有**在线档，
        # 结果是本机实测把已经证明可达的 MyMemory 也一起跳掉，
        # 且下次调用仍要重吃 10 秒 TCP 超时。按后端记账才对：
        # 谁坏了关谁，好的一直用，坏的自己到期再试。
        self._cooldown: dict[str, float] = {}
        #: 最近一次**成功**的后端。下一次调用把它排到链首，
        #: 这样"Google 超时 + MyMemory 可用"的机器上只有第一次付那 6 秒代价。
        self._preferred: str = ""
        self.last_report: BatchReport | None = None

    # ---------------------------------------------------------------- 实例
    def backend(self, name: str) -> TranslatorBackend | None:
        with self._lock:
            if name in self._instances:
                return self._instances[name]
            cls = BACKENDS.get(name)
            if cls is None:
                return None
            try:
                inst = cls(self.cfg)
            except Exception as exc:
                log.error("后端 %s 构造失败：%s", name, exc)
                return None
            self._instances[name] = inst
            return inst

    # ---------------------------------------------------------------- 链
    COOLDOWN_NETWORK = 90.0      # 网络失败后 90 秒内不再试该后端
    COOLDOWN_QUOTA = 300.0       # 配额类更久（当天基本不会恢复）
    COOLDOWN_AUTH = 600.0        # 密钥错：改了设置会立刻 reset
    COOLDOWN_ENGINE = 60.0       # 后端内部异常：短暂避开，避免每次截图都撞上去

    def _cool(self, name: str) -> float:
        return max(0.0, self._cooldown.get(name, 0.0) - time.monotonic())

    def _mark_cool(self, name: str, exc: TranslationError) -> None:
        """按错误分型设冷静期。

        四类都必须设：早先只设 network/quota/auth，engine 类异常
        （后端代码 bug、响应结构改版）不进冷静期，结果**每次截图都先撞一次**，
        白付一次失败延迟才降级。
        """
        secs = self.COOLDOWN_ENGINE
        if isinstance(exc, NetworkError):
            secs = self.COOLDOWN_NETWORK
        elif isinstance(exc, QuotaExceeded):
            secs = self.COOLDOWN_QUOTA
        elif isinstance(exc, AuthError):
            secs = self.COOLDOWN_AUTH
        self._cooldown[name] = time.monotonic() + secs

    def _chain(self) -> list[str]:
        """本次实际会依次尝试的后端名（缓存键与降级顺序共用它，保证一致）。

        三条排序规则：
          1. 冷静期内的后端直接跳过（NetworkError 90s / Quota 300s / Auth 600s）；
          2. 最近成功的后端提到最前 —— 本机 Google 必超时，
             不这么做的话**每次**截图都要先付 6~10 秒超时；
          3. 全在冷静期时返回完整链：宁可让用户多等一次，也不给"无后端可用"的假象。
        """
        want = self.cfg.translate.backend
        if want not in {"auto", *BACKENDS}:
            log.warning("未知翻译后端 %r，回退 auto", want)
            want = "auto"
        if want != "auto":
            return [want]

        alive: list[str] = []
        for name in AUTO_CHAIN:
            try:
                info = BACKENDS[name].probe(self.cfg)
            except Exception as exc:
                log.debug("跳过 %s：探测异常 %s", name, exc)
                continue
            if not info.available:
                log.debug("跳过 %s：%s", name, info.note)
                continue
            cool = self._cool(name)
            if cool > 0:
                log.debug("跳过 %s：冷静期还剩 %.0fs", name, cool)
                continue
            alive.append(name)
        if not alive:
            log.info("所有后端都在冷静期，临时按完整链重试")
            alive = [n for n in AUTO_CHAIN if BACKENDS[n].probe(self.cfg).available] or list(AUTO_CHAIN)
        if self._preferred in alive and alive[0] != self._preferred:
            alive.remove(self._preferred)
            alive.insert(0, self._preferred)
        return alive

    def chain(self) -> list[str]:
        """公开别名（设置页显示"当前生效链"）。"""
        return self._chain()

    def reset_backoff(self, name: str = "") -> None:
        """清空冷静期。UI 的"重试"按钮与设置页改密钥/改后端后必须调用，
        否则用户修好了配置也要干等 90~600 秒。"""
        if name:
            self._cooldown.pop(name, None)
        else:
            self._cooldown.clear()

    # 兼容旧调用点
    reset_degraded = reset_backoff

    # ---------------------------------------------------------------- 主流程
    def translate(self, texts: list[str], src: str = AUTO, dst: str = "",
                  *, use_cache: bool = True) -> BatchReport:
        dst = dst or self.cfg.translate.target_lang
        if src == AUTO and self.cfg.translate.source_lang != AUTO:
            src = self.cfg.translate.source_lang
        report = BatchReport(texts=[""] * len(texts),
                             engines=[""] * len(texts), errors=[""] * len(texts))
        if not texts:
            return report

        t0 = time.perf_counter()
        pending = list(range(len(texts)))

        # 先定链再查缓存：缓存键含**实际服务后端**，
        # 按链序探测意味着"切到更好的后端就读新后端的条目，读不到就重译"，
        # 反过来（用配置值 'auto' 当键）会在切后端后继续返回旧后端的译文。
        chain = self._chain()
        if use_cache:
            still = []
            for i in pending:
                for name in chain:
                    hit = self.cache.get(self.cache.make_key(name, src, dst, texts[i]))
                    if hit is not None:
                        report.texts[i] = hit
                        report.engines[i] = f"cache:{name}"
                        report.cache_hits += 1
                        break
                if not report.texts[i]:
                    still.append(i)
            pending = still
            if not pending:
                report.ms = int((time.perf_counter() - t0) * 1000)
                log.debug("全部命中缓存（%d 条，%dms）", len(texts), report.ms)
                self.last_report = report
                return report

        # ---- 沿降级链逐后端尝试，每次都重译"完整失败集合"
        if not chain:
            # 注意：errors 长度必须等于 texts 总数，不能按 pending 长度重建，
            # 否则 UI 按下标取错误信息时会整列错位。
            for i in pending:
                report.errors[i] = "无可用翻译后端"
            report.warnings.append("没有可用后端：检查网络，或在设置里配置密钥/离线模型")
            report.ms = int((time.perf_counter() - t0) * 1000)
            self.last_report = report
            return report

        errors_by_backend: dict[str, str] = {}
        for name in chain:
            if not pending:
                break
            be = self.backend(name)
            if be is None:
                errors_by_backend[name] = "实例化失败"
                continue
            batch = [texts[i] for i in pending]
            try:
                out = self._translate_via(be, batch, src, dst)
                got = self._accept(name, batch, out, src, dst, use_cache)
            except TranslationError as exc:
                # 日志与 warning 文案统一由 _note_failure 负责；
                # 这里只做两件事：记冷静期 + 继续下一档。
                # _accept 的条数校验也在 try 里：错条数和报错一样要留下
                # 具体原因（否则用户只看到"全部后端失败"，没有一行日志说明
                # 是谁、为什么）。
                errors_by_backend[name] = str(exc)
                self._note_failure(name, exc, report)
                self._mark_cool(name, exc)
                continue

            # got 里的下标是 **batch 内下标**，必须经 pending 映射回原始下标。
            # 直接当原始下标用会造成译文整列偏移（最难发现的一类 bug）。
            got = self._accept(name, batch, out, src, dst, use_cache)
            for local_i, text in got:
                orig_i = pending[local_i]
                report.texts[orig_i] = text
                report.engines[orig_i] = name
                report.errors[orig_i] = ""
            if got:
                # 成功即记为首选，并抹掉自己的冷静期
                self._preferred = name
                self._cooldown.pop(name, None)
            pending = [i for i in pending if not report.texts[i]]
            det = getattr(be, "last_detected", "")
            if det:
                report.detected_lang = det

        # ---- 剩余失败项：留空并写错误原因，保证 report 与 texts 等长
        if pending:
            if errors_by_backend:
                # 把每档的具体原因都带出来（MyMemory 返回 4xx 之类），
                # 而不是一句无法定位的"全部后端失败"
                final_reason = "；".join(f"{k}: {v}" for k, v in errors_by_backend.items())
            else:
                # 后端没抛错但条目仍失败 = 返回了空译文（服务端漏译/同语向）
                final_reason = "各后端均未给出译文（可能网络受限、配额耗尽或服务端漏译）"
            for i in pending:
                report.errors[i] = final_reason[:160]
            report.warnings.append(
                f"{len(pending)}/{len(texts)} 条翻译失败（对照表将只显示原文）。"
                f"尝试过：{', '.join(chain)}")
        report.ms = int((time.perf_counter() - t0) * 1000)
        self.last_report = report
        log.info("翻译完成 %d 条（主后端 %s，缓存 %d，失败 %d，%dms）",
                 len(texts), report.primary_engine, report.cache_hits,
                 len(report.failed), report.ms)
        return report

    # ---------------------------------------------------------------- 细节
    def _translate_via(self, be: TranslatorBackend, batch: list[str],
                       src: str, dst: str) -> list[str]:
        """调用后端；单条超过该后端单请求预算时先拆分，译完按原顺序无损拼回。

        为什么放在 manager 而不是各后端里：预算是后端属性（MyMemory 480
        字节、DeepL 4500 字符、百度 5800 字节各不相同），但"拆 → 译 → 拼回"
        的流程对所有后端一样。后端只需声明 `max_chars`（和字节限后端覆写
        `_size`），拆分策略统一在 base.split_item 实现。

        片段在整批内去重：OCR 常产出重复片段（长路径、相同短语），
        去重后相同的片段只发一次请求。
        """
        pieces: list[str] = []
        uniq: dict[str, int] = {}
        slot: list[list[int]] = []
        for t in batch:
            group: list[int] = []
            for p in be.split_item(t):
                j = uniq.get(p)
                if j is None:
                    j = len(pieces)
                    uniq[p] = j
                    pieces.append(p)
                group.append(j)
            slot.append(group)
        out = self._guarded(be, pieces, src, dst)
        if len(out) != len(pieces):
            raise TranslationError(
                f"{be.label} 返回条数 {len(out)} != 输入 {len(pieces)}（含拆分片段）",
                kind="engine")
        # "".join 拼回与 split_item 的无损切分严格互逆
        return ["".join(out[j] for j in group) for group in slot]

    def _guarded(self, be: TranslatorBackend, batch: list[str], src: str, dst: str) -> list[str]:
        """调用后端并把非预期异常归一成 TranslationError。

        曾经在这里开一条工作线程 + `join(timeout)` 做"整批超时护栏"，但那是错的：
        Python 无法中断运行中的线程，超时后那条线程仍在跑，
        连续失败几次就泄漏 N 条线程 + N 份连接，而 GUI 侧看到的还是"卡住"。
        正确做法是让**每个后端自己**用带超时的 HTTP 客户端
        （requests timeout / urllib timeout / transformers 是本地推理不会挂），
        这里只做异常归一。网络层面的挂起由 socket 超时兜住。
        """
        try:
            return be.translate(batch, src, dst)
        except TranslationError:
            raise
        except (TimeoutError, ConnectionError, OSError) as exc:
            raise NetworkError(f"{be.label} 网络异常：{exc}",
                               hint="检查网络/代理，或在设置里改用离线 OPUS-MT") from exc
        except Exception as exc:
            raise TranslationError(f"{be.label} 内部错误：{exc}", kind="engine") from exc

    def _accept(self, name: str, batch: list[str], out: list[str],
                src: str, dst: str, use_cache: bool) -> list[tuple[int, str]]:
        """校验条数并写缓存（键 = **实际服务后端**，不是配置值）。

        条数不等就**整批拒绝**（抛错而不是静默丢）：宁可少译几条，也不能让
        对照错位。抛错的原因会被记进 errors_by_backend，UI 能显示具体是谁
        为什么失败 —— 之前静默返回 [] 时用户只看到"全部后端失败"。
        返回 [(batch 内下标, 译文)]；调用方用 pending 列表映射回原始下标。
        两处下标体系不同（batch-local vs original），混用会让译文整列偏移一格 ——
        这类 bug 不崩溃、不报错，只是每一行都错一行，是最难发现的一种。
        """
        if len(out) != len(batch):
            raise TranslationError(
                f"{name} 返回条数 {len(out)} != 输入 {len(batch)}，为防对照错位整批拒绝",
                kind="engine")
        pairs = []
        for i, text in enumerate(out):
            text = (text or "").strip()
            if not text:
                continue
            pairs.append((i, text))
            if use_cache:
                self.cache.put(self.cache.make_key(name, src, dst, batch[i]), text)
        return pairs

    def _note_failure(self, name: str, exc: TranslationError, report: BatchReport) -> None:
        """失败分类：**日志与 warning 文案只在这里产生一次**。

        修掉的两条 bug：
          * engine 类失败过去在 `_note_failure` 与调用处各 log 一次，
            同一条错误在日志里刷两遍（`-v` 跑单测时直接暴露）；
          * 同一条路径又完全不 append warning，UI 只剩"3/5 条失败"，
            用户看不到真正原因（"某后端内部崩了"）。

        分类语义：
          NetworkError  -> info + 一行 warning（正常降级，不是用户错误，但要说清原因）
          AuthError     -> info + hint 进 warning（用户能自己修：填密钥）
          QuotaExceeded -> info + hint 进 warning（用户能自己修：看用量/换后端）
          其它          -> warning 级日志 + 一行进 warning（多半是程序 bug，需要看日志）
        """
        if isinstance(exc, NetworkError):
            log.info("后端 %s 网络失败：%s", name, exc)
            report.warnings.append(f"{name}：{exc}")
        elif isinstance(exc, (AuthError, QuotaExceeded)):
            log.info("后端 %s 配置/配额问题：%s", name, exc)
            report.warnings.append(exc.hint or str(exc))
        else:
            log.warning("后端 %s 失败：%s", name, exc)
            report.warnings.append(f"{name} 异常：{str(exc)[:120]}")

    # ---------------------------------------------------------------- 对外
    def status(self) -> list[tuple[BackendInfo, str]]:
        """设置页用：每个后端的可用性与当前角色。"""
        chain = self._chain()
        out = []
        for name, cls in BACKENDS.items():
            try:
                info = cls.probe(self.cfg)
            except Exception as exc:
                info = BackendInfo(name, cls.label, False, False, note=f"探测异常：{exc}")
            if name == self._preferred:
                tag = "首选（上次成功）"
            elif name in chain:
                tag = "链中"
            else:
                cool = self._cool(name)
                tag = f"冷静期 {cool:.0f}s" if cool > 0 else "未使用"
            out.append((info, tag))
        return out

    def single(self, text: str, src: str = AUTO, dst: str = "") -> tuple[str, str]:
        """单条翻译（设置页"试翻一句"用）。返回 (译文, 错误)。"""
        rep = self.translate([text], src, dst, use_cache=False)
        return rep.texts[0], rep.errors[0]

    def auto_pair(self, texts: list[str], dst: str) -> str:
        """源语言=auto 时先猜一次，给只接受显式源语言的接口用（MyMemory、OPUS）。"""
        return detect(" ".join(texts[:3])) or "en"

    def close(self) -> None:
        with self._lock:
            for be in self._instances.values():
                try:
                    be.close()
                except Exception:
                    pass
            self._instances.clear()
