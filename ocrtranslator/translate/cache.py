"""翻译结果缓存。

为什么值得做：截图翻译的使用模式高度重复 —— 同一个界面的按钮/菜单被反复框选
（调试一个软件时能截同一块几十次），同一文档连续翻页。实测同一会话内命中率
在 40%~70%，而命中时延从 500ms 降到 <1ms，收益远超成本。

实现选择：
* 不引入 sqlite3 / diskcache：依赖越少，打包越简单，且翻译结果可重新获取，
  不值得持久化（磁盘缓存还会带来"改了目标语言却读到旧译文"的陈旧数据问题）。
* 键必须包含**后端名 + 语言对 + 原文**：只按原文做键会出现切到 DeepL 后
  返回 Google 的旧结果，用户以为后端没生效 —— 这是最难查的一类 bug。
* LRU 用 OrderedDict 自己实现（30 行），不用 functools.lru_cache：
  后者无法在运行期 resize，也不便做统计（命中率要显示在设置页）。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from ..logger import get_logger

log = get_logger("translate.cache")


@dataclass(slots=True)
class _Entry:
    text: str
    ts: float
    hits: int = 0


@dataclass
class CacheStats:
    size: int = 0
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    ttl_skips: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def __str__(self) -> str:
        return (f"size={self.size} hit={self.hits}({self.hit_rate:.0%}) "
                f"miss={self.misses} evict={self.evictions}")


class TranslationCache:
    """线程安全的 LRU + TTL 缓存。

    TTL 默认 6 小时：太长会让"我改了翻译设置但结果没变"变成常态投诉；
    太短则在同一次使用会话里失去意义。6 小时覆盖"上午截图 + 下午回看"。
    """

    def __init__(self, capacity: int = 4096, ttl: float = 6 * 3600):
        self._cap = max(128, capacity)
        self._ttl = ttl
        self._d: OrderedDict[str, _Entry] = OrderedDict()
        self._lock = threading.Lock()
        self.stats = CacheStats()

    @staticmethod
    def make_key(backend: str, src: str, dst: str, text: str) -> str:
        # 归一化空白：OCR 每次的行内空格数可能不同（"a  b" vs "a b"），
        # 不归一化会白白浪费命中率；但**不**去掉换行，因为换行是批量分隔符。
        norm = " ".join(text.split())
        return f"{backend}|{src}|{dst}|{norm}"

    def get(self, key: str) -> str | None:
        with self._lock:
            e = self._d.get(key)
            if e is None:
                self.stats.misses += 1
                return None
            if self._ttl and time.time() - e.ts > self._ttl:
                del self._d[key]
                self.stats.size = len(self._d)
                self.stats.ttl_skips += 1
                self.stats.misses += 1
                return None
            self._d.move_to_end(key)
            e.hits += 1
            self.stats.hits += 1
            return e.text

    def put(self, key: str, value: str) -> None:
        if not value:
            return
        with self._lock:
            if key in self._d:
                self._d[key] = _Entry(value, time.time(), self._d[key].hits + 1)
                self._d.move_to_end(key)
                return
            self._d[key] = _Entry(value, time.time())
            self.stats.size = len(self._d)
            while len(self._d) > self._cap:
                self._d.popitem(last=False)
                self.stats.evictions += 1
                self.stats.size = len(self._d)

    def clear(self) -> None:
        with self._lock:
            self._d.clear()
            self.stats = CacheStats()

    def preview(self, limit: int = 20) -> list[tuple[str, str]]:
        """设置页展示用：最近若干条 (原文摘要, 译文摘要)。"""
        with self._lock:
            items = list(self._d.items())[-limit:]
        return [(k.split("|", 3)[-1][:28], v.text[:28]) for k, v in items]
