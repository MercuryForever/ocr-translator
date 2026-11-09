"""启动前置检查：只回答"这个解释器能不能把程序撑起来"。

为什么需要它（而不是在 .bat 里直接 `python main.py`）：
  本机实测 `python` 解析到 Anaconda 3.13，而 PyQt6 在它下面报
  `DLL load failed while importing QtWidgets: 找不到指定的模块`
  —— Anaconda 把它自带的 Qt5/ICU DLL 放进搜索路径，抢在 PyQt6 的 Qt6 DLL 前面。
  同一份代码在 python.org 的 3.12 下完全正常。
  这类"解释器相关"的失败在 batch 里用 `where python` 判断不出来，
  **只有真的 import 一次才知道**。所以 launcher 对每个候选解释器跑一次
  `main.py --doctor`，取第一个返回 0 的。

约束：不加载 OCR 模型、不联网、不建可见窗口、不写配置 —— 它会被
每个候选解释器调用一次，必须便宜。
退出码 0 = 可以启动 GUI；1 = 缺东西（stdout 给出可执行的修复建议）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _safe_output() -> None:
    """把 stdout/stderr 固定成 UTF-8 且永不因编码崩掉。

    真实坑：doctor 的输出含 ✓ ✗（U+2713/2717），它们**不在 GBK 里**。
    交互控制台上 Python 走 WriteConsoleW 所以没事，但 launcher.py 用
    `capture_output=True` 探测时，子进程 stdout 变成管道、按 locale(cp936)
    编码 —— `print("✓ 可以启动")` 会抛 UnicodeEncodeError，
    于是"检查器自己崩了"被 launcher 读成"这个解释器不可用"，
    把一个本来能跑的环境判死。实现统一在 logger.force_utf8。
    """
    from .logger import force_utf8

    force_utf8(sys.stdout)
    force_utf8(sys.stderr)


def _platform_plugin_present() -> tuple[bool, str]:
    """当前平台的 Qt 插件 DLL 是否存在。返回 (可放行, 插件文件名)。

    必须先查再决定要不要 `QApplication([])`：插件缺失时 Qt 会走 qFatal →
    abort()，在 Windows 上可能弹出 "Microsoft Visual C++ Runtime Library"
    断言框。launcher 是**无人值守**地逐个解释器跑 doctor 的，
    一个模态框会让整轮探测挂到超时（几十秒 × 每个候选），
    用户看到的是"双击后卡住不动"。所以这里把最坏情况绕开。

    ⚠ 判定必须保守：环境变量为空时**放行**。
    pip 正常安装的 PyQt6 不需要 QT_PLUGIN_PATH（插件在包里，由 PyQt6 自己定位），
    此时环境里根本没有这两个变量。若把"变量为空"当成"插件缺失"，
    就会把完全正常的安装判死 —— 假阳性比漏判危险得多，因为用户无法自证。
    只有"变量明确设了、但指向的目录里没有插件"才是真故障
    （典型成因：vendor 目录与当前解释器版本不匹配）。
    """
    names = {"win32": "qwindows.dll", "darwin": "libqcocoa.dylib"}.get(
        sys.platform, "libqxcb.so")
    roots = [r for r in (os.environ.get("QT_QPA_PLATFORM_PLUGIN_PATH", ""),
                         os.environ.get("QT_PLUGIN_PATH", "")) if r]
    if not roots:
        return True, names            # 未指定 → 交给 PyQt6 自己解析
    for r in roots:
        for cand in (Path(r) / names, Path(r) / "platforms" / names):
            if cand.is_file():
                return True, str(cand)
    return False, names


def _is_admin() -> bool:
    """当前进程是否以管理员令牌运行（Windows；其他平台恒 False）。"""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _data_dir_report(bad: list[str]) -> None:
    """数据目录可写性诊断 —— "要管理员权限才能启动"多半坏在这里。

    程序设计上**不需要**管理员：配置/日志/模型缓存全部写进每用户可写目录，
    候选链逐个探测。若连 %APPDATA% 都失败，最常见诱因是目录曾被"以管理员
    身份运行"创建过，ACL 归 Administrators 所有，普通权限从此写不进去。
    """
    from ocrtranslator.config import USING_TEMP_FALLBACK, FALLBACK_ERRORS, app_dir

    d = app_dir()
    if USING_TEMP_FALLBACK:
        print(f"✗ 数据目录 {d}（临时目录兜底！配置/缓存重启即丢）")
        for e in FALLBACK_ERRORS:
            print(f"    {e}")
        print("  常见原因：数据目录曾被管理员权限运行创建，ACL 归 Administrators，普通权限写不进去。")
        print("  修复（选一）：")
        print("    1) 删除旧目录让程序重建：%APPDATA%\\socrans（管理员删完再用普通权限启动）")
        print("    2) 把目录授权给当前用户：icacls \"%APPDATA%\\socrans\" /grant %USERNAME%:(OI)(CI)F /T")
        print("    3) 设 SOCRANS_HOME 指向任意可写目录")
        print("    4) 用 run_admin.bat 启动（每次弹 UAC，不推荐日常使用）")
        bad.append("数据目录")
    else:
        admin = _is_admin()
        print(f"✓ 数据目录 {d}{'（管理员身份）' if admin else ''}")


def doctor() -> int:
    _safe_output()
    print(f"python {sys.version.split()[0]}")
    print(f"路径 {sys.executable}")
    if sys.version_info < (3, 10):
        print("✗ 需要 Python 3.10 以上（当前 %d.%d）" % sys.version_info[:2])
        return 1

    from ocrtranslator import bootstrap

    info = bootstrap.bootstrap(headless=True)
    print(f"依赖目录 {info['vendor'] or '（未注入，使用系统环境）'}")

    bad: list[str] = []
    _data_dir_report(bad)

    # ---- GUI 栈：QtWidgets + 平台插件 + 屏幕枚举
    ok_plugin, name = _platform_plugin_present()
    if not ok_plugin:
        print(f"✗ 找不到 Qt 平台插件 {name}")
        print("  说明 PyQt6 装了但插件目录不对（多半是 vendor 目录与解释器版本不匹配）。")
        print(r"  解决：用当前解释器重跑 python tools\fetch_wheels.py")
        bad.append("Qt 插件")
    else:
        try:
            from PyQt6.QtCore import QT_VERSION_STR
            from PyQt6.QtWidgets import QApplication

            app = QApplication.instance() or QApplication([])
            screens = app.screens()
            if not screens:
                bad.append("屏幕")
                print("✗ Qt 起不来任何屏幕（远程桌面 / Wayland 无 XCB？）")
            else:
                g = app.primaryScreen().geometry()
                print(f"✓ PyQt6 可用（Qt {QT_VERSION_STR}，{len(screens)} 块屏幕，"
                      f"{g.width()}x{g.height()}）")
        except Exception as exc:
            msg = str(exc)
            if "DLL load failed" in msg or "could not load the Qt platform" in msg.lower():
                print(f"✗ PyQt6 无法加载：{msg}")
                print("  这个报错的真实原因通常是**解释器环境冲突**，不是代码问题：")
                print("  1) Anaconda/Miniconda 的 python 会把它自带的 Qt5/ICU DLL")
                print("     放进搜索路径，抢在 PyQt6 的 Qt6 DLL 前面。")
                print("     解决：改用 python.org 的 Python，或建一个干净 venv：")
                print(r"       python -m venv .venv && .venv\Scripts\activate")
                print("     也可设 SOCRANS_VENDOR 指向与该解释器匹配的 vendor 目录。")
                print("  2) vendor\\ 里的 wheel 是别的 Python 小版本装的（cp313 vs cp312）。")
                print("     解决：用当前解释器重跑 python tools\\fetch_wheels.py")
            else:
                print(f"✗ PyQt6 不可用：{type(exc).__name__}: {msg}")
                print("  解决：pip install -r requirements.txt")
                print(r"        或免 pip：python tools\fetch_wheels.py")
            bad.append("PyQt6")

    # ---- OCR 栈：只 import，不加载模型（加载要 0.5s，而 doctor 会被调用多次）
    for mod, hint in (("rapidocr_onnxruntime",
                       "pip install rapidocr-onnxruntime  或  python tools\\fetch_wheels.py"),
                      ("cv2", "pip install opencv-python"),
                      ("numpy", "pip install numpy"),
                      ("PIL.Image", "pip install Pillow")):
        try:
            __import__(mod)
        except Exception as exc:
            print(f"✗ {mod} 不可用：{type(exc).__name__}: {exc}")
            print(f"  {hint}")
            bad.append(mod)

    # ---- 可选依赖：缺了不阻止启动，只提示功能降级
    try:
        import docx  # noqa: F401
    except Exception:
        print("! python-docx 未安装：导出 DOCX 会自动降级为 RTF")

    if bad:
        print("\n✗ 无法启动，缺少：" + ", ".join(bad))
        return 1
    print("✓ 可以启动")
    return 0
