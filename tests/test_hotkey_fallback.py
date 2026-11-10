"""热键占用自动回退 + 全局互斥量单实例测试。

覆盖两处"被占用/双开"场景的修复：
  1. 主热键被 QQ/微信/Snipaste 等占用时，bind_hotkeys 必须自动换
     备用组合并写 hotkey_note（静默失败的用户体验 = "程序坏了/要提权"，
     用户 2026-11-10 的原话反馈）。
  2. Global 命名互斥量：两个实例的 app_dir 可能不同（管理员/沙箱
     重定向），文件锁互相看不见，但内核对象全机器可见。
"""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ocrtranslator import bootstrap

bootstrap.bootstrap(headless=False)

from ocrtranslator.capture import hotkey as hk


class _FakeBackend:
    """按"占用集合"决定注册成败的桩后端（不碰真实 Win32）。"""

    available = True

    def __init__(self, occupied=()):
        self.occupied = {o.lower() for o in occupied}
        self.last_error = ""
        self.registered: list[str] = []
        self.started = False

    def start(self) -> bool:
        self.started = True
        return True

    def register(self, spec, cb) -> bool:
        key = spec.raw.lower()
        if key in self.occupied:
            self.last_error = "已被其他程序占用（测试桩）"
            return False
        self.registered.append(key)
        return True

    def unregister_all(self) -> None:
        self.registered.clear()

    def shutdown(self) -> None:
        self.unregister_all()


def _make_manager(occupied=()) -> hk.HotkeyManager:
    mgr = hk.HotkeyManager(lambda name: None)
    mgr._backend = _FakeBackend(occupied)   # 替换真实 Win32 后端
    mgr.supported = True
    return mgr


class TestBindFallback(unittest.TestCase):
    """bind_fallback：占用时按链换键，全占用返回空串。"""

    def test_first_free_in_chain_wins(self):
        mgr = _make_manager(occupied=("ctrl+shift+a",))
        got = mgr.bind_fallback("capture", "ctrl+shift+a",
                                hk.HotkeyManager.FALLBACK_CAPTURE)
        self.assertEqual(got, "ctrl+alt+x")
        self.assertIn("ctrl+alt+x", mgr._backend.registered)

    def test_skips_occupied_and_primary(self):
        chain = ("ctrl+alt+x", "ctrl+shift+x", "ctrl+alt+q")
        # 前两个占用 → 落到链尾的 ctrl+alt+q
        mgr = _make_manager(occupied=("ctrl+alt+x", "ctrl+shift+x"))
        self.assertEqual(mgr.bind_fallback("capture", "ctrl+shift+a", chain),
                         "ctrl+alt+q")
        # 链上全部占用 → 空串
        mgr2 = _make_manager(occupied=("ctrl+alt+x", "ctrl+shift+x", "ctrl+alt+q"))
        self.assertEqual(mgr2.bind_fallback("capture", "ctrl+shift+a", chain), "")

    def test_never_rebind_primary(self):
        """primary 自己出现在链里且空闲时，bind_fallback 也不该重复注册它
        （它刚失败过，重复注册只会得到同样的失败）。"""
        chain = ("ctrl+shift+a", "ctrl+alt+x")
        mgr = _make_manager(occupied=("ctrl+shift+a",))
        self.assertEqual(mgr.bind_fallback("capture", "ctrl+shift+a", chain),
                         "ctrl+alt+x")

    def test_all_occupied_returns_empty(self):
        mgr = _make_manager(occupied=hk.HotkeyManager.FALLBACK_CAPTURE)
        self.assertEqual(
            mgr.bind_fallback("capture", "ctrl+shift+a",
                              hk.HotkeyManager.FALLBACK_CAPTURE), "")

    def test_empty_chain_returns_empty(self):
        mgr = _make_manager()
        self.assertEqual(mgr.bind_fallback("capture", "ctrl+shift+a", ()), "")


@unittest.skipUnless(sys.platform == "win32", "Global 命名互斥量仅 Windows")
class TestGlobalMutex(unittest.TestCase):
    """全局互斥量：同进程第二次获取必须判 False（183 = ALREADY_EXISTS）。"""

    def test_twice_in_same_process(self):
        import main as entry

        log = logging.getLogger("test.mutex")
        # 必须用本测试专属的名字：Global 互斥量是全机器可见的内核对象，
        # 用户/别的会话此刻可能正跑着一个真实实例占着正式名字
        # （2026-11-10 实测：测试被外部实例误伤 —— 这恰好证明互斥量有效）。
        name = "Global\\socrans-test-instance-mutex"
        self.assertTrue(entry._acquire_global_mutex(log, name))
        self.assertFalse(entry._acquire_global_mutex(log, name))

    def test_kernel_semantics_documented(self):
        """锚定 Win32 语义：首次 CreateMutexW 成功（err=0），再次创建同名
        互斥量返回**有效句柄**且 GetLastError=183（ERROR_ALREADY_EXISTS）。
        这正是第一版实现漏掉的地方——只判句柄非空就把双开放行了。"""
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateMutexW.argtypes = [wintypes.HANDLE, wintypes.BOOL, wintypes.LPCWSTR]
        h = k32.CreateMutexW(None, False, "Global\\socrans-test-mutex")
        self.assertTrue(h)
        self.assertEqual(ctypes.get_last_error(), 0)    # 首个创建者
        h2 = k32.CreateMutexW(None, False, "Global\\socrans-test-mutex")
        self.assertTrue(h2)                             # 句柄仍非空！
        self.assertEqual(ctypes.get_last_error(), 183)  # 但错误码是已存在
        k32.CloseHandle(h)
        k32.CloseHandle(h2)


class TestBindHotkeysNote(unittest.TestCase):
    """AppController.bind_hotkeys：占用→换备用键→写 hotkey_note。"""

    def test_note_written_when_primary_occupied(self):
        from ocrtranslator.app import AppController
        from ocrtranslator.config import AppConfig

        cfg = AppConfig()
        cfg.capture.hotkey = "ctrl+shift+a"
        cfg.capture.second_hotkey = "ctrl+shift+z"
        ctrl = AppController(cfg, parent=None)
        try:
            # 占用主热键 + 备用链第一位（ctrl+alt+x），逼回退落到链第二位
            ctrl.hotkeys = _make_manager(
                occupied=("ctrl+shift+a", "ctrl+shift+z", "ctrl+alt+x"))
            ok = ctrl.bind_hotkeys()
            self.assertTrue(ok)                      # 备用键接管，不算失败
            self.assertIn("ctrl+shift+x", ctrl.hotkey_note)
            self.assertIn("ctrl+shift+a", ctrl.hotkey_note)   # 讲清是谁被占
            self.assertIn("ctrl+shift+z", ctrl.hotkey_note)   # 副热键也回退了
        finally:
            try:
                ctrl.hotkeys.shutdown()
            except Exception:
                pass

    def test_no_note_when_all_free(self):
        from ocrtranslator.app import AppController
        from ocrtranslator.config import AppConfig

        cfg = AppConfig()
        ctrl = AppController(cfg, parent=None)
        try:
            ctrl.hotkeys = _make_manager()
            self.assertTrue(ctrl.bind_hotkeys())
            self.assertEqual(ctrl.hotkey_note, "")
        finally:
            try:
                ctrl.hotkeys.shutdown()
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
