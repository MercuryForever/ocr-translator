"""独立提示窗口：ToastWindow（应用内气泡）+ HubWindow（主窗口）。

为什么托盘通知之外还要这两个窗口（实测痛点）：
  * 托盘气泡走 Windows 通知中心 —— Win11 开了专注模式、或应用通知被关，
    showMessage 直接被吞，用户只觉得"程序没反应"；
  * Toast 是纯 Qt 窗口，不经过通知中心，任何系统状态都弹得出来；
  * Hub 是常规顶层窗口：任务栏 / Alt-Tab 可见可切，状态与提示有处可查，
    程序不再是"只在托盘里"。

⚠ HubWindow 必须用 Qt.WindowType.Window 旗标（windowType==1），不能用 QDialog：
  capture.grab.hide_app_windows 抓屏前只隐藏 windowType==Window 的顶层窗口
  （Tool=2 / Dialog=3 都被跳过）—— 用 QDialog 旗标的话主窗口在截图时
  不会被隐藏，主窗口内容会被截进 OCR 输入（与结果窗当初的 bug 同源）。
"""

from __future__ import annotations

from datetime import datetime

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (QApplication, QGridLayout, QHBoxLayout, QLabel,
                             QListWidget, QListWidgetItem, QPushButton,
                             QVBoxLayout, QWidget)

from ..config import AppConfig
from ..logger import get_logger
from .theme import build_qss, resolve

log = get_logger("ui.hub")

_LOG_CAP = 80          # 提示日志上限，超出丢最旧的
_TOAST_MS = 4500       # 气泡停留时长


class ToastWindow(QWidget):
    """应用内气泡提示：无边框、置顶、不抢焦点、自动消失、点击即关。

    位置固定在主屏右下角（任务栏之上）。同一时刻只保留一条：
    新提示直接覆盖旧内容并重置计时 —— 消息队列式轮播反而让人读不完。
    """

    def __init__(self, cfg: AppConfig, parent=None):
        super().__init__(parent, Qt.WindowType.Tool
                         | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint)
        self.setObjectName("toast")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._pal = resolve(cfg.ui.theme)
        self._accent = self._pal.accent

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 11, 16, 11)
        lay.setSpacing(4)
        self.lb_title = QLabel("")
        self.lb_title.setObjectName("toastTitle")
        self.lb_body = QLabel("")
        self.lb_body.setObjectName("toastBody")
        self.lb_body.setWordWrap(True)
        lay.addWidget(self.lb_title)
        lay.addWidget(self.lb_body)
        self.setFixedWidth(360)

        self._timer = QTimer(self, singleShot=True, interval=_TOAST_MS)
        self._timer.timeout.connect(self.hide)
        self._restyle()
        self.hide()

    def _restyle(self) -> None:
        p = self._pal
        # state → 左侧色条：idle 蓝 / busy 橙 / warn·error 红 / ok 绿
        self.setStyleSheet(
            f"#toast {{ background: {p.bg_alt}; border: 1px solid {p.border};"
            f"        border-left: 4px solid {self._accent}; border-radius: 8px; }}\n"
            f"QLabel#toastTitle {{ color: {p.fg}; font-weight: 600; background: transparent; }}\n"
            f"QLabel#toastBody  {{ color: {p.fg_dim}; background: transparent; }}")

    def show_message(self, title: str, body: str = "", state: str = "idle") -> None:
        colors = {"idle": self._pal.accent, "busy": self._pal.warn,
                  "warn": self._pal.error, "error": self._pal.error,
                  "ok": self._pal.ok}
        self._accent = colors.get(state, self._pal.accent)
        self.lb_title.setText(title)
        self.lb_body.setText(body)
        self.lb_body.setVisible(bool(body))
        self._restyle()
        self.adjustSize()
        scr = QApplication.primaryScreen()
        geo = scr.availableGeometry() if scr else self.screen().availableGeometry()
        self.move(geo.right() - self.width() - 18,
                  geo.bottom() - self.height() - 18)
        self.show()
        self.raise_()
        self._timer.start()

    def mousePressEvent(self, _e) -> None:  # noqa: N802
        self.hide()


class HubWindow(QWidget):
    """主窗口：状态一览 + 提示日志 + 常用操作。

    关闭按钮 = 隐藏到托盘（不退出）；退出走托盘菜单或 Ctrl+Q。
    """

    capture_requested = pyqtSignal()
    show_last = pyqtSignal()
    open_settings = pyqtSignal()
    open_logs = pyqtSignal()
    #: 托盘模式下首次点 ×（窗口隐藏而非退出）时发出 —— main 接 toast 提醒
    #: "程序仍在运行、托盘可退出"，避免用户以为退了、后台进程还在。
    minimized = pyqtSignal()

    def __init__(self, cfg: AppConfig, parent=None, exits_on_close: bool = False):
        super().__init__(parent, Qt.WindowType.Window)   # 见模块 docstring：不能用 QDialog
        self.setObjectName("hub")
        self.setWindowTitle("截图翻译 · 主窗口")
        self.resize(460, 420)
        # 没有托盘可回（--no-tray / 环境不支持）时，× 就是退出；
        # 有托盘时 × = 隐藏到托盘（QQ 语义），首次隐藏时弹气泡告知。
        self._exits_on_close = exits_on_close
        self._minimize_notified = False
        self._pal = resolve(cfg.ui.theme)
        self._apply_qss(cfg.ui.font_size, cfg.ui.mono_font)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)
        self.rows: dict[str, QLabel] = {}
        for i, key in enumerate(("热键", "OCR", "翻译链", "上次结果")):
            k = QLabel(key)
            k.setObjectName("hubKey")
            v = QLabel("—")
            v.setObjectName("hubVal")
            grid.addWidget(k, i, 0)
            grid.addWidget(v, i, 1)
            self.rows[key] = v
        grid.setColumnStretch(1, 1)
        root.addLayout(grid)

        self.log_list = QListWidget()
        self.log_list.setObjectName("hubLog")
        self.log_list.setAlternatingRowColors(False)
        self.log_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        root.addWidget(self.log_list, 1)

        btns = QHBoxLayout()
        self.btn_capture = QPushButton("截图翻译  Ctrl+Shift+A")
        self.btn_capture.setObjectName("primary")
        self.btn_capture.clicked.connect(self.capture_requested.emit)
        btn_last = QPushButton("显示上次结果")
        btn_last.clicked.connect(self.show_last.emit)
        btn_set = QPushButton("设置…")
        btn_set.clicked.connect(self.open_settings.emit)
        btn_log = QPushButton("打开日志")
        btn_log.clicked.connect(self.open_logs.emit)
        btn_hide = QPushButton("隐藏")
        btn_hide.setToolTip("隐藏到系统托盘；托盘菜单「主窗口」或 Ctrl+H 可再打开")
        btn_hide.clicked.connect(self.hide)
        btn_quit = QPushButton("退出")
        self.btn_quit = btn_quit
        btn_quit.setToolTip("完全退出程序（等同托盘菜单「退出」/ Ctrl+Q）")
        btn_quit.clicked.connect(lambda: QApplication.instance().quit())
        for b in (self.btn_capture, btn_last, btn_set, btn_log, btn_hide, btn_quit):
            btns.addWidget(b)
        root.addLayout(btns)

    # ---------------------------------------------------------------- 外观
    def _apply_qss(self, font_size: int, mono: str) -> None:
        p = self._pal
        # build_qss 的背景规则只覆盖 QMainWindow/QDialog；这里是 QWidget，
        # 用 objectName 规则补上，否则主窗口是系统默认浅色底。
        self.setStyleSheet(
            build_qss(p, font_size, mono)
            + f"\n#hub {{ background: {p.bg}; }}"
            + f"\nQLabel#hubKey {{ color: {p.fg_dim}; }}"
            + f"\nQLabel#hubVal {{ color: {p.fg}; }}"
            + f"\nQLabel#hubValOk {{ color: {p.ok}; }}"
            + f"\nQLabel#hubValBad {{ color: {p.error}; }}"
            + f"\nQListWidget#hubLog {{ background: {p.bg_alt}; border: 1px solid {p.border};"
              f" border-radius: 6px; }}"
            + f"\nQListWidget#hubLog::item {{ padding: 2px 6px; }}")

    def set_row(self, key: str, text: str, level: str = "info") -> None:
        """更新状态行。level: info / ok / error（决定文字颜色）。"""
        lb = self.rows.get(key)
        if lb is None:
            return
        lb.setText(text)
        lb.setObjectName({"ok": "hubValOk", "error": "hubValBad"}.get(level, "hubVal"))
        # objectName 变了必须显式 repolish，否则颜色不刷新
        self.style().unpolish(lb)
        self.style().polish(lb)

    # ---------------------------------------------------------------- 日志
    def add_log(self, text: str, level: str = "info") -> None:
        item = QListWidgetItem(f"{datetime.now():%H:%M:%S}  {text}")
        color = {"error": self._pal.error, "warn": self._pal.warn,
                 "ok": self._pal.ok}.get(level, self._pal.fg_dim)
        item.setForeground(QColor(color))
        self.log_list.addItem(item)
        while self.log_list.count() > _LOG_CAP:
            self.log_list.takeItem(0)
        self.log_list.scrollToBottom()

    # ---------------------------------------------------------------- 行为
    def set_busy(self, busy: bool) -> None:
        self.btn_capture.setEnabled(not busy)
        if busy:
            self.add_log("处理中…")

    def show_and_raise(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, e) -> None:  # noqa: N802
        if self._exits_on_close:
            # 无托盘环境：× 就是退出（否则窗口藏起来、进程还在，
            # 用户看到的就是"后台进程退出不彻底"）。
            e.accept()
            QApplication.instance().quit()
            return
        # 托盘模式：关闭 = 隐藏到托盘。托盘常驻应用点 × 直接退进程是反直觉
        # 设计，退出走托盘菜单 / Ctrl+Q / 主窗口「退出」按钮。
        e.ignore()
        self.hide()
        log.info("主窗口隐藏到托盘（程序仍在运行）")
        if not self._minimize_notified:
            self._minimize_notified = True
            self.minimized.emit()
