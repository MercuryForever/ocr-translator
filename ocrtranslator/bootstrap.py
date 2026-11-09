"""运行时引导：依赖目录、Qt 插件、DPI、线程数、临时目录。

为什么不靠 venv：本项目要同时支持三种跑法 ——
  (a) 源码目录 `python main.py`（开发者）
  (b) PyInstaller 单文件 exe（最终用户，机器上没有 Python）
  (c) 内网免安装：用 `tools/fetch_wheels.py` 把 wheel 解到 vendor/，
      拷整个目录就能跑（无 pip、无 venv、无网络）
三条路共用同一份代码，所以 sys.path 与 DLL 路径的调整必须写在程序内部。

调用顺序固定为 `bootstrap()` 一次搞定，必须在创建 QApplication **之前**，
因为 DPI 感知与 QT_PLUGIN_PATH 在 QApplication 构造后再设就无效了。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .logger import get_logger

log = get_logger("bootstrap")

ROOT = Path(__file__).resolve().parent.parent
FROZEN = getattr(sys, "frozen", False)
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", ROOT)) if FROZEN else ROOT

#: 需要能被解释器找到的关键依赖
_PROBE_MODULES = ("PyQt6.QtCore", "rapidocr_onnxruntime")


def _candidate_list() -> list[Path]:
    """按优先级给出 vendor 目录候选。"""
    if FROZEN:
        return [BUNDLE_DIR / "vendor", BUNDLE_DIR]
    env = os.environ.get("SOCRANS_VENDOR", "")
    out = [Path(p) for p in env.replace(os.pathsep, ";").split(";") if p.strip()] if env else []
    out += [ROOT / "vendor", ROOT / "vendor312"]
    return out


def already_present() -> bool:
    """依赖是否已经可导入 —— 决定还要不要注入 vendor。

    **绝不能无条件 `sys.path.insert(0, vendor)`。** 真实踩坑记录：
    本机 vendor/ 装的是 cp313 wheel，当前解释器是 cp312；前插后
    onnxruntime 去加载版本不匹配的 .pyd，报
    `DLL load failed while importing onnxruntime_pybind11_state: 找不到指定的模块`
    —— 错误信息与真实原因（Python 小版本不匹配）毫无关系，极难自查。
    只在确实缺包时才注入，并且注入后必须验证 import 成功。
    """
    import importlib.util

    for mod in _PROBE_MODULES:
        try:
            top = mod.split(".")[0]
            if importlib.util.find_spec(top) is None:
                return False
        except (ImportError, ValueError, ModuleNotFoundError):
            return False
    return True


def install(force: bool = False) -> Path | None:
    """把可用的 vendor 目录插到 sys.path 最前面，并**验证真的 import 得动**。

    返回实际生效的目录；None 表示无需注入（依赖已在）或所有候选都不兼容。

    顺序为什么必须前插：系统 site-packages 里可能有旧版 PyQt6/cv2，
    它们排在前面就会赢，导致 `DLL load failed while importing QtWidgets`
    （Anaconda 自带的 Qt5 DLL 抢占同名符号是常见成因）。
    """
    import importlib

    if not force and already_present():
        return None

    for cand in _candidate_list():
        if not cand.is_dir():
            continue
        if not any((cand / m).is_dir() for m in
                   ("PyQt6", "rapidocr_onnxruntime", "cv2", "docx", "onnxruntime")):
            continue
        s = str(cand)
        if s in sys.path:
            sys.path.remove(s)
        sys.path.insert(0, s)

        # 清掉半成品模块对象：失败过的包会留在 sys.modules 里污染后续尝试
        for name in ("PyQt6", "rapidocr_onnxruntime", "onnxruntime"):
            for k in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
                sys.modules.pop(k, None)
        try:
            for mod in _PROBE_MODULES:
                importlib.import_module(mod)
        except Exception as exc:
            log.warning("vendor %s 与当前解释器不兼容（%s: %s），试下一个",
                        cand, exc.__class__.__name__, str(exc)[:140])
            sys.path.remove(s)
            continue
        log.info("依赖目录生效：%s", cand)
        return cand

    log.warning("没有与 Python %d.%d 兼容的 vendor 目录；请运行 tools/fetch_wheels.py",
                sys.version_info.major, sys.version_info.minor)
    return None


def configure_qt() -> None:
    """定位 Qt 插件目录（platforms/qwindows.dll）。

    正常安装时 PyQt6 自己按包目录找插件；但 PyInstaller onefile 会把插件解到
    临时目录，此时不显式指定就报 "Could not find the Qt platform plugin windows"
    —— 这是本项目最常见的打包故障。
    """
    search = ([BUNDLE_DIR] if FROZEN else []) + _candidate_list()
    for cand in search:
        plug = cand / "PyQt6" / "Qt6" / "plugins"
        if plug.is_dir():
            os.environ.setdefault("QT_PLUGIN_PATH", str(plug))
            os.environ.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH", str(plug / "platforms"))
            break
    # Windows：把 Qt6/bin 加进 DLL 搜索目录，避免抓到 Anaconda 的 Qt5 同名 DLL
    if os.name == "nt":
        for cand in _candidate_list():
            qbin = cand / "PyQt6" / "Qt6" / "bin"
            if qbin.is_dir():
                try:
                    os.add_dll_directory(str(qbin))
                except (AttributeError, OSError):
                    os.environ["PATH"] = f"{qbin}{os.pathsep}{os.environ.get('PATH', '')}"
                break


def thread_budget(cpu_budget: int | None = None) -> int:
    """推理线程数。

    实测（tools/experiment_round2.py 的 B 段）：
        threads 1 → 875ms   2 → 613ms   4 → 484ms   8 → 675ms   -1(全核) → 1661ms
    所以**默认全核是最慢的选择**（比 4 线程慢 3.4 倍）：PP-OCR 的 det/rec
    batch 都很小，OpenMP 的核间同步开销大于并行收益，而且会抢走 GUI 线程的 CPU
    让界面卡顿。这个函数把 4 定为上限，与 ocr/rapid.py 里的默认值保持一致。
    """
    n = cpu_budget or max(2, min(4, (os.cpu_count() or 4) // 2))
    # OMP/ORT 的线程数必须在 import onnxruntime 之前设好，之后改无效
    for var in ("OMP_NUM_THREADS", "ORT_NUM_THREADS", "MKL_NUM_THREADS",
                "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ.setdefault(var, str(n))
    return n


def ensure_temp_writable() -> Path:
    """探测 %TEMP% 可写性，不可写就换到项目内并明确告知。

    PyInstaller onefile 每次启动都要往 TEMP 解 100+MB；企业机上常见
    "No space left" / ACL 拒绝，失败信息是 `Permission denied`，
    用户完全无从自证。这里主动探测并给出可执行的提示。
    """
    import tempfile

    probe = Path(tempfile.gettempdir())
    try:
        target = probe / ".socrans_probe"
        target.write_text("ok")
        target.unlink(missing_ok=True)
        return probe
    except OSError as exc:
        alt = BUNDLE_DIR / ".tmp" if not FROZEN else Path.home() / ".socrans-tmp"
        try:
            alt.mkdir(parents=True, exist_ok=True)
        except OSError:
            return probe
        os.environ["TEMP"] = os.environ["TMP"] = os.environ["TMPDIR"] = str(alt)
        log.warning("%s 不可写（%s），临时目录改用 %s", probe, exc, alt)
        return alt


def configure_dpi() -> None:
    """Windows 上声明 Per-Monitor v2 DPI 感知。

    不做的后果（实测）：4K 150% 缩放下 grabWindow 拿到的是**系统位图缩放后**的
    模糊图，OCR 小字精度显著下降，且框选坐标整体偏移。必须在 QApplication
    创建前调用；Qt 自己也会设，但只在它认为 manifest 缺失时设，版本行为不一致，
    所以这里显式做一次。调用失败不影响程序（只是精度打折）。
    """
    if os.name != "nt":
        return
    import ctypes

    try:
        try:                                   # Win10 1703+：Per-Monitor v2
            if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
                return
        except (AttributeError, OSError):
            pass
        try:                                   # Win8.1+：Shcore
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
            return
        except (AttributeError, OSError):
            pass
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception as exc:  # 非 CPython / 精简系统缺少 user32
        log.debug("DPI 感知设置失败：%s", exc)


def bootstrap(headless: bool = False) -> dict[str, object]:
    """一次性完成全部环境准备。返回摘要，供 `--selftest` 与日志打印。"""
    # 必须最先做：本函数里的 log.warning（"vendor 与当前解释器不兼容"）
    # 发生在 run_gui 调用 setup() **之前**，那时还没有任何 handler，
    # logging 会用 lastResort handler 按 locale(cp936) 写 stderr ——
    # 实测 run.bat 的输出里这一行就是花屏的，而它恰好是最关键的诊断信息。
    from .logger import force_utf8
    force_utf8(sys.stdout)
    force_utf8(sys.stderr)

    vendor = install()
    configure_qt()
    n = thread_budget()
    temp = ensure_temp_writable()
    if not headless:
        configure_dpi()
    info = {
        "vendor": str(vendor) if vendor else "(使用系统环境)",
        "threads": n,
        "temp": str(temp),
        "frozen": FROZEN,
        "python": sys.version.split()[0],
        "cpus": os.cpu_count(),
        "platform": sys.platform,
    }
    log.info("引导完成：%s", info)
    return info
