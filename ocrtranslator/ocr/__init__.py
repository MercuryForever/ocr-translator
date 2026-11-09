"""OCR 引擎注册表与工厂。

`auto` 的排序依据（按实测）：RapidOCR > PaddleOCR > Tesseract。
选第一个 probe() 可用且**不需要同步下载模型**的；Paddle 只有已缓存时才用，
否则首次截图会卡 30 秒（模型下载），这在"按下热键就要出结果"的场景里不可接受。
"""

from __future__ import annotations

from ..config import AppConfig
from ..logger import get_logger
from .base import EngineInfo, OcrEngine, OcrError
from .paddle import PaddleOcrEngine
from .rapid import RapidOcrEngine
from .tesseract import TesseractOcrEngine

log = get_logger("ocr")

ENGINE_ORDER = ("rapid", "paddle", "tesseract")
_REGISTRY: dict[str, type[OcrEngine]] = {
    RapidOcrEngine.name: RapidOcrEngine,
    PaddleOcrEngine.name: PaddleOcrEngine,
    TesseractOcrEngine.name: TesseractOcrEngine,
}


def available_engines() -> list[EngineInfo]:
    out = []
    for cls in (_REGISTRY[n] for n in ENGINE_ORDER):
        try:
            out.append(cls.probe())
        except Exception as exc:  # probe 自己炸了也要报，不能让设置页空白
            out.append(EngineInfo(cls.name, cls.label, False, "", f"探测失败：{exc}"))
    return out


def create(cfg: AppConfig) -> OcrEngine:
    """按配置创建引擎实例。`auto` 时挑第一个可用的。"""
    want = cfg.ocr.engine
    if want not in {"auto", *ENGINE_ORDER}:
        log.warning("未知 OCR 引擎 %r，回退 auto", want)
        want = "auto"

    order = ENGINE_ORDER if want == "auto" else (want, *tuple(n for n in ENGINE_ORDER if n != want))
    for name in order:
        cls = _REGISTRY[name]
        info = cls.probe()
        if not info.available:
            log.info("跳过不可用引擎 %s：%s", info.label, info.note)
            continue
        if name == "paddle" and not _paddle_model_cached():
            log.info("跳过 PaddleOCR：模型未缓存，首次识别需联网下载（约 30s）")
            continue
        try:
            eng = cls(cfg)
            log.info("OCR 引擎 -> %s", info)
            return eng
        except Exception as exc:
            log.warning("引擎 %s 构造失败：%s", name, exc)
    raise OcrError(
        "没有可用的 OCR 引擎",
        fatal=True,
        hint="至少安装一个：pip install rapidocr-onnxruntime（推荐，离线）"
             " 或 pip install paddleocr 或安装 Tesseract-OCR",
    )


def _paddle_model_cached() -> bool:
    """检测 paddle 的模型是否已在本地（.paddlex 或 ~/.paddleocr）。"""
    from pathlib import Path

    home = Path.home()
    for cand in (home / ".paddleocr", home / ".paddlex"):
        if not cand.is_dir():
            continue
        for p in cand.rglob("*.pdparams"):
            return True
        for p in cand.rglob("*.pdmodel"):
            return True
    return False
