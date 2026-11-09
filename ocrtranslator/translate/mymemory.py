"""MyMemory（翻译记忆库 + 神经机器翻译），无需密钥。

定位：**在线兜底**。当 Google 端点被墙/限流、且用户没配 DeepL/百度密钥时，
这条链保证"有网就能翻译"，不至于只能退回本地 OPUS-MT（那需要预下载模型）。

实测约束（本机验证，不是照抄文档）：
  * 可达性好：`api.mymemory.translated.net` TCP 正常（Google 端点在本机 TCP 超时），
    这正是它排在链条靠后但必须存在的原因。
  * **必须给显式 langpair**（如 `en|zh`）。`auto|zh` 会被拒 ——
    所以源语言为 auto 时先本地猜语向（languages.detect）。
  * `zh` 与 `zh-CN` 都接受；`en|en` 这类同语言请求返回
    HTTP 200 但包体内 `responseStatus=403 / "PLEASE SELECT TWO DISTINCT LANGUAGES"`。
    这不是错误而是语义："无需翻译"，必须原样返回原文
    （截图翻译里极常见：中英混排 + 目标语言=中文）。
  * 匿名配额约 5000 字符/天，超限是 401/429 或包体 status≠200。
  * 单请求上限 500 字节，**不支持批量**（supports_batch=False）；
    `matches[0].model == "neural"` 可用来区分"NMT 翻译"与"记忆库命中"。
"""

from __future__ import annotations

import html

from ..languages import AUTO, detect, tr_code
from ..logger import get_logger
from . import http
from .base import (AuthError, BackendInfo, NetworkError, QuotaExceeded,
                   TranslationError, TranslatorBackend)

log = get_logger("translate.mymemory")

ENDPOINT = "https://api.mymemory.translated.net/get"
_MAX_BYTES = 480        # 官方 500，留余量给控制字符
#: 包体里表示"语向相同，不需要翻译"的文案
_SAME_LANG = ("distinct", "same language")


class MyMemoryBackend(TranslatorBackend):
    name = "mymemory"
    label = "MyMemory（免费，无需密钥）"
    online = True
    needs_key = False
    supports_batch = False
    max_chars = 480         # 单位 = UTF-8 字节（见 _size），不是字符数

    def __init__(self, cfg, creds=None):
        super().__init__(cfg, creds)
        self.last_model = ""

    def _size(self, text: str) -> int:
        """预算按 **UTF-8 字节** 算：官方上限 500 字节，中文一字 3 字节，
        按字符数算预算会在服务端炸（实测：630 字节的混排段被拒）。
        """
        return len(text.encode("utf-8"))

    def _code(self, c: str) -> str:
        return tr_code(c, "mymemory") or c

    def _one(self, text: str, src: str, dst: str) -> str:
        s = src if src != AUTO else (detect(text) or "en")
        sc, dc = self._code(s), self._code(dst)
        # 同语向：语义是"无需翻译"。不能当错误抛，否则一张中英混排的截图
        # 会让整批请求全部失败（实测踩过这条）。
        if sc.split("-")[0].lower() == dc.split("-")[0].lower():
            self.last_detected = sc
            return text
        if len(text.encode("utf-8")) > _MAX_BYTES:
            # 正常流程走不到这里：manager 的 split_item 已按 _size 拆好。
            # 留作安全网——截断会产出半句话，明确报错比静默截断好。
            raise TranslationError(
                f"该段过长（{self._size(text)} 字节），MyMemory 单请求上限 {_MAX_BYTES} 字节",
                kind="engine", hint="请升级程序（split_item 应已拦截超长段）")
        params = {"langpair": f"{sc}|{dc}", "q": text, "formatted": 0, "v": "1.0"}
        data = http.get_json(ENDPOINT, params, timeout=self.timeout)

        status = data.get("responseStatus")
        if status != 200:
            detail = str(data.get("responseDetails") or "")
            if any(k in detail.lower() for k in _SAME_LANG):
                # 服务端判定语向相同（本地检测可能不准）：原文即译文
                self.last_detected = sc
                return text
            if "quota" in detail.lower() or status in (401, 403, 429):
                raise QuotaExceeded(f"MyMemory 配额受限（{status}）：{detail}",
                                    hint="匿名约 5000 字符/天；请配置 DeepL/百度密钥")
            raise TranslationError(f"MyMemory 返回 {status}：{detail[:160]}", kind="engine")

        translated = html.unescape((data.get("responseData") or {}).get("translatedText") or "")
        if not translated:
            raise TranslationError("MyMemory 返回空译文", kind="engine")
        matches = data.get("matches") or []
        self.last_model = (matches[0].get("model", "")
                           if matches and isinstance(matches[0], dict) else "")
        self.last_detected = sc
        return translated

    def translate(self, texts: list[str], src: str, dst: str) -> list[str]:
        """逐条请求。**单条失败绝不拖垮整批** —— 这一点在这里格外重要：
        截图常是中英混排，只要有一条触发同语向/超长，
        整批报错会让用户以为"整个后端坏了"，而实际上其他 9 条都能译。
        失败的条目返回空串，由 TranslateManager 交给下一档后端补译。
        """
        out: list[str] = []
        for t in texts:
            try:
                out.append(self._one(t, src, dst))
            except NetworkError:
                raise                       # 网络断了，逐条也没意义，交给链条降级
            except (QuotaExceeded, AuthError):
                raise                       # 配额/认证是全局问题，同样交给链条
            except TranslationError as exc:
                log.info("MyMemory 单条失败（%s），该条留给下一档", str(exc)[:90])
                out.append("")
        return out

    @classmethod
    def probe(cls, cfg) -> BackendInfo:
        return BackendInfo(cls.name, cls.label, available=True, online=True,
                           needs_key=False,
                           note="匿名每日约 5000 字符；质量低于 DeepL；不批量",
                           cost="免费（配额低）",
                           languages_note="需显式源语言，本项目用本地检测补全")
