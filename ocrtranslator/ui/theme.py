"""主题与 QSS。

不用 QPalette 而用字符串样式表，原因：
  * QPalette 在 Qt6 对 QFrame/QGroupBox 边框、QSplitter 手柄这些细节仍不生效，
    暗色下会出现"黑底白框"或"选中项文字看不见"；
  * 需要三档强调色（原文/译文/警告）分别上色，样式表直接写变量最省事；
  * 用户装第三方 Qt 风格（Windows 11 的 qt6 windows11 样式）时，
    显式 QSS 比依赖平台主题的 QPalette 更可控。

配色刻意避开纯黑纯白：截图工具要长时间盯，#000/#fff 的对比度过高会累眼，
且暗色下 OCR 结果窗的灰字（置信度、坐标）必须仍可读，
所以背景用 #1e2024，前景用 #d6d8dc，弱化字用 #8b9099（对比度 ≈ 4.6:1，达 WCAG AA）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Palette:
    name: str
    dark: bool
    bg: str
    bg_alt: str            # 卡片/交替行
    bg_hover: str
    border: str
    fg: str
    fg_dim: str
    accent: str            # 主操作
    accent_2: str          # 次操作/链接
    source: str            # 原文强调
    target: str            # 译文强调
    ok: str
    warn: str
    error: str
    selection: str

    @property
    def qss_vars(self) -> dict[str, str]:
        return {k: str(v) for k, v in self.__dict__.items() if k != "name"}


LIGHT = Palette(
    name="light", dark=False,
    bg="#f7f8fa", bg_alt="#ffffff", bg_hover="#eef1f6",
    border="#d6dae1", fg="#1f2328", fg_dim="#6b7280",
    accent="#1a73e8", accent_2="#0b6bcb",
    source="#374151", target="#0f766e",
    ok="#188038", warn="#b45309", error="#c5221f",
    selection="#d3e3fd",
)

DARK = Palette(
    name="dark", dark=True,
    bg="#1e2024", bg_alt="#26292e", bg_hover="#2f343a",
    border="#3c4148", fg="#d6d8dc", fg_dim="#8b9099",
    accent="#4d9ffd", accent_2="#7cc0ff",
    source="#b6bcc6", target="#5fd6bd",
    ok="#3ddc84", warn="#f0a742", error="#f2685f",
    selection="#2b4c73",
)

_SIZES = {"compact": 11, "normal": 12, "comfortable": 14}


def build_qss(p: Palette, font_size: int = 12, mono: str = "Consolas") -> str:
    """生成全局样式表。

    选择器一律限定到具体控件类型 + objectName，不用裸 `QWidget` 通配：
    裸通配会把样式漏进 QMenu 弹出层、QToolTip、以及第三方 widget，
    表现为"菜单项文字是白的看不见"。
    """
    px = max(10, int(font_size))
    return f"""
* {{ outline: 0; }}
QMainWindow, QDialog {{ background: {p.bg}; color: {p.fg}; }}
QWidget {{ color: {p.fg}; font-size: {px}px; font-family: "Segoe UI","Microsoft YaHei",
        -apple-system,"PingFang SC","Noto Sans CJK SC",sans-serif; }}
QToolTip {{
    background: {p.bg_alt}; color: {p.fg}; border: 1px solid {p.border};
    padding: 4px 7px; font-size: {px - 1}px;
}}
QMenuBar {{ background: {p.bg}; color: {p.fg}; border-bottom: 1px solid {p.border}; }}
QMenuBar::item {{ padding: 5px 10px; background: transparent; }}
QMenuBar::item:selected {{ background: {p.bg_hover}; }}
QMenu {{ background: {p.bg_alt}; color: {p.fg}; border: 1px solid {p.border};
        padding: 4px; }}
QMenu::item {{ padding: 5px 26px 5px 14px; border-radius: 4px; }}
QMenu::item:selected {{ background: {p.selection}; color: {p.fg}; }}
QMenu::separator {{ height: 1px; background: {p.border}; margin: 4px 6px; }}
QStatusBar {{ background: {p.bg}; color: {p.fg_dim}; font-size: {px - 1}px; }}

QPushButton {{
    background: {p.bg_alt}; color: {p.fg}; border: 1px solid {p.border};
    padding: 5px 12px; border-radius: 6px;
}}
QPushButton:hover {{ background: {p.bg_hover}; border-color: {p.accent}; }}
QPushButton:pressed {{ background: {p.selection}; }}
QPushButton:disabled {{ color: {p.fg_dim}; background: {p.bg}; }}
QPushButton#primary {{ background: {p.accent}; color: #ffffff; border: none;
                        font-weight: 600; padding: 6px 16px; }}
QPushButton#primary:hover {{ background: {p.accent_2}; }}
QPushButton#danger {{ color: {p.error}; }}
QPushButton#flat {{ border: none; background: transparent; padding: 4px 6px; }}
QPushButton#flat:hover {{ background: {p.bg_hover}; }}

QToolButton {{ background: transparent; border: 1px solid transparent;
                border-radius: 5px; padding: 4px 8px; color: {p.fg}; }}
QToolButton:hover {{ background: {p.bg_hover}; border-color: {p.border}; }}
QToolButton:checked {{ background: {p.selection}; border-color: {p.accent}; }}
QToolButton:disabled {{ color: {p.fg_dim}; }}

QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {p.bg_alt}; color: {p.fg}; border: 1px solid {p.border};
    border-radius: 6px; padding: 4px 7px; selection-background-color: {p.selection};
    selection-color: {p.fg};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus {{
    border-color: {p.accent}; }}
QLineEdit:disabled {{ color: {p.fg_dim}; }}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{
    background: {p.bg_alt}; color: {p.fg}; border: 1px solid {p.border};
    selection-background-color: {p.selection}; outline: none; }}

QScrollArea {{ background: {p.bg}; border: none; }}
/* 滚动区视口必须显式上色：Qt 给 viewport 画的是**窗口基色**（暗色下偏浅灰），
   内容比视口短时，下方会出现一块刺眼的浅色空白（dark 主题渲染图实测）。
   只针对 QScrollArea 的直接子 QWidget，不写 QAbstractScrollArea ——
   后者会连带给 QListWidget/QTableWidget 加边框，改一处坏三处。 */
QScrollArea > QWidget > QWidget {{ background: {p.bg}; }}
QListWidget, QTreeWidget, QTableWidget {{
    background: {p.bg_alt}; color: {p.fg}; border: 1px solid {p.border};
    border-radius: 6px; alternate-background-color: {p.bg};
    outline: none; }}
QListWidget::item, QTreeWidget::item {{ padding: 4px 6px; border-radius: 4px; }}
QListWidget::item:hover, QTreeWidget::item:hover {{ background: {p.bg_hover}; }}
QListWidget::item:selected, QTreeWidget::item:selected {{
    background: {p.selection}; color: {p.fg}; }}
QHeaderView::section {{ background: {p.bg}; color: {p.fg_dim};
    border: none; border-bottom: 1px solid {p.border}; padding: 5px 8px;
    font-weight: 600; }}

QGroupBox {{
    border: 1px solid {p.border}; border-radius: 8px; margin-top: 14px;
    padding: 10px 10px 8px 10px; background: {p.bg_alt}; font-weight: 600; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 5px;
                    color: {p.fg_dim}; }}

QTabWidget::pane {{ border: 1px solid {p.border}; border-radius: 6px;
                    background: {p.bg_alt}; top: -1px; }}
QTabBar::tab {{ background: transparent; color: {p.fg_dim}; padding: 6px 14px;
                border: 1px solid transparent; border-bottom: none;
                border-top-left-radius: 6px; border-top-right-radius: 6px; }}
QTabBar::tab:selected {{ background: {p.bg_alt}; color: {p.fg};
                        border-color: {p.border}; }}
QTabBar::tab:hover:!selected {{ color: {p.fg}; background: {p.bg_hover}; }}

QScrollBar:vertical {{ background: transparent; width: 11px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {p.border}; border-radius: 4px;
                                min-height: 26px; }}
QScrollBar::handle:vertical:hover {{ background: {p.fg_dim}; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {p.border}; border-radius: 4px;
                                 min-width: 26px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; background: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

QSplitter::handle {{ background: {p.border}; }}
QSplitter::handle:horizontal {{ width: 2px; }}
QSplitter::handle:vertical {{ height: 2px; }}
QSplitter::handle:hover {{ background: {p.accent}; }}

QCheckBox, QRadioButton {{ spacing: 7px; background: transparent; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 15px; height: 15px; }}
QCheckBox::indicator {{ border: 1px solid {p.border}; border-radius: 4px;
                        background: {p.bg}; }}
QCheckBox::indicator:checked {{ background: {p.accent}; border-color: {p.accent}; }}
QRadioButton::indicator {{ border: 1px solid {p.border}; border-radius: 8px;
                            background: {p.bg}; }}
QRadioButton::indicator:checked {{ border: 4px solid {p.accent}; background: {p.bg}; }}

QSlider::groove:horizontal {{ height: 4px; background: {p.border}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {p.accent}; width: 14px; margin: -5px 0;
                              border-radius: 7px; }}
QProgressBar {{ border: 1px solid {p.border}; border-radius: 6px;
                background: {p.bg_alt}; text-align: center; color: {p.fg};
                height: 16px; }}
QProgressBar::chunk {{ background: {p.accent}; border-radius: 5px; }}

QLabel#dim {{ color: {p.fg_dim}; }}
QLabel#mono {{ font-family: "{mono}","Cascadia Mono","Menlo",monospace; }}
QLabel#title {{ font-size: {px + 3}px; font-weight: 700; }}
QLabel#source {{ color: {p.source}; }}
QLabel#target {{ color: {p.target}; }}
QLabel#badge {{
    background: {p.bg_hover}; color: {p.fg_dim}; border-radius: 4px;
    padding: 1px 6px; font-size: {px - 2}px; }}
QLabel#badgeWarn {{
    background: {p.warn}; color: #ffffff; border-radius: 4px;
    padding: 1px 6px; font-size: {px - 2}px; font-weight: 600; }}
QLabel#badgeErr {{
    background: {p.error}; color: #ffffff; border-radius: 4px;
    padding: 1px 6px; font-size: {px - 2}px; font-weight: 600; }}

QFrame#card {{ background: {p.bg_alt}; border: 1px solid {p.border};
                border-radius: 8px; }}
QFrame#cardSelected {{ background: {p.selection}; border: 1px solid {p.accent};
                        border-radius: 8px; }}
QFrame#rowSource {{ background: {p.bg_alt}; border: none;
                     border-left: 3px solid {p.source}; border-radius: 4px; }}
QFrame#rowTarget {{ background: {p.bg}; border: none;
                     border-left: 3px solid {p.target}; border-radius: 4px; }}
"""


def resolve(theme: str = "auto") -> Palette:
    """auto 时读系统暗色设置。

    读不到的平台（Linux 无 gsettings / macOS 无 pyobjc）回落暗色 ——
    截图工具的使用场景多半是"看着屏幕内容"，暗底不容易和截图内容抢注意力。
    """
    if theme == "light":
        return LIGHT
    if theme == "dark":
        return DARK
    try:
        import os
        import sys

        if sys.platform == "win32":
            import winreg

            with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
                val, _ = winreg.QueryValueEx(k, "AppsUseLightTheme")
                return LIGHT if val else DARK
        if sys.platform == "darwin":
            from PyObjCFoundation import NSUserDefaults  # type: ignore

            d = NSUserDefaults.standardUserDefaults()
            mode = str(d.dictionaryRepresentation().get("AppleInterfaceStyle", "Light"))
            return DARK if "dark" in mode.lower() else LIGHT
        import subprocess

        out = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface",
                             "color-scheme"], capture_output=True, text=True, timeout=2)
        return DARK if "dark" in out.stdout else LIGHT
    except Exception:
        return DARK
