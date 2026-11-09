"""DeepL 翻译后端（免费账户 API）。

为什么值得写：截图翻译的用户里"看懂英文软件界面/论文"占绝大多数，
DeepL 的中英质量明显优于 Google，而免费账户每月 50 万字符
（约等于每天框选几百次）对单人桌面工具足够。

必须按 API 文档处理的真实约束：
  * 免费端点是 `api-free.deepl.com`，付费是 `api.deepl.com`，用错直接 403。
    官方约定免费 key 以 `:fx` 结尾，本后端据此自动选端点。
  * 源语言只接受 ZH/EN/ES/FR/IT/DE/PT/PL/RU/NL/SV 或 auto；
    目标语言不接受区域变体（`ZH-CN` 会 400）—— 映射统一在 languages.py。
  * `text` 是**数组参数**，必须展开成重复键（text=a&text=b）。
    拼成 `text=a\nb` 会被当成一整段，返回条数就变成 1 —— 对照会整体错位。
    一次最多 50 段、总字符 5000（免费档），超了 413，所以两个上限同时分批。
  * `translations` 与输入**严格同序等长**；一旦不等必须整批丢弃。
  * 429 常见：退避重试并尊重 `Retry-After`。
"""

from __future__ import annotations

import time

from ..languages import tr_code
from ..logger import get_logger
from . import http
from .base import (AuthError, BackendInfo, NetworkError, QuotaExceeded,
                   TranslationError, TranslatorBackend)

log = get_logger("translate.deepl")

_MAX_CHARS_FREE = 4500      # 官方 5000，留余量给多字节与控制字符
_MAX_SEGS = 50


class DeepLBackend(TranslatorBackend):
    name = "deepl"
    label = "DeepL（质量最佳）"
    online = True
    needs_key = True
    supports_batch = True

    @property
    def key(self) -> str:
        return self.secret("deepl")

    # ---------------------------------------------------------------- 端点
    @property
    def endpoint(self) -> str:
        base = self.cfg.deepl_endpoint or "https://api-free.deepl.com/v2/translate"
        free = self.key.endswith(":fx")
        if free and "api-free" not in base:
            base = base.replace("api.deepl.com", "api-free.deepl.com")
        elif not free and "api-free" in base:
            base = base.replace("api-free.deepl.com", "api.deepl.com")
        return base

    # ---------------------------------------------------------------- 请求
    def _post(self, texts: list[str], src: str, dst: str) -> dict:
        if not self.key:
            raise AuthError("未配置 DeepL 密钥",
                            hint="设置页填 Auth Key，或设环境变量 DEEPL_API_KEY")
        pairs: list[tuple[str, str]] = [("text", t) for t in texts]
        pairs.append(("target_lang", tr_code(dst, "deepl")))
        if src != "auto":
            pairs.append(("source_lang", tr_code(src, "deepl")))
        pairs += [("split_sentences", "nonewlines")]

        hdrs = {"Authorization": f"DeepL-Auth-Key {self.key}"}
        last: TranslationError | None = None
        for attempt in range(3):
            try:
                return http.post_form(self.endpoint, pairs,
                                      timeout=self.timeout, headers=hdrs)
            except QuotaExceeded as exc:
                # 413 也可能是配额耗尽（免费账户超 50 万字符/月），文案里带 quota 时提示查用量
                wait = 1.5 * (2 ** attempt)
                log.warning("DeepL 限流/配额，%.1fs 后重试（%d/3）：%s", wait, attempt + 1, exc)
                time.sleep(wait)
                last = exc
            except NetworkError:
                raise
            except AuthError:
                raise          # 密钥错重试三次也是错，立刻让链条换后端
            except TranslationError as exc:
                if exc.retryable and attempt < 2:
                    time.sleep(0.8 * (attempt + 1))
                    last = exc
                    continue
                raise
        raise last or QuotaExceeded("DeepL 请求失败")

    # ---------------------------------------------------------------- 翻译
    def translate(self, texts: list[str], src: str, dst: str) -> list[str]:
        out: list[str] = []
        for batch in self._batch(texts):
            data = self._post(batch, src, dst)
            got = data.get("translations") or []
            if len(got) != len(batch):
                # 条数不等 -> 对照会整列错位，必须拒绝而不是"尽力而为"地截取
                raise TranslationError(
                    f"DeepL 返回条数不符（{len(got)} != {len(batch)}）",
                    kind="engine", hint="已丢弃该批以避免原文/译文错位")
            out.extend(t.get("text", "") or "" for t in got)
            det = next((t.get("detected_source_language", "") for t in got
                        if t.get("detected_source_language")), "")
            if det:
                self.last_detected = det
        return out

    @staticmethod
    def _batch(texts: list[str]) -> list[list[str]]:
        batches: list[list[str]] = []
        cur: list[str] = []
        size = 0
        for t in texts:
            if cur and (size + len(t) > _MAX_CHARS_FREE or len(cur) >= _MAX_SEGS):
                batches.append(cur)
                cur, size = [], 0
            cur.append(t)
            size += len(t)
        if cur:
            batches.append(cur)
        return batches

    # ---------------------------------------------------------------- 用量
    def usage(self) -> str:
        if not self.key:
            return "未配置密钥"
        url = self.endpoint.rsplit("/translate", 1)[0] + "/usage"
        try:
            d = http.get_json(url, timeout=self.timeout,
                              headers={"Authorization": f"DeepL-Auth-Key {self.key}"})
            used, lim = int(d.get("character_count", 0)), int(d.get("character_limit", 0))
            self._usage_remaining = max(0, lim - used)
            return f"已用 {used:,} / {lim:,} 字符"
        except Exception as exc:
            return f"用量查询失败（{exc.__class__.__name__}）"

    @classmethod
    def probe(cls, cfg) -> BackendInfo:
        key = _key_of(cfg)
        return BackendInfo(cls.name, cls.label, available=bool(key), online=True,
                           needs_key=True,
                           note="中英质量最佳；免费账户 50 万字符/月" if key
                                else "需要 Auth Key（设置页或 DEEPL_API_KEY）",
                           cost="免费额度内 $0",
                           languages_note="约 30 种语言，源语言可 auto")


def _key_of(cfg) -> str:
    """兼容三种调用形态：AppConfig / TranslateSettings / 已解出的明文 key。"""
    if hasattr(cfg, "secret"):
        try:
            return cfg.secret("deepl") or ""
        except Exception:
            pass
    inner = getattr(cfg, "translate", cfg)
    return str(getattr(inner, "deepl_auth_key", "") or "")
