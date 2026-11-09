"""百度翻译开放平台后端（国内网络可达性最好的一档）。

实测：本机 `fanyi-api.baidu.com` TCP 可直连（Google 端点超时），
所以对中国大陆用户它常常是**唯一能用的在线后端**。

签名算法（官方文档；写错就是 54001 sign error，最难查）：
    salt = 随机串
    sign = MD5(appid + q + salt + key)
关键点：
  * `q` 参与签名时必须是**原文**，发送时由 urlencode 编码 —— 两边必须是同一个
    变量，手写两次字符串拼接就会出现"签名时对、发送时错"的隐性不一致。
  * 中文原文必须按 UTF-8 取 MD5（`hashlib.md5(str.encode("utf-8"))`），
    在某些中文 Windows 默认 GBK 的机器上，漏写 encoding 会直接 54001。
  * 错误码在**HTTP 200 的包体**里（error_code 字段），不是 HTTP 状态码。
    只看 raise_for_status 会以为"成功了但译文为空"。
  * 54003/54004 是并发与频率限制，必须归到 quota 触发降级，
    而不是弹"配置有误"（用户会去反复重填密钥，其实什么都没坏）。
"""

from __future__ import annotations

import hashlib
import random
import time

from ..languages import tr_code
from ..logger import get_logger
from . import http
from .base import (AuthError, BackendInfo, NetworkError, QuotaExceeded,
                   TranslationError, TranslatorBackend)

log = get_logger("translate.baidu")

_ERR_HINT = {
    "52001": "百度翻译网络超时",
    "52002": "appid 错误",
    "52003": "翻译账户密钥错误",
    "54000": "必填参数为空",
    "54001": "签名错误（sign）",
    "54002": "IP 访问频率受限",
    "54003": "账户流量不足或并发超限",
    "54004": "账号冻结",
    "54005": "服务不支持当前语言",
    "58000": "客户端 IP 未绑定 appid",
}
_AUTH_CODES = {"52002", "52003", "54001", "58000", "54004"}
_QUOTA_CODES = {"54002", "54003"}


class BaiduBackend(TranslatorBackend):
    name = "baidu"
    label = "百度翻译开放平台"
    online = True
    needs_key = True
    supports_batch = False     # 一次一个 q；拼接会破坏签名语义
    max_chars = 5800           # 官方单次 6000 字节，留余量（单位 = UTF-8 字节，见 _size）

    def __init__(self, cfg, creds=None):
        super().__init__(cfg, creds)
        self.appid = self.secret("baidu_appid")
        self.key = self.secret("baidu_key")

    def _sign(self, q: str, salt: str) -> str:
        raw = f"{self.appid}{q}{salt}{self.key}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def _size(self, text: str) -> int:
        """官方上限按字节计，中文一字 3 字节；按字符算预算会超限。"""
        return len(text.encode("utf-8"))

    def _one(self, text: str, src: str, dst: str) -> str:
        if not self.appid or not self.key:
            raise AuthError("未配置百度翻译密钥",
                            hint="设置页填 appid 与密钥，或设环境变量 BAIDU_APPID/BAIDU_KEY")
        if self._size(text) > self.max_chars:
            # 安全网：manager 的 split_item 已按字节预算拆好，正常走不到
            raise TranslationError(f"该段超过百度单次 {self.max_chars} 字节上限",
                                   kind="engine", hint="请升级程序（split_item 应已拦截）")
        salt = str(random.randint(10000, 99999))
        params = {
            "appid": self.appid,
            "q": text,                                  # 签名与发送共用这一个值
            "from": tr_code(src, "baidu") or "auto",
            "to": tr_code(dst, "baidu"),
            "salt": salt,
            "sign": self._sign(text, salt),
        }
        for attempt in range(2):
            try:
                data = http.get_json(self.cfg.baidu_endpoint, params, timeout=self.timeout)
            except QuotaExceeded as exc:
                time.sleep(1.2 * (attempt + 1))
                log.warning("百度限流，重试（%d/2）：%s", attempt + 1, exc)
                continue
            except NetworkError:
                raise
            else:
                return self._extract(data)
        raise QuotaExceeded("百度连续限流", hint="免费标准版 5 万字符/月，去控制台看用量")

    def _extract(self, data: dict) -> str:
        code = str(data.get("error_code", ""))
        if code:
            self._raise_for(code, str(data.get("error_msg", "")))
        trans = data.get("trans_result") or []
        if not trans:
            raise TranslationError("百度返回空 trans_result", kind="engine")
        self.last_detected = data.get("from", "") or ""
        return "\n".join(t.get("dst", "") for t in trans)

    @staticmethod
    def _raise_for(code: str, msg: str) -> None:
        hint = _ERR_HINT.get(code, "见百度翻译开放平台错误码表")
        if code in _AUTH_CODES:
            raise AuthError(f"百度翻译配置有误（{code}）：{hint} {msg}".strip(),
                            hint="重新填 appid/密钥；注意普通版与高级版的密钥不通用")
        if code in _QUOTA_CODES:
            raise QuotaExceeded(f"百度配额/频率限制（{code}）：{hint}")
        if code == "54005":
            raise TranslationError(f"百度不支持该语言对（{code}）", kind="engine",
                                   hint="该接口语种少于 Google；换后端或在 languages.py 补映射")
        raise TranslationError(f"百度翻译错误 {code}：{hint} {msg}".strip(),
                               kind="engine", retryable=code == "52001")

    def translate(self, texts: list[str], src: str, dst: str) -> list[str]:
        out: list[str] = []
        for t in texts:
            try:
                out.append(self._one(t, src, dst))
            except (NetworkError, AuthError, QuotaExceeded):
                raise
            except TranslationError as exc:
                log.info("百度单条失败（%s），该条留给下一档", str(exc)[:90])
                out.append("")
        return out

    @classmethod
    def probe(cls, cfg) -> BackendInfo:
        appid, key = _ids_of(cfg)
        ok = bool(appid and key)
        return BackendInfo(cls.name, cls.label, available=ok, online=True, needs_key=True,
                           note="国内直连可用；月 5 万字符免费额度" if ok
                                else "需要 appid + 密钥",
                           cost="免费额度内 $0",
                           languages_note="约 200 种语言")


def _ids_of(cfg) -> tuple[str, str]:
    if hasattr(cfg, "baidu_ids"):
        try:
            return cfg.baidu_ids
        except Exception:
            pass
    inner = getattr(cfg, "translate", cfg)
    return (str(getattr(inner, "baidu_appid", "") or ""),
            str(getattr(inner, "baidu_key", "") or ""))
