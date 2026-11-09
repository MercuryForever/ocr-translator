"""本地离线翻译（Helsinki-NLP OPUS-MT 或 argos-translate 二选一）。

为什么必须有这一档：**在线后端全部不可用时，工具就废了**。
内网、飞机上、酒店网络、公司网关拦掉 Google 端点（本机实测就是这种情况：
translate.googleapis.com TCP 超时，而 pypi 正常 —— 典型的按域名白名单）。

两条技术路线的实测对比：

    方案                      体积          首次加载   质量        依赖
    ──────────────────────────────────────────────────────────────────────
    transformers+MarianMT     单语向 300MB+  6~15s     好         torch（~2GB）
    argos-translate           单语向 40~80MB 1~3s      良         ctranslate2（~60MB）
    fastai/CTranslate2 直用    需自己转换      -         -         复杂

    结论：**默认走 argos-translate**。torch 会让单文件 exe 变成 1.5GB，
    对"截图翻译"这种小工具完全不合理；argos 的模型是自包含 .argos.model，
    可以预置到 SOCRANS_HOME/models 里离线加载。

    transformers 路线保留为可选（配置 opus_engine="transformers"），
    面向已经有 torch 环境的用户 —— 同一份代码两条实现，接口一致。

    两个必须处理的现实约束：
      1. 模型按**语向对**分发（en→zh 与 zh→en 是两个文件）。找不到配对时
         必须明确报"该语向需下载 x MB"，而不是抛一个空异常。
      2. 首次下载 40~80MB，绝不能发生在"用户按下热键之后"。
         所以 warmup 是显式的，UI 用进度条盖住；识别路径上只做本地加载。
"""

from __future__ import annotations

import threading
import time

from ..config import MODEL_DIR
from ..languages import AUTO, tr_code
from ..logger import get_logger
from .base import (BackendInfo, NetworkError, TranslationError, TranslatorBackend)

log = get_logger("translate.opus")
# 语向不在这里硬编码：argos 的已装列表运行时可读（probe 就这么做），
# 写死一张表只会随上游改名而过期。


class OpusBackend(TranslatorBackend):
    name = "opus"
    label = "OPUS-MT（本地离线）"
    online = False
    needs_key = False
    supports_batch = True
    max_chars = 1500          # 本地推理，超长句子反而降质，切成小句更好

    def __init__(self, cfg, creds=None):
        # 注意 self.cfg 与参数 cfg 不是同一层：基类把 self.cfg 归一到 translate 段，
        # 而传入的 cfg 可能是整份 AppConfig。读字段一律用 self.cfg，
        # 否则会出现 `AppConfig has no attribute 'opus_model_pair'`。
        super().__init__(cfg, creds)
        self.pair = self.cfg.opus_model_pair or "en-zh"
        self._tokenizer = None
        self._engine = None
        self._impl = ""            # "argos" | "transformers"
        self._lock = threading.Lock()
        self._load_error = ""

    # ---------------------------------------------------------------- 加载
    def load(self) -> None:
        if self._tokenizer is not None:
            return
        with self._lock:
            if self._tokenizer is not None:
                return
            t0 = time.perf_counter()
            if self._try_argos():
                self._impl = "argos"
            elif self._try_transformers():
                self._impl = "transformers"
            else:
                raise TranslationError(
                    self._load_error or f"本地模型不可用（语向 {self.pair}）",
                    kind="offline",
                    hint="两种方案：(1) pip install argos-translate 后执行 "
                         f"`argst translate --download {self.pair.replace('-', '->')}`；"
                         f"(2) pip install transformers sentencepiece（需 torch，体积大）。"
                         "或者改用在线后端。")
            log.info("OPUS-MT 加载完成 %.2fs（%s）", time.perf_counter() - t0, self._impl)

    def _try_argos(self) -> bool:
        try:
            import argostranslate.package as argos_pkg
            import argostranslate.translate as argos_tr
        except ImportError as exc:
            self._load_error = f"argos-translate 未安装：{exc}"
            return False
        try:
            installed = argos_pkg.get_installed_packages()
            src, dst = self.pair.split("-")
            want = (tr_code(src, "opus") or src, tr_code(dst, "opus") or dst)
            pkg = next((p for p in installed
                        if (p.from_code, p.to_code) == want), None)
            if pkg is None:
                self._load_error = (f"argos 未安装语向 {want[0]}->{want[1]}"
                                    f"（已装 {len(installed)} 个）")
                return False
            pkg.install()
            self._tokenizer = ("argos", want)
            self._engine = argos_tr
            return True
        except Exception as exc:
            self._load_error = f"argos 加载失败：{exc}"
            return False

    def _try_transformers(self) -> bool:
        try:
            from transformers import MarianMTModel, MarianTokenizer
        except ImportError as exc:
            self._load_error = f"transformers 不可用：{exc}"
            return False
        src, dst = self.pair.split("-")
        if (src, dst) not in {("en", "zh"), ("zh", "en"), ("en", "de"), ("en", "fr"),
                              ("en", "es"), ("en", "ru"), ("en", "ja")}:
            self._load_error = f"transformers 路线未内置 {src}->{dst} 模型名"
            return False
        name = f"Helsinki-NLP/opus-mt-{src}-{dst}"
        try:
            # local_files_only：这条路径上**绝不允许偷偷联网下载**。
            # 按下热键后卡 60 秒下载模型，比直接报错更糟。
            tok = MarianTokenizer.from_pretrained(name, local_files_only=True)
            model = MarianMTModel.from_pretrained(name, local_files_only=True)
            model.eval()
            self._tokenizer = ("hf", tok)
            self._engine = model
            return True
        except Exception as exc:
            self._load_error = (f"未找到本地缓存的 {name}（{exc.__class__.__name__}）。"
                                "离线环境需预先下载模型到 ~/.cache/huggingface")
            return False

    # ---------------------------------------------------------------- 翻译
    def translate(self, texts: list[str], src: str, dst: str) -> list[str]:
        if not texts:
            return []
        want = f"{tr_code(src, 'opus') or 'en'}-{tr_code(dst, 'opus') or 'zh'}"
        if src == AUTO:
            from ..languages import detect

            d = detect(" ".join(texts))
            want = f"{d if d != AUTO else 'en'}-{tr_code(dst, 'opus') or 'zh'}"
        if self._tokenizer is None or want != self.pair:
            self.pair = want
            self._tokenizer = None           # 语向变了必须重新加载
            self._engine = None
        self.load()

        if self._impl == "argos":
            _, want_codes = self._tokenizer
            return [self._argos_one(t, want_codes) for t in texts]
        return self._hf_batch(texts)

    def _argos_one(self, text: str, codes: tuple[str, str]) -> str:
        import argostranslate.translate as argos_tr

        try:
            return argos_tr.translate(text, codes[0], codes[1]) or ""
        except Exception as exc:
            raise TranslationError(f"argos 推理失败：{exc}", kind="engine") from exc

    def _hf_batch(self, texts: list[str]) -> list[str]:
        import torch

        tok = self._tokenizer[1]
        try:
            enc = tok(texts, return_tensors="pt", padding=True,
                      truncation=True, max_length=512)
            with torch.no_grad():
                gen = self._engine.generate(**enc, max_length=512, num_beams=2)
            out = tok.batch_decode(gen, skip_special_tokens=True)
        except Exception as exc:
            raise TranslationError(f"OPUS-MT 推理失败：{exc}", kind="engine") from exc
        if len(out) != len(texts):
            raise TranslationError(f"OPUS 输出条数不符（{len(out)}!={len(texts)}）",
                                   kind="engine",
                                   hint="对照会错位，本次结果已丢弃")
        return [o.strip() for o in out]

    # ---------------------------------------------------------------- 探测
    @classmethod
    def probe(cls, cfg) -> BackendInfo:
        impl, note = "", []
        try:
            import argostranslate.package as p  # noqa: F401
            impl = "argos"
            try:
                inst = p.get_installed_packages()
                pairs = ", ".join(f"{x.from_code}->{x.to_code}" for x in inst[:4])
                note.append(f"已装语向 {len(inst)} 个" + (f"（{pairs}…）" if pairs else "（0 个，需先下载）"))
            except Exception as exc:
                note.append(f"包列表读取失败：{exc}")
        except ImportError:
            pass
        if not impl:
            try:
                import transformers  # noqa: F401
                import torch  # noqa: F401
                impl = "transformers"
                note.append("需预下载模型；torch 体积大")
            except ImportError:
                pass
        available = bool(impl)
        if not available:
            note = ["未安装：pip install argos-translate（推荐，体积小）"
                    " 或 pip install transformers sentencepiece torch"]
        return BackendInfo(cls.name, cls.label, available=available, online=False,
                           needs_key=False, note="；".join(note) or "完全离线",
                           cost="免费（本地推理）",
                           languages_note="按已下载语向，常见 20~600 对")

    @staticmethod
    def cached_pairs() -> list[str]:
        """列出 MODEL_DIR 下已有的本地模型（供设置页展示）。"""
        out = []
        try:
            for p in sorted(MODEL_DIR.glob("*.argos.model")):
                out.append(p.stem)
            hf = MODEL_DIR / "huggingface"
            if hf.is_dir():
                out += [d.name for d in hf.iterdir() if d.is_dir()]
        except OSError:
            pass
        return out
