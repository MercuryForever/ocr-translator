"""PyInstaller runtime hook：在**任何应用代码之前**修好打包环境的三个坑。

runtime hook 在 bootloader 之后、main.py 之前执行，是唯一能
"在 import onnxruntime 之前设置 DLL 搜索路径"的时机。

1. windowed 模式下 sys.stdout/stderr 是 None。
   任何未捕获异常（包括 import 期错误）都会静默消失，用户只看到
   "双击没反应"。这里把它们接到一个日志文件。

2. onnxruntime / opencv 的同名依赖 DLL（msvcp140、libcrypto 等）
   在 _MEIPASS 根目录与子目录各有一份，Windows 按 PATH 顺序取，
   可能取到版本不匹配的那份，报 "找不到指定的模块"。
   显式 add_dll_directory(_MEIPASS) 让搜索顺序确定。

3. Qt 插件目录。PyInstaller 的 PyQt6 hook 会把插件放到 _MEIPASS/PyQt6/Qt6/plugins，
   但 Qt 在 frozen 下有时仍按 exe 目录找 —— 显式设环境变量兜住。
"""

import os
import sys


def _bundle() -> str:
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.argv[0])))


def main() -> None:
    base = _bundle()

    # --- 1. 日志兜底 ------------------------------------------------
    if sys.stdout is None or sys.stderr is None:
        try:
            if sys.platform == "win32":
                log_dir = os.path.join(os.environ.get("APPDATA", base), "socrans", "logs")
            elif sys.platform == "darwin":
                log_dir = os.path.join(os.path.expanduser("~/Library/Application Support"),
                                       "socrans", "logs")
            else:
                log_dir = os.path.join(os.environ.get("XDG_CONFIG_HOME",
                                             os.path.expanduser("~/.config")),
                                       "socrans", "logs")
            os.makedirs(log_dir, exist_ok=True)
            f = open(os.path.join(log_dir, "startup.log"), "a", encoding="utf-8",
                     buffering=1)
            f.write(f"\n===== 启动 {os.getpid()} =====\n")
            if sys.stdout is None:
                sys.stdout = f
            if sys.stderr is None:
                sys.stderr = f
        except OSError:
            # 连日志都开不了就算了，至少别因为 hook 自己崩掉程序
            if sys.stdout is None:
                class _Null:
                    def write(self, *_a): return 0
                    def flush(self): pass
                sys.stdout = _Null()
            if sys.stderr is None:
                sys.stderr = sys.stdout

    # --- 2. DLL 搜索顺序 --------------------------------------------
    if sys.platform == "win32" and hasattr(os, "add_dll_directory"):
        for sub in ("", os.path.join("onnxruntime", "dll"),
                   os.path.join("numpy", "lib"), os.path.join("cv2")):
            p = os.path.join(base, sub) if sub else base
            if os.path.isdir(p):
                try:
                    os.add_dll_directory(p)
                except OSError:
                    pass

    # --- 3. Qt 插件 --------------------------------------------------
    for cand in (os.path.join(base, "PyQt6", "Qt6", "plugins"),
                 os.path.join(base, "_internal", "PyQt6", "Qt6", "plugins"),
                 os.path.join(base, "plugins")):
        if os.path.isdir(cand):
            os.environ.setdefault("QT_PLUGIN_PATH", cand)
            plat = os.path.join(cand, "platforms")
            if os.path.isdir(plat):
                os.environ.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH", plat)
            break

    # --- 4. 线程数（必须在 import onnxruntime 之前）------------------
    n = max(2, min(4, ((os.cpu_count() or 4) // 2)))
    for var in ("OMP_NUM_THREADS", "ORT_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ.setdefault(var, str(n))


main()
