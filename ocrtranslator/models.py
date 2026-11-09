"""核心数据模型。

刻意不依赖 Qt：这些结构在 Worker 线程里构造、跨线程传递，纯 Python 对象
最安全（QtGui.QImage 虽可传递但共享隐式共享数据，跨线程读也不加锁）。
需要与 Qt 交互时，只在 UI 层用 ``to_qimage``/``from_qpixmap`` 边界转换。
"""

from __future__ import annotations

import enum
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any


class BlockKind(str, enum.Enum):
    """OCR 块的语义类型，决定 UI 图标与导出排版。

    继承 str 是为了能直接当字符串用（`b.kind == "text"` 成立、可 json 序列化），
    同时保留 `.value` 给导出层。用 enum 而不是裸 Literal 类型别名，是因为
    Literal 不可子类化（`class X(str, Literal)` 直接 TypeError）。
    """

    TEXT = "text"
    HEADING = "heading"
    CODE = "code"
    TABLE = "table"
    IGNORED = "ignored"

    @property
    def icon(self) -> str:
        return {"text": "¶", "heading": "H", "code": "</>",
                "table": "▦", "ignored": "·"}[self.value]


@dataclass(slots=True)
class OcrLine:
    """一行识别结果。坐标一律使用**逻辑像素**（已除以 DPR），与截图选区对齐。"""

    text: str
    conf: float
    box: tuple[float, float, float, float]  # x0, y0, x1, y1（轴对齐包围盒）
    poly: tuple[tuple[float, float], ...] = ()  # 四点，旋转文本用

    @property
    def height(self) -> float:
        return max(0.0, self.box[3] - self.box[1])

    @property
    def width(self) -> float:
        return max(0.0, self.box[2] - self.box[0])

    @property
    def center_y(self) -> float:
        return (self.box[1] + self.box[3]) / 2.0


@dataclass(slots=True)
class OcrBlock:
    """版面段落块：若干视觉相邻、语义连续的 OCR 行。"""

    kind: BlockKind
    lines: list[OcrLine]
    box: tuple[float, float, float, float]
    reading_order: int = 0

    @property
    def source(self) -> str:
        """块内原文，行内无空格拼接（CJK 友好），行间换行。"""
        return "\n".join(ln.text for ln in self.lines)

    @property
    def source_inline(self) -> str:
        """把块内多行折叠成一句：CJK 直接拼，拉丁文补空格。"""
        return join_lines([ln.text for ln in self.lines])

    @property
    def avg_conf(self) -> float:
        if not self.lines:
            return 0.0
        return sum(ln.conf for ln in self.lines) / len(self.lines)


@dataclass(slots=True)
class TranslationUnit:
    """双语对照的最小单元：一段原文 + 一段译文。"""

    block: OcrBlock
    target_text: str = ""
    engine: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.target_text) and not self.error

    @property
    def source_text(self) -> str:
        return self.block.source_inline


@dataclass(slots=True)
class Pairing:
    """OCR 行 → 译文行的**词级对齐**，用于结果窗口的行内高亮。"""

    src_line: int
    src_text: str
    src_box: tuple[float, float, float, float]
    tgt_spans: list[tuple[int, int]] = field(default_factory=list)  # 译文字符区间


@dataclass(slots=True)
class CaptureResult:
    """一次截图的完整产物（RGBA 字节 + 元数据）。"""

    width: int
    height: int
    bytes_bmp: bytes
    dpr: float = 1.0
    screen_name: str = ""
    region: tuple[int, int, int, int] = (0, 0, 0, 0)  # 全局逻辑坐标 x,y,w,h

    @property
    def digest(self) -> str:
        return hashlib.sha1(self.bytes_bmp).hexdigest()[:16]


@dataclass(slots=True)
class PipelineResult:
    """端到端结果，交给 UI 渲染 / 导出。"""

    capture: CaptureResult
    units: list[TranslationUnit]
    source_lang: str
    target_lang: str
    ocr_engine: str
    translate_engine: str
    elapsed_ms: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "meta": {
                "ocr": self.ocr_engine,
                "translate": self.translate_engine,
                "source_lang": self.source_lang,
                "target_lang": self.target_lang,
                "elapsed_ms": self.elapsed_ms,
                "region": list(self.capture.region),
                "dpr": self.capture.dpr,
                "digest": self.capture.digest,
            },
            "warnings": self.warnings,
            "pairs": [
                {
                    "kind": u.block.kind.value,
                    "source": u.source_text,
                    "target": u.target_text,
                    "conf": round(u.block.avg_conf, 4),
                    "box": list(u.block.box),
                    "error": u.error,
                    "lines": [
                        {"text": ln.text, "conf": round(ln.conf, 4), "box": list(ln.box)}
                        for ln in u.block.lines
                    ],
                }
                for u in self.units
            ],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def to_document(self, title: str = "") -> "BilingualDoc":
        """转成导出层的中立结构。

        刻意保持**行数不变**：翻译失败的条目译文留空但仍然占一行。
        丢掉失败行看起来"更干净"，但用户会以为截图里就只有这么多文字，
        而实际上是有几行没译出来 —— 这是更难发现的错误。
        """
        meta = {
            "OCR": f"{self.ocr_engine}",
            "翻译": self.translate_engine,
            "语向": f"{self.source_lang} -> {self.target_lang}",
            "选区": "x{} y{} {}x{}".format(*self.capture.region),
            "缩放": f"{self.capture.dpr:.2f}x",
            "条数": str(len(self.units)),
        }
        if self.elapsed_ms:
            parts = [f"{k} {v}ms" for k, v in self.elapsed_ms.items()]
            meta["耗时"] = " / ".join(parts)
        if self.warnings:
            meta["提示"] = "; ".join(self.warnings)[:180]
        items = [(u.source_text, u.target_text if u.ok else "") for u in self.units]
        head = title or time.strftime("截图翻译 %Y-%m-%d %H:%M:%S")
        return BilingualDoc(title=head, meta=meta, items=items)

    @property
    def plain_bilingual(self) -> str:
        """TXT / 剪贴板用的纯文本双语对照。

        失败行必须显式写出原因：只给原文会被误读成"翻译结果就是原文"
        （中英混排截图里这尤其常见，用户以为模型把英文原样返回了）。
        """
        out: list[str] = []
        for i, u in enumerate(self.units, 1):
            if u.error:
                out.append(f"[{i}] {u.source_text}\n    ⚠ 翻译失败：{u.error}")
            else:
                out.append(f"[{i}] {u.source_text}\n    {u.target_text}")
        return "\n\n".join(out)


@dataclass(slots=True)
class BilingualDoc:
    """导出文档的中立结构（与 OCR/翻译解耦，可被"导入 TXT 做批翻译"复用）。"""

    title: str
    meta: dict[str, str]
    items: list[tuple[str, str]]  # (source, target)

    @property
    def count(self) -> int:
        return len(self.items)


def join_lines(lines: list[str]) -> str:
    """把同一逻辑段的多个 OCR 行折叠为一行。

    规则（与 LibreTranslate/常见字幕处理一致）：
      * 若前一行以 CJK / 全角标点结尾，或后一行以 CJK 开头 —— 直接拼接，不补空格；
      * 若前一行以连字符 '-' 结尾（英文断词）—— 去掉连字符拼接；
      * 其余补一个空格。
    """
    parts = [ln.strip() for ln in lines if ln and ln.strip()]
    if not parts:
        return ""

    def is_cjk(ch: str) -> bool:
        o = ord(ch)
        return (
            0x3040 <= o <= 0x30FF  # 日文假名
            or 0x3400 <= o <= 0x4DBF
            or 0x4E00 <= o <= 0x9FFF
            or 0xF900 <= o <= 0xFAFF
            or 0xFF00 <= o <= 0xFF65  # 全角标点
            or 0x20000 <= o <= 0x2A6DF
        )

    out = parts[0]
    for nxt in parts[1:]:
        if out.endswith("-") and out[-1].isascii() and nxt[:1].isascii() and nxt[0].islower():
            out = out[:-1] + nxt
            continue
        prev_ch, next_ch = out[-1:], nxt[:1]
        if not prev_ch or not next_ch:
            out += nxt
            continue
        if is_cjk(prev_ch) or is_cjk(next_ch) or prev_ch in "，。、；：？！》〉】”’…":
            out += nxt
        else:
            out += " " + nxt if not out.endswith(" ") else nxt
    return out
