"""语言清单与三方（OCR / 翻译 / 显示名）代码映射。

痛点在于各体系的语言代码不统一：
    PaddleOCR  -> zh / en / japan / korean / french
    RapidOCR   -> 同 PaddleOCR（PP-OCR 模型）
    Tesseract  -> chi_sim / eng / jpn / kor / fra
    Google API -> zh-CN / en / ja / ko / fr
    DeepL      -> ZH / EN / JA / KO / FR（源语言只接受 "ZH"/"EN"，或 Detect）
    百度翻译   -> zh / en / jp / kor / fr
    OPUS-MT    -> 仅内置了若干对，用 "zh-en" 形式

因此本模块以 **BCP-47 风格的小写代码**（zh/en/ja/ko/fr/...）作为内部唯一真值，
在各自适配器中做转换，绝不允许散落 if/else。
"""

from __future__ import annotations

from dataclasses import dataclass, field

AUTO = "auto"


@dataclass(frozen=True, slots=True)
class Language:
    code: str            # 内部代码，唯一真值
    name_zh: str         # 中文显示名
    name_en: str
    ocr: dict[str, str] = field(default_factory=dict)     # engine -> 代码
    translate: dict[str, str] = field(default_factory=dict)  # engine -> 代码
    rtl: bool = False

    @property
    def display(self) -> str:
        return f"{self.name_zh} ({self.name_en})"


def _L(code, zh, en, ocr=None, tr=None, rtl=False) -> Language:
    return Language(code, zh, en, ocr or {}, tr or {}, rtl)


LANGUAGES: list[Language] = [
    _L("zh", "简体中文", "Chinese",
       {"paddle": "zh", "tesseract": "chi_sim", "google": "zh-CN", "deepl": "ZH", "baidu": "zh", "opus": "zh"},
       {"google": "zh-CN", "deepl": "ZH", "baidu": "zh", "opus": "zh"}),
    _L("zh-TW", "繁體中文", "Chinese (Traditional)",
       {"paddle": "chinese_cht", "tesseract": "chi_tra", "google": "zh-TW", "deepl": "ZH", "baidu": "cht", "opus": "zh"},
       {"google": "zh-TW", "deepl": "ZH", "baidu": "cht", "opus": "zh"}),
    _L("en", "英语", "English",
       {"paddle": "en", "tesseract": "eng", "google": "en", "deepl": "EN", "baidu": "en", "opus": "en"},
       {"google": "en", "deepl": "EN", "baidu": "en", "opus": "en"}),
    _L("ja", "日语", "Japanese",
       {"paddle": "japan", "tesseract": "jpn", "google": "ja", "deepl": "JA", "baidu": "jp", "opus": "ja"},
       {"google": "ja", "deepl": "JA", "baidu": "jp", "opus": "ja"}),
    _L("ko", "韩语", "Korean",
       {"paddle": "korean", "tesseract": "kor", "google": "ko", "deepl": "KO", "baidu": "kor", "opus": "ko"},
       {"google": "ko", "deepl": "KO", "baidu": "kor", "opus": "ko"}),
    _L("fr", "法语", "French",
       {"paddle": "french", "tesseract": "fra", "google": "fr", "deepl": "FR", "baidu": "fr", "opus": "fr"},
       {"google": "fr", "deepl": "FR", "baidu": "fr", "opus": "fr"}),
    _L("de", "德语", "German",
       {"paddle": "german", "tesseract": "deu", "google": "de", "deepl": "DE", "baidu": "de", "opus": "de"},
       {"google": "de", "deepl": "DE", "baidu": "de", "opus": "de"}),
    _L("es", "西班牙语", "Spanish",
       {"paddle": "spanish", "tesseract": "spa", "google": "es", "deepl": "ES", "baidu": "spa", "opus": "es"},
       {"google": "es", "deepl": "ES", "baidu": "spa", "opus": "es"}),
    _L("ru", "俄语", "Russian",
       {"paddle": "russian", "tesseract": "rus", "google": "ru", "deepl": "RU", "baidu": "ru", "opus": "ru"}),
    _L("pt", "葡萄牙语", "Portuguese",
       {"paddle": "portuguese", "tesseract": "por", "google": "pt", "deepl": "PT", "baidu": "pt", "opus": "pt"}),
    _L("it", "意大利语", "Italian",
       {"tesseract": "ita", "google": "it", "deepl": "IT", "baidu": "it"}),
    _L("ar", "阿拉伯语", "Arabic",
       {"tesseract": "ara", "google": "ar", "deepl": "AR", "baidu": "ara"}, rtl=True),
    _L("th", "泰语", "Thai",
       {"paddle": "th", "tesseract": "tha", "google": "th", "baidu": "th"}),
    _L("vi", "越南语", "Vietnamese",
       {"paddle": "viet", "tesseract": "vie", "google": "vi", "deepl": "VI", "baidu": "vie"}),
    _L("id", "印尼语", "Indonesian",
       {"paddle": "indonesia", "tesseract": "ind", "google": "id", "baidu": "id"}),
]

_INDEX: dict[str, Language] = {lg.code: lg for lg in LANGUAGES}


def get(code: str) -> Language:
    """按内部代码取语言；未知代码返回英文兜底（绝不抛异常打断截图流程）。"""
    return _INDEX.get(code, _INDEX["en"])


def ocr_code(code: str, engine: str) -> str:
    if code == AUTO:
        return ""
    lg = get(code)
    return lg.ocr.get(engine) or lg.translate.get(engine) or code


def tr_code(code: str, engine: str) -> str:
    if code == AUTO:
        return "auto"
    lg = get(code)
    return lg.translate.get(engine) or code


def supported_by(engine: str) -> list[Language]:
    """某 OCR 引擎真正支持的语言（用于设置页灰化不可选项）。"""
    return [lg for lg in LANGUAGES if engine in lg.ocr]


# 语言检测：按字符区间统计，避免引入 langdetect 依赖
def detect(text: str, threshold: float = 0.05) -> str:
    """极简语向猜测。返回内部代码；无法判断时返回 AUTO 让后端自动检测。

    只做"值得走离线快速路径"的判断：CJK / 拉丁。判不准时交给服务端 autodetect,
    比在客户端硬猜更安全。
    """
    if not text.strip():
        return AUTO
    zh = ja = ko = cyr = arab = latin = 0
    total = 0
    for ch in text:
        if ch.isspace() or not ch.isalnum():
            continue
        total += 1
        o = ord(ch)
        if 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF:
            zh += 1
        elif 0x3040 <= o <= 0x30FF:
            ja += 1  # 假名 => 必为日语（中文不含假名）
        elif 0xAC00 <= o <= 0xD7A3 or 0x1100 <= o <= 0x11FF:
            ko += 1
        elif 0x0400 <= o <= 0x04FF:
            cyr += 1
        elif 0x0600 <= o <= 0x06FF:
            arab += 1
        elif 0x0041 <= o <= 0x005A or 0x0061 <= o <= 0x007A:
            latin += 1
    if total == 0:
        return AUTO
    scores = {"ja": ja / total, "ko": ko / total, "zh": zh / total,
              "ru": cyr / total, "ar": arab / total, "en": latin / total}
    best = max(scores, key=scores.__getitem__)
    return best if scores[best] >= threshold else AUTO
