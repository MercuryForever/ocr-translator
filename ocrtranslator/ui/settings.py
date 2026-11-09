"""设置页。

五个标签页对应五个配置段。三条设计约束：

1. **改完立刻生效，不需要重启**。OCR 引擎/线程数这类需要重建的项，
   通过 `apply_requested` 通知 AppController 去 warm；
   语言对、布局这类纯参数直接写 cfg 即可。
2. **密钥永不明文回显**，且优先显示"来自环境变量"而不是把值读出来展示。
   环境变量里的 key 我们不该知道内容。
3. **每个可能失败的项旁边给"测试"按钮**。用户填完 DeepL key 不该靠
   "下次截图看看行不行"来验证 —— 那会把配置问题伪装成翻译问题。
"""

from __future__ import annotations

import os

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDoubleSpinBox,
                            QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                            QLineEdit, QMessageBox, QPushButton, QSpinBox,
                            QTabWidget, QVBoxLayout, QWidget)

from ..config import AppConfig, app_dir
from ..languages import AUTO, LANGUAGES
from ..logger import get_logger
from ..ocr import available_engines
from ..translate import BACKENDS, TranslateManager
from .theme import build_qss, resolve

log = get_logger("ui.settings")


class SettingsDialog(QDialog):
    apply_requested = pyqtSignal()          # 需要重建引擎/重绑热键
    hotkey_rebind = pyqtSignal(str)

    def __init__(self, cfg: AppConfig, translator: TranslateManager | None = None,
                 parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.translator = translator or TranslateManager(cfg)
        self.setWindowTitle("设置")
        self.setMinimumSize(620, 520)
        self.pal = resolve(cfg.ui.theme)
        self.setStyleSheet(build_qss(self.pal, cfg.ui.font_size, cfg.ui.mono_font))
        self._build()
        self._load()

    # ---------------------------------------------------------------- 构建
    def _build(self) -> None:
        root = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._tab_ocr(), "识别")
        self.tabs.addTab(self._tab_translate(), "翻译")
        self.tabs.addTab(self._tab_capture(), "截图与热键")
        self.tabs.addTab(self._tab_ui(), "界面")
        self.tabs.addTab(self._tab_export(), "导出")
        self.tabs.addTab(self._tab_advanced(), "高级")
        root.addWidget(self.tabs)

        bar = QHBoxLayout()
        self.lbl_status = QLabel("")
        self.lbl_status.setObjectName("dim")
        bar.addWidget(self.lbl_status, 1)
        btn_reset = QPushButton("恢复本页默认")
        btn_reset.setToolTip("只重置当前标签页对应的配置段")
        btn_reset.clicked.connect(self._reset_section)
        bar.addWidget(btn_reset)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.close)
        bar.addWidget(btn_close)
        self.btn_save = QPushButton("保存")
        self.btn_save.setObjectName("primary")
        self.btn_save.clicked.connect(self._save)
        bar.addWidget(self.btn_save)
        root.addLayout(bar)

    def _tab_ocr(self) -> QWidget:
        w = QWidget(); f = QFormLayout(w); f.setContentsMargins(16, 16, 16, 16)
        f.setSpacing(10)
        self.ocr_engine = QComboBox()
        self.ocr_engine.setToolTip("RapidOCR 离线、体积小、中文强；PaddleOCR 语种更多但体积大；Tesseract 需外部程序")
        self.ocr_info = QLabel("")
        self.ocr_info.setObjectName("dim")
        self.ocr_info.setWordWrap(True)
        self.ocr_engine.currentIndexChanged.connect(self._refresh_ocr_info)
        self.lang_hint = QComboBox()
        self.lang_hint.setToolTip("RapidOCR 用同一模型识别多语种，这里主要影响 Paddle/Tesseract")
        self.min_conf = QDoubleSpinBox()
        self.min_conf.setRange(0.05, 0.95)
        self.min_conf.setSingleStep(0.05)
        self.min_conf.setToolTip("低于该置信度的行直接丢弃。调高更干净，调低不漏字")
        self.upscale = QCheckBox("启用预处理（实测：2x 放大提升精度并更快）")
        self.restore_spaces = QCheckBox("尝试修复丢失的拉丁空格（实测净收益 0，默认关）")
        self.det = QCheckBox("启用文字检测（关闭则整图当一行识别，仅适合单行截图）")
        self.threads = QSpinBox()
        self.threads.setRange(0, 32)
        self.threads.setSpecialValueText("自动（推荐 4）")
        self.threads.setToolTip("实测全核反而最慢：4 线程 484ms vs 全核 1661ms")
        self.tess_cmd = QLineEdit()
        self.tess_cmd.setPlaceholderText("仅 Tesseract 需要：tesseract.exe 路径")
        for label, widget in (
                ("OCR 引擎", self.ocr_engine), ("", self.ocr_info),
                ("语言倾向", self.lang_hint), ("最低置信度", self.min_conf),
                ("推理线程数", self.threads), ("", self.upscale),
                ("", self.restore_spaces), ("", self.det),
                ("Tesseract 路径", self.tess_cmd)):
            f.addRow(label, widget)
        return w

    def _tab_translate(self) -> QWidget:
        w = QWidget(); f = QFormLayout(w); f.setContentsMargins(16, 16, 16, 16)
        f.setSpacing(10)
        self.backend = QComboBox()
        self.backend.setToolTip("auto = 按可用性依次尝试，失败自动降级")
        self.chain_label = QLabel("")
        self.chain_label.setObjectName("dim")
        self.chain_label.setWordWrap(True)
        self.src_lang = QComboBox()
        self.dst_lang = QComboBox()
        self.deepl_key = QLineEdit()
        self.deepl_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.deepl_key.setPlaceholderText("留空则不启用；也可用环境变量 DEEPL_API_KEY")
        self.baidu_appid = QLineEdit()
        self.baidu_key = QLineEdit()
        self.baidu_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.timeout = QDoubleSpinBox()
        self.timeout.setRange(2.0, 60.0)
        self.timeout.setSuffix(" 秒")
        self.cache_size = QSpinBox(); self.cache_size.setRange(128, 65536)
        self.cache_size.setSingleStep(256)
        self.opus_pair = QLineEdit()
        self.opus_pair.setPlaceholderText("en-zh")

        row = QHBoxLayout()
        self.btn_test = QPushButton("测试翻译")
        self.btn_test.clicked.connect(self._test_translate)
        self.btn_usage = QPushButton("查 DeepL 用量")
        self.btn_usage.clicked.connect(self._check_usage)
        self.lbl_test = QLabel("")
        self.lbl_test.setObjectName("dim")
        self.lbl_test.setWordWrap(True)
        row.addWidget(self.btn_test); row.addWidget(self.btn_usage); row.addWidget(self.lbl_test, 1)
        box = QWidget(); box.setLayout(row)

        for label, widget in (("后端选择", self.backend), ("生效链", self.chain_label),
                              ("源语言", self.src_lang), ("目标语言", self.dst_lang),
                              ("DeepL Auth Key", self.deepl_key),
                              ("百度 appid", self.baidu_appid),
                              ("百度密钥", self.baidu_key),
                              ("OPUS 语向", self.opus_pair),
                              ("请求超时", self.timeout), ("缓存条数", self.cache_size),
                              ("", box)):
            f.addRow(label, widget)
        self.backend.currentIndexChanged.connect(self._refresh_chain)
        return w

    def _tab_capture(self) -> QWidget:
        w = QWidget(); f = QFormLayout(w); f.setContentsMargins(16, 16, 16, 16)
        f.setSpacing(10)
        self.hotkey = QLineEdit()
        self.hotkey.setPlaceholderText("ctrl+shift+a")
        self.hotkey.setToolTip("示例：ctrl+shift+a、alt+ctrl+x、f9、ctrl+alt+space")
        self.hotkey2 = QLineEdit()
        self.second_enabled = QCheckBox("启用第二个热键（主窗口打开时也能截）")
        self.sys_hotkey = QCheckBox("注册系统级全局热键（其他程序前台时也响应）")
        self.hide_own = QCheckBox("截图前隐藏本程序窗口（避免把自己的结果窗 OCR 进去）")
        self.clip_cap = QCheckBox("框选完成后把图片复制到剪贴板")
        self.magnifier = QCheckBox("框选时显示像素放大镜与取色器")
        self.save_hist = QCheckBox("保存截图历史")
        row = QHBoxLayout()
        self.btn_probe = QPushButton("检测热键可用性")
        self.btn_probe.clicked.connect(self._probe_hotkey)
        self.lbl_hotkey = QLabel("")
        self.lbl_hotkey.setObjectName("dim")
        row.addWidget(self.btn_probe); row.addWidget(self.lbl_hotkey, 1)
        box = QWidget(); box.setLayout(row)
        for label, widget in (("主热键", self.hotkey), ("副热键", self.hotkey2),
                              ("", self.second_enabled), ("", self.sys_hotkey),
                              ("", box), ("", self.hide_own), ("", self.clip_cap),
                              ("", self.magnifier), ("", self.save_hist)):
            f.addRow(label, widget)
        return w

    def _tab_ui(self) -> QWidget:
        w = QWidget(); f = QFormLayout(w); f.setContentsMargins(16, 16, 16, 16)
        f.setSpacing(10)
        self.theme = QComboBox()
        self.layout_mode = QComboBox()
        self.font_size = QSpinBox(); self.font_size.setRange(9, 20)
        self.mono_font = QLineEdit()
        self.show_conf = QCheckBox("结果显示置信度")
        self.show_raw = QCheckBox("默认展开原始 OCR 行（调试版面对有用）")
        self.highlight = QCheckBox("悬停时联动高亮对应译文")
        self.autoscroll = QCheckBox("新结果自动回到顶部")
        self.on_top = QCheckBox("结果窗口保持置顶")
        self.remember = QCheckBox("记住结果窗口大小与位置")
        self.tray = QCheckBox("最小化到系统托盘（无任务栏按钮）")
        for label, widget in (("主题", self.theme), ("默认布局", self.layout_mode),
                              ("字号", self.font_size), ("等宽字体", self.mono_font),
                              ("", self.show_conf), ("", self.show_raw),
                              ("", self.highlight), ("", self.autoscroll),
                              ("", self.on_top), ("", self.remember), ("", self.tray)):
            f.addRow(label, widget)
        self.theme.currentIndexChanged.connect(self._preview_theme)
        self.font_size.valueChanged.connect(lambda v: setattr(self.cfg.ui, "font_size", v))
        return w

    def _tab_export(self) -> QWidget:
        w = QWidget(); f = QFormLayout(w); f.setContentsMargins(16, 16, 16, 16)
        f.setSpacing(10)
        self.export_dir = QLineEdit()
        self.export_dir.setPlaceholderText("留空 = 程序数据目录下的 exports")
        row = QHBoxLayout()
        btn = QPushButton("选择…")
        btn.clicked.connect(self._pick_dir)
        btn2 = QPushButton("打开")
        btn2.clicked.connect(self._open_dir)
        row.addWidget(self.export_dir, 1); row.addWidget(btn); row.addWidget(btn2)
        box = QWidget(); box.setLayout(row)
        self.pattern = QLineEdit()
        self.pattern.setToolTip("可用占位符：{date} {time} {datetime} {lang} {srclang} {engine} {count}")
        self.txt_tpl = QComboBox()
        self.docx_two = QCheckBox("DOCX 用表格排版（原文/译文两列）")
        self.meta_head = QCheckBox("导出文件头部写入引擎与耗时信息")
        for label, widget in (("默认目录", box), ("文件名模板", self.pattern),
                              ("TXT 样式", self.txt_tpl), ("", self.docx_two),
                              ("", self.meta_head)):
            f.addRow(label, widget)
        return w

    def _tab_advanced(self) -> QWidget:
        w = QWidget(); v = QVBoxLayout(w); v.setContentsMargins(16, 16, 16, 16)
        g = QGroupBox("路径")
        gv = QVBoxLayout(g)
        for label, path in (("配置", app_dir() / "config.json"), ("数据目录", app_dir()),
                            ("日志", app_dir() / "logs" / "socrans.log")):
            row = QHBoxLayout()
            row.addWidget(QLabel(f"{label}：{path}"), 1)
            b = QPushButton("打开")
            b.setObjectName("flat")
            b.clicked.connect(lambda _=False, p=path: self._open_path(p))
            row.addWidget(b)
            box = QWidget(); box.setLayout(row)
            gv.addWidget(box)
        v.addWidget(g)
        g2 = QGroupBox("后端诊断")
        g2v = QVBoxLayout(g2)
        for info, tag in self.translator.status():
            line = QLabel(f"{'●' if info.available else '○'} {info.label} "
                          f"<span style='color:{self.pal.fg_dim}'>"
                          f"{info.note}｜{tag}</span>")
            line.setTextFormat(Qt.TextFormat.RichText)
            g2v.addWidget(line)
        v.addWidget(g2)
        btn = QPushButton("清空翻译缓存")
        btn.clicked.connect(lambda: (self.translator.cache.clear(),
                                     self.lbl_status.setText("缓存已清空")))
        v.addWidget(btn)
        v.addStretch()
        return w

    # ---------------------------------------------------------------- 读写
    def _fill_combo(self, combo: QComboBox, values, current, labels=None):
        combo.clear()
        for i, v in enumerate(values):
            combo.addItem(labels[i] if labels else str(v), v)
        idx = combo.findData(current)
        combo.setCurrentIndex(max(0, idx))

    def _load(self) -> None:
        # _loading 守卫：_fill_combo 会触发 currentIndexChanged，
        # 而 _preview_theme 里读 self.font_size.value() —— 此时字号还没从配置写入，
        # QSpinBox 默认值是 0，会生成 font-size:0px 的样式表把整个对话框弄空白。
        self._loading = True
        try:
            self._load_inner()
        finally:
            self._loading = False
        self._refresh_ocr_info()
        self._refresh_chain()

    def _load_inner(self) -> None:
        c = self.cfg
        infos = available_engines()
        self._fill_combo(self.ocr_engine,
                         ["auto", *[i.name for i in infos]], c.ocr.engine,
                         ["自动（推荐）", *[i.label for i in infos]])
        self._fill_combo(self.lang_hint,
                         ["ch", "en", "japan", "korean", "french", "german",
                          "spanish", "russian", "portuguese", "th", "viet", "indonesia"],
                         c.ocr.lang_hint)
        self.min_conf.setValue(c.ocr.min_conf)
        self.threads.setValue(c.ocr.threads)
        self.upscale.setChecked(c.ocr.upscale)
        self.restore_spaces.setChecked(c.ocr.restore_spaces)
        self.det.setChecked(c.ocr.use_det)
        self.tess_cmd.setText(c.ocr.tesseract_cmd)
        self._refresh_ocr_info()

        self._fill_combo(self.backend, ["auto", *BACKENDS], c.translate.backend,
                         ["自动降级（推荐）", *[BACKENDS[n].label for n in BACKENDS]])
        langs = [AUTO] + [l.code for l in LANGUAGES]
        names = ["自动检测"] + [l.display for l in LANGUAGES]
        self._fill_combo(self.src_lang, langs, c.translate.source_lang, names)
        self._fill_combo(self.dst_lang, [l.code for l in LANGUAGES],
                         c.translate.target_lang, [l.display for l in LANGUAGES])
        self.deepl_key.setText("(来自环境变量)" if os.environ.get("DEEPL_API_KEY")
                               else c.translate.deepl_auth_key)
        self.baidu_appid.setText(c.translate.baidu_appid)
        self.baidu_key.setText("(来自环境变量)" if os.environ.get("BAIDU_KEY")
                               else c.translate.baidu_key)
        self.opus_pair.setText(c.translate.opus_model_pair)
        self.timeout.setValue(c.translate.timeout)
        self.cache_size.setValue(c.translate.cache_size)
        self._refresh_chain()

        self.hotkey.setText(c.capture.hotkey)
        self.hotkey2.setText(c.capture.second_hotkey)
        self.second_enabled.setChecked(bool(c.capture.second_hotkey))
        self.sys_hotkey.setChecked(c.capture.system_hotkey)
        self.hide_own.setChecked(c.capture.hide_own_windows)
        self.clip_cap.setChecked(c.capture.clipboard_after_capture)
        self.magnifier.setChecked(c.capture.magnifier)
        self.save_hist.setChecked(c.capture.save_history)

        self._fill_combo(self.theme, ["auto", "light", "dark"], c.ui.theme,
                         ["跟随系统", "浅色", "深色"])
        self._fill_combo(self.layout_mode,
                         ["side_by_side", "stacked", "interleaved"], c.ui.layout_mode,
                         ["左右分栏", "上下堆叠", "逐段交替"])
        self.font_size.setValue(c.ui.font_size)
        self.mono_font.setText(c.ui.mono_font)
        for cb, val in ((self.show_conf, c.ui.show_confidence),
                        (self.show_raw, c.ui.show_raw_lines),
                        (self.highlight, c.ui.highlight_pair),
                        (self.autoscroll, c.ui.auto_scroll),
                        (self.on_top, c.ui.always_on_top),
                        (self.remember, c.ui.remember_result_window),
                        (self.tray, c.ui.tray_enabled)):
            cb.setChecked(val)

        self.export_dir.setText(c.export.default_dir)
        self.pattern.setText(c.export.filename_pattern)
        self._fill_combo(self.txt_tpl, ["numbered", "plain", "markdown"],
                         c.export.txt_template,
                         ["编号 [1] 原文/译文", "原文空行译文", "Markdown 表格"])
        self.docx_two.setChecked(c.export.docx_two_column)
        self.meta_head.setChecked(c.export.add_meta_header)

    def _collect(self) -> None:
        c = self.cfg
        c.ocr.engine = self.ocr_engine.currentData()
        c.ocr.lang_hint = self.lang_hint.currentData()
        c.ocr.min_conf = self.min_conf.value()
        c.ocr.threads = self.threads.value()
        c.ocr.upscale = self.upscale.isChecked()
        c.ocr.restore_spaces = self.restore_spaces.isChecked()
        c.ocr.use_det = self.det.isChecked()
        c.ocr.tesseract_cmd = self.tess_cmd.text().strip()

        c.translate.backend = self.backend.currentData()
        c.translate.source_lang = self.src_lang.currentData()
        c.translate.target_lang = self.dst_lang.currentData()
        if not self.deepl_key.text().startswith("("):
            c.translate.deepl_auth_key = self.deepl_key.text().strip()
        if not self.baidu_key.text().startswith("("):
            c.translate.baidu_key = self.baidu_key.text().strip()
        c.translate.baidu_appid = self.baidu_appid.text().strip()
        c.translate.opus_model_pair = self.opus_pair.text().strip() or "en-zh"
        c.translate.timeout = self.timeout.value()
        c.translate.cache_size = self.cache_size.value()

        c.capture.hotkey = self.hotkey.text().strip() or "ctrl+shift+a"
        c.capture.second_hotkey = (self.hotkey2.text().strip()
                                   if self.second_enabled.isChecked() else "")
        c.capture.system_hotkey = self.sys_hotkey.isChecked()
        c.capture.hide_own_windows = self.hide_own.isChecked()
        c.capture.clipboard_after_capture = self.clip_cap.isChecked()
        c.capture.magnifier = self.magnifier.isChecked()
        c.capture.save_history = self.save_hist.isChecked()

        c.ui.theme = self.theme.currentData()
        c.ui.layout_mode = self.layout_mode.currentData()
        c.ui.font_size = self.font_size.value()
        c.ui.mono_font = self.mono_font.text().strip() or "Consolas"
        c.ui.show_confidence = self.show_conf.isChecked()
        c.ui.show_raw_lines = self.show_raw.isChecked()
        c.ui.highlight_pair = self.highlight.isChecked()
        c.ui.auto_scroll = self.autoscroll.isChecked()
        c.ui.always_on_top = self.on_top.isChecked()
        c.ui.remember_result_window = self.remember.isChecked()
        c.ui.tray_enabled = self.tray.isChecked()

        c.export.default_dir = self.export_dir.text().strip()
        c.export.filename_pattern = self.pattern.text().strip() or "ocr_{date}_{time}"
        c.export.txt_template = self.txt_tpl.currentData()
        c.export.docx_two_column = self.docx_two.isChecked()
        c.export.add_meta_header = self.meta_head.isChecked()

    def _save(self) -> None:
        self._collect()
        ok = self.cfg.save()
        self.lbl_status.setText("已保存" if ok else "保存失败，详见日志")
        if ok:
            self.apply_requested.emit()
            self.hotkey_rebind.emit(self.cfg.capture.hotkey)

    def _reset_section(self) -> None:
        idx = self.tabs.currentIndex()
        names = {0: "ocr", 1: "translate", 2: "capture", 3: "ui", 4: "export"}
        if idx in names:
            self.cfg.reset_section(names[idx])
            self._load()
            self.lbl_status.setText(f"已恢复「{self.tabs.tabText(idx)}」默认值（未保存）")

    # ---------------------------------------------------------------- 辅助
    def _refresh_ocr_info(self) -> None:
        if getattr(self, "_loading", False):
            return
        name = self.ocr_engine.currentData()
        infos = {i.name: i for i in available_engines()}
        if name == "auto":
            first = next((i for i in infos.values() if i.available), None)
            self.ocr_info.setText(f"自动选择：{first}" if first else "⚠ 没有可用 OCR 引擎")
        else:
            i = infos.get(name)
            self.ocr_info.setText(("✓ " + str(i)) if i and i.available
                                  else f"⚠ {i.note if i else '未知引擎'}")

    def _refresh_chain(self) -> None:
        if getattr(self, "_loading", False):
            return
        self.chain_label.setText(" → ".join(self.translator.chain()))

    def _test_translate(self) -> None:
        self._collect()
        self.lbl_test.setText("测试中…")
        QGuiApplication.processEvents()
        probe = "Quarterly revenue grew 18 percent year over year."
        text, err = self.translator.single(probe, self.src_lang.currentData(),
                                           self.dst_lang.currentData())
        if err:
            self.lbl_test.setText(f"✗ {err}")
            self.lbl_test.setStyleSheet(f"color:{self.pal.error};")
        else:
            self.lbl_test.setText(f"✓ {text}")
            self.lbl_test.setStyleSheet(f"color:{self.pal.ok};")

    def _check_usage(self) -> None:
        be = self.translator.backend("deepl")
        self.lbl_test.setText(be.usage() if be else "DeepL 未启用")

    def _probe_hotkey(self) -> None:
        from ..capture.hotkey import HotkeyManager, parse

        spec = self.hotkey.text().strip()
        if parse(spec) is None:
            self.lbl_hotkey.setText(f"✗ 无法解析「{spec}」，请用 ctrl+shift+a 这类写法")
            return
        mgr = HotkeyManager(lambda _n: None)
        ok = mgr.start() and mgr.bind("probe", spec)
        mgr.unbind_all()
        self.lbl_hotkey.setText("✓ 可注册" if ok else
                                f"✗ 注册失败：{spec} 可能已被其他程序占用")

    def _preview_theme(self) -> None:
        if getattr(self, "_loading", False):
            return
        self.pal = resolve(self.theme.currentData())
        self.setStyleSheet(build_qss(self.pal, max(9, self.font_size.value()),
                                     self.mono_font.text() or "Consolas"))

    def _pick_dir(self) -> None:
        from PyQt6.QtWidgets import QFileDialog
        d = QFileDialog.getExistingDirectory(self, "选择导出目录",
                                             self.export_dir.text() or str(app_dir()))
        if d:
            self.export_dir.setText(d)

    def _open_dir(self) -> None:
        from ..config import data_dir
        self._open_path(data_dir("exports"))

    @staticmethod
    def _open_path(path) -> None:
        import subprocess
        import sys
        from pathlib import Path

        p = Path(path)
        try:
            if not p.exists():
                p.mkdir(parents=True, exist_ok=True)
            target = p if p.is_dir() else p.parent
            if sys.platform == "win32":
                os.startfile(str(target))            # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(target)])
            else:
                subprocess.Popen(["xdg-open", str(target)])
        except Exception as exc:
            QMessageBox.warning(None, "打开失败", f"{exc}\n路径：{path}")
