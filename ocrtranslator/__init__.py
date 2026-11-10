"""socrans —— 截图 OCR 双语对照翻译工具。

模块分层（严格单向依赖，便于单测与替换实现）：

    capture   抓屏 / 框选 / 全局热键
    ocr       OCR 引擎适配层（RapidOCR、PaddleOCR、Tesseract）
    layout    基于几何的 OCR 块 → 行 → 段落配对
    translate 翻译后端（Google/DeepL/百度/OPUS-MT）+ 结果缓存
    export    TXT / DOCX / JSON / 剪贴板导出
    ui        PyQt6 窗口层
    app       流程编排（Worker/信号）
"""

from __future__ import annotations

__version__ = "1.1.2"
__appname__ = "截屏翻译"
APP_NAME = "socrans"
