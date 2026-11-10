"""独立提示窗口测试：ToastWindow（应用内气泡）+ HubWindow（主窗口）。

覆盖两个真实需求对应的回归：
  1. Toast 不依赖 Windows 通知中心（专注模式会吞托盘气泡）；
  2. HubWindow 必须是 windowType==Window 的顶层窗口 ——
     grab.hide_app_windows 只隐藏 Window(1)，QDialog(3) 会被跳过，
     主窗口内容会被截进 OCR 输入。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ocrtranslator import bootstrap

bootstrap.bootstrap(headless=False)

from PyQt6.QtWidgets import QApplication

from ocrtranslator.config import AppConfig

_APP = None


def _app():
    # 与 test_capture 同理：实例必须挂在模块全局，防止被 GC
    global _APP
    _APP = QApplication.instance() or QApplication(sys.argv[:1])
    return _APP


class TestToast(unittest.TestCase):

    def _toast(self):
        from ocrtranslator.ui.hub import ToastWindow

        _app()
        return ToastWindow(AppConfig.load())

    def test_show_message_updates_labels(self):
        t = self._toast()
        t.show_message("翻译完成", "3 段 · mymemory · 2.1s", "ok")
        self.assertEqual(t.lb_title.text(), "翻译完成")
        self.assertEqual(t.lb_body.text(), "3 段 · mymemory · 2.1s")
        self.assertTrue(t.lb_body.isVisible())
        self.assertTrue(t._timer.isActive())

    def test_second_message_replaces_first(self):
        t = self._toast()
        t.show_message("第一条", "a")
        t.show_message("第二条")
        self.assertEqual(t.lb_title.text(), "第二条")
        self.assertFalse(t.lb_body.isVisible())   # 空 body 应隐藏

    def test_click_dismisses(self):
        t = self._toast()
        t.show_message("点我", "x")
        t.mousePressEvent(None)                   # 实现只用 _e 占位
        self.assertFalse(t.isVisible())


class TestHub(unittest.TestCase):

    def _hub(self, **kw):
        from ocrtranslator.ui.hub import HubWindow

        _app()
        return HubWindow(AppConfig.load(), **kw)

    def test_window_type_is_window_not_dialog(self):
        """hide_app_windows 只隐藏 windowType==Window(1)：QDialog(3) 会被跳过。"""
        from PyQt6.QtCore import Qt as _Qt

        hub = self._hub()
        self.assertEqual(int(hub.windowType()),
                         int(_Qt.WindowType.Window),
                         "必须用 Window 旗标，否则截图时不会被隐藏，"
                         "主窗口内容会被截进 OCR")

    def test_set_row_updates_text(self):
        hub = self._hub()
        hub.set_row("热键", "Ctrl+Shift+A 已注册", "ok")
        self.assertEqual(hub.rows["热键"].text(), "Ctrl+Shift+A 已注册")
        hub.set_row("OCR", "初始化失败", "error")
        self.assertEqual(hub.rows["OCR"].text(), "初始化失败")

    def test_log_capped(self):
        from ocrtranslator.ui.hub import _LOG_CAP

        hub = self._hub()
        for i in range(_LOG_CAP + 5):
            hub.add_log(f"消息 {i}")
        self.assertEqual(hub.log_list.count(), _LOG_CAP)
        first = hub.log_list.item(0).text()
        self.assertIn("消息 5", first, "超出的旧条目应被丢掉")

    def test_capture_button_emits_signal(self):
        hub = self._hub()
        fired = []
        hub.capture_requested.connect(lambda: fired.append(1))
        hub.btn_capture.click()
        self.assertEqual(fired, [1])

    def test_quit_button_wired(self):
        hub = self._hub()
        self.assertTrue(hasattr(hub, "btn_quit"), "主窗口必须有明确的「退出」按钮")
        self.assertEqual(hub.btn_quit.text(), "退出")

    def test_close_with_tray_hides_and_notifies_once(self):
        """托盘模式：× 隐藏不退出，但首次必须弹气泡告知"程序仍在运行"。"""
        from ocrtranslator.ui import hub as hub_mod

        hub = self._hub()
        fired = []
        hub.minimized.connect(lambda: fired.append(1))
        hub.show()
        hub.close()
        self.assertFalse(hub.isVisible())
        self.assertEqual(fired, [1], "首次隐藏应发 minimized 信号（接 toast 提醒）")
        hub.show()
        hub.close()
        self.assertEqual(fired, [1], "只有第一次隐藏提醒，不刷屏")

    def test_close_without_tray_quits(self):
        """无托盘（--no-tray/不支持）：× 就是退出，否则窗口藏起来进程还在。"""
        from ocrtranslator.ui import hub as hub_mod

        hub = self._hub(exits_on_close=True)

        class _StubApp:
            def __init__(self):
                self.quit_calls = 0

            def instance(self):
                return self

            def quit(self):
                self.quit_calls += 1

        stub = _StubApp()
        orig = hub_mod.QApplication
        hub_mod.QApplication = stub
        try:
            hub.show()
            hub.close()
        finally:
            hub_mod.QApplication = orig
        self.assertEqual(stub.quit_calls, 1, "无托盘时 × 必须触发退出")


if __name__ == "__main__":
    unittest.main(verbosity=2)
