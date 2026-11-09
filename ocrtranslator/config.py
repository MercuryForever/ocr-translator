"""应用配置：JSON 持久化 + 环境变量兜底 + 迁移容错。

原则：
* 读配置永远不抛异常。损坏的 config.json 备份后重建，因为用户可能正按了热键
  等着截图，配置解析失败不能让程序退出。
* 密钥一律从环境变量优先读取，避免把 key 明文写进可能被同步/上传的文件。
* 配置项用 dataclass 而非 dict，避免 UI 代码里散落字符串键名。
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import sys
from dataclasses import asdict, dataclass, field, fields
from functools import lru_cache
from pathlib import Path
from typing import Any

from .logger import get_logger

log = get_logger("config")

APP_DIRNAME = "socrans"

#: app_dir() 落到临时目录兜底时置 True（配置/缓存重启即丢，多半意味着
#: 所有常规候选目录都不可写 —— 常见诱因：目录曾被管理员权限运行创建，
#: ACL 归 Administrators 所有，普通权限写不进去）。
USING_TEMP_FALLBACK = False
#: 逐个候选目录的失败原因（"目录: 异常名 详情"），供 --doctor 与自动提权提示展示。
FALLBACK_ERRORS: list[str] = []


def _win_folder(env: str, fallback: str) -> Path:
    return Path(os.environ.get(env, str(Path.home() / fallback)))


@lru_cache(maxsize=1)
def app_dir() -> Path:
    """可写数据目录（配置、模型缓存、导出、日志）。

    带缓存：模块导入期 CONFIG_PATH/LOG_DIR/MODEL_DIR 会各问一次，
    不缓存就会重复 mkdir + 写探测文件，也把警告刷屏（实测打过 5 次）。

    优先级：
      1. `SOCRANS_HOME` 环境变量 —— 便携安装/CI/沙箱环境用它把工作目录指到任意位置；
      2. Windows `%APPDATA%`、macOS `~/Library/Application Support`、Linux XDG；
      3. 项目根下的 `.socrans` —— 最后兜底，让"绿色版拷到 U 盘"也能跑。

    为什么 Windows 不直接用 `~`：主目录常被 OneDrive 重定向，
    而 OCR 模型有 100+MB，放上去会被持续同步上传、拖慢整机。

    这个函数**只保证返回一个存在的目录，绝不抛异常**：它在模块导入期就会被调用，
    任何异常都会导致整个程序起不来，而用户完全看不到错误。
    """
    override = os.environ.get("SOCRANS_HOME")
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override))
    if sys.platform == "win32":
        candidates.append(_win_folder("APPDATA", "AppData/Roaming") / APP_DIRNAME)
    elif sys.platform == "darwin":
        candidates.append(Path.home() / "Library" / "Application Support" / APP_DIRNAME)
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        candidates.append(Path(xdg) / APP_DIRNAME)
    candidates.append(Path.home() / f".{APP_DIRNAME}")
    candidates.append(_project_root() / f".{APP_DIRNAME}")

    errors: list[str] = []
    for p in candidates:
        try:
            p.mkdir(parents=True, exist_ok=True)
            with open(p / ".wtest", "w") as f:     # 建目录成功不等于可写，必须实测
                f.write("1")
            (p / ".wtest").unlink(missing_ok=True)
            if errors:
                # 用 logger 而不是 print：这条信息必须同时进日志文件，
                # 否则打包后用户遇到"配置保存不了"时无从查起（gui 程序没有控制台）。
                log.warning("数据目录回退到 %s（原目录不可写：%s）", p, errors[0])
            return p
        except OSError as exc:
            errors.append(f"{p}: {exc.__class__.__name__} {exc}")
    # 全都不行：用系统临时目录，至少程序能启动，日志会说明情况
    import tempfile
    global USING_TEMP_FALLBACK

    tmp = Path(tempfile.gettempdir()) / APP_DIRNAME
    try:
        tmp.mkdir(parents=True, exist_ok=True)
        USING_TEMP_FALLBACK = True
        FALLBACK_ERRORS.extend(errors)
        log.warning("所有常规目录不可写，改用临时目录 %s；配置与模型缓存退出后可能丢失。"
                    "可设 SOCRANS_HOME 指定可写目录。详情：%s", tmp, " | ".join(errors))
    except OSError:
        log.error("连临时目录都无法创建（%s），配置将无法持久化。详情：%s", tmp, " | ".join(errors))
    return tmp


def _project_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


CONFIG_PATH = app_dir() / "config.json"
LOG_DIR = app_dir() / "logs"


def data_dir(sub: str = "") -> Path:
    p = app_dir() / sub if sub else app_dir()
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return p


MODEL_DIR = data_dir("models")
EXPORT_DIR = data_dir("exports")
HISTORY_DIR = data_dir("history")

# 用 Windows 凭据管理器 / macOS keyring 太重，这里只做"混淆存储"：
# 明确告知用户这不是安全边界，只是为了不把 key 明文躺在 json 里被顺手 commit。
_OBFUSCATE_PREFIX = "v1:"


def _protect(secret: str) -> str:
    if not secret:
        return ""
    return _OBFUSCATE_PREFIX + base64.b64encode(secret.encode()).decode()


def _unprotect(stored: str) -> str:
    if not stored:
        return ""
    if stored.startswith(_OBFUSCATE_PREFIX):
        try:
            return base64.b64decode(stored[len(_OBFUSCATE_PREFIX):]).decode()
        except Exception:
            return ""
    return stored  # 兼容用户手填明文


@dataclass
class OcrSettings:
    engine: str = "auto"            # auto | rapid | paddle | tesseract
    lang_hint: str = "ch"           # ch | en | japan | korean | ...（RapidOCR 语言集）
    use_det: bool = True
    use_rec: bool = True
    min_conf: float = 0.35          # 低于此置信度的行丢弃（仍保留在 raw 结果里）
    upscale: bool = True            # 预处理总开关（实测：2x 放大是净收益，见 ocr/preprocess.py）
    threads: int = 0                # 0 = 自动（min(4, cores/2)）；实测 -1 全速反而慢 3.4 倍
    restore_spaces: bool = False    # 空格修复：round2 实测净收益 +0，故默认关
    binarize: bool = False          # 自适应二值化（截图一般不需要，反而丢信息）
    drop_punctuation_only: bool = True
    tesseract_cmd: str = ""         # Windows 上常需手填 C:/Program Files/Tesseract/ocr.exe
    tessdata_dir: str = ""


@dataclass
class TranslateSettings:
    backend: str = "auto"           # auto | google | deepl | baidu | opus | none
    source_lang: str = "auto"
    target_lang: str = "zh"
    deepl_auth_key: str = ""
    baidu_appid: str = ""
    baidu_key: str = ""
    google_endpoint: str = "https://translate.googleapis.com/translate_a/single"
    deepl_endpoint: str = "https://api-free.deepl.com/v2/translate"
    baidu_endpoint: str = "https://fanyi-api.baidu.com/api/trans/vip/translate"
    opus_model_pair: str = "en-zh"
    batch_join_threshold: int = 200  # 少于该字符数时合并成一次请求
    timeout: float = 12.0
    cache_size: int = 4096


@dataclass
class CaptureSettings:
    hotkey: str = "ctrl+shift+a"    # 应用内全局热键
    system_hotkey: bool = True      # 尝试注册系统级全局钩子
    second_hotkey: str = "ctrl+shift+z"  # 再次截图（不退出主窗口）
    include_cursor: bool = False
    magnifier: bool = True
    hide_own_windows: bool = True   # 截图时先隐藏自己，避免把结果窗口 OCR 进去
    clipboard_after_capture: bool = True
    save_history: bool = True


@dataclass
class UiSettings:
    theme: str = "auto"             # auto | light | dark
    layout_mode: str = "side_by_side"   # side_by_side | stacked | interleaved
    show_confidence: bool = True
    show_raw_lines: bool = False
    font_size: int = 11
    mono_font: str = "Consolas"
    window_geometry: str = ""       # 序列化的 size|x,y
    splitter_sizes: list[int] = field(default_factory=lambda: [500, 500])
    remember_result_window: bool = True
    result_window_geometry: str = ""
    always_on_top: bool = True
    tray_enabled: bool = True
    auto_scroll: bool = True
    highlight_pair: bool = True     # 悬停原文高亮对应译文


@dataclass
class ExportSettings:
    default_dir: str = ""           # 空 => data_dir('exports')
    txt_template: str = "numbered"  # numbered | plain | markdown
    docx_two_column: bool = True
    filename_pattern: str = "ocr_{date}_{time}"
    add_meta_header: bool = True


@dataclass
class AppConfig:
    ocr: OcrSettings = field(default_factory=OcrSettings)
    translate: TranslateSettings = field(default_factory=TranslateSettings)
    capture: CaptureSettings = field(default_factory=CaptureSettings)
    ui: UiSettings = field(default_factory=UiSettings)
    export: ExportSettings = field(default_factory=ExportSettings)
    first_run_done: bool = False
    version: str = "1.0.0"

    # ---------- 密钥：环境变量优先 ----------
    #: kind -> (环境变量名, 配置字段名)。规则只在这一处，后端不再各自猜。
    _SECRETS = {
        "deepl": ("DEEPL_API_KEY", "deepl_auth_key"),
        "baidu_appid": ("BAIDU_APPID", "baidu_appid"),
        "baidu_key": ("BAIDU_KEY", "baidu_key"),
    }

    def secret(self, kind: str) -> str:
        """取密钥：环境变量 > 配置文件（混淆存储）。未知 kind 返回空串。"""
        env, field = self._SECRETS.get(kind, (None, None))
        if env is None:
            return ""
        val = os.environ.get(env, "")
        if val:
            return val
        return _unprotect(str(getattr(self.translate, field, "") or ""))

    @property
    def deepl_key(self) -> str:
        return self.secret("deepl")

    @property
    def baidu_ids(self) -> tuple[str, str]:
        return self.secret("baidu_appid"), self.secret("baidu_key")

    def has_online_credentials(self) -> bool:
        appid, key = self.baidu_ids
        return bool(self.deepl_key) or bool(appid and key)

    # ---------- 持久化 ----------
    def save(self, path: Path | None = None) -> bool:
        path = path or CONFIG_PATH
        payload = asdict(self)
        # 落盘前把明文 key 混淆
        t = payload["translate"]
        for k, env in (("deepl_auth_key", "DEEPL_API_KEY"),
                       ("baidu_appid", "BAIDU_APPID"),
                       ("baidu_key", "BAIDU_KEY")):
            if os.environ.get(env):
                t[k] = ""            # 环境变量存在就不重复存储
            elif t.get(k):
                t[k] = _protect(str(t[k]))
        try:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)   # 原子写：崩溃不会留下半个 config
            return True
        except OSError as exc:
            log.error("配置保存失败 %s: %s", path, exc)
            return False

    @classmethod
    def load(cls, path: Path | None = None) -> "AppConfig":
        path = path or CONFIG_PATH
        cfg = cls()
        if not path.exists():
            log.info("无配置文件，使用默认值：%s", path)
            return cfg
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            bad = path.with_suffix(".bad")
            try:
                shutil.copy2(path, bad)
                path.write_text("{}", encoding="utf-8")
            except OSError:
                pass
            log.error("配置损坏（已备份到 %s），回退默认值: %s", bad, exc)
            return cfg
        cfg = cls._from_nested(raw)
        log.info("已加载配置 %s", path)
        return cfg

    @classmethod
    def _from_nested(cls, raw: dict[str, Any]) -> "AppConfig":
        """按 dataclass 字段逐个填充，忽略未知键 —— 保证降级/升级兼容。"""
        def fill(obj, data: dict[str, Any]):
            known = {f.name for f in fields(obj)}
            for k, v in (data or {}).items():
                if k not in known:
                    continue
                cur = getattr(obj, k)
                if isinstance(v, list) and isinstance(cur, list):
                    setattr(obj, k, [x for x in v if isinstance(x, type(cur[0]) if cur else int)])
                elif isinstance(v, type(cur)) or (isinstance(cur, bool) and isinstance(v, int)):
                    setattr(obj, k, v)
                elif isinstance(cur, (int, float)) and isinstance(v, (int, float)):
                    setattr(obj, k, type(cur)(v))
                elif isinstance(cur, str) and isinstance(v, str):
                    setattr(obj, k, v)
            return obj

        cfg = cls()
        for sub in ("ocr", "translate", "capture", "ui", "export"):
            if isinstance(raw.get(sub), dict):
                fill(getattr(cfg, sub), raw[sub])
        for k in ("first_run_done", "version"):
            if k in raw and isinstance(raw[k], type(getattr(cfg, k))):
                setattr(cfg, k, raw[k])
        return cfg

    def reset_section(self, name: str) -> None:
        if name in {"ocr", "translate", "capture", "ui", "export"}:
            default = {"ocr": OcrSettings, "translate": TranslateSettings,
                       "capture": CaptureSettings, "ui": UiSettings,
                       "export": ExportSettings}[name]()
            setattr(self, name, default)
