"""OCR 引擎适配层的公共约定。

统一约定，屏蔽三家引擎差异：
  * 输入统一是 `CaptureResult`（RGBA 字节），适配器内部自己转 numpy；
  * 输出统一是 `list[OcrLine]`，坐标为**物理像素**（调用方再按 DPR 折算逻辑坐标）；
  * 引擎初始化放在工作线程，首次调用会下载/加载模型（Paddle 首次约 30s），
    因此 `warmup()` 是显式的，UI 用进度条盖住它；
  * 任何引擎内部异常都包成 `OcrError`，上层靠它决定是否降级到备用引擎。
"""

from __future__ import annotations

import abc
from dataclasses import dataclass

from ..models import CaptureResult, OcrLine


class OcrError(RuntimeError):
    def __init__(self, msg: str, *, fatal: bool = False, hint: str = ""):
        super().__init__(msg)
        self.hint = hint
        # fatal=True 表示"换引擎也没用"（例如依赖缺失），否则视为可重试/可降级
        self.fatal = fatal


@dataclass(slots=True)
class EngineInfo:
    name: str
    label: str
    available: bool
    version: str = ""
    note: str = ""
    offline: bool = True

    def __str__(self) -> str:
        s = f"{self.label} {self.version}".strip()
        return s + ("" if self.available else "（不可用）") + (f" - {self.note}" if self.note else "")


class OcrEngine(abc.ABC):
    """所有 OCR 后端的基类。"""

    name = "base"
    label = "Base"

    def __init__(self, cfg):
        self.cfg = cfg.ocr
        self._ready = False

    # ---- 生命周期
    @abc.abstractmethod
    def warmup(self) -> None:
        """加载模型。必须幂等：重复调用只做一次实际工作。"""

    @abc.abstractmethod
    def recognize(self, cap: CaptureResult, lang_hint: str = "auto") -> list[OcrLine]:
        """识别整张图。返回按 (y, x) 粗排序的行列表。"""

    @classmethod
    @abc.abstractmethod
    def probe(cls) -> EngineInfo:
        """静态探测依赖是否就绪（不加载模型，UI 启动时调用）。"""

    def close(self) -> None:
        self._ready = False

    # ---- 供子类复用的工具
    @staticmethod
    def to_numpy(cap: CaptureResult, mode: str = "bgr"):
        """RGBA 字节 -> HxWx3 uint8（cv2 约定 BGR / PIL 约定 RGB）。"""
        import numpy as np

        arr = np.frombuffer(cap.bytes_bmp, dtype=np.uint8).reshape(cap.height, cap.width, 4)
        if mode == "bgr":
            return np.ascontiguousarray(arr[:, :, [2, 1, 0]])
        if mode == "rgb":
            return np.ascontiguousarray(arr[:, :, :3])
        if mode == "gray":
            return np.ascontiguousarray(arr[:, :, :3].astype("float32")
                                        @ np.array([0.299, 0.587, 0.114], dtype="float32")
                                        ).astype("uint8")
        if mode == "rgba":
            return arr
        raise ValueError(f"未知模式 {mode}")

    @staticmethod
    def poly_to_line(poly) -> tuple[float, float, float, float]:
        """四点/任意多点 -> 轴对齐包围盒。"""
        xs = [float(p[0]) for p in poly]
        ys = [float(p[1]) for p in poly]
        return (min(xs), min(ys), max(xs), max(ys))
