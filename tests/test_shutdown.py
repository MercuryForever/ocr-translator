"""退出清理测试：QThread 必须 wait 完才放进程走。

背景（用户实测："实例反复运行退出几次后，后台进程就退出不彻底了"）：
shutdown() 原来只等 worker、不预热线程 _warm/_re —— 预热（OCR 模型加载
最长可到数秒）或重译进行中退出 → QThread 销毁仍在运行 → Qt abort/hang，
表现就是进程残留。修复后所有流水线 QThread 登记进 _threads，shutdown
逐个 wait；且 shutdown 幂等（aboutToQuit 与 finally 都可能调用）。
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ocrtranslator import bootstrap

bootstrap.bootstrap(headless=False)

from PyQt6.QtCore import QThread, QTimer
from PyQt6.QtWidgets import QApplication

from ocrtranslator.config import AppConfig

_APP = None


def _app():
    global _APP
    _APP = QApplication.instance() or QApplication(sys.argv[:1])
    return _APP


class _StubHotkeys:
    def __init__(self):
        self.shutdown_calls = 0

    def shutdown(self) -> None:
        self.shutdown_calls += 1


class _StubEngine:
    def __init__(self):
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _SlowThread(QThread):
    """跑 0.25s 的真 QThread：shutdown 必须 wait 到它结束再返回。"""

    def __init__(self):
        super().__init__()
        self.done_at = None

    def run(self):
        time.sleep(0.25)
        self.done_at = time.monotonic()


class TestShutdown(unittest.TestCase):

    def _ctrl(self):
        from ocrtranslator.app import AppController

        _app()
        ctrl = AppController(AppConfig.load())
        ctrl.hotkeys = _StubHotkeys()      # 替换真实 Win32 热键管理器
        return ctrl

    def test_shutdown_waits_for_running_thread(self):
        ctrl = self._ctrl()
        t = _SlowThread()
        ctrl._threads = [t]
        t.start()
        self.assertTrue(t.isRunning())
        ctrl.shutdown()
        # wait 语义：返回时线程必然已结束（run 里的 done_at 已被写上）
        self.assertIsNotNone(t.done_at, "shutdown 应等 QThread 跑完再返回")
        self.assertFalse(t.isRunning())

    def test_shutdown_idempotent(self):
        ctrl = self._ctrl()
        ctrl.shutdown()
        ctrl.shutdown()
        ctrl.shutdown()
        self.assertEqual(ctrl.hotkeys.shutdown_calls, 1,
                         "shutdown 必须幂等：aboutToQuit 与 finally 都会调")

    def test_shutdown_closes_engine_and_translator(self):
        ctrl = self._ctrl()
        eng = _StubEngine()
        ctrl._ocr = eng
        closed = []
        real_close = ctrl.translator.close
        ctrl.translator.close = lambda: closed.append(1) or real_close()
        ctrl.shutdown()
        self.assertTrue(eng.closed)
        self.assertEqual(closed, [1])

    def test_shutdown_with_no_threads_is_safe(self):
        ctrl = self._ctrl()
        ctrl._ocr = None
        ctrl.shutdown()          # 从未预热/截图过也要能干净退出
        self.assertEqual(ctrl.hotkeys.shutdown_calls, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
