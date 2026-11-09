"""共享 HTTP 原语：只用 urllib，不依赖 requests。

为什么不用 requests：
  1. 本机实测 `import requests` 直接炸（`No module named 'urllib3'`）——
     Anaconda 环境里 requests 与 urllib3 版本撕扯很常见；
  2. 打包体积：requests+urllib3+certifi+idna ~1.5MB，而这里只需要
     GET/POST 表单/JSON 解析三件事，urllib 全够用；
  3. 依赖越少，`fetch_wheels.py`/PyInstaller 的故障面越小。

统一在这里做，是为了把**错误分型**收在一处：
    URLError/超时 -> NetworkError（触发降级链跳过其余在线后端）
    429/403 配额文案 -> QuotaExceeded
    401/403 认证文案 -> AuthError
    5xx -> 可重试的 TranslationError
各后端只看语义，不再各自复制一遍 isinstance 判断。
"""

from __future__ import annotations

import json
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from ..logger import get_logger
from .base import AuthError, NetworkError, QuotaExceeded, TranslationError

log = get_logger("translate.http")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 socrans/1.0")

#: host -> (可达时刻, 不可达时刻)。被墙/防火墙 DROP 的域名，
#: TCP 握手不会立刻失败，而是挂到超时 —— 实测本机 Google 端点挂满 12 秒。
#: 用户第一次按热键等 14 秒是不可接受的，所以先用 2 秒探一次 TCP：
#: 探不通就当作不可达，省掉整个读超时。
_PROBE_TTL_OK = 600.0
_PROBE_TTL_BAD = 60.0
_probe_cache: dict[str, tuple[float, bool]] = {}
_probe_lock = threading.Lock()
CONNECT_PROBE_SECONDS = 2.0


def reachable(url: str) -> bool:
    """便宜的连通性预检。**只在失败时抛 NetworkError**，成功则静默返回。"""
    host = urllib.parse.urlsplit(url).hostname or ""
    port = urllib.parse.urlsplit(url).port or 443
    if not host:
        return True
    now = time.monotonic()
    with _probe_lock:
        hit = _probe_cache.get(host)
        if hit:
            ts, ok = hit
            ttl = _PROBE_TTL_OK if ok else _PROBE_TTL_BAD
            if now - ts < ttl:
                if not ok:
                    raise NetworkError(f"{host} 近期不可达（已缓存判定）",
                                       hint="该端点可能被防火墙或网络策略拦截；"
                                            "工具会自动改用其他后端")
                return True
    try:
        with socket.create_connection((host, port), timeout=CONNECT_PROBE_SECONDS):
            ok = True
    except OSError:
        ok = False
    with _probe_lock:
        _probe_cache[host] = (time.monotonic(), ok)
    if not ok:
        raise NetworkError(
            f"{host} 在 {CONNECT_PROBE_SECONDS:.0f} 秒内无法建立连接",
            hint="该端点被拦截（本机实测 Google 网页端点即如此）；"
                 "已跳过并将改用其他后端")
    return True


def forget_host(url: str) -> None:
    """请求实际成功了但预检曾判不可达时清掉记录，避免误判固化。"""
    host = urllib.parse.urlsplit(url).hostname or ""
    with _probe_lock:
        _probe_cache.pop(host, None)


def clear_probe_cache() -> None:
    with _probe_lock:
        _probe_cache.clear()


def get_json(url: str, params: dict | None = None, timeout: float = 12.0,
             headers: dict | None = None):
    full = url + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(full, headers={"User-Agent": UA, **(headers or {})})
    return _request(req, timeout, url)


def post_form(url: str, pairs: list[tuple[str, str]], timeout: float = 12.0,
              headers: dict | None = None):
    """表单 POST。list[(k,v)] 而不是 dict：数组参数（DeepL 的 text[]）需要重复键。"""
    body = urllib.parse.urlencode(pairs).encode("utf-8")
    hdrs = {"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded",
            **(headers or {})}
    req = urllib.request.Request(url, data=body, method="POST", headers=hdrs)
    return _request(req, timeout, url)


def _request(req: urllib.request.Request, timeout: float, where: str):
    url = req.full_url
    # 预检放在真正请求前：被 DROP 的域名不会立刻报错，而是挂满 timeout。
    reachable(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
        forget_host(url)          # 通了就忘掉"不可达"的旧判定，避免误判固化
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        _classify_http(exc.code, detail, where)
    except socket.timeout as exc:
        raise NetworkError(f"{where} 读取超时（{timeout:.0f}s）",
                           hint="网络太慢或被中间设备限速；稍后重试或改用离线后端") from exc
    except ssl.SSLError as exc:
        raise NetworkError(f"{where} TLS 握手失败（{exc.__class__.__name__}）",
                           hint="常见于企业网关做 SSL 拦截；需要安装企业根证书") from exc
    except (urllib.error.URLError, ConnectionError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        name = reason.__class__.__name__ if isinstance(reason, Exception) else str(reason)
        raise NetworkError(f"{where} 连接失败（{name}）",
                           hint="该端点不可达：可能被墙/防火墙拦截；"
                                "工具会自动降级到可达的后端") from exc
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError) as exc:
        snippet = raw[:160].decode("utf-8", "replace")
        raise TranslationError(
            f"{where} 返回了非 JSON 内容", kind="engine",
            hint=f"常见原因是登录网关/代理拦截。前 160 字节：{snippet!r}") from exc


def _classify_http(code: int, detail: str, where: str) -> None:
    low = detail.lower()
    # "两个语言必须不同"是**参数错**，不是配额：MyMemory 在 zh→zh 时返回它。
    # 早先按 403 一律归到 QuotaExceeded，会让降级链以为是配额问题、
    # 提示用户"稍后再试"，而真实原因是语向相同（应该直接原文返回）。
    if "distinct" in low or "same language" in low:
        raise TranslationError(f"{where}：源语言与目标语言相同", kind="engine",
                               hint="这种情况应由调用方按『原文即译文』处理")
    if code in (401, 403):
        if "auth" in low or "key" in low or "invalid" in low:
            raise AuthError(f"{where} HTTP {code}: {detail[:160]}",
                            hint="密钥无效或账户受限")
        if "quota" in low or "limit" in low or "exceed" in low:
            raise QuotaExceeded(f"{where} HTTP {code}: {detail[:160]}")
        raise TranslationError(f"{where} HTTP {code}: {detail[:160]}", kind="engine")
    if code == 429:
        raise QuotaExceeded(f"{where} 限流（HTTP 429）", hint="稍后重试，或改用其他后端")
    if code >= 500:
        raise TranslationError(f"{where} 服务端错误 HTTP {code}", kind="engine",
                               retryable=True)
    raise TranslationError(f"{where} HTTP {code}: {detail[:160]}", kind="engine")
