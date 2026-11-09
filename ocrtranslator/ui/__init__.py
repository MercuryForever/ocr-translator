"""UI 层。

窗口分工：
    result_window  双语对照主窗口（核心）
    settings       设置页（识别 / 翻译 / 截图 / 界面 / 导出 / 高级）
    tray           托盘、程序化图标、无主窗口时的生命周期锚点
    theme          QSS 生成与明暗解析

"看看 OCR 到底认了什么"这个调试需求由 result_window 的
「视图 → 显示原始行」承担（逐行置信度 + 坐标），
不单独做叠加窗口 —— 少一个窗口就少一套坐标换算与生命周期。
"""

from __future__ import annotations
