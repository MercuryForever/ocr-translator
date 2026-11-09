"""Google 翻译（免费 web 端点，非官方 Cloud API）。

选它做**默认在线后端**的理由：零配置、零密钥、中文质量好、支持批量。
诚实说明代价（README 同样写明）：
  * `translate.googleapis.com/translate_a/single?client=gtx` 是网页内部接口，
    官方不保证稳定、可能限流。生产工具应把它当"可用则用"的一档，
    必须有别的后端兜底 —— 这正是本项目降级链存在的动机。
  * **本机实测该端点 TCP 超时**（被网络侧拦截），而 pypi/baidu/deepl/mymemory
    都通。也就是说"排在第一位的后端在第一台机器上就失败了"是常态，
    不是假设。测试必须覆盖这条路径。
  * 若要 SLA，把 `google_endpoint` 换成 Cloud Translation v2 并填 key：
    返回结构不同，但差异全部隔离在 `_parse` 里，改动只在那一处。

响应结构（gtx 经典形态）：
    [[["译文","原文",null,null,10]], null, "检测到的语言", ...]
批量时服务端把多个输入拼成一个响应，片段数**不一定等于输入条数**
（它可能重排换行）。因此这里用换行分隔输入、按换行拆分输出，
拆不整齐就退化为逐条请求 —— 宁可慢，不能错位。

不用 requests：本机 `import requests` 因缺 urllib3 直接失败，
而 urllib 完全够用（见 translate/http.py 的说明）。
"""

from __future__ import annotations

import random
import time

from ..languages import tr_code
from ..logger import get_logger
from . import http
from .base import (BackendInfo, NetworkError, QuotaExceeded, TranslationError,
                   TranslatorBackend, join_batch, split_batch)

log = get_logger("translate.google")


class GoogleBackend(TranslatorBackend):
    name = "google"
    label = "Google Translate（免费网页端点）"
    online = True
    needs_key = False
    max_chars = 4500

    def __init__(self, cfg, creds=None):
        super().__init__(cfg, creds)
        self._retries = 3
        self._backoff = 0.6

    # ---------------------------------------------------------------- 解析
    @staticmethod
    def _parse(payload) -> tuple[str, str]:
        """返回 (整段译文, 检测到的源语言)。

        gtx 的 payload[0] 可能含多个片段（服务端自己做的分句），
        必须全部按顺序拼接 —— 只取 [0][0] 会丢后半句，
        这是所有 gtx 封装最常见的 bug。
        """
        if not isinstance(payload, list) or not payload or not isinstance(payload[0], list):
            raise TranslationError(f"Google 响应结构异常：{type(payload).__name__}",
                                   kind="engine",
                                   hint="端点可能已改版；试试设置页换后端")
        segs = [item[0] for item in payload[0]
                if isinstance(item, list) and item and isinstance(item[0], str)]
        if not segs:
            raise TranslationError("Google 响应里没有译文片段", kind="engine")
        detected = payload[2] if len(payload) > 2 and isinstance(payload[2], str) else ""
        return "".join(segs), detected

    def _one(self, text: str, src: str, dst: str) -> str:
        params = {
            "client": "gtx",
            "sl": tr_code(src, "google") or "auto",
            "tl": tr_code(dst, "google"),
            "dt": "t",          # 只要译文；不带 t 时服务端可能不返回可拆分的片段
            "q": text,
        }
        last: TranslationError | None = None
        for attempt in range(self._retries):
            try:
                data = http.get_json(self.cfg.google_endpoint, params, timeout=self.timeout)
            except QuotaExceeded as exc:
                wait = self._backoff * (2 ** attempt) + random.uniform(0, 0.25)
                log.warning("Google 限流，%.1fs 后重试（%d/%d）", wait, attempt + 1, self._retries)
                time.sleep(wait)
                last = exc
                continue
            except NetworkError:
                raise          # 连不上就立刻放弃：重试三次只会让截图多卡 20 秒
            except TranslationError as exc:
                if exc.retryable:
                    time.sleep(self._backoff * (attempt + 1))
                    last = exc
                    continue
                raise
            else:
                translated, detected = self._parse(data)
                self.last_detected = detected
                return translated
        raise last or TranslationError("Google 翻译失败", kind="engine")

    # ---------------------------------------------------------------- 批量
    def translate(self, texts: list[str], src: str, dst: str) -> list[str]:
        if not texts:
            return []
        out: list[str] = []
        for batch in self._chunks(texts):
            # 先试一次请求 + 换行拆分；拆不齐就逐条。条数一定对得上。
            try:
                reply = self._one(join_batch(batch), src, dst)
                pieces = split_batch(reply, len(batch))
                if pieces is not None and all(pieces):
                    out.extend(pieces)
                    continue
                log.debug("Google 批量拆分不齐（%d 条），退化为逐条", len(batch))
            except NetworkError:
                raise
            except TranslationError as exc:
                log.debug("Google 批量失败（%s），退化为逐条", str(exc)[:90])
            for t in batch:
                try:
                    out.append(self._one(t, src, dst))
                except TranslationError:
                    # 单条失败留空，交给降级链的下一档补译，不拖垮整批
                    out.append("")
        return out

    # ---------------------------------------------------------------- 探测
    @classmethod
    def probe(cls, cfg) -> BackendInfo:
        # 这里不做真实连通性测试：probe 会在启动与设置页被反复调用，
        # 一个 3 秒 TCP 超时会明显拖慢 UI。可达性交给降级链在后台验证。
        return BackendInfo(cls.name, cls.label, available=True, online=True,
                           needs_key=False,
                           note="零配置；网页端点，可能不可达或被限流",
                           cost="免费",
                           languages_note="约 130 种语言，支持自动检测")
