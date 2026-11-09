"""双语对照结果窗口。

三种布局（`ui.layout_mode`）：
    side_by_side  左右两列：长文对照最直观，列宽可拖
    stacked       上下堆叠：窄窗、竖排截图、手机式速读
    interleaved   逐段交替：一段原文紧跟一段译文，适合逐句精读

为什么用"一个滚动区 + 逐行 QGridLayout"而不是两个 QListWidget 联动：
  两栏各自滚动时，只要行高有任何差异（译文换行数不同）就会**逐渐错位**，
  用户会以为"翻译对应错了行"。单滚动容器 + 每行一个卡片，
  原文与译文被同一个布局单元绑定，永远对齐。

富文本而不是 QTableView：需要置信度色条、代码块等宽、失败行红标、
悬停联动高亮，delegate 方案代码量三倍且行高管理更别扭；
本项目行数在几十到几百，性能不是瓶颈。

线程约定：本窗口只在 GUI 线程构造。Worker 通过信号送 PipelineResult，
数据类是纯 Python（见 models.py 的设计说明），跨线程传递安全。
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QFont, QGuiApplication, QKeySequence
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QMainWindow, QMenu,
                             QProgressBar, QPushButton, QScrollArea, QSizePolicy,
                             QToolButton, QVBoxLayout, QWidget)

from ..languages import AUTO, get as get_lang
from ..logger import get_logger
from ..models import BlockKind, PipelineResult, TranslationUnit
from .theme import Palette, build_qss, resolve

log = get_logger("ui.result")

KIND_ICON = {BlockKind.HEADING: "H", BlockKind.CODE: "</>",
             BlockKind.TABLE: "▦", BlockKind.TEXT: "¶"}
KIND_TIP = {BlockKind.HEADING: "标题（字号明显大于正文）",
            BlockKind.CODE: "代码/命令（建议直接复制原文）",
            BlockKind.TABLE: "表格行", BlockKind.TEXT: "正文段落"}


# ---------------------------------------------------------------- 单个对照卡片
class PairCard(QFrame):
    """一段原文 + 一段译文。悬停时同步高亮，点击可复制该条译文。"""

    hovered = pyqtSignal(int, bool)
    copy_requested = pyqtSignal(str)

    def __init__(self, index: int, unit: TranslationUnit, pal: Palette,
                 layout_mode: str, show_conf: bool, parent=None):
        super().__init__(parent)
        self.index = index
        self.unit = unit
        self.pal = pal
        self.mode = layout_mode
        self._hover = False
        self.setMouseTracking(True)
        self.setObjectName("card")
        self.setProperty("state", "err" if unit.error else "ok")
        self._build(show_conf)

    # ---------------- 构造
    def _build(self, show_conf: bool) -> None:
        """结构：顶栏（徽标 + 置信度 + 行内操作按钮）+ 正文。

        操作按钮刻意放在**顶栏右侧**而不是卡片底部：
        底部竖排两个小按钮会把卡片撑高、视觉重心下坠（第一版就是这样，
        渲染出来是左下角两坨）。放顶栏与徽标同行，行高不额外增加。
        """
        u = self.unit
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 7, 10, 8)
        outer.setSpacing(6)
        outer.addWidget(self._header(show_conf))

        src = self._label(u.source_text, "source", selectable=True)
        if u.error:
            tgt = self._label(f"⚠ 翻译失败：{u.error}", "err", selectable=False)
        elif not u.target_text:
            tgt = self._label("（无译文）", "dim", selectable=False)
        else:
            tgt = self._label(u.target_text, "target", selectable=True)

        if self.mode == "side_by_side":
            body = QHBoxLayout(); body.setSpacing(10)
            body.addWidget(self._column(src, self.pal.source), 1)
            body.addWidget(self._column(tgt, self.pal.target if not u.error
                                        else self.pal.error), 1)
            outer.addLayout(body)
        else:
            outer.addWidget(self._column(src, self.pal.source))
            outer.addWidget(self._column(tgt, self.pal.target if not u.error
                                         else self.pal.error))

    def _column(self, widget, color: str):
        """带左侧色条的一列（原文/译文共用），保证两列视觉对称。"""
        w = QWidget(); h = QHBoxLayout(w); h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(7)
        h.addWidget(self._stripe(color))
        h.addWidget(widget, 1)
        return w

    def _header(self, show_conf: bool):
        wrap = QWidget(); h = QHBoxLayout(wrap); h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        kind = QLabel(KIND_ICON.get(self.unit.block.kind, "·"))
        kind.setObjectName("mono")
        kind.setToolTip(KIND_TIP.get(self.unit.block.kind, ""))
        kind.setStyleSheet(
            f"background:{self.pal.bg_hover};color:{self.pal.fg_dim};"
            f"border-radius:4px;padding:0 5px;font-weight:600;")
        h.addWidget(kind)
        num = QLabel(f"#{self.index + 1}")
        num.setStyleSheet(f"color:{self.pal.fg_dim};")
        h.addWidget(num)
        if show_conf:
            conf = self.unit.block.avg_conf
            col = self.pal.ok if conf >= 0.85 else (self.pal.warn if conf >= 0.6
                                                    else self.pal.error)
            c = QLabel(f"{conf * 100:.0f}%")
            c.setToolTip("该行 OCR 平均置信度。低于 60% 建议重新框选或放大截图")
            c.setStyleSheet(f"color:{col};font-weight:600;")
            h.addWidget(c)
        if len(self.unit.block.lines) > 1:
            m = QLabel(f"{len(self.unit.block.lines)} 行")
            m.setObjectName("badge")
            m.setToolTip("该段由多行 OCR 结果合并而成（断行已按 CJK/拉丁规则拼接）")
            h.addWidget(m)
        h.addStretch()
        h.addLayout(self._tools())
        return wrap

    def _label(self, text: str, role: str, selectable: bool):
        lab = QLabel(text)
        lab.setWordWrap(True)
        lab.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
            if selectable else Qt.TextInteractionFlag.NoTextInteraction)
        if role == "source":
            lab.setStyleSheet(f"color:{self.pal.source};font-size:13px;")
        elif role == "target":
            lab.setStyleSheet(f"color:{self.pal.target};font-size:13px;font-weight:500;")
        elif role == "err":
            lab.setStyleSheet(f"color:{self.pal.error};font-style:italic;font-size:12px;")
        else:
            lab.setStyleSheet(f"color:{self.pal.fg_dim};font-size:12px;")
        if self.unit.block.kind == BlockKind.CODE:
            f = QFont("Consolas")
            f.setStyleHint(QFont.StyleHint.Monospace)
            lab.setFont(f)
        return lab

    def _stripe(self, color: str):
        f = QFrame()
        f.setFixedWidth(3)
        f.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        f.setStyleSheet(f"background:{color};border-radius:1px;")
        return f

    def _tools(self):
        """顶栏右侧的行内操作：复制译文 / 复制原文。"""
        h = QHBoxLayout(); h.setSpacing(3)
        has_tgt = bool(self.unit.target_text and not self.unit.error)
        for name, tip, payload, enabled in (
                ("复制", "复制本条译文", self.unit.target_text, has_tgt),
                ("原文", "复制本条原文（也可 Ctrl+点击正文）",
                 self.unit.source_text, True)):
            b = QPushButton(name)
            b.setObjectName("flat")
            b.setToolTip(tip)
            b.setEnabled(enabled)
            b.setFixedSize(40, 18)
            b.setStyleSheet(
                f"QPushButton{{font-size:11px;padding:0;border-radius:4px;"
                f"color:{self.pal.fg_dim};}}"
                f"QPushButton:hover{{background:{self.pal.bg_hover};"
                f"color:{self.pal.accent};}}")
            b.clicked.connect(lambda _=False, p=payload: self.copy_requested.emit(p))
            h.addWidget(b)
        return h

    # ---------------- 交互
    def enterEvent(self, e) -> None:  # noqa: N802
        self._hover = True
        self._repaint()
        self.hovered.emit(self.index, True)
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:  # noqa: N802
        self._hover = False
        self._repaint()
        self.hovered.emit(self.index, False)
        super().leaveEvent(e)

    def mousePressEvent(self, e) -> None:  # noqa: N802
        # Ctrl+点击复制原文：选中文本复制太绕，看图时常用
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.copy_requested.emit(self.unit.source_text)
            e.accept()
            return
        super().mousePressEvent(e)

    def _repaint(self) -> None:
        border = self.pal.accent if self._hover else self.pal.border
        bg = self.pal.bg_hover if self._hover else (
            self.pal.bg_alt if not self.unit.error else self.pal.bg)
        self.setStyleSheet(
            f"QFrame#card{{background:{bg};border:1px solid {border};"
            f"border-radius:8px;}}")
        if self.unit.error and not self._hover:
            self.setStyleSheet(
                f"QFrame#card{{background:{bg};border:1px solid {self.pal.error};"
                f"border-left:3px solid {self.pal.error};border-radius:8px;}}")


# ---------------------------------------------------------------- 主窗口
class ResultWindow(QMainWindow):
    """双语对照结果窗口。"""

    recapture_requested = pyqtSignal()
    retranslate_requested = pyqtSignal()
    export_requested = pyqtSignal(str)          # 格式名
    closed_result = pyqtSignal()

    def __init__(self, cfg, result: PipelineResult | None = None):
        super().__init__()
        self.cfg = cfg
        self.result = result
        self.pal = resolve(cfg.ui.theme)
        self.cards: list[PairCard] = []
        # 逐段模式下每段的两块（原文卡 + 译文卡），用于悬停联动
        self.halves: dict[int, list[QFrame]] = {}
        self.setObjectName("ResultWindow")
        self.setWindowTitle("截图翻译 · 双语对照")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setMinimumSize(620, 420)
        self._build_chrome()
        if result:
            self.set_result(result)
        else:
            self._empty_hint()
        self._apply_theme()
        if cfg.ui.remember_result_window and cfg.ui.result_window_geometry:
            self._restore_geometry(cfg.ui.result_window_geometry)
        if cfg.ui.always_on_top:
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
            self.show()

    # ---------------- 结构
    def _build_chrome(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 8, 10, 6)
        root.setSpacing(6)

        # 顶栏：标题 + 语向 + 布局切换 + 操作
        top = QHBoxLayout(); top.setSpacing(8)
        self.title = QLabel("双语对照")
        self.title.setObjectName("title")
        top.addWidget(self.title)
        self.meta = QLabel("")
        self.meta.setObjectName("dim")
        top.addWidget(self.meta)
        top.addStretch()

        self.layout_group = QHBoxLayout(); self.layout_group.setSpacing(2)
        for mode, label in (("side_by_side", "左右"), ("stacked", "上下"),
                            ("interleaved", "逐段")):
            b = QPushButton(label)
            b.setObjectName("flat")
            b.setCheckable(True)
            b.setToolTip(f"{label}布局")
            b.clicked.connect(lambda _=False, m=mode: self.set_layout(m))
            self.layout_group.addWidget(b)
            if mode == self.cfg.ui.layout_mode:
                b.setChecked(True)
        top.addLayout(self.layout_group)

        self.btn_copy = QPushButton("复制全部")
        self.btn_copy.setToolTip("复制原文+译文（Ctrl+Shift+C）")
        self.btn_copy.clicked.connect(lambda: self._copy_text(self._plain()))
        top.addWidget(self.btn_copy)

        # QToolButton 而不是 QPushButton：只有它支持 setMenu + InstantPopup
        # （QPushButton 要 popup 得自己装 eventFilter，那是重复造轮子）
        self.btn_export = QToolButton()
        self.btn_export.setText("导出")
        self.btn_export.setToolTip("选择格式导出双语对照结果")
        self.btn_export.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.btn_export.setMenu(self._export_menu())
        self.btn_export.setStyleSheet(
            f"QToolButton{{background:{self.pal.accent};color:#fff;border:none;"
            f"border-radius:6px;padding:5px 12px;font-weight:600;}}"
            f"QToolButton::menu-button{{border-left:1px solid rgba(255,255,255,.45);"
            f"width:16px;}}")
        top.addWidget(self.btn_export)
        root.addLayout(top)

        # 进度/状态条（Worker 期间显示）
        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        self.progress.setFixedHeight(16)
        root.addWidget(self.progress)

        self.status_line = QLabel(" ")
        self.status_line.setObjectName("dim")
        root.addWidget(self.status_line)

        # 滚动容器
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.host = QWidget()
        self.host_lay = QVBoxLayout(self.host)
        self.host_lay.setContentsMargins(2, 2, 6, 2)
        self.host_lay.setSpacing(7)
        self.host_lay.addStretch()
        self.scroll.setWidget(self.host)
        root.addWidget(self.scroll, 1)

        self.setCentralWidget(central)
        self._actions()

    def _export_menu(self) -> QMenu:
        m = QMenu(self)
        for fmt, tip in (("txt", "纯文本双语对照"), ("md", "Markdown 表格"),
                         ("docx", "Word 表格（需 python-docx，缺则自动转 RTF）"),
                         ("csv", "Excel 可直接打开（UTF-8 BOM）"),
                         ("html", "带样式的网页，可打印成 PDF"),
                         ("json", "机器可读：含坐标与置信度")):
            a = m.addAction(f"导出 {fmt.upper()}")
            a.setToolTip(tip)
            a.triggered.connect(lambda _=False, f=fmt: self.export_requested.emit(f))
        m.addSeparator()
        m.addAction("保存到上次目录…").triggered.connect(
            lambda: self.export_requested.emit("last"))
        return m

    def _actions(self) -> None:
        def act(text, shortcut, slot, tip=""):
            a = QAction(text, self)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            a.triggered.connect(slot)
            if tip:
                a.setToolTip(tip)
            return a

        am = self.menuBar().addMenu("操作(&A)")
        am.addAction(act("再次截图", "Ctrl+Shift+A",
                         self.recapture_requested.emit, "隐藏本窗口并重新框选"))
        am.addAction(act("重新翻译", "Ctrl+R", self.retranslate_requested.emit,
                         "用当前语言对重新翻译（不重新 OCR）"))
        am.addSeparator()
        am.addAction(act("复制全部译文", "Ctrl+C",
                         lambda: self._copy_text(self._targets_only())))
        am.addAction(act("复制双语对照", "Ctrl+Shift+C",
                         lambda: self._copy_text(self._plain())))
        am.addAction(act("复制为 Markdown 表格", None,
                         lambda: self._copy_text(self._markdown())))
        am.addSeparator()
        am.addAction(act("关闭", "Esc", self.close))

        vm = self.menuBar().addMenu("视图(&V)")
        self.act_conf = act("显示置信度", None, self._toggle_conf,
                            "每段右侧显示 OCR 平均置信度")
        self.act_conf.setCheckable(True)
        self.act_conf.setChecked(self.cfg.ui.show_confidence)
        vm.addAction(self.act_conf)
        self.act_raw = act("显示原始行", None, self._toggle_raw,
                           "展开该段包含的每一条 OCR 行（调试版面对有用）")
        self.act_raw.setCheckable(True)
        self.act_raw.setChecked(self.cfg.ui.show_raw_lines)
        vm.addAction(self.act_raw)
        self.act_top = act("窗口置顶", None, self._toggle_top)
        self.act_top.setCheckable(True)
        self.act_top.setChecked(self.cfg.ui.always_on_top)
        vm.addAction(self.act_top)
        vm.addSeparator()
        for size, label in ((10, "小"), (11, "标准"), (12, "大"), (14, "特大")):
            vm.addAction(act(f"字号 {label}", None,
                             lambda _=False, s=size: self._font_size(s)))

    # ---------------- 数据
    def set_result(self, result: PipelineResult) -> None:
        self.result = result
        self._clear_cards()
        mode = self.cfg.ui.layout_mode
        for i, unit in enumerate(result.units):
            if mode == "interleaved":
                self._add_interleaved(i, unit)
            else:
                card = PairCard(i, unit, self.pal,
                                "side_by_side" if mode == "side_by_side" else "stacked",
                                self.cfg.ui.show_confidence)
                card.copy_requested.connect(lambda t: self._copy_text(t))
                card.hovered.connect(self._on_hover)
                self._insert(card)
                self.cards.append(card)
                if self.cfg.ui.show_raw_lines and len(unit.block.lines) > 1:
                    self._insert(self._raw_block(unit))

        self.title.setText("双语对照")
        n, ok = len(result.units), sum(1 for u in result.units if u.ok)
        ocr_ms = result.elapsed_ms.get("ocr", 0)
        tr_ms = result.elapsed_ms.get("translate", 0)
        det = f"（检测到 {get_lang(result.source_lang).name_zh}）" \
            if result.source_lang and result.source_lang != AUTO else ""
        self.meta.setText(
            f"{result.source_lang or 'auto'} → {result.target_lang}{det} · "
            f"{ok}/{n} 条成功 · OCR {ocr_ms}ms + 翻译 {tr_ms}ms · "
            f"OCR={result.ocr_engine} 翻译={result.translate_engine}")
        warn = " · ".join(result.warnings[:2])
        self.status_line.setText(warn or " ")
        self.status_line.setStyleSheet(
            f"color:{self.pal.warn if warn else self.pal.fg_dim};")
        self.progress.setVisible(False)
        if self.cfg.ui.auto_scroll:
            self.scroll.verticalScrollBar().setValue(0)
        log.info("结果窗口渲染 %d 条", n)

    def _insert(self, widget) -> None:
        """插到 stretch 之前（保持 stretch 恒在最后，内容少时不会顶到上方）。"""
        self.host_lay.insertWidget(self.host_lay.count() - 1, widget)

    def _add_interleaved(self, index: int, unit: TranslationUnit) -> None:
        """逐段模式：原文一段紧跟译文一段，两段各自成卡片。

        与 stacked 的区别不只是"分开还是并排"：
        这里原文/译文是**两个独立卡片**，悬停任意一个会同时点亮另一个
        （`self.halves` 记下同一段的两块）。逐句精读时这个联动是主要价值 ——
        眼睛在长译文里找对应原文很费劲。
        """
        head = QLabel(f"#{index + 1} · "
                      f"{KIND_ICON.get(unit.block.kind, '·')} · "
                      f"{len(unit.block.lines)} 行 · "
                      f"{unit.block.avg_conf * 100:.0f}%")
        head.setObjectName("dim")
        self._insert(head)

        src = self._half(index, unit.source_text, "source", unit)
        tgt_text = (f"⚠ 翻译失败：{unit.error}" if unit.error
                    else (unit.target_text or "（无译文）"))
        tgt = self._half(index, tgt_text, "target", unit,
                         error=bool(unit.error))
        self.halves.setdefault(index, []).extend([src, tgt])
        for w in (src, tgt):
            self._insert(w)

    def _half(self, index: int, text: str, role: str, unit: TranslationUnit,
              error: bool = False):
        w = QFrame()
        w.setObjectName(f"row{role.capitalize()}")
        w.setMouseTracking(True)
        lay = QHBoxLayout(w); lay.setContentsMargins(10, 6, 10, 6); lay.setSpacing(8)
        lab = QLabel(text)
        lab.setWordWrap(True)
        lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        col = (self.pal.error if error else
               (self.pal.source if role == "source" else self.pal.target))
        lab.setStyleSheet(f"color:{col};font-size:13px;"
                         f"{'font-style:italic;' if error else ''}")
        lay.addWidget(lab, 1)
        tag = QLabel("原文" if role == "source" else "译文")
        tag.setObjectName("badge")
        lay.addWidget(tag)
        w.enterEvent = lambda e, i=index: self._pair_hover(i, True)
        w.leaveEvent = lambda e, i=index: self._pair_hover(i, False)
        return w

    def _pair_hover(self, index: int, on: bool) -> None:
        if not self.cfg.ui.highlight_pair:
            return
        for w in self.halves.get(index, []):
            border = self.pal.accent if on else self.pal.border
            bg = self.pal.bg_hover if on else self.pal.bg_alt
            w.setStyleSheet(f"QFrame{{background:{bg};border:1px solid {border};"
                            f"border-radius:6px;}}")

    def _raw_block(self, unit: TranslationUnit):
        from PyQt6.QtWidgets import QFrame as _F
        box = _F()
        box.setObjectName("card")
        v = QVBoxLayout(box); v.setContentsMargins(12, 6, 10, 6); v.setSpacing(1)
        for ln in unit.block.lines:
            col = self.pal.ok if ln.conf >= 0.85 else (
                self.pal.warn if ln.conf >= 0.6 else self.pal.error)
            lab = QLabel(f'<span style="color:{col}">{ln.conf:4.2f}</span>  '
                         f'<span style="color:{self.pal.fg_dim}">'
                         f'{int(ln.box[0])},{int(ln.box[1])}</span>  {ln.text}')
            lab.setTextFormat(Qt.TextFormat.RichText)
            lab.setFont(QFont("Consolas", max(8, self.cfg.ui.font_size - 2)))
            lab.setStyleSheet("background:transparent;border:none;")
            v.addWidget(lab)
        return box

    def _clear_cards(self) -> None:
        for c in self.cards:
            c.deleteLater()
        self.cards.clear()
        self.halves.clear()
        i = 0
        while i < self.host_lay.count() - 1:
            it = self.host_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
            i += 1

    def _empty_hint(self) -> None:
        lab = QLabel("还没有截图。\n按 Ctrl+Shift+A 框选屏幕区域。")
        lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lab.setStyleSheet(f"color:{self.pal.fg_dim};font-size:14px;")
        self.host_lay.insertWidget(0, lab, 1)

    # ---------------- 交互
    def set_layout(self, mode: str) -> None:
        if mode == self.cfg.ui.layout_mode or not self.result:
            return
        self.cfg.ui.layout_mode = mode
        for b, m in zip(self.layout_group.children(), ("side_by_side", "stacked",
                                                       "interleaved")):
            if isinstance(b, QPushButton):
                b.setChecked(m == mode)
        pos = self.scroll.verticalScrollBar().value()
        self.set_result(self.result)          # 重绘；保持滚动位置
        self.scroll.verticalScrollBar().setValue(pos)

    def _on_hover(self, index: int, on: bool) -> None:
        """悬停联动：让另一列同一段也带强调边框（side_by_side 已经同卡片，
        这里给 stacked 模式下"原文/译文分两行"的场景加视觉绑定）。"""
        if not self.cfg.ui.highlight_pair:
            return
        for i, c in enumerate(self.cards):
            c.setProperty("pair", on and i == index)

    def _toggle_conf(self) -> None:
        self.cfg.ui.show_confidence = self.act_conf.isChecked()
        if self.result:
            self.set_result(self.result)

    def _toggle_raw(self) -> None:
        self.cfg.ui.show_raw_lines = self.act_raw.isChecked()
        if self.result:
            self.set_result(self.result)

    def _toggle_top(self) -> None:
        self.cfg.ui.always_on_top = self.act_top.isChecked()
        vis = self.isVisible()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint,
                           self.cfg.ui.always_on_top)
        if vis:
            self.show()

    def _font_size(self, size: int) -> None:
        self.cfg.ui.font_size = size
        self._apply_theme()
        if self.result:
            self.set_result(self.result)

    def _copy_text(self, text: str) -> None:
        if not text:
            self.status_line.setText("没有可复制的内容")
            return
        QGuiApplication.clipboard().setText(text)
        self.status_line.setText(f"已复制到剪贴板（{len(text)} 字符）")
        QTimer.singleShot(2600, lambda: self.status_line.setText(" "))

    def _plain(self) -> str:
        return self.result.plain_bilingual if self.result else ""

    def _targets_only(self) -> str:
        if not self.result:
            return ""
        return "\n".join(u.target_text for u in self.result.units if u.ok)

    def _markdown(self) -> str:
        from ..export import to_markdown
        if not self.result:
            return ""
        return to_markdown(self.result.to_document())

    # ---------------- 主题与几何
    def _apply_theme(self) -> None:
        self.pal = resolve(self.cfg.ui.theme)
        self.setStyleSheet(build_qss(self.pal, self.cfg.ui.font_size,
                                     self.cfg.ui.mono_font))
        for c in self.cards:
            c._repaint()

    def _restore_geometry(self, token: str) -> None:
        try:
            wh, pos = token.split("|")
            w, h = (int(x) for x in wh.split("x"))
            x, y = (int(v) for v in pos.split(","))
            if w >= 400 and h >= 300:
                self.resize(w, h)
            # 位置只有在仍落在某块屏内时才恢复（拔掉副屏后旧坐标在屏幕外）
            if any(s.geometry().contains(x + 40, y + 40)
                   for s in QGuiApplication.screens()):
                self.move(x, y)
        except (ValueError, TypeError):
            log.debug("窗口几何恢复失败：%r", token)

    def geometry_token(self) -> str:
        g = self.geometry()
        return f"{g.width()}x{g.height()}|{g.x()},{g.y()}"

    def set_busy(self, text: str) -> None:
        self.progress.setVisible(True)
        self.status_line.setText(text)
        for b in (self.btn_copy, self.btn_export):
            b.setEnabled(False)

    def clear_busy(self) -> None:
        self.progress.setVisible(False)
        for b in (self.btn_copy, self.btn_export):
            b.setEnabled(True)

    def closeEvent(self, e) -> None:  # noqa: N802
        if self.cfg.ui.remember_result_window:
            self.cfg.ui.result_window_geometry = self.geometry_token()
        self.closed_result.emit()
        super().closeEvent(e)

    # ---------------- 便捷
    def show_error(self, message: str, hint: str = "") -> None:
        """识别/翻译彻底失败时的可见反馈。

        必须可见：只写日志等于"软件没反应"，用户会反复按热键。
        """
        self.progress.setVisible(False)
        self.status_line.setText(f"✗ {message}" + (f"  —  {hint}" if hint else ""))
        self.status_line.setStyleSheet(f"color:{self.pal.error};font-weight:600;")
        log.error("%s | %s", message, hint)
