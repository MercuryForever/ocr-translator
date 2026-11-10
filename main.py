"""程序入口。

    python main.py                 正常启动（托盘常驻 + 全局热键）
    python main.py --selftest      环境自检：依赖/引擎/后端/热键/目录，逐项打分
    python main.py --capture       立刻截一次并出结果（调试用，跳过托盘）
    python main.py --file a.png    翻译一张已有图片（离线验证 OCR+翻译）
    python main.py --no-tray       不装托盘（远程桌面/Wayland 下常需要）
    python main.py --verbose       DEBUG 日志

启动顺序里有两个不能调换的环节：
  bootstrap() 必须在 import PyQt6 之前（否则 vendor 注入不进去）；
  configure_dpi() 必须在 QApplication() 之前（否则 DPI 感知不生效，
  高分屏抓屏是模糊的，坐标也会偏）。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _early_bootstrap(headless: bool) -> dict:
    from ocrtranslator import bootstrap

    return bootstrap.bootstrap(headless=headless)


# ---------------------------------------------------------------- 自检
def selftest(argv: list[str]) -> int:
    info = _early_bootstrap(headless=True)
    from ocrtranslator import __version__
    from ocrtranslator.config import AppConfig, app_dir
    from ocrtranslator.logger import setup
    from ocrtranslator.ocr import available_engines, create as create_ocr

    log = setup(verbose=True)
    cfg = AppConfig.load()
    score = 0
    total = 0

    def check(label: str, ok: bool, detail: str = "", fatal: bool = False) -> None:
        nonlocal score, total
        total += 1
        score += bool(ok)
        mark = "✓" if ok else ("✗" if fatal else "!")
        print(f"  [{mark}] {label:<26} {detail}")
        if fatal and not ok:
            print("\n关键依赖缺失，后续检查已跳过。")
            raise SystemExit(2)

    print(f"socrans {__version__} 自检\n" + "=" * 62)
    print(f"Python {info['python']}  平台 {info['platform']}  CPU {info['cpus']} 核")
    print(f"依赖目录 {info['vendor'] or '（系统环境）'}   推理线程 {info['threads']}")
    print(f"数据目录 {app_dir()}")
    print("=" * 62)

    print("\n[1] GUI 与截图")
    try:
        from PyQt6.QtCore import QT_VERSION_STR, PYQT_VERSION_STR
        check("PyQt6", True, f"PyQt {PYQT_VERSION_STR} / Qt {QT_VERSION_STR}")
    except Exception as exc:
        check("PyQt6", False, f"{exc}", fatal=True)
    try:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtGui import QGuiApplication
        app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
        screens = app.screens()
        check("屏幕枚举", bool(screens),
              "；".join(f"{s.name()} {s.geometry().width()}x{s.geometry().height()}"
                        f"@{s.devicePixelRatio():g}x" for s in screens))
        from ocrtranslator.capture import grab
        caps = grab.grab_all_screens()
        check("抓屏", bool(caps), f"{len(caps)} 屏成功"
              if caps else "返回空（远程桌面/Wayland 权限？）")
        if caps:
            one = next(iter(caps.values()))
            sub = grab.crop_from_capture(one, (0, 0, min(200, one.width),
                                               min(120, one.height)),
                                         one.region[:2])
            check("裁剪与 DPR 换算", sub.width > 0,
                  f"{one.width}x{one.height} -> {sub.width}x{sub.height}")
    except Exception as exc:
        check("抓屏链路", False, f"{type(exc).__name__}: {exc}")

    print("\n[2] OCR 引擎")
    for e in available_engines():
        check(e.label, e.available, f"{e.version} {e.note}")
    try:
        eng = create_ocr(cfg)
        import time
        t0 = time.perf_counter()
        eng.warmup()
        dt = time.perf_counter() - t0
        check("模型加载", True, f"{eng.label} 用时 {dt:.2f}s")
        # 真识别一次：生成一张带字的图走完整路径
        from ocrtranslator.models import CaptureResult
        img = _synth_capture()
        t0 = time.perf_counter()
        lines = eng.recognize(img)
        check("实际识别", len(lines) > 0,
              f"{len(lines)} 行 / {time.perf_counter() - t0:.2f}s："
              + (lines[0].text[:34] if lines else "无结果"))
        eng.close()
    except Exception as exc:
        check("OCR 可用", False, f"{type(exc).__name__}: {exc}")

    print("\n[3] 版面分析")
    try:
        from ocrtranslator.layout import build_blocks
        from ocrtranslator.models import OcrLine
        rows = [OcrLine(text=t, conf=0.9, box=(40, 100 + i * 28, 460, 120 + i * 28))
                for i, t in enumerate(["第一行文字内容属于同一段落被切成两行",
                                       "第二行继续这一段",
                                       "第三行与上面之间留了空行应另起一段"])]
        rows[2].box = (40, 240, 420, 260)
        blocks = build_blocks(rows)
        check("段落合并", len(blocks) == 2, f"{len(rows)} 行 -> {len(blocks)} 段")
    except Exception as exc:
        check("版面分析", False, str(exc))

    print("\n[4] 翻译后端")
    from ocrtranslator.translate import BACKENDS, TranslateManager
    mgr = TranslateManager(cfg)
    for info_b, tag in mgr.status():
        check(info_b.label, info_b.available, f"{info_b.note}（{tag}）")
    print(f"  生效链：{' → '.join(mgr.chain())}")
    try:
        import time
        t0 = time.perf_counter()
        text, err = mgr.single("Quarterly revenue grew 18 percent.", "auto",
                               cfg.translate.target_lang)
        dt = time.perf_counter() - t0
        check("真实翻译一次", bool(text) and not err,
              f"{text[:38]}（{dt:.2f}s）" if text else f"失败：{err[:60]}")
    except Exception as exc:
        check("真实翻译一次", False, f"{type(exc).__name__}: {exc}")

    print("\n[5] 导出")
    try:
        from ocrtranslator.export import FORMATS, export
        from ocrtranslator.models import BilingualDoc
        d = BilingualDoc("自检", {"工具": "selftest"}, [("hello", "你好")])
        outdir = app_dir() / "exports"
        made = []
        for fmt in FORMATS:
            try:
                made.append(export(d, fmt, outdir, f"selftest_{fmt}", overwrite=True))
            except Exception as exc:
                check(f"导出 {fmt}", False, str(exc)[:70])
        check("导出全部格式", len(made) == len(FORMATS),
              f"{len(made)}/{len(FORMATS)} 成功 -> {outdir}")
    except Exception as exc:
        check("导出", False, str(exc))

    print("\n[6] 全局热键")
    try:
        from ocrtranslator.capture.hotkey import HotkeyManager, parse
        spec = cfg.capture.hotkey
        check("热键语法", parse(spec) is not None, f"{spec}"
              + (f" -> {parse(spec).display}" if parse(spec) else ""))
        hm = HotkeyManager(lambda _n: None)
        ok = hm.start() and hm.bind("selftest", spec)
        hm.unbind_all()
        check("实际注册", ok, "成功" if ok else "失败：可能被其他程序占用，"
              "可在设置里换一组或改用应用内快捷键")
    except Exception as exc:
        check("热键", False, f"{type(exc).__name__}: {exc}")

    print("\n[7] 离线能力")
    try:
        import socket
        blocked = []
        for host in ("translate.googleapis.com", "api-free.deepl.com",
                     "fanyi-api.baidu.com", "api.mymemory.translated.net"):
            try:
                socket.create_connection((host, 443), timeout=3).close()
            except OSError:
                blocked.append(host)
        check("在线端点可达", not blocked,
              "全部可达" if not blocked else f"不可达：{', '.join(blocked)}")
        offline = [i.name for i, _ in mgr.status() if not i.online and i.available]
        check("离线兜底可用", bool(offline), ", ".join(offline) or
              "无（建议 pip install argos-translate 并下载一个语向）")
    except Exception as exc:
        check("网络探测", False, str(exc))

    mgr.close()
    print("\n" + "=" * 62)
    print(f"结论：{score}/{total} 项通过")
    if score < total:
        print("未通过项不影响启动，但对应功能会降级。日志见 "
              f"{app_dir() / 'logs'}")
    return 0 if score == total else 1


def _synth_capture() -> "object":
    """造一张确定能被识别的图，走 CaptureResult 的真实字节路径。"""
    from PIL import Image, ImageDraw, ImageFont

    from ocrtranslator.models import CaptureResult

    im = Image.new("RGB", (620, 120), "white")
    d = ImageDraw.Draw(im)
    f = None
    for p in (r"C:\Windows\Fonts\msyh.ttc", "/System/Library/Fonts/PingFang.ttc",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            f = ImageFont.truetype(p, 26)
            break
        except OSError:
            continue
    d.text((20, 20), "Quarterly revenue grew 18 percent", fill="black", font=f)
    d.text((20, 66), "本季度营收同比增长 18%", fill="black", font=f)
    rgba = im.convert("RGBA")
    return CaptureResult(width=rgba.width, height=rgba.height,
                         bytes_bmp=rgba.tobytes(), dpr=1.0,
                         region=(0, 0, rgba.width, rgba.height))


# ---------------------------------------------------------------- 单文件模式
def translate_file(path: str, src: str, dst: str, out: str) -> int:
    """离线翻译一张图片：不弹窗、不装热键，结果写文件 + 打印。"""
    info = _early_bootstrap(headless=True)
    from ocrtranslator.config import AppConfig
    from ocrtranslator.logger import setup
    from ocrtranslator.models import CaptureResult
    from ocrtranslator.ocr import create as create_ocr
    from ocrtranslator.translate import TranslateManager

    setup()
    cfg = AppConfig.load()
    p = Path(path)
    if not p.is_file():
        print(f"文件不存在：{p}", file=sys.stderr)
        return 2

    from PIL import Image

    im = Image.open(p).convert("RGBA")
    cap = CaptureResult(width=im.width, height=im.height, bytes_bmp=im.tobytes(),
                        dpr=1.0, screen_name=p.name, region=(0, 0, im.width, im.height))
    print(f"读入 {p.name} {im.width}x{im.height}  依赖目录 {info['vendor']}")

    eng = create_ocr(cfg)
    eng.warmup()
    lines = eng.recognize(cap)
    print(f"OCR：{len(lines)} 行")
    if not lines:
        print("没有识别到文字。", file=sys.stderr)
        return 3
    for ln in lines:
        print(f"   {ln.conf:.2f}  {ln.text}")

    from ocrtranslator.layout import block_to_units, build_blocks
    from ocrtranslator.models import TranslationUnit

    blocks = build_blocks(lines)
    units = [TranslationUnit(block=b) for blk in blocks for b in block_to_units(blk)]
    mgr = TranslateManager(cfg)
    rep = mgr.translate([u.source_text for u in units], src or "auto",
                        dst or cfg.translate.target_lang)
    for u, t, e, err in zip(units, rep.texts, rep.engines, rep.errors):
        u.target_text, u.engine, u.error = t, e, err
    from ocrtranslator.models import PipelineResult

    result = PipelineResult(capture=cap, units=units,
                            source_lang=rep.detected_lang or (src or "auto"),
                            target_lang=dst or cfg.translate.target_lang,
                            ocr_engine=eng.label,
                            translate_engine=rep.primary_engine or cfg.translate.backend,
                            warnings=rep.warnings)
    text = result.plain_bilingual
    print("\n" + text)
    target = Path(out) if out else p.with_name(p.stem + "_bilingual.txt")
    target.write_text(text, encoding="utf-8")
    failed = sum(1 for u in units if u.error)
    print(f"\n已写出 {target}（{len(units)} 段，失败 {failed} 段，"
          f"引擎 {result.translate_engine}）")
    mgr.close()
    eng.close()
    return 0


# ---------------------------------------------------------------- GUI
def _offer_elevated_restart(app, log) -> bool:
    """数据目录全不可写时弹窗询问是否以管理员身份重启。

    返回 True 表示已成功交棒提权进程（调用方应立即退出本进程）。
    「不再提示」把标记写进（临时的）数据目录，下次启动静默跳过。
    """
    import ctypes
    import subprocess

    from PyQt6.QtWidgets import QMessageBox

    from ocrtranslator.config import app_dir

    marker = app_dir() / ".no_admin_prompt"
    if marker.exists():
        log.info("数据目录不可写，但用户此前已选择不再提示提权")
        return False

    box = QMessageBox(
        QMessageBox.Icon.Warning, "数据目录不可写",
        "所有常规数据目录都不可写，配置、日志和模型缓存将无法保存。\n\n"
        "常见原因：数据目录曾被「以管理员身份运行」创建过，"
        "普通权限从此写不进去。\n\n"
        "程序本身并不需要管理员权限 —— 更彻底的修复方法见 --doctor 输出。\n"
        "要改用管理员权限重新启动吗？")
    yes = box.addButton("以管理员重启", QMessageBox.ButtonRole.YesRole)
    box.addButton("继续运行", QMessageBox.ButtonRole.NoRole)
    never = box.addButton("不再提示", QMessageBox.ButtonRole.ActionRole)
    box.exec()
    clicked = box.clickedButton()
    if clicked is never:
        try:
            marker.write_text("1", encoding="utf-8")
            log.info("已标记不再提示提权（%s）", marker)
        except OSError:
            log.warning("无法写入提权提示标记 %s", marker)
        return False
    if clicked is not yes:
        log.info("用户选择以当前权限继续运行（数据目录不可写）")
        return False

    # ShellExecuteW "runas" = 标准的"启动前提权"：弹 UAC，用户确认后
    # 由系统创建管理员令牌的新进程。返回 >32 视为成功。
    if getattr(sys, "frozen", False):
        exe, args = sys.executable, sys.argv[1:]
    else:
        exe, args = sys.executable, [sys.argv[0], *sys.argv[1:]]
    params = subprocess.list2cmdline(args)
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
    if rc <= 32:
        log.error("提权重启失败（ShellExecuteW=%s）——可右键 exe 手动选择"
                  "「以管理员身份运行」，或按 --doctor 的建议修复目录 ACL", rc)
        return False
    log.info("已交棒提权进程（PID 由系统分配），本进程退出")
    app.quit()
    return True


def run_gui(no_tray: bool = False, capture_now: bool = False,
           verbose: bool = False) -> int:
    """常驻模式：托盘 + 全局热键 + 结果窗。

    退出码：0 正常；2 依赖缺失；3 已有实例在跑。
    """
    info = _early_bootstrap(headless=False)
    from PyQt6.QtCore import Qt, QTimer
    from PyQt6.QtWidgets import QApplication, QMessageBox

    from ocrtranslator import __appname__, __version__
    from ocrtranslator.app import AppController
    from ocrtranslator.config import AppConfig, app_dir, data_dir
    from ocrtranslator.logger import get_logger, setup
    from ocrtranslator.ui.hub import HubWindow, ToastWindow
    from ocrtranslator.ui.result_window import ResultWindow
    from ocrtranslator.ui.settings import SettingsDialog
    from ocrtranslator.ui.tray import HiddenHost, Tray

    log = setup(log_dir=str(app_dir() / "logs"), verbose=verbose)
    log.info("启动 %s %s（%s）", __appname__, __version__, info)

    # 单实例第一道闸：Global\ 命名互斥量（见 _acquire_global_mutex 的注释）。
    if not _acquire_global_mutex(log):
        return 3
    # 单实例第二道闸：app_dir 下的 .lock 文件（带 PID 复用检测）。
    if not _acquire_instance_lock(app_dir(), log):
        return 3

    app = QApplication(sys.argv)
    app.setApplicationName(__appname__)
    app.setApplicationVersion(__version__)
    app.setQuitOnLastWindowClosed(False)     # 关结果窗不该退出程序
    cfg = AppConfig.load()

    # HiddenHost 是"无主窗口"应用的锚点：QMenu/QDialog 需要 parent，
    # 同时它在托盘不可用时（部分 Linux 桌面）降级成一个真的小窗口。
    host = HiddenHost(cfg)
    host.show()
    ctrl = AppController(cfg, parent=host)
    tray = Tray(cfg, parent=host)
    if no_tray or not tray.supported:
        host.show_fallback()
    else:
        tray.show()

    windows: dict[int, ResultWindow] = {}

    # ---- 独立提示窗口（不依赖 Win11 通知中心）+ 主窗口 ------------------
    # 托盘气泡走系统通知：专注模式/通知权限一开就被吞，用户只觉得"没反应"。
    # Toast 是纯 Qt 窗口必达；Hub 给程序一个任务栏可见的常规窗口。
    toast = ToastWindow(cfg, parent=host)
    # 点 × 一律退出进程（用户反馈两次"退出不彻底"后的最终语义）。
    # 想驻留后台用「隐藏到托盘」按钮；托盘菜单「主窗口」/ Ctrl+H 可召回。
    hub = HubWindow(cfg, parent=host)
    hub.open_settings.connect(lambda: _open_settings(cfg, ctrl))
    hub.open_logs.connect(_open_logs)

    # ---- 数据目录全部不可写 → 提供一键提权重启（UAC） --------------------
    # 程序设计上不需要管理员；走到这里说明 %APPDATA% 等候选目录全被 ACL 挡住
    # （最常见：目录曾被"以管理员身份运行"创建过）。与其让用户反复手动
    # 右键提权，不如在启动早期给一次选择；「不再提示」写入临时目录标记。
    from ocrtranslator.config import USING_TEMP_FALLBACK
    if USING_TEMP_FALLBACK and sys.platform == "win32":
        if _offer_elevated_restart(app, log):
            return 0                 # 已交棒给提权进程，本进程立即退出

    hub_state = {"visible": True}   # 截图开始时主窗口是否可见（结束后按此还原）

    def begin_capture(*a, **k) -> None:
        toast.hide()                       # 气泡残影会被截进 OCR 输入
        hub_state["visible"] = hub.isVisible()
        ctrl.start_capture(*a, **k)

    def restore_hub() -> None:
        # 截图期间主窗口被覆盖层隐藏；成功/失败后按截图前的可见性还原。
        # 不在覆盖层里还原：成功路径下结果窗先弹出，主窗口随后出现，
        # 顺序反了会把"空主窗口"留在最上层。
        if hub_state["visible"] and not hub.isVisible():
            hub.show()

    def show_last_result() -> None:
        if ctrl.last_result:
            show_result(ctrl.last_result)
        else:
            toast.show_message("还没有结果", "先按热键截一张")
            hub.add_log("还没有可显示的结果，先按热键截一张", "warn")

    def show_result(result) -> None:
        win = windows.get(0)
        if win is None:
            win = ResultWindow(cfg)
            win.recapture_requested.connect(begin_capture)
            win.retranslate_requested.connect(ctrl.retranslate)
            win.export_requested.connect(lambda f: do_export(f, win))
            win.closed_result.connect(lambda: windows.pop(0, None))
            windows[0] = win
        win.set_result(result)
        win.show()
        win.raise_()
        win.activateWindow()
        total_s = sum(result.elapsed_ms.values()) / 1000
        toast.show_message(
            "翻译完成",
            f"{len(result.units)} 段 · {result.translate_engine} · {total_s:.1f}s")
        hub.add_log(f"翻译完成：{len(result.units)} 段 · "
                    f"{result.translate_engine} · {total_s:.1f}s", "ok")
        hub.set_row("上次结果", f"{len(result.units)} 段 · {total_s:.1f}s · "
                                f"{result.translate_engine}", "ok")
        hub.set_row("翻译链", " → ".join(ctrl.translator.chain()))
        restore_hub()
        tray.set_state("idle")

    def do_export(fmt: str, win: ResultWindow) -> None:
        from ocrtranslator.export import ExportError, export, pattern_filename

        res = win.result
        if res is None:
            return
        folder = cfg.export.default_dir or data_dir("exports")
        stem = pattern_filename(cfg.export.filename_pattern,
                                {"target_lang": res.target_lang,
                                 "srclang": res.source_lang or "auto",
                                 "engine": res.ocr_engine,
                                 "count": str(len(res.units))})
        try:
            path = export(res.to_document(), "txt" if fmt == "last" else fmt,
                          folder, stem)
        except ExportError as exc:
            QMessageBox.warning(win, "导出失败", str(exc))
            return
        toast.show_message("已导出", str(path))
        hub.add_log(f"已导出 {path}")
        win.status_line.setText(f"已导出 {path.name}")

    def on_error(msg: str, hint: str) -> None:
        tray.set_state("warn", msg)
        toast.show_message("截图翻译出错", f"{msg}\n{hint}" if hint else msg, "warn")
        hub.add_log(f"错误：{msg}" + (f"（{hint}）" if hint else ""), "error")
        restore_hub()
        if msg.startswith("OCR"):
            hub.set_row("OCR", msg, "error")
        win = windows.get(0)
        if win and win.isVisible():
            win.show_error(msg, hint)
        # 结果窗不可见时不额外弹系统通知：Toast 已必达，
        # 而系统通知中心（专注模式）可能整层吞掉气泡。
        log.warning("用户可见错误：%s | %s", msg, hint)

    ctrl.result_ready.connect(show_result)
    ctrl.error.connect(on_error)
    ctrl.busy.connect(lambda b: (tray.set_state("busy" if b else "idle"),
                                 hub.set_busy(b)))

    def on_status(text: str) -> None:
        # 写成显式分支而不是 `windows.get(0) and ...setText(t)`：
        # 后者依赖"QWidget 恒为真值"这个隐含前提，可读性也差
        win = windows.get(0)
        if win is not None:
            win.status_line.setText(text)
        hub.add_log(text)
        if text.startswith("OCR 就绪"):
            hub.set_row("OCR", text.removeprefix("OCR 就绪："), "ok")
        elif "仍在加载" in text or "模型仍在加载" in text:
            hub.set_row("OCR", "模型加载中…", "warn")

    ctrl.status.connect(on_status)

    tray.capture.connect(begin_capture)
    tray.open_hub.connect(hub.show_and_raise)
    hub.capture_requested.connect(begin_capture)
    hub.show_last.connect(show_last_result)
    tray.open_settings.connect(lambda: _open_settings(cfg, ctrl))
    tray.open_logs.connect(_open_logs)
    tray.show_last.connect(show_last_result)
    tray.quit_app.connect(app.quit)

    _install_shortcuts(app, host, ctrl, cfg, hub)

    hub.set_row("OCR", "模型加载中…", "warn")
    hub.set_row("翻译链", " → ".join(ctrl.translator.chain()))
    hub.set_row("热键", "注册中…")
    hub.add_log(f"{__appname__} {__version__} 已启动"
                + ("（托盘不可用，主窗口即主要入口）" if no_tray or not tray.supported else ""))

    ctrl.warm_ocr()
    if not ctrl.bind_hotkeys():
        # 静默降级是错误设计：用户之前就是"按了没反应"以为程序坏了。
        # 用独立气泡提示告知（不依赖系统通知中心，也不能弹模态框卡启动）。
        reason = ctrl.hotkeys.last_error or "原因未知"
        hub.set_row("热键", f"{cfg.capture.hotkey.upper()} 未注册：{reason}", "error")
        hub.add_log(f"全局热键 {cfg.capture.hotkey.upper()} 注册失败：{reason}", "error")
        toast.show_message(
            "全局热键不可用",
            f"{cfg.capture.hotkey.upper()} 注册失败：{reason}\n"
            "仍可用：托盘菜单 / 主窗口按钮 / 应用内快捷键 Ctrl+Shift+A", "warn")
        log.warning("全局热键注册失败：%s（仅应用内快捷键可用）", reason)
    else:
        hub.set_row("热键", f"{cfg.capture.hotkey.upper()} 已注册", "ok")
        hub.add_log(f"全局热键 {cfg.capture.hotkey.upper()} 已注册", "ok")
        tray.set_state("idle")

    if ctrl.hotkey_note:
        # 备用热键生效：必须让用户**看见换成了哪个键**，否则按原键没反应，
        # 又是一轮"程序坏了"的误会（自动回退不能静默）。
        hub.set_row("热键", ctrl.hotkey_note, "warn")
        hub.add_log(f"热键调整：{ctrl.hotkey_note}", "warn")
        toast.show_message("全局热键已调整", ctrl.hotkey_note, "warn")
        tray.set_state("warn", ctrl.hotkey_note)
        log.warning("热键自动调整：%s", ctrl.hotkey_note)

    hub.show()          # 启动即给一个可见窗口；×=隐藏到托盘（无托盘时=退出）

    if capture_now:
        QTimer.singleShot(700, ctrl.start_capture)

    # aboutToQuit 顺序：ctrl.shutdown()（等热键泵与所有 QThread 结束）→
    # 释放文件锁 → 收托盘图标。shutdown 是幂等的。
    def _on_about_to_quit() -> None:
        _release_instance_lock(app_dir(), ctrl)
        tray.hide()

    app.aboutToQuit.connect(_on_about_to_quit)
    log.info("进入事件循环")
    return app.exec()


#: 锁文件内容："pid|进程创建时间(100ns 计数)"。
#: 只存 PID 是不够的 —— Windows 会复用 PID。实测本机就踩过：
#: 上一次会话用 Stop-Process -Force 杀掉了 GUI，进程没机会执行 aboutToQuit，
#: 于是 .lock 留在磁盘上；若那个 PID 之后被别的程序拿到，
#: `_pid_alive()` 返回 True，本程序会误判"已有实例在运行"，
#: 弹一句提示然后**直接退出**，用户看到的就是"双击没反应"。
#: 加上创建时间就能区分"PID 复用"与"真的是同一个程序还在跑"。
def _proc_start_time(pid: int) -> int | None:
    """进程创建时刻（FILETIME 的 100ns 计数）。取不到返回 None。

    ⚠ 四个 FILETIME 参数**必须全部传真实结构体，不能传 None**。
    MSDN 没写清这点，实测传 None 会让 Windows 直接往空指针里写：
    `OSError: exception: access violation writing 0x0000000000000000`。
    后果不是崩溃而是**静默失效** —— ctypes 把访问违例转成 Python 异常，
    被 `_acquire_instance_lock` 的 `except OSError` 吞掉，于是锁永远写不成、
    PID 复用永远检测不出来，日志里只留一行"实例锁检查失败（继续启动）"。
    这就是本机实测到的现象（GUI 进程活着但 .lock 没升级）。

    ⚠ restype 也要设成 c_void_p：ctypes 默认按 C int（32 位）取返回值，
    而 HANDLE 是 64 位指针，高位非零时会被截断成废句柄。
    """
    if os.name != "nt":
        try:
            return int(os.stat(f"/proc/{pid}").st_ctime * 1e7)   # st_ctime，不是 ctime
        except OSError:
            return None
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.CloseHandle.restype = ctypes.c_int
    h = k32.OpenProcess(0x1000, False, pid)     # QUERY_LIMITED_INFORMATION，同 _pid_alive
    if not h:
        return None
    try:
        creation, exit_t, kernel_t, user_t = (wintypes.FILETIME() for _ in range(4))
        if not k32.GetProcessTimes(h, ctypes.byref(creation), ctypes.byref(exit_t),
                                   ctypes.byref(kernel_t), ctypes.byref(user_t)):
            return None
        return (creation.dwHighDateTime << 32) | creation.dwLowDateTime
    finally:
        k32.CloseHandle(h)


#: 锁文件内容格式："v2:<pid>|<创建时间>"。带前缀是为了区分旧格式（只有 pid）。
LOCK_PREFIX = "v2:"

#: 跨用户/跨提权的单实例互斥量名。Global\ 命名空间全机器可见。
MUTEX_NAME = "Global\\socrans-instance-v1"


def _acquire_global_mutex(log, name: str = MUTEX_NAME) -> bool:
    """单实例第一道闸：Global\\ 命名互斥量。返回 False = 已有实例在跑。

    为什么只有 .lock 文件不够 —— 本机实测事故（2026-11-10）：
    用户先启动了一个实例 A，之后又双击启动实例 B。两个进程的 app_dir()
    解析到了不同目录（A 是管理员/沙箱环境跑的，数据目录被重定向），
    于是 B 看不到 A 写的 .lock，双双通过文件锁检查；接着 B 注册
    Ctrl+Shift+A 必然失败（被 A 占着），用户看到的就是
    "热键没反应 / 需要以管理员身份运行才行"。

    Global\\ 互斥量是内核对象，**全机器可见、与数据目录无关**，
    正好补上这个盲区；且进程退出（含崩溃、被杀）由 OS 自动释放，
    不可能像文件锁那样留下残留。

    ⚠ kernel32 必须用 `WinDLL(..., use_last_error=True)` 加载：
    `ctypes.windll.kernel32` 不捕获 GetLastError，get_last_error() 恒为 0，
    会把"已存在(183)/拒绝访问(5)"误判成"创建失败但放行"。

    其他未知错误（如某些沙箱禁建内核对象）不拦启动 —— 宁可放过双开，
    也不能因为一个辅助检查拒绝启动（与文件锁的保守取向一致）。
    """
    if os.name != "nt":
        return True
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    k32.CreateMutexW.argtypes = [wintypes.HANDLE, wintypes.BOOL, wintypes.LPCWSTR]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    # ⚠ CreateMutexW 对"已存在的互斥量"会返回**有效句柄**并置
    # GetLastError=ERROR_ALREADY_EXISTS —— 不能只判句柄非空就算首个实例，
    # 必须紧接着取 get_last_error()（use_last_error=True 才能捕获到）。
    h = k32.CreateMutexW(None, False, name)
    err = ctypes.get_last_error()
    if h:
        if err == 183:      # ERROR_ALREADY_EXISTS
            k32.CloseHandle(h)
            log.info("全局互斥量已存在 —— 已有实例在运行")
            _inform_existing(0)
            return False
        return True         # 首个创建者；句柄不关，进程活着=锁在，退出即释放
    if err == 5:            # ERROR_ACCESS_DENIED：互斥量被更高完整性（管理员）
        # 进程持有 —— 同样说明已有实例
        log.info("全局互斥量被其他完整性级别的进程持有 —— 已有实例在运行")
        _inform_existing(0)
        return False
    log.debug("全局互斥量创建失败（GetLastError=%d），退回文件锁检查", err)
    return True


def _lock_text() -> str:
    return f"{LOCK_PREFIX}{os.getpid()}|{_proc_start_time(os.getpid()) or 0}"


def _parse_lock(raw: str) -> tuple[int, str]:
    """返回 (pid, 创建时间戳)。旧格式（无 v2 前缀）返回 (pid, "")。"""
    raw = raw.strip()
    if not raw.startswith(LOCK_PREFIX):
        try:
            return int(raw.partition("|")[0] or "0"), ""
        except ValueError:
            return 0, ""
    pid, _, stamp = raw[len(LOCK_PREFIX):].partition("|")
    try:
        return int(pid or "0"), stamp
    except ValueError:
        return 0, ""


def _acquire_instance_lock(dir_: Path, log) -> bool:
    """单实例保护。返回 False = 确认已有实例在跑，调用方应退出。

    ⚠ 只存 PID 不够：Windows 会复用 PID。本机就踩过 ——
    上一次会话用 Stop-Process -Force 杀掉 GUI，进程没机会执行 aboutToQuit，
    .lock 就永久留在磁盘上。一旦那个 PID 被别的程序拿到，
    `_pid_alive()` 返回 True，本程序误判"已有实例在运行"，
    弹一句提示然后**直接退出** —— 用户看到的现象就是"双击 run.bat 没反应"。

    判定规则（保守取向）：只有"v2 格式 + PID 活着 + 创建时间完全一致"
    才认定是同一个程序还在跑。其余情况一律当残留覆盖并升级格式。
    理由：双开保护只是锦上添花（第二个进程的热键注册会失败并明确提示，
    不会静默出错），而"启动不了"是致命故障 —— 宁可偶尔放过双开，
    也绝不能因为一个无法验证的旧锁文件拒绝启动。
    """
    lock = dir_ / ".lock"
    try:
        if lock.exists():
            pid, stamp = _parse_lock(lock.read_text(encoding="utf-8", errors="replace"))
            if pid and _pid_alive(pid):
                current = _proc_start_time(pid) if stamp else None
                if stamp and current is not None and str(current) == stamp:
                    _inform_existing(pid)
                    return False
                if not stamp:
                    log.info("锁文件是旧格式（无创建时间戳），无法证伪 PID 复用，"
                             "按残留处理并升级为 v2")
                else:
                    log.info("锁里的 PID %s 创建时间 %s≠%s，判定为 PID 复用，覆盖锁",
                             pid, stamp, current)
            else:
                log.info("清理残留锁文件（PID %s 已不存在）", pid or "?")
        lock.write_text(_lock_text(), encoding="utf-8")
        return True
    except OSError as exc:
        # 锁本身有问题不该阻止启动
        log.warning("实例锁检查失败（继续启动）：%s", exc)
        return True


def _release_instance_lock(dir_: Path, ctrl) -> None:
    try:
        (dir_ / ".lock").unlink(missing_ok=True)
    except OSError:
        pass
    ctrl.shutdown()


def _open_settings(cfg, ctrl) -> None:
    from ocrtranslator.ui.settings import SettingsDialog

    dlg = SettingsDialog(cfg, ctrl.translator)
    dlg.apply_requested.connect(lambda: _after_settings(cfg, ctrl))
    dlg.hotkey_rebind.connect(lambda spec: ctrl.hotkeys.rebind("capture", spec))
    dlg.exec()


def _after_settings(cfg, ctrl) -> None:
    """设置保存后的重建。

    OCR 引擎/线程数无法热更（onnx session 已建），必须 close 再 warm；
    翻译缓存必须清空 —— 否则改了目标语言或后端却仍读到旧译文，
    用户会以为"设置没生效"（这是最难自查的一类反馈）。
    """
    ctrl._ocr_warmed = False
    if ctrl._ocr is not None:
        try:
            ctrl._ocr.close()
        except Exception:
            pass
        ctrl._ocr = None
    ctrl._ocr_error = ctrl._ocr_hint = ""
    ctrl.translator.cache.clear()
    ctrl.translator.reset_backoff()
    ctrl.warm_ocr()


def _install_shortcuts(app, host, ctrl, cfg, hub) -> None:
    """应用内快捷键：全局热键被占用、或托盘不可用时的退路。

    QShortcut 必须给一个**真实窗口父级**：传 None 时 Qt 会报
    "QShortcut: No window parent defined" 且 ApplicationShortcut 在
    没有任何窗口激活时可能完全不触发。用 HiddenHost 当父级正好。
    """
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QKeySequence, QShortcut

    pairs = [("Ctrl+Shift+A", ctrl.start_capture),
             ("Ctrl+H", hub.show_and_raise),
             ("Ctrl+,", lambda: _open_settings(cfg, ctrl)),
             ("Ctrl+Q", app.quit)]
    _shortcuts.clear()
    for key, slot in pairs:
        sc = QShortcut(QKeySequence(key), host)
        sc.setContext(Qt.ShortcutContext.ApplicationShortcut)
        sc.activated.connect(slot)
        _shortcuts.append(sc)      # 必须持有引用：局部变量被回收后快捷键静默失效


_shortcuts: list = []


def _open_logs() -> None:
    from ocrtranslator.config import app_dir

    p = app_dir() / "logs"
    try:
        p.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(str(p))                      # type: ignore[attr-defined]
        else:
            import subprocess

            subprocess.Popen(["xdg-open", str(p)])
    except Exception as exc:
        print(f"打开日志目录失败：{exc}\n路径：{p}", file=sys.stderr)


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        k32 = ctypes.windll.kernel32
        # restype 必须是 c_void_p：默认按 C int 取返回值会把 64 位 HANDLE 截断，
        # 截断后的废句柄让 GetExitCodeProcess 失败，而下面失败时**按存活处理**，
        # 于是别的程序占用的 PID 会被误判成"本程序已在运行"→ 直接退出。
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.CloseHandle.restype = ctypes.c_int
        h = k32.OpenProcess(0x1000, False, pid)      # QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_uint32()
            if k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return code.value == 259              # STILL_ACTIVE
            return True
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _inform_existing(pid: int = 0) -> None:
    """已有实例时的提示。

    ⚠ 必须用**带超时**的消息框，不能用 `MessageBoxW`。
    实测：第二个实例弹模态框后无人点击，进程会**永久挂住**（25s 测试直接
    TimeoutExpired），run.bat 也就永远不返回 —— 用户看到的是"又开了一个窗口
    然后卡死"，比原来的问题更糟。
    `MessageBoxTimeoutW` 是 user32 的未公开导出（Vista 起一直在，ordinal 500），
    本机实测 3 秒自动关闭并返回。它不可用时直接退回 stderr，
    绝不退化成阻塞式模态框。
    """
    where = f"（进程 {pid}）" if pid else ""
    msg = (f"截图翻译已经在运行{where}，刚才双击没有打开新窗口。\n\n"
           f"· 看屏幕右下角托盘图标（可能藏在 ∧ 折叠区）——右键它可打开主窗口或退出；\n"
           f"· 按 Ctrl+Shift+A 直接开始截图。\n"
           f"· 如果托盘里找不到图标但进程确实还在，可在任务管理器结束"
           f"'截屏翻译'后再启动。")
    if os.name == "nt":
        try:
            import ctypes

            u32 = ctypes.windll.user32
            u32.MessageBoxTimeoutW.restype = ctypes.c_int
            # MB_ICONINFORMATION=0x40, wLanguageID=0, 5000ms
            shown = u32.MessageBoxTimeoutW(None, msg, "已在运行", 0x40, 0, 5000)
            if shown:        # 返回 0 才是"没弹出来"，那时必须退回 stderr
                return
        except (AttributeError, OSError):
            pass             # 这个 API 在精简系统上可能不存在
    print(msg, file=sys.stderr)


def doctor() -> int:
    """启动前置检查。实现见 ocrtranslator/doctor.py（唯一一份，避免两处漂移）。"""
    from ocrtranslator.doctor import doctor as _doctor

    return _doctor()


def _detach_console() -> None:
    """双击启动 GUI 时关掉 console=True 带来的黑窗。

    打包为 console=True 是为了让 --file/--doctor/--selftest 的输出在
    pwsh 里可见；代价是双击 exe 会多出一个没人要的黑窗。
    FreeConsole() 让为本进程创建的 conhost 在没有其他附着者时自行退出；
    从 pwsh 带参数启动时控制台属于 shell， detachment 只影响本进程。
    FreeConsole 之后原 stdout/stderr 句柄失效，print() 会抛 OSError，
    所以必须先重定向到日志文件（正经日志走 logger，这里只兜散落的 print）。
    只在 argv 为空时调用：从 pwsh 显式带 --capture 启动的人是想看输出的。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        k32 = ctypes.windll.kernel32
        if not k32.FreeConsole():      # 本就无控制台（windowed 构建）→ 无事可做
            return
        log_dir = Path(os.environ.get("APPDATA", str(Path.home()))) / "socrans" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        f = open(log_dir / "console.log", "a", buffering=1,
                 encoding="utf-8", errors="replace")
        sys.stdout = f
        sys.stderr = f
    except Exception:                  # noqa: BLE001 - 兜底失败也不能挡 GUI
        # 日志开不出来时必须给 stdout/stderr 一个**可写**的替代品：
        # 原句柄在 FreeConsole 后已死，后续 print() 会抛 OSError，
        # 那会绕过所有业务逻辑直接砸进 _fatal（GUI 双击变"崩溃弹窗"）。
        # 走到 except 说明 FreeConsole 已成功（返回 False 会提前 return），
        # 所以无条件替换是安全的。
        class _Null:
            def write(self, *_a): return 0
            def flush(self): pass
        sys.stdout = _Null()
        sys.stderr = _Null()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="socrans", description="截图 OCR 双语对照翻译")
    ap.add_argument("--doctor", action="store_true",
                    help="只做启动前置检查（快，不加载模型），成功返回 0")
    ap.add_argument("--selftest", action="store_true", help="环境自检")
    ap.add_argument("--file", help="翻译指定图片文件（不启动 GUI）")
    ap.add_argument("--out", help="配合 --file 的输出路径")
    ap.add_argument("--src", default="", help="源语言代码，默认 auto")
    ap.add_argument("--dst", default="", help="目标语言代码，默认取配置")
    ap.add_argument("--capture", action="store_true", help="启动后立即截一次")
    ap.add_argument("--no-tray", action="store_true", help="不使用系统托盘")
    ap.add_argument("--verbose", "-v", action="store_true", help="DEBUG 日志")
    ns = ap.parse_args(argv)

    try:
        if ns.doctor:
            # doctor 会被 launcher **无人值守**地逐个解释器调用，
            # 所以这条路径上任何失败都不许等输入、不许弹窗（见 _fatal 的 interactive 参数）
            try:
                return doctor()
            except Exception as exc:                # noqa: BLE001
                return _fatal(exc, interactive=False)
        if ns.selftest:
            return selftest(argv)
        if ns.file:
            return translate_file(ns.file, ns.src, ns.dst, ns.out)
        if not argv:                   # 纯双击（无任何参数）→ 收掉控制台黑窗
            _detach_console()
        return run_gui(no_tray=ns.no_tray, capture_now=ns.capture, verbose=ns.verbose)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        # 必须早于下面的兜底分支：否则 Ctrl+C 会被当成崩溃，
        # 打一整段 traceback 还要用户"按回车退出"
        print("\n已取消", file=sys.stderr)
        return 130
    except Exception as exc:                        # noqa: BLE001 - 启动期兜底
        # 这一层是"窗口一闪就没了"的真正解药。
        # 之前 run_gui 里 `from PyQt6.QtWidgets import QApplication` 在 Anaconda
        # 解释器下抛 ImportError: DLL load failed，没人捕获，于是 traceback 打到
        # stderr 后进程直接退出：双击运行的人只看到控制台闪一下，
        # 连自己看到了什么都不知道。任何启动期异常都必须变成**可读的结论**。
        return _fatal(exc)


def _fatal(exc: Exception, *, interactive: bool = True) -> int:
    import traceback
    from datetime import datetime

    # 显式传入 exc，不用 traceback.format_exc()：
    # 后者依赖隐式的 sys.exc_info()，虽然从 except 块里调用的函数通常能拿到，
    # 但一旦 _fatal 被从 except 之外调用就会打印 "NoneType: None"，
    # 而这正是最需要看到堆栈的时刻。
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    print(text, file=sys.stderr)
    # 尽力落盘：GUI 程序没有控制台时，日志是唯一证据
    try:
        from ocrtranslator.config import app_dir

        d = app_dir() / "logs"
        d.mkdir(parents=True, exist_ok=True)
        (d / "crash.log").write_text(
            f"崩溃于 {datetime.now().isoformat(timespec='seconds')}\n"
            f"python {sys.version.split()[0]}  {sys.executable}\n\n{text}", encoding="utf-8")
        print(f"已写入崩溃日志：{d / 'crash.log'}", file=sys.stderr)
    except Exception as log_exc:                    # noqa: BLE001
        print(f"（崩溃日志写入失败：{log_exc}）", file=sys.stderr)

    msg = str(exc)
    if isinstance(exc, ImportError) and "DLL load failed" in msg:
        print("\n当前 Python 环境无法加载 PyQt6。"
              "\n最常见原因：这个 python 是 Anaconda/Miniconda 的，"
              "它自带的 Qt/ICU DLL 抢在 PyQt6 的 Qt6 DLL 前面。")
        print(r"推荐做法：双击 run.bat（它会自动挑一个能用的解释器），或运行 python tools\launcher.py")
        print("手动排查：python main.py --doctor")
    elif isinstance(exc, ImportError):
        print(f"\n缺少依赖：{exc}")
        print("  pip install -r requirements.txt"
              r"   或免 pip：python tools\fetch_wheels.py")
    else:
        print(f"\n未预期错误：{type(exc).__name__}: {exc}")

    if interactive:
        _pause_if_interactive()
    return 1


def _pause_if_interactive() -> None:
    """交互式运行时按回车再退出；管道/CI 下绝不阻塞。"""
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            input("\n按回车键退出…")
    except (OSError, ValueError):
        pass


if __name__ == "__main__":
    raise SystemExit(main())
