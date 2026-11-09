"""抓屏与选区裁剪。

两个关键坑，代码里都处理了：

1. **高 DPI / 混合缩放**。`QScreen.geometry()` 返回*逻辑*坐标，
   `grabWindow(0)` 在 Windows 上返回*物理*像素图。若不做 DPR 换算，
   在 150% 缩放的屏上框选 100px 会裁出 66px 的区域（内容错位）。
   本模块统一以逻辑坐标做交互，用 `image.width()/geometry.width()` 反推真实 DPR。

2. **截图时把自己拍进去**。结果窗口若在最上层，会被 OCR 识别成自己的译文，
   形成"翻译翻译"的套娃。`hide_app_windows()` 在抓屏前短暂隐藏。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..logger import get_logger
from ..models import CaptureResult
from PyQt6.QtCore import QPoint
from PyQt6.QtGui import QImage

log = get_logger("capture")


@dataclass(slots=True)
class Monitor:
    """供 UI 与自检使用的屏幕描述（纯数据，不持有 QScreen 引用）。"""

    name: str
    geometry: tuple[int, int, int, int]
    dpr: float
    primary: bool

    @property
    def area(self) -> int:
        return self.geometry[2] * self.geometry[3]

    def __str__(self) -> str:
        x, y, w, h = self.geometry
        return f"{self.name} {w}x{h} @{x},{y} dpr={self.dpr:g}{' [主屏]' if self.primary else ''}"


def _dpr_of(screen) -> float:
    """devicePixelRatio 在部分驱动下会是 0 或 1 而实际已缩放，做一次保守修正。"""
    dpr = float(screen.devicePixelRatio())
    if dpr <= 0 or dpr > 8:
        log.warning("异常 DPR=%s，回退为 1.0", dpr)
        return 1.0
    return dpr


def list_monitors() -> list[Monitor]:
    from PyQt6.QtGui import QGuiApplication  # 延迟导入：CLI 自检时也需要能 import 本模块

    app = QGuiApplication.instance()
    if app is None:
        return []
    out = []
    for s in app.screens():
        g = s.geometry()
        out.append(Monitor(s.name(), (g.x(), g.y(), g.width(), g.height()),
                           _dpr_of(s), s == app.primaryScreen()))
    return out


def virtual_bounds() -> tuple[int, int, int, int]:
    """所有屏幕并集的全局逻辑矩形（多屏拼接后覆盖层的几何）。"""
    from PyQt6.QtCore import QRect
    from PyQt6.QtGui import QGuiApplication

    app = QGuiApplication.instance()
    if app is None:
        return (0, 0, 1920, 1080)
    rect = QRect()
    for s in app.screens():
        rect = rect.united(s.geometry()) if rect.isValid() else s.geometry()
    return (rect.x(), rect.y(), rect.width(), rect.height())


def grab_screen(screen=None) -> CaptureResult:
    """抓取指定屏幕（默认主屏）全屏像素，返回未裁剪的整屏图。

    用 `QScreen.grabWindow(0)`：0 表示"该屏幕本身"，比 `QWindow.grabWindow`
    跨平台行为一致，且在 Wayland 下是唯一可行路径。
    """
    from PyQt6.QtGui import QGuiApplication

    app = QGuiApplication.instance()
    if app is None:
        raise RuntimeError("QGuiApplication 未创建，无法抓屏")
    target = screen or app.primaryScreen()
    if target is None:
        raise RuntimeError("检测不到任何屏幕")

    geo = target.geometry()
    dpr = _dpr_of(target)
    t0 = time.perf_counter()
    pixmap = target.grabWindow(0)
    if pixmap.isNull():
        raise RuntimeError("抓屏返回空图（远程桌面 / Wayland 权限受限？）")

    # 校验真实缩放：图片物理宽 / 逻辑宽
    real_dpr = pixmap.width() / max(1, geo.width())
    if abs(real_dpr - dpr) > 0.05:
        log.info("DPR 校正：声明 %.2f，实测 %.2f", dpr, real_dpr)
        dpr = real_dpr

    image = pixmap.toImage()
    image = image.convertToFormat(image.Format.Format_RGBA8888)
    buf = image.constBits()
    n = image.sizeInBytes()
    data = bytes(buf.asstring(n)[:n]) if n else b""
    ms = int((time.perf_counter() - t0) * 1000)
    log.debug("抓屏 %dx%d dpr=%.2f 用时 %dms", image.width(), image.height(), dpr, ms)
    return CaptureResult(width=image.width(), height=image.height(), bytes_bmp=data,
                         dpr=dpr, screen_name=target.name(),
                         region=(geo.x(), geo.y(), geo.width(), geo.height()))


def grab_all_screens() -> dict[str, CaptureResult]:
    """多屏时逐屏抓取（覆盖层显示各屏全屏拼图用）。"""
    from PyQt6.QtGui import QGuiApplication

    app = QGuiApplication.instance()
    out: dict[str, CaptureResult] = {}
    if app is None:
        return out
    for s in app.screens():
        try:
            out[s.name()] = grab_screen(s)
        except Exception as exc:  # 某块屏抓不到不应阻断整体
            log.warning("屏幕 %s 抓屏失败: %s", s.name(), exc)
    return out


def crop_from_capture(cap: CaptureResult, sel_logical: tuple[int, int, int, int],
                      origin_logical: tuple[int, int]) -> CaptureResult:
    """从整屏抓图中按逻辑选区裁出子图（内部完成 DPR 换算）。

    :param sel_logical: 选区 (x, y, w, h)，逻辑坐标，**相对于屏幕左上角**
    :param origin_logical: 该屏全局逻辑原点 (x, y)，用于换算多屏偏移
    """
    dpr = cap.dpr or 1.0
    sx = int(round((sel_logical[0] - origin_logical[0]) * dpr))
    sy = int(round((sel_logical[1] - origin_logical[1]) * dpr))
    sw = max(1, int(round(sel_logical[2] * dpr)))
    sh = max(1, int(round(sel_logical[3] * dpr)))

    x0 = max(0, min(sx, cap.width - 1))
    y0 = max(0, min(sy, cap.height - 1))
    x1 = max(x0 + 1, min(sx + sw, cap.width))
    y1 = max(y0 + 1, min(sy + sh, cap.height))
    w, h = x1 - x0, y1 - y0
    stride = cap.width * 4
    row_bytes = w * 4
    out = bytearray(row_bytes * h)
    src = cap.bytes_bmp
    for r in range(h):
        s_off = (y0 + r) * stride + x0 * 4
        out[r * row_bytes:(r + 1) * row_bytes] = src[s_off:s_off + row_bytes]

    gx = origin_logical[0] + int(round(x0 / dpr))
    gy = origin_logical[1] + int(round(y0 / dpr))
    log.debug("裁剪 逻辑(%d,%d,%d,%d) -> 物理(%d,%d,%d,%d)",
              sel_logical[0], sel_logical[1], sel_logical[2], sel_logical[3], x0, y0, w, h)
    return CaptureResult(width=w, height=h, bytes_bmp=bytes(out), dpr=dpr,
                         screen_name=cap.screen_name, region=(gx, gy, w, h))


def capture_from_image(image: QImage, sel_logical: tuple[int, int, int, int],
                       origin_logical: tuple[int, int], dpr: float) -> CaptureResult:
    """从覆盖层已冻结的全屏拼图（RGBA8888、物理像素）直接裁剪。

    为什么 confirm 不再抓实屏：confirm 时主窗口/托盘气泡都已被还原到屏上，
    而覆盖层是分层窗口（WA_TranslucentBackground），BitBlt 抓屏路径对分层
    窗口的合成时机不受我们控制——实测会把主窗口的残影截进 OCR 输入。
    覆盖层 start() 时拼的冻结图本来就是在隐藏自身窗口之后抓的（干净），
    直接复用它 = 所见即所选，也顺带获得"框选期间画面冻结"的 QQ 截图语义。
    """
    dpr = dpr or 1.0
    im = (image if image.format() == QImage.Format.Format_RGBA8888
          else image.convertToFormat(QImage.Format.Format_RGBA8888))
    buf = im.constBits()
    n = im.sizeInBytes()
    data = bytes(buf.asstring(n)[:n]) if n else b""
    cap = CaptureResult(width=im.width(), height=im.height(), bytes_bmp=data,
                        dpr=dpr, screen_name="frozen",
                        region=(origin_logical[0], origin_logical[1],
                                int(im.width() / dpr), int(im.height() / dpr)))
    return crop_from_capture(cap, sel_logical, origin_logical)


def capture_region(sel_logical: tuple[int, int, int, int]) -> CaptureResult:
    """按全局逻辑坐标裁一块 —— 覆盖层选完后的一站式入口（自动挑对应屏幕）。"""
    from PyQt6.QtGui import QGuiApplication

    app = QGuiApplication.instance()
    if app is None:
        raise RuntimeError("QGuiApplication 未创建")
    x, y, w, h = sel_logical
    # screenAt 只接受 QPoint，不接受两个 int —— PyQt6 会直接抛
    # TypeError("argument 1 has unexpected type 'int'")，覆盖层 confirm 崩掉。
    center = QPoint(x + w // 2, y + h // 2)
    screen = app.screenAt(center) or app.primaryScreen()
    if screen is None:  # 选区落在已拔掉的屏上
        screen = app.primaryScreen()
    geo = screen.geometry()
    if not geo.contains(center):
        log.warning("选区中心 (%d,%d) 不在任何屏内，吸附到主屏", x, y)
    cap = grab_screen(screen)
    return crop_from_capture(cap, (x, y, w, h), (geo.x(), geo.y()))


def hide_app_windows(app) -> list:
    """抓屏前隐藏本程序所有可见窗口，返回被隐藏的列表以便恢复。

    ⚠ `w.windowType().name` 返回 `'Window'`（不是 `'Qt.WindowType.Window'`）。
    第一版写成 `name != "Qt.WindowType.Window"`，结果**每个可见窗口都命中
    continue 被跳过**，实际隐藏了 0 个 —— 实测：结果窗口在抓屏后仍可见，
    自己的 UI 被截进了 OCR 输入里。修成用 `int(w.windowType())` 比较。
    """
    hidden = []
    if app is None:
        return hidden
    for w in app.topLevelWidgets():
        try:
            if not w.isVisible():
                continue
            # Qt.WindowType.Window = 1（顶层窗口）；Tool = 2；Dialog = 3 …
            wt = int(w.windowType())
            if wt != 1:               # 只隐藏顶层窗口
                continue
            w.hide()
            hidden.append(w)
        except RuntimeError:  # C++ 对象已被删除
            continue
    if hidden:
        # 必须等合成器真的把窗口移走，否则 Windows DWM 还会拍到旧帧
        app.processEvents()
        time.sleep(0.08)
    return hidden


def restore_windows(windows: list) -> None:
    for w in windows:
        try:
            w.show()
            w.raise_()
        except RuntimeError:
            pass
