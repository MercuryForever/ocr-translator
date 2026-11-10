"""全屏覆盖层：类 QQ/微信截图的框选交互。

交互约定（README 同步维护）：

    左键拖拽          画矩形选区
    选中后拖拽内部     移动选区；拖拽 8 个控制点缩放
    方向键            微调 1px；Ctrl+方向键 10px；+/- 等比缩放
    Enter / 双击 / 中键  确认并进入 OCR
    C                仅复制图片到剪贴板（不 OCR）
    R                清空选区
    Esc / 右键        取消

三条实现约束，都是踩过坑的：

1. **覆盖层几何 = 所有屏幕并集**。副屏在主屏左侧时其逻辑坐标为负，
   若覆盖层只盖 (0,0,w,h) 则整块副屏点不到。因此窗口必须 `move` 到并集原点，
   并把局部坐标 + 原点偏移换算成全局坐标（`_to_global`）。

2. **遮罩用 4 个矩形挖洞**，而不是 `setClipPath` + 半透明层：
   QGraphicsOpacityEffect / 裁剪路径在 Per-Monitor DPI v2 下会出现 1px 缝隙和
   选区内容被遮罩染灰的问题。四矩形法零依赖且像素精确。

3. **不做"单击智能选窗口"**。Windows 上要靠 `WindowFromPoint`+DWM 边框推断，
   跨平台就废了；且纯像素扫描找文字行在大图上要几十毫秒，会卡住鼠标移动。
   改为拖拽 + 放大镜像素级定位，可控性和手感都更好。

4. **抓屏前要等一拍（settle）**。`hide()` 只是向窗口管理器发出请求，DWM
   异步合成——hide 返回后立刻抓屏，屏幕上还是旧画面，正在消失的窗口像素
   被定格进冻结背景 = 残影。抓屏前等 200ms 让桌面真正画完（QQ/Snipaste
   同款处理）。
"""

from __future__ import annotations

from PyQt6.QtCore import QEventLoop, QPoint, QRect, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (QColor, QCursor, QFont, QImage, QKeyEvent, QMouseEvent,
                         QPainter, QPen, QPixmap)
from PyQt6.QtWidgets import QApplication, QWidget

from ..logger import get_logger
from ..models import CaptureResult
from . import grab

log = get_logger("capture.overlay")

_MIN_DRAG = 4          # 小于此位移视为"没拖"，不当选区
_HANDLE = 9            # 控制点命中半径（物理像素感觉太大时可改）

_C = {
    "mask": QColor(0, 0, 0, 110),
    "border": QColor(38, 139, 250),
    "handle": QColor(255, 255, 255),
    "guide": QColor(255, 90, 90, 170),
    "label_bg": QColor(20, 20, 20, 215),
    "label_fg": QColor(235, 235, 235),
    "tip_bg": QColor(20, 20, 20, 205),
}


class Magnifier(QWidget):
    """像素放大镜 + 取色器。小字号、表格线、图标密集区靠它对齐边界。"""

    ZOOM = 12
    SIDE = 144

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Tool
                         | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(self.SIDE + 2, self.SIDE + 44)
        self._src: QImage | None = None
        self._pos = QPoint()
        self._dpr = 1.0
        self._color = QColor(0, 0, 0)
        self.hide()

    def feed(self, image: QImage | None, global_pos: QPoint, dpr: float) -> None:
        self._src = image
        if image is None or image.isNull():
            self.hide()
            return
        self._pos, self._dpr = global_pos, dpr
        x, y = int(global_pos.x() * dpr), int(global_pos.y() * dpr)
        if 0 <= x < image.width() and 0 <= y < image.height():
            self._color = image.pixelColor(x, y)
        # 靠近屏幕右下边缘时把放大镜甩到左上，避免自己框不到自己
        scr = QApplication.primaryScreen()
        if scr is not None:
            geo = scr.availableGeometry()
            fx = global_pos.x() + 26
            fy = global_pos.y() + 26
            if fx + self.width() > geo.right():
                fx = global_pos.x() - self.width() - 26
            if fy + self.height() > geo.bottom():
                fy = global_pos.y() - self.height() - 26
            self.move(fx, fy)
        if not self.isVisible():
            self.show()
        self.update()

    def hide_mag(self) -> None:
        self.hide()

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(15, 15, 15))
        p.setPen(QPen(_C["border"], 1))
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))
        if self._src is None:
            p.end()
            return
        half = self.SIDE // 2 // self.ZOOM
        cx, cy = int(self._pos.x() * self._dpr), int(self._pos.y() * self._dpr)
        src = QRect(cx - half, cy - half, half * 2, half * 2)
        p.drawPixmap(QRect(1, 1, self.SIDE, self.SIDE),
                     QPixmap.fromImage(self._src.copy(src)))
        mx, my = 1 + self.SIDE // 2, 1 + self.SIDE // 2
        p.setPen(QPen(_C["guide"], 1))
        p.drawLine(mx - 7, my, mx + 7, my)
        p.drawLine(mx, my - 7, mx, my + 7)
        p.setPen(QPen(_C["guide"], 1, Qt.PenStyle.DashLine))
        p.drawRect(mx - half * self.ZOOM // 2, my - half * self.ZOOM // 2,
                   half * self.ZOOM, half * self.ZOOM)
        p.setFont(QFont("Segoe UI", 8))
        p.setPen(_C["label_fg"])
        p.drawText(5, self.SIDE + 15, f"{self._pos.x()},{self._pos.y()}")
        p.drawText(5, self.SIDE + 31,
                   f"#{self._color.name().removeprefix('#').upper()}")
        p.end()


class CaptureOverlay(QWidget):
    """一次实例服务一次截图；结果通过 `capture_finished` 发出。"""

    capture_finished = pyqtSignal(object)   # CaptureResult | None
    capture_cancelled = pyqtSignal()
    selection_changed = pyqtSignal(QRect)   # 供外部预览/状态栏使用

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._bg_image: QImage | None = None
        self._bg: QPixmap | None = None
        self._origin = QPoint(0, 0)
        self._dpr = 1.0
        self._sel = QRect()
        self._drag_origin = QPoint()
        self._press_sel = QRect()
        self._edge: int = 0
        self._mode = ""                    # new | move | resize | ""
        self._cursor = QPoint()
        self._mag = Magnifier()
        self._hint = "拖拽框选 · Enter 确认 · Esc 取消 · C 仅复制"
        self._hidden: list = []            # start() 隐藏的本程序窗口（退出时恢复）
        self._keep_hidden = False          # confirm 成功后保持隐藏，由结果窗接管
        self._settle_ms = 200              # hide→抓屏 的 DWM 合成等待（见模块注释 4）
        self._hint_timer = QTimer(self, singleShot=True, interval=1600)
        self._hint_timer.timeout.connect(lambda: setattr(self, "_hint",
                                                         "拖拽框选 · Enter 确认 · Esc 取消"))

    # ------------------------------------------------------------------ 生命周期
    def _wait_settle(self) -> None:
        """给桌面合成器留出把"窗口隐藏"真正绘制出来的时间。

        hide() 是异步请求，DWM 合成器随后才更新屏幕画面；hide 返回后立刻
        抓屏会把正在消失的窗口定格进冻结背景（残影）。QEventLoop 等待期间
        事件照常运转，hide 的窗口请求也被刷给操作系统。
        """
        if self._settle_ms <= 0:
            return
        loop = QEventLoop()
        QTimer.singleShot(self._settle_ms, loop.quit)
        loop.exec()

    def start(self) -> bool:
        """抓全屏 → 铺背景 → 显示。返回 False 表示抓屏失败（已发信号）。

        本程序窗口在抓屏前隐藏、**成功后保持隐藏**：冻结拼图是干净的，
        选区确认时直接从拼图裁剪（capture_from_image），主窗口不会残影；
        取消/复制路径在退出时恢复，成功路径由结果窗/主窗口重新 show。
        """
        app = QApplication.instance()
        self._hidden = []
        try:
            self._hidden = grab.hide_app_windows(app) if self.cfg.capture.hide_own_windows else []
            self._wait_settle()               # DWM 竞态：等旧画面真正消失（含 begin_capture 里提前隐藏的气泡）
            try:
                screens = grab.grab_all_screens()
            except BaseException:
                grab.restore_windows(self._hidden)   # 失败立刻还原，窗口不能凭空消失
                self._hidden = []
                raise
            if not screens:
                raise RuntimeError("检测不到可用屏幕")
            self._compose_background(screens)
        except Exception as exc:
            grab.restore_windows(self._hidden)
            self._hidden = []
            log.error("抓屏失败：%s", exc)
            self.capture_finished.emit(None)
            return False

        x, y, w, h = grab.virtual_bounds()
        self._origin = QPoint(x, y)
        self.setGeometry(0, 0, w, h)          # 局部坐标从 (0,0) 起，全局 = 局部 + origin
        self._sel = QRect()
        self.show()
        self.move(x, y)                      # 必须在 show 之后，frameless 多屏才落对位置
        self.raise_()
        self.activateWindow()
        return True

    def _compose_background(self, screens: dict[str, CaptureResult]) -> None:
        """把各屏抓图拼成覆盖整个虚拟桌面的大图（局部逻辑坐标 → 像素坐标）。"""
        app = QApplication.instance()
        if app is None:
            return
        self._dpr = max((c.dpr for c in screens.values()), default=1.0)
        x, y, w, h = grab.virtual_bounds()
        canvas = QImage(int(w * self._dpr), int(h * self._dpr),
                        QImage.Format.Format_RGBA8888)
        canvas.fill(QColor(28, 28, 28))
        painter = QPainter(canvas)
        for scr in app.screens():
            cap = screens.get(scr.name())
            if cap is None:
                continue
            img = QImage(cap.bytes_bmp, cap.width, cap.height,
                         cap.width * 4, QImage.Format.Format_RGBA8888)
            g = scr.geometry()
            painter.drawImage(int((g.x() - x) * self._dpr), int((g.y() - y) * self._dpr), img)
        painter.end()
        self._bg_image = canvas
        self._bg = QPixmap.fromImage(canvas)

    def closeEvent(self, e) -> None:  # noqa: N802
        self._mag.hide_mag()
        if not self._keep_hidden:       # 成功路径保持隐藏，其余任何关闭方式都还原窗口
            grab.restore_windows(self._hidden)
            self._hidden = []
        super().closeEvent(e)

    # ------------------------------------------------------------------ 坐标
    def _global(self, local: QPoint) -> QPoint:
        return QPoint(local.x() + self._origin.x(), local.y() + self._origin.y())

    def _dpr_at(self, local: QPoint) -> float:
        """选区所在屏的 DPR。混合缩放（主屏 150% + 副屏 100%）必须按屏换算。"""
        app = QApplication.instance()
        if app is None:
            return self._dpr
        scr = app.screenAt(self._global(local)) or app.primaryScreen()
        return grab._dpr_of(scr) if scr else self._dpr

    def _clamp(self, r: QRect) -> QRect:
        return r.normalized().intersected(self.rect())

    # ------------------------------------------------------------------ 鼠标
    def mousePressEvent(self, e: QMouseEvent) -> None:  # noqa: N802
        btn = e.button()
        if btn == Qt.MouseButton.RightButton:
            self.cancel()
            return
        if btn == Qt.MouseButton.MiddleButton:
            self.confirm()
            return
        if btn != Qt.MouseButton.LeftButton:
            return
        pos = e.position().toPoint()
        self._drag_origin = pos
        edge = self._edge_bits(pos)
        if self._sel.isValid() and self._sel.width() > 1 and edge:
            self._mode, self._edge = "resize", edge
            self._press_sel = QRect(self._sel)
        elif self._sel.isValid() and self._sel.contains(pos):
            self._mode, self._press_sel = "move", QRect(self._sel)
        else:
            self._mode, self._sel = "new", QRect()

    def mouseMoveEvent(self, e: QMouseEvent) -> None:  # noqa: N802
        pos = e.position().toPoint()
        self._cursor = pos
        if self.cfg.capture.magnifier:
            self._mag.feed(self._bg_image, self._global(pos), self._dpr_at(pos))
        self._update_cursor_shape(pos)

        if self._mode == "new":
            if (pos - self._drag_origin).manhattanLength() >= _MIN_DRAG:
                # ⚠ QRect(p1, p2) 按角点**原样**存储（x1=p1.x, x2=p2.x），
                # 拖拽方向决定角点顺序：只有"从右往左"恰好是正序，其余三个
                # 方向 width/height 为负 → paintEvent 的 r.width()>1 判 False，
                # 选区画不出来，confirm 也拿不到矩形 —— 实测现象是
                # "只能从右往左拖拽，不能自由拖拽"。
                # 注意不能只用 .normalized() 兜底：它对反序角点会丢四角的
                # 1px（实测 (260,180)-(100,100) normalized → (101,101,159,79)，
                # 比正序 (100,100,161,81) 四边各少 1px），左右两个方向选区
                # 就差 2px。先排序坐标再用双 QPoint 角点构造，任意方向
                # 结果逐像素一致。
                x1, x2 = sorted((pos.x(), self._drag_origin.x()))
                y1, y2 = sorted((pos.y(), self._drag_origin.y()))
                self._set_sel(QRect(QPoint(x1, y1), QPoint(x2, y2)))
        elif self._mode == "move":
            d = pos - self._drag_origin
            r = QRect(self._press_sel)
            r.translate(d)
            self._set_sel(self._clamp(r))
        elif self._mode == "resize":
            r = QRect(self._press_sel)
            if self._edge & 1:      # left
                r.setLeft(pos.x())
            if self._edge & 2:      # right
                r.setRight(pos.x())
            if self._edge & 4:      # top
                r.setTop(pos.y())
            if self._edge & 8:      # bottom
                r.setBottom(pos.y())
            self._set_sel(self._clamp(r))
        self.update()

    def mouseReleaseEvent(self, e: QMouseEvent) -> None:  # noqa: N802
        if e.button() != Qt.MouseButton.LeftButton:
            return
        if self._mode == "new" and (e.position().toPoint() - self._drag_origin).manhattanLength() < _MIN_DRAG:
            self._sel = QRect()      # 单击 = 清空，不是"智能选窗口"（见模块 docstring）
        if self._sel.width() < 2 or self._sel.height() < 2:
            self._sel = QRect()
        self._mode = ""
        self._edge = 0
        self.update()

    def mouseDoubleClickEvent(self, e: QMouseEvent) -> None:  # noqa: N802
        if self._sel.width() > 1:
            self.confirm()

    def _set_sel(self, r: QRect) -> None:
        if r != self._sel:
            self._sel = r
            self.selection_changed.emit(r)

    def _edge_bits(self, pos: QPoint) -> int:
        r = self._sel
        if r.width() <= 1 or not r.adjusted(-_HANDLE, -_HANDLE, _HANDLE, _HANDLE).contains(pos):
            return 0
        bits = 0
        bits |= 1 if abs(pos.x() - r.left()) <= _HANDLE else 0
        bits |= 2 if abs(pos.x() - r.right()) <= _HANDLE else 0
        bits |= 4 if abs(pos.y() - r.top()) <= _HANDLE else 0
        bits |= 8 if abs(pos.y() - r.bottom()) <= _HANDLE else 0
        return bits

    def _update_cursor_shape(self, pos: QPoint) -> None:
        bits = self._edge_bits(pos)
        shapes = {
            1: Qt.CursorShape.SizeHorCursor, 2: Qt.CursorShape.SizeHorCursor,
            4: Qt.CursorShape.SizeVerCursor, 8: Qt.CursorShape.SizeVerCursor,
            5: Qt.CursorShape.SizeFDiagCursor, 10: Qt.CursorShape.SizeFDiagCursor,
            6: Qt.CursorShape.SizeBDiagCursor, 9: Qt.CursorShape.SizeBDiagCursor,
            3: Qt.CursorShape.SplitHCursor, 12: Qt.CursorShape.SplitVCursor,
            15: Qt.CursorShape.SizeFDiagCursor, 7: Qt.CursorShape.SizeBDiagCursor,
            13: Qt.CursorShape.SizeBDiagCursor, 11: Qt.CursorShape.SizeFDiagCursor,
            14: Qt.CursorShape.SizeBDiagCursor,
        }
        if bits:
            self.setCursor(shapes.get(bits, Qt.CursorShape.CrossCursor))
        elif self._sel.contains(pos):
            self.setCursor(Qt.CursorShape.SizeAllCursor)
        else:
            self.setCursor(Qt.CursorShape.CrossCursor)

    # ------------------------------------------------------------------ 键盘
    def keyPressEvent(self, e: QKeyEvent) -> None:  # noqa: N802
        k, mod = e.key(), e.modifiers()
        if k == Qt.Key.Key_Escape:
            self.cancel()
        elif k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.confirm()
        elif k == Qt.Key.Key_C and self._sel.width() > 1:
            self.confirm(copy_only=True)
        elif k == Qt.Key.Key_R:
            self._sel = QRect()
            self.update()
        elif k in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down):
            self._nudge(k, 10 if mod & Qt.KeyboardModifier.ControlModifier else 1)
        elif k in (Qt.Key.Key_Plus, Qt.Key.Key_Equal, Qt.Key.Key_Minus):
            self._resize_uniform(4 if k != Qt.Key.Key_Minus else -4)
        else:
            super().keyPressEvent(e)

    def _nudge(self, key, step: int) -> None:
        if self._sel.width() <= 1:
            return
        r = QRect(self._sel)
        if key == Qt.Key.Key_Left:
            r.setLeft(r.left() + step)
        elif key == Qt.Key.Key_Right:
            r.setRight(r.right() + step)
        elif key == Qt.Key.Key_Up:
            r.setTop(r.top() + step)
        else:
            r.setBottom(r.bottom() + step)
        self._set_sel(self._clamp(r.normalized()))
        self.update()

    def _resize_uniform(self, d: int) -> None:
        if self._sel.width() <= 1:
            return
        r = QRect(self._sel)
        r.adjust(-d, -d, d, d)
        self._set_sel(self._clamp(r.normalized()))
        self.update()

    # ------------------------------------------------------------------ 出图
    def confirm(self, copy_only: bool = False) -> None:
        r = self._sel
        if r.width() < 2 or r.height() < 2:
            self._hint = "选区太小：请按住左键拖出一个矩形"
            self._hint_timer.start()
            self.update()
            return
        g = QRect(r)
        g.translate(self._origin)
        sel = (g.x(), g.y(), g.width(), g.height())
        try:
            if self._bg_image is not None and not self._bg_image.isNull():
                # 冻结拼图裁剪：干净（拼图抓取时已隐藏自身窗口）、无 DWM 竞态
                result = grab.capture_from_image(self._bg_image, sel,
                                                 (self._origin.x(), self._origin.y()),
                                                 self._dpr)
            else:                       # 拼图缺失（异常环境）才退回实抓
                result = grab.capture_region(sel)
        except Exception as exc:
            log.error("裁剪失败：%s", exc)
            self._hint = f"裁剪失败：{exc}"
            self._hint_timer.start()
            self.update()
            return
        self._mag.hide_mag()
        if copy_only:
            img = QImage(result.bytes_bmp, result.width, result.height,
                         result.width * 4, QImage.Format.Format_RGBA8888)
            QApplication.clipboard().setImage(img)
            grab.restore_windows(self._hidden)   # 只复制：回到用户原来界面
            self._hidden = []
            self.capture_cancelled.emit()
            self.close()
            return
        self._keep_hidden = True        # 成功：保持隐藏，结果窗/主窗口接管显示
        self.capture_finished.emit(result)
        self.close()

    def cancel(self) -> None:
        self._mag.hide_mag()
        grab.restore_windows(self._hidden)
        self._hidden = []
        self.capture_cancelled.emit()
        self.close()

    # ------------------------------------------------------------------ 绘制
    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        if self._bg:
            p.drawPixmap(self.rect(), self._bg, QRect(0, 0, self._bg.width(), self._bg.height()))
        else:
            p.fillRect(self.rect(), QColor(40, 40, 40))

        r = self._sel
        if r.width() > 1 and r.height() > 1:
            self._paint_mask(p, r)
            # 选区内画原图（不套遮罩），等价于"挖洞"
            src = QRect(int(r.x() * self._dpr), int(r.y() * self._dpr),
                        max(1, int(r.width() * self._dpr)), max(1, int(r.height() * self._dpr)))
            if self._bg:
                p.drawPixmap(r, self._bg, src)
            p.setPen(QPen(_C["border"], 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(r)
            self._paint_handles(p, r)
            self._paint_guides(p, r)
            self._paint_size_label(p, r)
        else:
            p.fillRect(self.rect(), _C["mask"])
        self._paint_hint(p)
        p.end()

    def _paint_mask(self, p: QPainter, r: QRect) -> None:
        """四矩形挖洞：比 clipPath 稳，且在 DPR 缩放下无 1px 漏光。"""
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_C["mask"])
        p.drawRect(QRect(0, 0, self.width(), r.top()))                       # 上
        p.drawRect(QRect(0, r.bottom() + 1, self.width(), self.height() - r.bottom() - 1))
        p.drawRect(QRect(0, r.top(), r.left(), r.height()))                  # 左
        p.drawRect(QRect(r.right() + 1, r.top(), self.width() - r.right() - 1, r.height()))

    def _paint_handles(self, p: QPainter, r: QRect) -> None:
        p.setBrush(_C["handle"])
        p.setPen(QPen(_C["border"], 1))
        for pt in self._handle_points(r):
            p.drawRect(QRect(pt.x() - _HANDLE // 2, pt.y() - _HANDLE // 2, _HANDLE, _HANDLE))

    @staticmethod
    def _handle_points(r: QRect) -> list[QPoint]:
        cx, cy = r.center().x(), r.center().y()
        return [QPoint(r.left(), r.top()), QPoint(cx, r.top()), QPoint(r.right(), r.top()),
                QPoint(r.left(), cy), QPoint(r.right(), cy),
                QPoint(r.left(), r.bottom()), QPoint(cx, r.bottom()), QPoint(r.right(), r.bottom())]

    def _paint_guides(self, p: QPainter, r: QRect) -> None:
        if r.width() < 40 or r.height() < 40:
            return
        p.setPen(QPen(QColor(255, 255, 255, 60), 1, Qt.PenStyle.DotLine))
        for i in (1, 2):
            p.drawLine(r.left() + r.width() * i // 3, r.top(), r.left() + r.width() * i // 3, r.bottom())
            p.drawLine(r.left(), r.top() + r.height() * i // 3, r.right(), r.top() + r.height() * i // 3)

    def _paint_size_label(self, p: QPainter, r: QRect) -> None:
        txt = f"{r.width()} × {r.height()}   逻辑坐标 {r.x() + self._origin.x()},{r.y() + self._origin.y()}"
        p.setFont(QFont("Segoe UI", 9))
        w = p.fontMetrics().horizontalAdvance(txt) + 14
        y = r.top() - 22 if r.top() > 34 else r.bottom() + 3
        box = QRect(r.left(), y, w, 19)
        p.fillRect(box, _C["label_bg"])
        p.setPen(_C["label_fg"])
        p.drawText(box.adjusted(7, 0, 0, 0), Qt.AlignmentFlag.AlignVCenter, txt)

    def _paint_hint(self, p: QPainter) -> None:
        p.setFont(QFont("Microsoft YaHei", 9))
        w = p.fontMetrics().horizontalAdvance(self._hint) + 24
        box = QRect(self.width() // 2 - w // 2, 10, w, 26)
        p.fillRect(box, _C["tip_bg"])
        p.setPen(_C["label_fg"])
        p.drawText(box, Qt.AlignmentFlag.AlignCenter, self._hint)
