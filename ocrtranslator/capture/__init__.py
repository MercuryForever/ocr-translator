"""截图子系统：抓屏、框选覆盖层、全局热键。

依赖方向：本包只向上依赖 models/logger/config，**不依赖 ocr/translate/ui**。
覆盖层通过信号把 CaptureResult 交出去，由 app.py 决定后续流程，
这样"截图"与"识别"可以各自独立测试与替换。

这里刻意**不做** `from . import overlay` 之类的预导入：
overlay 在模块级就 import PyQt6.QtWidgets，预导入会让"只想拿 grab 做抓屏"
的无 GUI 场景（CLI 自检、服务端批量识别）被迫加载 Qt。
子模块按需 `from ocrtranslator.capture import grab` 即可。
"""

from __future__ import annotations

__all__ = ["grab", "hotkey", "overlay"]
