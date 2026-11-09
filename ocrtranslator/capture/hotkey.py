"""全局热键。

关键取舍：Windows 上不用 `keyboard` 库。它需要管理员权限才能装低级钩子，
且会 hook **所有**进程（包括密码框），是常见的安全告警点。这里直接调
`RegisterHotKey` + Win32 消息泵，语义正是"系统级、独占、无需提权"。

非 Windows 平台（macOS/Linux）没有等价的零依赖方案：
  * macOS 需要 Accessibility 授权（用 `pynput` 会弹权限框）；
  * Linux 的 Wayland 直接禁止全局键盘钩子，只能让用户在桌面环境里配快捷键再回调本程序。
所以这两处回落到 `pynput`（若可用），否则仅支持应用内快捷键 —— 并在 UI 上明说。
"""

from __future__ import annotations

import ctypes
import queue
import re
import sys
import threading
from dataclasses import dataclass
from typing import Callable

from ..logger import get_logger

log = get_logger("hotkey")

# ---------------------------------------------------------------- 解析
_MOD_MAP = {"ctrl": 0x0002, "control": 0x0002, "alt": 0x0001,
            "shift": 0x0004, "win": 0x0008, "super": 0x0008, "cmd": 0x0008}

_VK = {c: 0x30 + i for i, c in enumerate("0123456789")}
_VK.update({chr(65 + i): 0x41 + i for i in range(26)})
_VK.update({
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B,
    "backspace": 0x08, "printscreen": 0x2C, "snapshot": 0x2C,
    "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "prior": 0x21, "pagedown": 0x22, "next": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "volume_mute": 0xAD, "volume_down": 0x6A, "volume_up": 0x6E,
})
# 主键盘区之外的符号键
_VK.update({";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF,
            "`": 0xC0, "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE})

MOD_NOREPEAT = 0x4000


@dataclass(frozen=True, slots=True)
class HotkeySpec:
    mods: int
    vk: int
    raw: str

    @property
    def display(self) -> str:
        parts = []
        if self.mods & 0x0002:
            parts.append("Ctrl")
        if self.mods & 0x0001:
            parts.append("Alt")
        if self.mods & 0x0004:
            parts.append("Shift")
        if self.mods & 0x0008:
            parts.append("Win")
        parts.append(self.raw.split("+")[-1].upper())
        return "+".join(parts)


def parse(spec: str) -> HotkeySpec | None:
    """`"ctrl+shift+a"` -> HotkeySpec。解析失败返回 None（绝不抛，热键写错不该崩）。"""
    if not spec:
        return None
    tokens = [t.strip().lower() for t in re.split(r"[+\-_\s]+", spec) if t.strip()]
    # "ctrl-shift" 里的 '-' 被当分隔符了，需要还原符号键本身
    if len(tokens) >= 2 and tokens[-1] == "" :
        tokens = tokens[:-1]
    mods = 0
    key = None
    for t in tokens:
        if t in _MOD_MAP:
            mods |= _MOD_MAP[t]
        elif t in _VK:
            key = _VK[t]
        elif len(t) == 1 and t.upper() in _VK:
            key = _VK[t.upper()]
        elif t.startswith("f") and t[1:].isdigit() and 1 <= int(t[1:]) <= 24:
            key = 0x70 + int(t[1:]) - 1
    if key is None:
        log.warning("无法解析热键 %r", spec)
        return None
    return HotkeySpec(mods, key, spec)


# ---------------------------------------------------------------- Windows 后端
class _Win32Backend:
    """RegisterHotKey + 线程消息泵。

    ⚠ 注册必须在泵线程上执行。`RegisterHotKey(NULL, …)` 把 WM_HOTKEY 投递到
    **调用线程**的消息队列，而不是"某个全局队列"。第一版在主线程（run_gui
    调用 bind_hotkeys）里调 RegisterHotKey，泵跑在另一个线程里 GetMessage——
    结果 WM_HOTKEY 进了主线程队列，被 Qt 事件循环拿走又丢弃，
    泵线程永远收不到 → 日志显示"注册成功 5 次、触发 0 次"。
    用户看到的现象是：按热键毫无反应，也没有任何错误。

    同理 `UnregisterHotKey` 也必须在同一个线程调用（Windows 要求
    注销线程 = 注册线程），否则返回 0 并静默失败。

    回调在泵线程触发，调用方自己 marshal 到 GUI 线程（用 Qt 信号而不是
    `QTimer.singleShot` —— 后者在非 Qt 线程上没有事件调度器，timer 永不触发）。
    """

    WM_HOTKEY = 0x0312
    WM_QUIT = 0x0012
    WM_NULL = 0x0000

    def __init__(self):
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._id = 0xB000
        self._map: dict[int, Callable[[], None]] = {}
        self._thread: threading.Thread | None = None
        self._tid: int = 0
        self._ready = threading.Event()
        self._pending: queue.Queue = queue.Queue()

    # ---- 跨线程调用：把 fn 提交到泵线程执行，等待结果
    def _on_pump(self, fn: Callable[[], bool]) -> bool:
        """把 fn 提交到泵线程执行并等待结果。泵线程死掉时返回 False。"""
        if not self._thread or not self._thread.is_alive():
            log.warning("热键泵线程已退出，无法注册")
            return False
        result: queue.Queue = queue.Queue()
        self._pending.put((fn, result))
        # 唤醒泵线程（GetMessage 正阻塞等消息）
        if self._tid:
            self._user32.PostThreadMessageW(self._tid, self.WM_NULL, 0, 0)
        try:
            return result.get(timeout=5.0)
        except queue.Empty:
            log.warning("热键注册超时（泵线程 5s 内未响应）")
            return False

    # ---- 消息泵线程
    class _MSG(ctypes.Structure):
        _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint),
                    ("wParam", ctypes.c_void_p), ("lParam", ctypes.c_ssize_t),
                    ("time", ctypes.c_uint),
                    ("ptx", ctypes.c_long), ("pty", ctypes.c_long)]

    def _pump(self) -> None:
        """消息泵。只在此线程上做 RegisterHotKey / UnregisterHotKey / GetMessage。"""
        import ctypes as _ct

        msg = _Win32Backend._MSG()
        self._tid = self._kernel32.GetCurrentThreadId()
        # peek 一次以创建该线程的消息队列（RegisterHotKey 需要队列已存在）
        self._user32.PeekMessageW(_ct.byref(msg), 0, 0, 0, 0)
        self._ready.set()
        while True:
            # 1) 处理待办的注册/注销请求
            self._drain_pending()
            # 2) 泵消息（阻塞直到有消息）
            r = self._user32.GetMessageW(_ct.byref(msg), 0, 0, 0)
            # GetMessage 返回 0 = WM_QUIT；返回 -1 = 错误。
            # 注意运算符优先级：`r == -1 & 0xFFFFFFFF` 会先算 -1 & 0xFFFF… = 4294967295，
            # 永远不等于 r —— 第一版就因为 precedence bug 让错误分支形同虚设。
            if r == 0:
                break
            if r == -1:
                err = _ct.get_last_error()
                log.warning("GetMessageW 返回 -1（GetLastError=%d），退出泵", err)
                break
            if msg.message == _Win32Backend.WM_HOTKEY:
                cb = self._map.get(int(msg.wParam) if msg.wParam else 0)
                if cb:
                    try:
                        cb()
                    except Exception:
                        log.exception("热键回调异常")

    def _drain_pending(self) -> None:
        """执行所有待处理的注册/注销（只在泵线程上运行）。"""
        while True:
            try:
                fn, result = self._pending.get_nowait()
            except queue.Empty:
                break
            try:
                result.put(fn())
            except Exception as exc:
                log.warning("泵线程操作异常：%s", exc)
                result.put(False)

    def start(self) -> bool:
        if sys.platform != "win32":
            return False
        self._thread = threading.Thread(target=self._pump, name="hotkey-pump", daemon=True)
        self._thread.start()
        ok = self._ready.wait(2.0)
        if not ok:
            log.warning("热键泵线程 2s 内未就绪")
        return ok

    def register(self, spec: HotkeySpec, cb: Callable[[], None]) -> bool:
        """注册热键。**必须在泵线程上执行 RegisterHotKey**（线程亲和性）。"""
        self._id += 1
        hid = self._id

        def do() -> bool:
            self._user32.RegisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                                    ctypes.c_uint, ctypes.c_uint]
            ok = bool(self._user32.RegisterHotKey(None, hid,
                                                 spec.mods | MOD_NOREPEAT, spec.vk))
            if not ok:
                err = ctypes.get_last_error()
                # 1409 = ERROR_HOTKEY_ALREADY_REGISTERED：常见于 QQ/微信/Snipaste
                # 的截图热键。必须把原因讲清楚，否则用户以为程序坏了。
                hint = ("已被其他程序占用（QQ/微信/Snipaste 等截图工具常用这个组合），"
                        "请在设置里换一组热键，或关闭对应软件的快捷键"
                        if err == 1409 else f"GetLastError={err}")
                log.warning("RegisterHotKey(%s) 失败：%s", spec.display, hint)
                self.last_error = hint     # 供 UI 读取；不要写成 _last_error（读不到）
                return False
            self._map[hid] = cb
            log.info("已注册全局热键 %s（泵线程 tid=%d）", spec.display, self._tid)
            return True

        return self._on_pump(do)

    last_error: str = ""

    def unregister_all(self) -> None:
        """注销所有热键。必须在泵线程上执行（与注册同线程）。

        ⚠ 这里**不能**顺带把泵线程杀掉：设置页"换一组热键"走 rebind()
        → unbind_all() → bind()，若 unbind 就退出泵，第二次 bind 必然
        报"泵线程已退出"（实测）。关泵是 shutdown() 的职责。
        """
        def do() -> bool:
            self._user32.UnregisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int]
            for hid in list(self._map):
                self._user32.UnregisterHotKey(None, hid)
            self._map.clear()
            return True

        return self._on_pump(do)

    def shutdown(self) -> None:
        """注销热键并结束泵线程。只在应用退出时调用一次。"""
        try:
            self.unregister_all()
        except Exception:
            log.debug("注销热键异常（继续关泵）", exc_info=True)
        if self._tid:
            self._user32.PostThreadMessageW(self._tid, self.WM_QUIT, 0, 0)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._tid = 0
        self._thread = None

    available = True


# ---------------------------------------------------------------- pynput 兜底
class _PynputBackend:
    """跨平台兜底（macOS 需辅助功能权限；Wayland 下基本不可用）。"""

    def __init__(self):
        from pynput import keyboard  # noqa: PLC0415
        self._kb = keyboard
        self._Listener = keyboard.GlobalHotKeys
        self._bindings: dict[str, Callable[[], None]] = {}
        self._listener = None

    def start(self) -> bool:
        return True

    def register(self, spec: HotkeySpec, cb: Callable[[], None]) -> bool:
        self._bindings[spec.raw.lower()] = cb
        if self._listener is not None:
            self._listener.stop()
        self._listener = self._Listener(self._bindings)
        self._listener.daemon = True
        self._listener.start()
        log.info("pynput 已监听 %s", spec.display)
        return True

    def unregister_all(self) -> None:
        if self._listener:
            self._listener.stop()
            self._listener = None

    available = True


def _make_backend():
    if sys.platform == "win32":
        try:
            return _Win32Backend()
        except Exception as exc:  # 非 CPython / 无 user32
            log.warning("Win32 热键后端不可用: %s", exc)
    try:
        return _PynputBackend()
    except Exception as exc:
        log.info("pynput 不可用（%s），全局热键关闭，仅保留应用内快捷键", exc)
        return None


class HotkeyManager:
    """对外统一门面。`trigger` 信号由调用方（AppController）转成 Qt 信号投递到主线程。"""

    def __init__(self, on_trigger: Callable[[str], None]):
        self._on_trigger = on_trigger
        self._backend = _make_backend()
        self._live: list[HotkeySpec] = []
        self.supported = self._backend is not None

    def bind(self, name: str, spec_str: str) -> bool:
        spec = parse(spec_str)
        if spec is None:
            self._last_error = f"无法解析热键「{spec_str}」"
            return False
        if not self.supported:
            log.info("平台不支持全局热键，跳过 %s", spec_str)
            return False
        if self._backend is None:
            return False
        ok = self._backend.register(spec, lambda name=name: self._on_trigger(name))
        if ok:
            self._live.append(spec)
        return ok

    @property
    def last_error(self) -> str:
        """最近一次 bind 失败的可读原因（占用/解析失败），供 UI 提示。"""
        be = getattr(self._backend, "last_error", "")
        return be or getattr(self, "_last_error", "")

    def rebind(self, name: str, spec_str: str) -> bool:
        """热键冲突或被占用时，UI 的"重试"按钮会走到这里。"""
        self.unbind_all()
        return self.bind(name, spec_str)

    def unbind_all(self) -> None:
        if self._backend:
            self._backend.unregister_all()
        self._live.clear()

    def shutdown(self) -> None:
        """应用退出时调用：注销热键并结束泵线程。之后本实例不可再用。"""
        if self._backend:
            self._backend.shutdown()
        self._live.clear()

    def start(self) -> bool:
        if not self._backend:
            return False
        return self._backend.start()
