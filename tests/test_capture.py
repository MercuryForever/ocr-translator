"""抓屏与坐标换算测试。

重点覆盖两个**真实截图才暴露**的 bug：
  1. `QGuiApplication.screenAt()` 只接受 QPoint —— 传两个 int 会在
     覆盖层 confirm() 时抛 TypeError，用户看到"裁剪失败"提示。
  2. `CaptureResult.width/height` 是**物理像素**，选区是逻辑坐标 ——
     断言时不能直接相等，必须乘 DPR（150% 缩放下 569 逻辑 px = 854 物理 px）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ocrtranslator import bootstrap

bootstrap.bootstrap(headless=False)

from PyQt6.QtCore import QPoint
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import QApplication

from ocrtranslator.capture import grab
from ocrtranslator.models import CaptureResult


_APP = None


def _app():
    # 必须把实例存进模块全局：调用方若不接收返回值，
    # 局部 QApplication 会被 GC 销毁，下一个 QWidget 构造直接
    # "Must construct a QApplication before a QWidget" 崩掉。
    global _APP
    _APP = QApplication.instance() or QApplication(sys.argv[:1])
    return _APP


class TestScreenAt(unittest.TestCase):
    """回归：screenAt 必须传 QPoint。"""

    def test_screen_at_requires_qpoint(self):
        app = _app()
        screen = app.screenAt(QPoint(10, 10))
        # 不崩溃且返回一个屏（单屏环境返回主屏）
        self.assertIsNotNone(screen)
        with self.assertRaises(TypeError):
            app.screenAt(10, 10)          # 两个 int：PyQt6 直接拒绝

    def test_capture_region_no_type_error(self):
        """核心回归：confirm() 走的路径，选区中心必须能找到所属屏。"""
        app = _app()
        s = app.primaryScreen()
        g = s.geometry()
        sel = (g.x() + g.width() // 4, g.y() + g.height() // 4,
               g.width() // 3, g.height() // 3)
        cap = grab.capture_region(sel)      # 修复前在这里抛 TypeError
        dpr = cap.dpr or 1.0
        self.assertEqual(cap.width, round(sel[2] * dpr))
        self.assertEqual(cap.height, round(sel[3] * dpr))
        self.assertEqual(len(cap.bytes_bmp), cap.width * cap.height * 4)


class TestCropMath(unittest.TestCase):
    """纯计算：DPR 换算与多屏偏移，不依赖真实抓屏。"""

    def _cap(self, w, h, dpr, origin):
        return CaptureResult(width=w, height=h, bytes_bmp=b"\x00" * (w * h * 4),
                             dpr=dpr, screen_name="t", region=origin)

    def test_dpr_scaling(self):
        cap = self._cap(2560, 1600, 1.5, (0, 0, 2560, 1600))
        out = grab.crop_from_capture(cap, (100, 50, 300, 200), (0, 0))
        self.assertEqual((out.width, out.height), (450, 300))
        self.assertAlmostEqual(out.dpr, 1.5)

    def test_dpr_one_is_identity(self):
        cap = self._cap(1000, 800, 1.0, (0, 0, 1000, 800))
        out = grab.crop_from_capture(cap, (10, 10, 100, 80), (0, 0))
        self.assertEqual((out.width, out.height), (100, 80))

    def test_multi_screen_offset(self):
        """副屏在 (1920,0)：选区全局坐标要减去屏原点再乘 DPR。"""
        cap = self._cap(1920, 1080, 1.0, (1920, 0, 1920, 1080))
        out = grab.crop_from_capture(cap, (2000, 100, 200, 150), (1920, 0))
        self.assertEqual((out.width, out.height), (200, 150))

    def test_selection_clamped_to_capture(self):
        """选区越界（拖到屏外）不能崩溃，也不能产生负尺寸。"""
        cap = self._cap(1000, 800, 1.0, (0, 0, 1000, 800))
        out = grab.crop_from_capture(cap, (900, 700, 500, 400), (0, 0))
        self.assertGreater(out.width, 0)
        self.assertGreater(out.height, 0)
        self.assertLessEqual(out.width, cap.width)

    def test_min_size_one(self):
        cap = self._cap(100, 100, 1.0, (0, 0, 100, 100))
        out = grab.crop_from_capture(cap, (50, 50, 1, 1), (0, 0))
        self.assertEqual((out.width, out.height), (1, 1))


class TestFrozenCrop(unittest.TestCase):
    """回归：confirm() 从覆盖层冻结拼图裁剪，不再抓实屏。

    抓实屏会把已被还原的主窗口/气泡截进 OCR 输入（残影 bug）；
    capture_from_image 必须给出与 crop_from_capture 相同的裁剪语义。
    """

    def _image(self, w=400, h=300, dpr=1.0):
        from PyQt6.QtGui import QColor, QImage

        img = QImage(int(w * dpr), int(h * dpr), QImage.Format.Format_RGBA8888)
        img.fill(QColor(10, 20, 30))
        # 在物理坐标 (50,40) 起画一块 80x60 的纯色标记块，验证裁剪像素对位
        for y in range(40, 100):
            for x in range(50, 130):
                img.setPixel(x, y, 0xFFFF0000)      # 不透明红
        return img

    def test_crops_exact_region_at_dpr1(self):
        img = self._image(dpr=1.0)
        # 选区 (60,50,40,30) 完全落在标记块（物理 50..130 × 40..100）内部
        out = grab.capture_from_image(img, (60, 50, 40, 30), (0, 0), 1.0)
        self.assertEqual((out.width, out.height), (40, 30))
        self.assertEqual(len(out.bytes_bmp), out.width * out.height * 4)
        # 选区左上角应为纯红（标记块内部），证明像素对位正确
        r, g, b, a = out.bytes_bmp[0], out.bytes_bmp[1], out.bytes_bmp[2], out.bytes_bmp[3]
        self.assertEqual((r, g, b, a), (255, 0, 0, 255))

    def test_crops_at_dpr_150(self):
        """150% 缩放：逻辑选区 (20,10,30,20) → 物理 (30,15,45,30)。"""
        img = self._image(dpr=1.5)
        out = grab.capture_from_image(img, (20, 10, 30, 20), (0, 0), 1.5)
        self.assertEqual((out.width, out.height), (45, 30))

    def test_respects_virtual_origin(self):
        """虚拟桌面原点非零（如副屏在 (1920,0)）：减原点后再乘 DPR。"""
        img = self._image(dpr=1.0)
        out = grab.capture_from_image(img, (1920 + 30, 10, 50, 40), (1920, 0), 1.0)
        self.assertEqual((out.width, out.height), (50, 40))

    def test_non_rgba_input_converted(self):
        from PyQt6.QtGui import QImage

        img = self._image(dpr=1.0).convertToFormat(QImage.Format.Format_RGB32)
        out = grab.capture_from_image(img, (0, 0, 10, 10), (0, 0), 1.0)
        self.assertEqual((out.width, out.height), (10, 10))


class TestHideWindows(unittest.TestCase):
    """回归：windowType().name 恒为 'Window'，旧比较隐藏了 0 个窗口。"""

    def test_hides_toplevel_and_restores(self):
        from PyQt6.QtWidgets import QWidget

        app = _app()
        w = QWidget()
        w.setWindowFlag(__import__("PyQt6.QtCore", fromlist=["Qt"]).Qt.WindowType.Window)
        w.show()
        app.processEvents()
        self.assertTrue(w.isVisible())
        hidden = grab.hide_app_windows(app)
        try:
            self.assertIn(w, hidden, "顶层窗口应被隐藏（旧代码比较恒 False 隐藏 0 个）")
            self.assertFalse(w.isVisible())
        finally:
            grab.restore_windows(hidden)
            app.processEvents()
            self.assertTrue(w.isVisible())
            w.close()

    def test_ignores_already_hidden(self):
        from PyQt6.QtWidgets import QWidget

        app = _app()
        w = QWidget()                      # 从未 show
        before = list(grab.hide_app_windows(app))
        grab.restore_windows(before)
        self.assertNotIn(w, before)


class TestRubberBand(unittest.TestCase):
    """回归：新选区必须 normalized()，否则只有"从右往左拖"能用。

    QRect(p1, p2) 按角点原样存储：从左往右拖时 x1>x2，width 为负，
    paintEvent 的 r.width()>1 判 False → 选区画不出来、confirm 拿不到矩形。
    用户实测现象就是"只能从右往左拖拽，不能自由拖拽"。
    """

    def _overlay(self):
        from ocrtranslator.config import AppConfig
        from ocrtranslator.capture.overlay import CaptureOverlay

        _app()
        return CaptureOverlay(AppConfig.load())

    def _mouse(self, ov, etype, pos, button, buttons):
        from PyQt6.QtCore import QEvent, QPointF
        from PyQt6.QtGui import QMouseEvent
        from PyQt6.QtCore import Qt as _Qt

        ev = QMouseEvent(etype, QPointF(*pos), QPointF(*pos),
                         button, buttons, _Qt.KeyboardModifier.NoModifier)
        if etype == QEvent.Type.MouseButtonPress:
            ov.mousePressEvent(ev)
        elif etype == QEvent.Type.MouseMove:
            ov.mouseMoveEvent(ev)
        else:
            ov.mouseReleaseEvent(ev)

    def _drag(self, ov, p1, p2):
        from PyQt6.QtCore import QEvent
        from PyQt6.QtCore import Qt as _Qt

        self._mouse(ov, QEvent.Type.MouseButtonPress, p1,
                    _Qt.MouseButton.LeftButton, _Qt.MouseButton.LeftButton)
        self._mouse(ov, QEvent.Type.MouseMove, p2,
                    _Qt.MouseButton.NoButton, _Qt.MouseButton.LeftButton)
        self._mouse(ov, QEvent.Type.MouseButtonRelease, p2,
                    _Qt.MouseButton.LeftButton, _Qt.MouseButton.NoButton)

    def test_all_directions_same_rect(self):
        from PyQt6.QtCore import QRect

        ov = self._overlay()
        # 每次拖拽放在互不重叠的区域：若起点落在上一次选区的控制点
        # （9px 命中半径）或选区内，会进入 resize/move 模式而不是画新选区。
        cases = (((100, 100), (260, 180), QRect(100, 100, 161, 81)),  # →↓
                 ((460, 280), (300, 200), QRect(300, 200, 161, 81)),  # ←↑（修复前唯一可用）
                 ((300, 300), (460, 220), QRect(300, 220, 161, 81)),  # →↑
                 ((460, 100), (300, 180), QRect(300, 100, 161, 81)))  # ←↓
        for p1, p2, expect in cases:
            self._drag(ov, p1, p2)
            self.assertEqual(ov._sel, expect,
                             f"拖拽 {p1}->{p2} 应得到 {expect}，实得 {ov._sel}")

    def test_release_keeps_selection(self):
        from PyQt6.QtCore import QRect

        ov = self._overlay()
        self._drag(ov, (40, 40), (140, 90))
        self.assertEqual(ov._sel, QRect(40, 40, 101, 51))


class TestSettle(unittest.TestCase):
    """回归：抓屏前必须等 DWM 合成完"窗口隐藏"。

    hide() 是异步请求——hide 返回后立刻抓屏，正在消失的窗口像素会被
    定格进冻结背景（用户实测残影）。start() 在 hide 与 grab 之间等
    _settle_ms（默认 200ms，测试置 0 秒过）。
    """

    def _overlay(self):
        from ocrtranslator.config import AppConfig
        from ocrtranslator.capture.overlay import CaptureOverlay

        _app()
        return CaptureOverlay(AppConfig.load())

    def test_default_settle_positive(self):
        ov = self._overlay()
        self.assertGreaterEqual(ov._settle_ms, 100)   # 真实路径必须等待

    def test_zero_settle_returns_immediately(self):
        import time

        ov = self._overlay()
        ov._settle_ms = 0
        t0 = time.perf_counter()
        ov._wait_settle()
        self.assertLess(time.perf_counter() - t0, 0.05)

    def test_settle_actually_waits(self):
        import time

        ov = self._overlay()
        ov._settle_ms = 30                            # 用短值验证等待真实发生
        t0 = time.perf_counter()
        ov._wait_settle()
        self.assertGreaterEqual(time.perf_counter() - t0, 0.025)


if __name__ == "__main__":
    unittest.main(verbosity=2)
