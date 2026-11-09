"""系统托盘 + 主窗口外壳。

为什么需要托盘而不是普通主窗口：这个工具的"主窗口"就是截图覆盖层，
常驻一个空的主窗口既占任务栏又没意义。托盘提供三件事：
  1. 一个可点的入口（不依赖热键 —— 热键可能被别的软件占用）；
  2. 状态可见（OCR 是否就绪、热键是否注册成功、网络是否降级）；
  3. 退出路径（否则用户只能任务管理器杀进程）。

图标全部用 QPainter 画，不带任何 .ico/.png 资源文件：
  * PyInstaller 打包资源路径是长期痛点（sys._MEIPASS vs __file__），
    零资源文件直接消除这一类 bug；
  * 尺寸自适应（托盘 16px、菜单 32px、通知 64px）由同一函数生成。
"""

from __future__ import annotations

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget

from ..config import AppConfig
from ..logger import get_logger

log = get_logger("ui.tray")


def make_icon(size: int = 32, state: str = "idle", glyph: str = "译") -> QIcon:
    """程序化生成图标。

    state: idle（蓝）/ busy（橙，正在识别）/ off（灰，OCR 未就绪）/
           warn（红，热键或网络异常）
    """
    colors = {"idle": ("#1a73e8", "#ffffff"), "busy": ("#f0a742", "#1f2328"),
              "off": ("#8b9099", "#ffffff"), "warn": ("#c5221f", "#ffffff")}
    bg, fg = colors.get(state, colors["idle"])
    pm = QPixmap(size, size)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(bg))
    p.setPen(Qt.PenStyle.NoPen)
    r = size * 0.08
    p.drawRoundedRect(1, 1, size - 2, size - 2, r, r)
    f = QFont("Microsoft YaHei", int(size * 0.46), QFont.Weight.Bold)
    p.setFont(f)
    p.setPen(QColor(fg))
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, glyph)
    p.end()
    return QIcon(pm)


class Tray(QObject):
    """托盘与它承载的菜单动作。"""

    capture = pyqtSignal()
    capture_to_file = pyqtSignal()
    open_hub = pyqtSignal()
    show_last = pyqtSignal()
    open_settings = pyqtSignal()
    open_logs = pyqtSignal()
    quit_app = pyqtSignal()

    def __init__(self, cfg: AppConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._icons = {s: make_icon(32, s) for s in ("idle", "busy", "off", "warn")}
        self.tray = QSystemTrayIcon(self._icons["idle"])
        self.tray.setToolTip("截图翻译\n按 Ctrl+Shift+A 框选")
        self.menu = QMenu()
        self._build_menu()
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._activated)
        self._supported = QSystemTrayIcon.isSystemTrayAvailable()
        if not self._supported:
            log.warning("当前桌面不支持托盘（Linux 无 SNI 扩展时常见），"
                        "改用普通窗口 + 应用内快捷键")

    def _build_menu(self) -> None:
        m = self.menu
        self.act_capture = m.addAction("截图翻译\tCtrl+Shift+A", self.capture.emit)
        self.act_capture.setIcon(make_icon(16, "idle", "截"))
        self.act_hub = m.addAction("主窗口…\tCtrl+H", self.open_hub.emit)
        self.act_hub.setIcon(make_icon(16, "idle", "窗"))
        m.addAction("翻译图片文件…", self.capture_to_file.emit)
        m.addSeparator()
        self.act_last = m.addAction("显示上次结果", self.show_last.emit)
        m.addAction("设置…\tCtrl+,", self.open_settings.emit)
        m.addAction("打开日志", self.open_logs.emit)
        m.addSeparator()
        self.status_action = m.addAction("状态：就绪")
        self.status_action.setEnabled(False)
        m.addSeparator()
        m.addAction("退出", self.quit_app.emit)

    def _activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.capture.emit()          # 单击托盘 = 直接截图，最常用
        elif reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_last.emit()

    # ---------------------------------------------------------------- 状态
    def set_state(self, state: str, text: str = "") -> None:
        if not self._supported:
            return
        self.tray.setIcon(self._icons.get(state, self._icons["idle"]))
        tip = {"idle": "截图翻译 · 就绪", "busy": "截图翻译 · 处理中…",
               "off": "截图翻译 · OCR 未就绪",
               "warn": "截图翻译 · 有问题"}[state]
        hk = self.cfg.capture.hotkey.upper().replace("+", " + ")
        self.tray.setToolTip(f"{tip}\n热键 {hk}" + (f"\n{text}" if text else ""))
        self.status_action.setText(f"状态：{tip.split(' · ')[-1]}"
                                   + (f" — {text[:40]}" if text else ""))

    def notify(self, title: str, body: str, state: str = "idle") -> None:
        """系统通知。

        截图工具的典型用法：OCR 在后台跑完，用户已经切回原程序看内容了，
        这时候通知比抢焦点弹窗更合适。
        """
        if not self._supported:
            log.info("通知（无托盘）：%s %s", title, body)
            return
        try:
            self.tray.showMessage(title, body, self._icons.get(state, self._icons["idle"]),
                                  4000)
        except Exception as exc:
            log.debug("通知失败：%s", exc)

    def show(self) -> None:
        if self._supported and self.cfg.ui.tray_enabled:
            self.tray.show()

    def hide(self) -> None:
        self.tray.hide()

    @property
    def supported(self) -> bool:
        return self._supported


class HiddenHost(QWidget):
    """无主窗口的 Qt 需要一个不可见的 QWidget 作为窗口父级与生命周期锚点。

    不能省：QMenu/QDialog 没有 parent 时会在应用退出前留悬挂引用；
    同时它也是"托盘不可用时"的降级载体（Linux 精简桌面）。
    """

    def __init__(self, cfg: AppConfig):
        super().__init__()
        self.cfg = cfg
        self.setWindowFlags(Qt.WindowType.Tool)
        self.resize(1, 1)
        self.move(-32000, -32000)

    def show_fallback(self) -> None:
        """托盘不可用时显示一个真的小窗口，否则用户找不到退出入口。"""
        from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton
        self.setLayout(QHBoxLayout())
        self.layout().addWidget(QLabel("截图翻译（当前桌面不支持托盘）"))
        b = QPushButton("退出")
        b.clicked.connect(lambda: QApplication.instance().quit())
        self.layout().addWidget(b)
        self.resize(320, 48)
        self.move(100, 100)
        self.show()
