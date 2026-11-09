"""翻译后端公共约定。

对上层只暴露一件事：`translate(texts, src, dst) -> list[str]`（保序、等长）。
所有后端都必须遵守：**输入 N 条则输出 N 条**，条数不匹配时后端自己补齐或抛错，
绝不允许返回长度不同的列表 —— 双语对照一旦错位，UI 上的对应关系就全错了，
比翻译质量差更严重。
"""

from __future__ import annotations

import abc
import re
from dataclasses import dataclass


class TranslationError(RuntimeError):
    def __init__(self, msg: str, *, kind: str = "engine", hint: str = "", retryable: bool = False):
        super().__init__(msg)
        self.kind = kind          # engine | network | quota | auth | offline
        self.hint = hint
        self.retryable = retryable


class QuotaExceeded(TranslationError):
    def __init__(self, msg: str, hint: str = ""):
        super().__init__(msg, kind="quota", hint=hint, retryable=False)


class AuthError(TranslationError):
    def __init__(self, msg: str, hint: str = ""):
        super().__init__(msg, kind="auth", hint=hint, retryable=False)


class NetworkError(TranslationError):
    def __init__(self, msg: str, hint: str = ""):
        super().__init__(msg, kind="network", hint=hint, retryable=True)


@dataclass(slots=True)
class BackendInfo:
    name: str
    label: str
    available: bool
    online: bool
    needs_key: bool = False
    note: str = ""
    cost: str = ""              # 免费 / 按字符 / 本地推理
    languages_note: str = ""

    def __str__(self) -> str:
        s = self.label
        if not self.available:
            s += "（不可用）"
        elif self.needs_key:
            s += "（需密钥）"
        return f"{s} - {self.note}" if self.note else s


class TranslatorBackend(abc.ABC):
    name = "base"
    label = "Base"
    online = True
    needs_key = False
    #: 单次请求可接受的字符上限，超出自动分批
    max_chars = 4500
    #: 是否支持批量（不支持的后端逐条调用即可）
    supports_batch = True

    def __init__(self, cfg, creds: dict[str, str] | None = None):
        """`cfg` 可以是 AppConfig 或它的 translate 段；`creds` 由 manager 注入。

        为什么要单独传 creds：段对象**不知道自己属于哪个 AppConfig**，
        而 `deepl_key` / `baidu_ids` 这两个 property 定义在 AppConfig 上
        （它们要实现"环境变量优先于配置文件"）。早先后端里写
        `self.cfg.deepl_key` 是拿不到的（属性在上一级），只能落到明文配置项，
        于是"环境变量优先"这条规则在 DeepL 后端里静默失效。
        现在由 manager 统一取一次传进来，规则只在一处生效。
        """
        self.app = cfg                      # 传进来的原始对象（可能是 AppConfig）
        self.cfg = cfg.translate if hasattr(cfg, "translate") else cfg
        self._creds = dict(creds or {})
        self.timeout = float(getattr(self.cfg, "timeout", 12.0) or 12.0)
        self.last_detected = ""
        self._usage_remaining: int | None = None

    def secret(self, kind: str) -> str:
        """取密钥。manager 注入值优先，其余全部交给 AppConfig.secret()。

        规则（环境变量 > 混淆配置）只在 config.py 一处实现，
        后端不再各自 `hasattr` 猜测 —— 之前 `self.cfg.deepl_key` 拿不到值
        就是因为 property 定义在 AppConfig 上而 self.cfg 是它的 translate 段。
        """
        if self._creds.get(kind):
            return self._creds[kind]
        if hasattr(self.app, "secret"):
            try:
                return self.app.secret(kind) or ""
            except Exception:
                pass
        return str(getattr(self.cfg, f"{kind}_key", "") or "")

    @abc.abstractmethod
    def translate(self, texts: list[str], src: str, dst: str) -> list[str]:
        """批量翻译。必须返回与 texts 等长的列表。"""

    @classmethod
    @abc.abstractmethod
    def probe(cls, cfg) -> BackendInfo:
        """探测可用性（不发实际请求，或只做一次极轻量的 HEAD）。"""

    def close(self) -> None:
        pass

    # ---- 子类可复用
    def _chunks(self, texts: list[str]) -> list[list[str]]:
        """按字符预算把待译列表切成批。

        批处理是提速关键（10 段文字：10 次 RTT -> 1 次），但把 100 段拼成
        一个超长请求会让服务端截断/降质，所以要设预算。分隔符用 \\n，
        因为 Google/DeepL 都保留换行结构。预算单位用 `_size`（字节限后端
        覆写成 UTF-8 字节数），否则中文按 1 字符计入、实际占 3 字节。
        """
        batches: list[list[str]] = []
        cur: list[str] = []
        size = 0
        for t in texts:
            add = self._size(t) + 1
            if cur and size + add > self.max_chars:
                batches.append(cur)
                cur, size = [], 0
            cur.append(t)
            size += add
        if cur:
            batches.append(cur)
        return batches

    # ---- 单条超长拆分 ------------------------------------------------------
    # 修的是真实故障（2026-11-09 用户截图）：MyMemory 单请求上限 480 字节，
    # OCR 块经常是几百字节的中英混排长段。原先只做多条合批，单条超长直接
    # 失败，而后端逐条失败只返回空串（不算错误），manager 里就没有任何一档
    # 后端"报过错"，用户只看到一句笼统的"全部后端失败"。
    _SENT_RE = re.compile(r".+?[。！？!?；;\n]+|.+?\Z", re.S)
    _CLAUSE_RE = re.compile(r".+?[，,、：:]+|[^\s，,、：:]+(?:\s+|\Z)|.+\Z", re.S)

    def _size(self, text: str) -> int:
        """拆分/合批预算的度量单位。默认字符数；按字节限流的后端
        （MyMemory 500 字节、百度 6000 字节）必须覆写成 UTF-8 字节数。
        """
        return len(text)

    def split_item(self, text: str) -> list[str]:
        """把超过单请求预算的一条拆成多段，保证 `"".join(结果) == text`。

        三级策略：句子（。！？!?；换行，标点归前段）→ 子句（逗号/顿号/
        冒号/空格）→ 按预算硬切（逐字符累计 `_size`，绝不切碎一个字符）。
        拼接无损的原因：切分点符号都留在前一段末尾，没有引入也没有吃掉
        任何字符。最后把相邻小段贪心合并，避免把一段话拆成十几个碎请求。
        """
        budget = self.max_chars
        if self._size(text) <= budget:
            return [text]
        pieces: list[str] = []
        for sent in self._greedy(text, self._SENT_RE):
            if self._size(sent) <= budget:
                pieces.append(sent)
                continue
            for clause in self._greedy(sent, self._CLAUSE_RE):
                if self._size(clause) <= budget:
                    pieces.append(clause)
                else:
                    pieces.extend(self._hard(clause, budget))
        return self._merge(pieces, budget)

    def _greedy(self, text: str, rex: re.Pattern) -> list[str]:
        """按正则切成"原子单元"后贪心装箱：能塞进当前段就塞，塞不下另起一段。
        单个单元自身超预算时不切（交给下一级策略处理）。
        """
        out: list[str] = []
        cur = ""
        for m in rex.finditer(text):
            u = m.group(0)
            if cur and self._size(cur + u) > self.max_chars:
                out.append(cur)
                cur = u
            else:
                cur += u
        if cur:
            out.append(cur)
        return out

    def _hard(self, text: str, budget: int) -> list[str]:
        """最后的兜底：无任何标点/空格可切时按预算硬切，逐字符累计 `_size`，
        保证字节限后端不会切出半个多字节字符。
        """
        out: list[str] = []
        cur = ""
        acc = 0
        for ch in text:
            sz = self._size(ch)
            if cur and acc + sz > budget:
                out.append(cur)
                cur, acc = "", 0
            cur += ch
            acc += sz
        if cur:
            out.append(cur)
        return out

    def _merge(self, pieces: list[str], budget: int) -> list[str]:
        """相邻片段能合则合（在预算内），减少请求数与译文接缝。"""
        out: list[str] = []
        for p in pieces:
            if out and self._size(out[-1] + p) <= budget:
                out[-1] += p
            else:
                out.append(p)
        return out


def join_batch(texts: list[str]) -> str:
    return "\n".join(texts)


def split_batch(reply: str, n: int) -> list[str] | None:
    """把批量响应拆回 n 条。拆不出来返回 None，调用方改逐条请求。

    这是整个翻译层最容易出错的点：服务端会把换行合并、丢掉空行、
    或在译文里插入额外换行。因此**只有行数恰好等于 n 才接受**，
    任何不一致都视为批量失败 —— 宁可慢，也不能错位。
    """
    lines = reply.split("\n")
    if len(lines) == n:
        return [ln.strip() for ln in lines]
    # 尾部空行是常见噪声：去掉后再试一次
    while lines and not lines[-1].strip():
        lines.pop()
    if len(lines) == n:
        return [ln.strip() for ln in lines]
    return None
