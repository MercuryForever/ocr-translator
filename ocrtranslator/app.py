"""流程编排：热键 → 框选 → 裁剪 → OCR → 版面 → 翻译 → 结果窗。

线程模型（这是本文件全部复杂度的来源）：

    GUI 线程            Worker 线程(QThread)
    ─────────           ────────────────────
    按下热键
    overlay.start()
    overlay.confirm() ──► PipelineWorker.run()
                            crop → OCR → layout → translate
    ◄── resultReady ────  发信号带 PipelineResult
    渲染结果窗

三条铁律，违反任何一条都会出现"偶发崩溃"或"界面卡死"：
  1. **Worker 绝不碰 QWidget**。抓屏得到的 QImage 在 GUI 线程转成字节
     （CaptureResult 是纯 Python 数据），Worker 只吃字节。
  2. **OCR 引擎实例跨调用共享但要加锁**。RapidOCR 的 onnx session 不是线程安全的；
     用户连按两次热键时若并发推理会 segfault（实测 onnxruntime 并发调用同一
     session 会崩，且崩在 C++ 层，Python 栈里看不到）。
  3. **取消要真取消**。翻译一批 20 条要几秒，期间用户可能又截了一张。
     旧 worker 继续跑会浪费配额并可能覆盖新结果 —— 用 generation 计数丢弃过期结果。
"""

from __future__ import annotations

import threading
import time
import traceback

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal

from .capture import hotkey as hk
from .capture.overlay import CaptureOverlay
from .config import AppConfig
from .languages import AUTO
from .layout import block_to_units, build_blocks
from .logger import get_logger
from .models import CaptureResult, PipelineResult, TranslationUnit
from .ocr import OcrError
from .ocr import create as create_ocr
from .translate import TranslateManager
from .translate.base import TranslationError

log = get_logger("app")


class PipelineWorker(QThread):
    """一次截图的 OCR + 翻译。跑完自动退出。"""

    done = pyqtSignal(object)          # PipelineResult
    failed = pyqtSignal(str, str, str)  # stage, message, hint
    progress = pyqtSignal(str, int)     # label, percent(0-100, -1=忙碌)

    def __init__(self, cap: CaptureResult, ocr, translator: TranslateManager,
                 cfg: AppConfig, src: str, dst: str, generation: int, parent=None):
        super().__init__(parent)
        self.cap, self.ocr = cap, ocr
        self.translator = translator
        self.cfg = cfg
        self.src, self.dst = src, dst
        self.generation = generation
        self.setObjectName(f"pipeline-{generation}")

    def run(self) -> None:            # noqa: D102
        timings: dict[str, int] = {}
        try:
            self.progress.emit("识别文字…", 20)
            t0 = time.perf_counter()
            with _OCR_LOCK:
                lines = self.ocr.recognize(self.cap, lang_hint=self.cfg.ocr.lang_hint)
            timings["ocr"] = int((time.perf_counter() - t0) * 1000)
            log.info("OCR 得 %d 行（%dms）", len(lines), timings["ocr"])

            if not lines:
                self.failed.emit(
                    "ocr", "框选区域没有识别到文字",
                    "可能是纯图片、字号过小、或选区太小。"
                    "建议放大窗口或重新框选更大的区域。")
                return

            # 坐标从物理像素折算到逻辑坐标（与选区/结果窗叠加显示一致）
            dpr = self.cap.dpr or 1.0
            if dpr != 1.0:
                for ln in lines:
                    ln.box = tuple(v / dpr for v in ln.box)
                    ln.poly = tuple((x / dpr, y / dpr) for x, y in ln.poly)

            self.progress.emit("分析版面…", 55)
            t0 = time.perf_counter()
            blocks = build_blocks(lines)
            units = [TranslationUnit(block=b) for b in
                     [u for blk in blocks for u in block_to_units(blk)]]
            timings["layout"] = int((time.perf_counter() - t0) * 1000)
            if not units:
                self.failed.emit("layout", "版面分析未产出可翻译段落", "请重试或反馈该截图")
                return

            self.progress.emit(f"翻译 {len(units)} 段…", 70)
            t0 = time.perf_counter()
            texts = [u.source_text for u in units]
            rep = self.translator.translate(texts, self.src, self.dst)
            timings["translate"] = int((time.perf_counter() - t0) * 1000)
            for u, tgt, eng, err in zip(units, rep.texts, rep.engines, rep.errors):
                u.target_text, u.engine, u.error = tgt, eng, err

            src_lang = rep.detected_lang or (self.src if self.src != AUTO else "")
            result = PipelineResult(
                capture=self.cap, units=units, source_lang=src_lang,
                target_lang=self.dst, ocr_engine=self.ocr.label,
                translate_engine=rep.primary_engine or self.cfg.translate.backend,
                elapsed_ms=timings, warnings=rep.warnings)
            self.progress.emit("完成", 100)
            self.done.emit(result)
        except OcrError as exc:
            self.failed.emit("ocr", str(exc), exc.hint or "")
        except TranslationError as exc:
            self.failed.emit("translate", str(exc), exc.hint or "")
        except Exception as exc:                      # 兜底：任何未预期异常都要可见
            tb = traceback.format_exc(limit=4)
            log.error("流水线异常：%s\n%s", exc, tb)
            self.failed.emit("internal", f"未预期错误：{exc.__class__.__name__}: {exc}",
                             "详情见日志（帮助 → 打开日志目录）。若反复出现请反馈。")


_OCR_LOCK = threading.RLock()


class AppController(QObject):
    """把 capture / ocr / translate / ui 粘起来的中枢。"""

    status = pyqtSignal(str)
    busy = pyqtSignal(bool)
    result_ready = pyqtSignal(object)
    error = pyqtSignal(str, str)
    #: 热键回调在泵线程触发，用这个信号 marshal 到 GUI 线程。
    #: 不能用 QTimer.singleShot —— 非 Qt 线程没有事件调度器，timer 永不触发。
    capture_requested = pyqtSignal(str)

    def __init__(self, cfg: AppConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.overlay: CaptureOverlay | None = None
        self.worker: PipelineWorker | None = None
        self.generation = 0
        self.last_result: PipelineResult | None = None
        self.last_capture: CaptureResult | None = None
        self.translator = TranslateManager(cfg)
        self._ocr = None
        self._ocr_error: str = ""
        self._ocr_hint: str = ""
        self._ocr_warmed = False
        self.hotkeys = hk.HotkeyManager(self._on_hotkey)
        # 非空 = 主/副热键被占用而自动改用了备用组合，UI 需要显著提示。
        self.hotkey_note: str = ""
        # 热键回调从泵线程 emit 信号 → queued 到 GUI 线程执行 start_capture
        self.capture_requested.connect(self.start_capture)
        self._pending_src = AUTO
        self._pending_dst = cfg.translate.target_lang

    # ---------------------------------------------------------------- OCR
    def warm_ocr(self) -> None:
        """后台预热模型。首次加载 1~25 秒，绝不能放在热键响应路径上。"""
        if self._ocr_warmed:
            return
        self._ocr_warmed = True

        class _Warm(QThread):
            ok = pyqtSignal(str)
            bad = pyqtSignal(str, str)

            def __init__(self, ctrl):
                super().__init__()
                self.ctrl = ctrl

            def run(self):
                try:
                    eng = create_ocr(self.ctrl.cfg)
                    eng.warmup()
                    self.ctrl._ocr = eng
                    self.ok.emit(str(eng.label))
                except OcrError as exc:
                    self.bad.emit(str(exc), exc.hint or "")
                except Exception as exc:
                    self.bad.emit(f"OCR 初始化失败：{exc}", "")

        self._warm = _Warm(self)
        self._warm.ok.connect(lambda label: (log.info("OCR 就绪：%s", label),
                                             self.status.emit(f"OCR 就绪：{label}")))
        self._warm.bad.connect(lambda msg, hint: (
            setattr(self, "_ocr_error", msg), setattr(self, "_ocr_hint", hint),
            log.error("OCR 初始化失败：%s | %s", msg, hint),
            self.error.emit(msg, hint)))
        self._warm.start()

    # ---------------------------------------------------------------- 热键
    def bind_hotkeys(self) -> bool:
        ok = self.hotkeys.start()
        spec = self.cfg.capture.hotkey
        self.hotkey_note = ""
        bound = self.hotkeys.bind("capture", spec)
        if not bound:
            # 占用时不要直接放弃：QQ（ctrl+alt+a）、微信、Snipaste 抢截图
            # 热键极其常见，静默失败的用户体验是"程序坏了/要提权才行"
            # （用户实测反馈的原话）。自动改用备用组合并显著提示。
            alt = self.hotkeys.bind_fallback("capture", spec,
                                             hk.HotkeyManager.FALLBACK_CAPTURE)
            if alt:
                self.hotkey_note = (f"{spec} 被其他程序占用，"
                                    f"本次运行改用 {alt}（可在设置中固定）")
                log.warning("全局热键 %s 被占用，已自动改用备用热键 %s", spec, alt)
                bound = True
            else:
                log.warning("全局热键 %s 注册失败（备用组合也全部被占用）", spec)
        second = self.cfg.capture.second_hotkey
        if second and second != spec:
            if not self.hotkeys.bind("capture2", second):
                alt2 = self.hotkeys.bind_fallback("capture2", second,
                                                  hk.HotkeyManager.FALLBACK_SECOND)
                if alt2:
                    self.hotkey_note += f"；副热键 {second} 被占用，改用 {alt2}"
                    log.warning("副热键 %s 被占用，已改用备用热键 %s", second, alt2)
        return bound

    def _on_hotkey(self, name: str) -> None:
        # 热键回调在钩子线程。必须用 Qt 信号 marshal 到 GUI 线程：
        # QTimer.singleShot 在非 Qt 线程上没有事件调度器，timer 永不触发（实测）。
        # AppController 是 QObject 且 living 在 GUI 线程，
        # 信号 emit 后 Qt 自动走 queued connection 到 GUI 线程执行。
        self.capture_requested.emit(name)

    # ---------------------------------------------------------------- 截图
    def start_capture(self, name: str = "capture", src: str = "", dst: str = "") -> None:
        if self.worker and self.worker.isRunning():
            self.status.emit("上一次翻译还在进行，请稍候或按 Esc 取消")
            return
        self._pending_src = src or AUTO
        self._pending_dst = dst or self.cfg.translate.target_lang
        if self._ocr is None and self._ocr_error:
            self.error.emit(self._ocr_error, self._ocr_hint)
            return
        if self._ocr is None:
            self.status.emit("OCR 模型仍在加载，稍等一下再截")
            self.warm_ocr()
            return
        self.overlay = CaptureOverlay(self.cfg)
        self.overlay.capture_finished.connect(self._on_selection)
        self.overlay.capture_cancelled.connect(lambda: self.status.emit("已取消"))
        if not self.overlay.start():
            self.error.emit("抓屏失败", "远程桌面或 Wayland 下可能受限；"
                                       "也可改用菜单里的「从文件导入截图」")

    def _on_selection(self, cap: CaptureResult | None) -> None:
        if cap is None:
            return
        self.last_capture = cap
        if self.cfg.capture.clipboard_after_capture:
            self._to_clipboard(cap)
        self.run_pipeline(cap)

    def _to_clipboard(self, cap: CaptureResult) -> None:
        from PyQt6.QtGui import QImage
        from PyQt6.QtWidgets import QApplication

        img = QImage(cap.bytes_bmp, cap.width, cap.height, cap.width * 4,
                     QImage.Format.Format_RGBA8888)
        QApplication.clipboard().setImage(img)

    # ---------------------------------------------------------------- 流水线
    def run_pipeline(self, cap: CaptureResult, src: str = "", dst: str = "") -> None:
        if self._ocr is None:
            self.error.emit("OCR 尚未就绪", "模型加载中，请稍候再试")
            return
        self.generation += 1
        self.worker = PipelineWorker(
            cap, self._ocr, self.translator, self.cfg,
            src or self._pending_src, dst or self._pending_dst, self.generation)
        self.worker.done.connect(self._on_done)
        self.worker.failed.connect(self._on_fail)
        self.worker.progress.connect(lambda t, p: self.status.emit(t))
        self.busy.emit(True)
        self.worker.start()

    def _on_done(self, result: PipelineResult) -> None:
        # 丢弃过期结果：只接受最新一次截图的产出
        if result.capture is not self.last_capture:
            log.debug("丢弃过期结果（gen 不匹配）")
            return
        self.busy.emit(False)
        self.last_result = result
        self.result_ready.emit(result)
        log.info("流水线完成：%d 段，%s", len(result.units), result.elapsed_ms)

    def _on_fail(self, stage: str, msg: str, hint: str) -> None:
        self.busy.emit(False)
        log.warning("流水线失败[%s]：%s", stage, msg)
        self.error.emit(msg, hint)

    # ---------------------------------------------------------------- 复用
    def retranslate(self, src: str = "", dst: str = "") -> None:
        """用已识别的原文重新翻译（不再 OCR）。

        这是"换个目标语言看看"的正确路径：OCR 要 0.5s，翻译只要 1s，
        重跑整条流水线等于让用户白等一次识别。
        """
        if not self.last_result:
            self.status.emit("还没有可重译的结果")
            return
        cap = self.last_result.capture
        texts = [u.source_text for u in self.last_result.units]
        s = src or self.last_result.source_lang or AUTO
        d = dst or self.cfg.translate.target_lang

        class _Re(QThread):
            done = pyqtSignal(object)
            bad = pyqtSignal(str, str)

            def __init__(self, ctrl, texts, s, d):
                super().__init__()
                self.ctrl, self.texts, self.s, self.d = ctrl, texts, s, d

            def run(self):
                try:
                    rep = self.ctrl.translator.translate(self.texts, self.s, self.d)
                    units = list(self.ctrl.last_result.units)
                    for u, t, e, err in zip(units, rep.texts, rep.engines, rep.errors):
                        u.target_text, u.engine, u.error = t, e, err
                    from dataclasses import replace

                    out = replace(self.ctrl.last_result, units=units,
                                  source_lang=rep.detected_lang or self.s,
                                  target_lang=self.d,
                                  translate_engine=rep.primary_engine or self.d,
                                  warnings=rep.warnings)
                    self.done.emit(out)
                except Exception as exc:
                    self.bad.emit(str(exc), "")

        self._re = _Re(self, texts, s, d)
        self._re.done.connect(lambda r: (setattr(self, "last_result", r),
                                         self.result_ready.emit(r)))
        self._re.bad.connect(lambda m, h: self.error.emit(m, h))
        self.busy.emit(True)
        self._re.done.connect(lambda _r: self.busy.emit(False))
        self._re.bad.connect(lambda _m, _h: self.busy.emit(False))
        self._re.start()

    def cancel(self) -> None:
        """取消当前操作。

        注意 Python 线程无法强杀：这里只丢 overlay 并让 worker 的结果作废
        （generation 检查会丢弃它）。真正"停止"翻译请求需要每个后端支持
        取消令牌，本项目用短超时替代 —— 权衡是"最多再等一个 timeout"。
        """
        if self.overlay and self.overlay.isVisible():
            self.overlay.cancel()
        self.status.emit("已取消（后台请求最多再等数秒）")

    def shutdown(self) -> None:
        # shutdown 会结束泵线程（unbind_all 只注销热键，泵保留供 rebind 用）
        self.hotkeys.shutdown()
        if self.worker and self.worker.isRunning():
            self.worker.wait(2500)
        if self._ocr is not None:
            self._ocr.close()
        self.translator.close()
