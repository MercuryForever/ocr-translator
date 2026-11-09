"""Tesseract 后端（兜底：CPU 很弱的老机器 / 需要 400+ 语种）。

诚实的定位说明：中文场景下它**不是**好选择（PP-OCR 明显更强），
本项目留它只有两个真实理由：
  1. 推理极快（单行 ~10ms 级），在 2 核老本本上比 onnx 版 PP-OCR 更跟手；
  2. 语种覆盖 400+（含各种小语种、古文字），RapidOCR 的字典做不到的它能做。

真实坑（代码逐个处理）：
  * 需要**外部安装** tesseract.exe，Python 包装不出来 —— 所以 probe 要检查
    可执行文件是否存在，Windows 上还要允许用户在设置里手填路径；
  * `lang` 必须是已安装的 traineddata，写错直接抛 `TesseractError`；
    我们按 `lang + 备选` 依次尝试，并缓存"哪些 lang 实际可用"；
  * 中文默认 `chi_sim` 会把繁简混着认，需要 `chi_tra` 时要单独装语言包；
  * `image_to_data` 返回的是**逐词**（word-level），必须按 (block,par,line)
    分组再拼行，否则会一行出十几个 OcrLine，版面配对彻底乱套；
  * `conf` 是 -1（非文字块）到 95 的整数百分比，需要归一到 0~1；
  * PSM 模式很关键：截图片段用 `6`（单块均匀文本），整屏用 `3`（自动分页），
    单行用 `7`。选错 PSM 是最常见的"识别为空"原因。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time

from ..languages import AUTO, ocr_code
from ..logger import get_logger
from ..models import CaptureResult, OcrLine
from . import preprocess
from .base import EngineInfo, OcrEngine, OcrError

log = get_logger("ocr.tesseract")

# 主语言认不出来时的降级链：装包顺序不确定时提高可用性
_LANG_FALLBACK = {
    "chi_sim": ["chi_sim", "chi_sim_vert", "eng"],
    "chi_tra": ["chi_tra", "chi_sim", "eng"],
    "jpn": ["jpn", "jpn_vert", "eng"],
    "kor": ["kor", "eng"],
    "eng": ["eng"],
}


class TesseractOcrEngine(OcrEngine):
    name = "tesseract"
    label = "Tesseract OCR"

    def __init__(self, cfg):
        super().__init__(cfg)
        self._api = None
        self._exe = ""
        self._langs_ok: set[str] = set()
        self._lang_fail: set[str] = set()

    # -------------------------------------------------- 定位可执行文件
    @staticmethod
    def find_exe(cfg) -> str:
        """优先级：配置手填 > PATH > 常见安装目录。"""
        if cfg.ocr.tesseract_cmd:
            if os.path.isfile(cfg.ocr.tesseract_cmd):
                return cfg.ocr.tesseract_cmd
            log.warning("配置里的 tesseract_cmd 不存在：%s", cfg.ocr.tesseract_cmd)
        which = shutil.which("tesseract")
        if which:
            return which
        if os.name == "nt":
            for root in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                         os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                         os.environ.get("LOCALAPPDATA", "")):
                if not root:
                    continue
                cand = os.path.join(root, "Tesseract-OCR", "tesseract.exe")
                if os.path.isfile(cand):
                    return cand
        return ""

    def warmup(self) -> None:
        if self._api is not None:
            return
        try:
            import pytesseract
        except ImportError as exc:
            raise OcrError(f"pytesseract 未安装：{exc}", fatal=True,
                           hint="pip install pytesseract，并另外安装 Tesseract-OCR 程序本体") from exc
        self._exe = self.find_exe(self.cfg)
        if not self._exe:
            raise OcrError(
                "找不到 tesseract 可执行文件",
                fatal=False,
                hint="Windows 装 UB-Mannheim 安装包，或在设置页手填 tesseract.exe 路径；"
                     "本项目推荐改用 RapidOCR（无需外部程序）")
        pytesseract.pytesseract.tesseract_cmd = self._exe
        if self.cfg.tessdata_dir:
            os.environ["TESSDATA_PREFIX"] = self.cfg.tessdata_dir
        self._api = pytesseract
        log.info("Tesseract 就绪：%s", self._exe)
        try:
            out = subprocess.run([self._exe, "--list-langs"], capture_output=True,
                                 text=True, timeout=10)
            got = {l.strip() for l in out.stdout.splitlines()[1:] if l.strip()}
            self._langs_ok = got
            log.info("已安装语言包 %d 个", len(got))
        except Exception as exc:
            log.warning("无法列出语言包：%s", exc)
        self._ready = True

    # -------------------------------------------------- 语言选择
    def _pick_lang(self, hint: str, sample: str) -> str:
        """按配置/样本决定 lang 串，并只保留**实际装了**的语言包。"""
        primary = ocr_code(hint or AUTO, "tesseract") or "eng"
        cands = []
        for c in (primary, "eng", *_LANG_FALLBACK.get(primary, [])):
            if c and c not in cands:
                cands.append(c)
        usable = [c for c in cands if not self._langs_ok or c in self._langs_ok]
        if not usable:
            raise OcrError(
                f"缺少 Tesseract 语言包（尝试 {', '.join(cands)}）",
                hint=f"下载对应 traineddata 放到 {os.path.dirname(self._exe)}/tessdata")
        # 中英混排：把两个都带上，让 tesseract 自己判
        return "+".join(usable[:2])

    def recognize(self, cap: CaptureResult, lang_hint: str = "auto") -> list[OcrLine]:
        self.warmup()
        bgr = self.to_numpy(cap, "bgr")
        prep = preprocess.prepare(bgr, enable=self.cfg.upscale)
        # tesseract 对二值化敏感，但它内部已经做自适应阈值；
        # 只在深色主题（文字亮、背景暗）时反色，否则整片亮字会被当背景丢掉。
        img = self._maybe_invert(prep.image)

        lang = self._pick_lang(lang_hint, "")
        psm = 6 if prep.image.shape[0] < 900 else 3   # 小图强制"单块"，PSM3 常判成多页返回空
        t0 = time.perf_counter()
        try:
            data = self._api.image_to_data(img, lang=lang,
                                           config=f"--psm {psm} -c preserve_interword_spaces=1",
                                           output_type=self._api.Output.DICT)
        except Exception as exc:
            msg = str(exc)
            if "Failed to open" in msg or "Error opening data file" in msg:
                raise OcrError(f"Tesseract 语言包不可用（{lang}）", hint=msg[:180]) from exc
            raise OcrError(f"Tesseract 识别失败：{msg[:200]}") from exc
        log.info("Tesseract 用时 %.2fs（lang=%s psm=%d）", time.perf_counter() - t0, lang, psm)

        lines = self._group_words(data, prep.scale)
        return lines

    @staticmethod
    def _maybe_invert(bgr):
        """深底亮字时反色。用少数类=墨迹的原则判断（与 preprocess 一致）。"""
        import numpy as np

        gray = bgr[..., 2] * 0.299 + bgr[..., 1] * 0.587 + bgr[..., 0] * 0.114
        gray = gray.astype(np.uint8)
        hi = float((gray > 160).mean())
        lo = float((gray < 90).mean())
        if lo < hi:                      # 亮像素占多 => 深色主题
            return np.stack([255 - bgr[..., 2], 255 - bgr[..., 1], 255 - bgr[..., 0]], axis=-1)
        return bgr

    @staticmethod
    def _group_words(data, scale: float) -> list[OcrLine]:
        """image_to_data 是逐词的，必须按 (block_num, par_num, line_num) 重组行。

        这一步不做，输出会是"十几个单词行"，版面配对完全失效。
        tesseract 的 line_num 在同一 block 内连续，但跨 block 会重号，
        所以键里必须带 block_num。
        """
        from ..models import join_lines

        groups: dict[tuple[int, int, int], list] = {}
        n = len(data.get("text", []))
        for i in range(n):
            word = (data["text"][i] or "").strip()
            if not word:
                continue
            try:
                conf = float(data["conf"][i])
            except (TypeError, ValueError, IndexError):
                conf = -1.0
            key = (int(data["block_num"][i]), int(data["par_num"][i]), int(data["line_num"][i]))
            x, y, w, h = (int(data["left"][i]), int(data["top"][i]),
                          int(data["width"][i]), int(data["height"][i]))
            groups.setdefault(key, []).append((x, y, w, h, word, conf))

        lines: list[OcrLine] = []
        for key, words in groups.items():
            words.sort(key=lambda t: t[0])            # 词内按 x 排序
            x0 = min(w[0] for w in words)
            y0 = min(w[1] for w in words)
            x1 = max(w[0] + w[2] for w in words)
            y1 = max(w[1] + w[3] for w in words)
            # -1 表示"这个词不是文字"（图片块里的噪声），取平均时剔除
            confs = [w[5] for w in words if w[5] >= 0]
            conf = (sum(confs) / len(confs) / 100.0) if confs else 0.0
            if conf < 0.05 and not any(w[5] > 30 for w in words):
                continue
            text = join_lines([w[4] for w in words])
            if not text.strip():
                continue
            inv = 1.0 / scale if scale else 1.0
            lines.append(OcrLine(text=text, conf=conf,
                                 box=(x0 * inv, y0 * inv, x1 * inv, y1 * inv),
                                 poly=((x0 * inv, y0 * inv), (x1 * inv, y0 * inv),
                                       (x1 * inv, y1 * inv), (x0 * inv, y1 * inv))))
        lines.sort(key=lambda l: (round(l.box[1] / 8), l.box[0]))
        return lines

    @classmethod
    def probe(cls) -> EngineInfo:
        try:
            import pytesseract
        except ImportError:
            return EngineInfo(cls.name, cls.label, False, "", "pytesseract 未安装")
        exe = cls.find_exe(_Lite())
        if not exe:
            return EngineInfo(cls.name, cls.label, False, "",
                              "未找到 tesseract.exe（需另外安装程序本体）")
        ver = ""
        try:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
            first = (out.stdout or out.stderr).splitlines()[0]
            ver = first.replace("tesseract", "").strip()
        except Exception:
            ver = "?"
        langs = 0
        try:
            out = subprocess.run([exe, "--list-langs"], capture_output=True, text=True, timeout=10)
            langs = len([l for l in out.stdout.splitlines()[1:] if l.strip()])
        except Exception:
            pass
        return EngineInfo(cls.name, cls.label, True, ver,
                          f"需外部安装本体；已装语言包 {langs} 个；速度最快、中文精度弱")


class _Lite:
    """probe 只需要 ocr.tesseract_cmd / 不需要完整 AppConfig。"""

    class ocr:  # noqa: N801
        tesseract_cmd = ""
        tessdata_dir = ""
