"""版面分析：把散落的 OCR 行重组成"逻辑段落"，并给出阅读顺序。

OCR 引擎返回的是**检测框级别**的碎片。直接逐行翻译会得到两种典型劣化：

    原文（实际 2 段，被检成 7 行）:
        This paper proposes a novel framework for
        cross-modal representation learning. The key
        idea is to align visual and textual features
        本文提出一种跨模态表示学习框架，其核心思想
        是对齐视觉与文本特征空间，并在低资源场景下
        通过对比目标进行
        预训练。

    逐行翻译 -> "本文提出一种新颖的框架，用于 / 跨模态表示学习。关键 / 想法是…"
    （句子被切碎、语序崩坏，对照表里 7 行译文读不通）

算法（纯几何 + 语言启发式，不依赖模型，因此零额外延迟与依赖）：

  1. **行带归并**：按 y 中心归入同一视觉行，带内按 x 排序 —— 比纯 y 排序更抗多列。
  2. **分栏检测**：把行的 x 区间投到列轴，找"贯穿多行的垂直空白带"。
     要求空白宽度 ≥6% 图宽、位于中间 25~75% 区域、两栏垂直重叠 ≥50%，
     否则那只是段间留白或插图空隙 —— 误判分栏是"读起来顺序不对"的头号原因。
  3. **段落合并**：区分软断行（同段）与硬换行（新段），见 merge_paragraphs 的六个信号。
  4. **块分类**：标题（字号 >1.45× 中位且单行短）、代码（符号密度 + 缩进）、
     表格（竖线/制表符，或行内多个大空档成网格）、正文。
  5. **表格/代码再切分**：整块送翻译会毁掉结构，按行切成独立 unit 保持行对齐。

注意 CJK 与拉丁的断行习惯不同：中文用标点判句末、英文看行是否写满；
两个判据都必须参与，只用一个会在中英混排文档上大量误切。
"""

from __future__ import annotations

import re

from .logger import get_logger
from .models import BlockKind, OcrBlock, OcrLine

log = get_logger("layout")

#: 句末/停顿标点：出现即认为"句子可能结束"。
#: 必须包含 ASCII 句号 —— 漏掉它会让英文段落的正常断行被判成"句子未结束"，
#: 反过来又把"…points." 这种真句尾误判。中英混排时这两个方向都要对。
_SENT_END = set("。！？…：；」』）】”\"'!?;.")
_LIST_PREFIX = re.compile(
    r"^\s*(?:[-•·*▪◦‣]|\(?\d{1,3}[.)、]|[①-⑳]|[IVXLCDM]+[.)、])\s+")
_CODE_HINT = re.compile(r"[{};=<>].*[{};=<>]|[A-Za-z_]\w*\s*\(")
_TABLE_CELL = re.compile(r"^\s*(?:\||\t)")


class _RowBand:
    """一个视觉行（可能含多个横向并列的片段）。"""

    __slots__ = ("lines", "y_center")

    def __init__(self, lines: list[OcrLine], y_center: float):
        self.lines = lines
        self.y_center = y_center


def _median(xs) -> float:
    s = sorted(xs)
    if not s:
        return 0.0
    m = len(s) // 2
    return float(s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2)


# ------------------------------------------------------------------ 行带
def group_into_bands(lines: list[OcrLine], tol_ratio: float = 0.6) -> list[_RowBand]:
    """按 y 中心把行归入同一视觉行，容忍轻微倾斜与上下标。"""
    if not lines:
        return []
    ordered = sorted(lines, key=lambda l: l.center_y)
    med_h = _median([l.height for l in ordered]) or 12.0
    tol = max(3.0, med_h * tol_ratio)
    bands: list[_RowBand] = [_RowBand([ordered[0]], ordered[0].center_y)]
    for ln in ordered[1:]:
        cur = bands[-1]
        if abs(ln.center_y - cur.y_center) <= tol:
            cur.lines.append(ln)
            cur.y_center = sum(x.center_y for x in cur.lines) / len(cur.lines)
        else:
            bands.append(_RowBand([ln], ln.center_y))
    for b in bands:
        b.lines.sort(key=lambda l: l.box[0])
    return bands


# ------------------------------------------------------------------ 分栏
def detect_columns(bands: list[_RowBand]) -> list[list[_RowBand]]:
    """检测两栏版式；不是两栏则返回单元素列表。

    ⚠ 必须**按行**分栏再各自重新归并行带，不能按"行带中心 x"分：
    横跨两栏的行带（同一 y 上左右各一行文字）中心恰好落在分割线上，
    实测所有行带都被判到同一侧，`len(left) < 2` 于是放弃分栏 ——
    两栏文档被当单栏逐行合并，阅读顺序彻底乱掉。
    """
    flat = [ln for b in bands for ln in b.lines]
    if len(flat) < 6:
        return [bands]
    x_min = min(l.box[0] for l in flat)
    width = max(l.box[2] for l in flat) - x_min
    if width <= 0:
        return [bands]

    cols = 240
    cov = [0] * cols
    for l in flat:
        a = max(0, min(cols - 1, int((l.box[0] - x_min) / width * cols)))
        b = max(a + 1, min(cols, int((l.box[2] - x_min) / width * cols) + 1))
        for i in range(a, b):
            cov[i] += 1

    runs, start = [], None
    for i, c in enumerate(cov):
        if c == 0:
            if start is None:
                start = i
        elif start is not None:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, cols))

    best = None
    for a, b in runs:
        frac = (b - a) / cols
        center = (a + b) / 2 / cols
        if frac >= 0.06 and 0.25 <= center <= 0.75 and (best is None or frac > best[1]):
            best = (center, frac)
    if best is None:
        return [bands]
    split_x = x_min + best[0] * width

    left_lines = [l for l in flat if (l.box[0] + l.box[2]) / 2 < split_x]
    right_lines = [l for l in flat if (l.box[0] + l.box[2]) / 2 >= split_x]
    if len(left_lines) < 2 or len(right_lines) < 2:
        return [bands]

    def yspan(ls):
        ys = [l.center_y for l in ls]
        return min(ys), max(ys)

    ly, ry = yspan(left_lines), yspan(right_lines)
    overlap = min(ly[1], ry[1]) - max(ly[0], ry[0])
    total = max(ly[1], ry[1]) - min(ly[0], ry[0])
    if total <= 0 or overlap / total < 0.5:
        return [bands]
    log.info("检测到两栏版式，分割线 x≈%.0f（左 %d 行 / 右 %d 行）",
             split_x, len(left_lines), len(right_lines))
    # 分栏后必须**各自重新归并行带**：左右两栏的行 y 坐标相同，
    # 若沿用原 band 会把"L0 与 R0"塞进同一行带，段落合并随即错乱。
    return [group_into_bands(left_lines), group_into_bands(right_lines)]


# ------------------------------------------------------------------ 段落
def _is_list_item(text: str) -> bool:
    return bool(_LIST_PREFIX.match(text))


def _looks_like_table_row(ln: OcrLine) -> bool:
    return bool(_TABLE_CELL.match(ln.text)) or ln.text.count("|") >= 2


def _ends_sentence(text: str) -> bool:
    tail = text.strip()[-1:]
    return tail in _SENT_END if tail else True


def merge_paragraphs(bands: list[_RowBand]) -> list[list[OcrLine]]:
    """把行带合并成段落。

    **空行阈值必须相对"本文档自己的行距分布"，而不是相对行高。**
    绝对阈值（gap > 1.9 × 行高）在行距宽松的排版上会失效：实测 18px 字、
    行距 1.9 倍的文档里段内 gap=8、段间 gap=64 尚可区分，
    但换一张行距 2.5 倍的图，段内 gap 就可能超过 1.9 × 行高 ——
    于是"每行都算新段"和"所有段并成一段"两种错误都可能发生，
    而且取决于具体排版，单测里看不出来（数据是我手写的固定值）。

    做法：把所有相邻 gap 排序，段内 gap 聚在小值、段间 gap 是大值，
    取"最大相邻跳跃点"当分界；跳跃不明显说明间距都差不多（单段文档），
    退回绝对阈值。

    其余判据（优先级从高到低）：
      1. 空行 / 结构变化（列表↔正文、表格↔正文、首行缩进）—— 不可被推翻
      2. 英文 forced wrap（前一行几乎写满 + 缩进一致）—— 必合
      3. 字号档位变化、句子已结束且留有空隙 —— 分

    ⚠ 判据要写成显式分支。曾经写成 `hard and not soft`，而 soft 只要求
    "同缩进 + x 重叠"，于是同缩进的正常段落永远满足 soft 并落到最后的 else
    被合并 —— 实测 6 段文档只出 3 段，整页被当成一句话送翻译。
    """
    if not bands:
        return []
    all_lines = [ln for b in bands for ln in b.lines]
    med_h = _median([l.height for l in all_lines]) or 12.0
    med_w = _median([l.width for l in all_lines]) or 100.0
    right_edge = max(l.box[2] for l in all_lines)

    # ---- 自适应空行阈值
    gaps: list[float] = []
    for prev_b, cur_b in zip(bands, bands[1:]):
        g = min(l.box[1] for l in cur_b.lines) - max(l.box[3] for l in prev_b.lines)
        if g >= 0:
            gaps.append(g)
    abs_threshold = med_h * 1.15                 # 兜底：>1.15 倍行高算空行
    if len(gaps) >= 3:
        s = sorted(gaps)
        jumps = [(s[i + 1] - s[i], s[i]) for i in range(len(s) - 1)]
        biggest, lower = max(jumps) if jumps else (0.0, s[-1])
        if biggest >= max(med_h * 0.6, lower * 0.5):
            blank_gap = max(abs_threshold, lower + biggest * 0.5)
        else:                                    # 间距均匀：整页基本是一类段落
            blank_gap = max(abs_threshold, s[-1] * 1.35)
    else:
        blank_gap = abs_threshold
    log.debug("段落阈值：行高=%.1f 空行>=%.1f（gap 样本 %s）",
              med_h, blank_gap, [round(g, 1) for g in sorted(gaps)][:8])

    paras: list[list[OcrLine]] = []
    cur: list[OcrLine] = []
    prev: OcrLine | None = None

    for band in bands:
        for ln in band.lines:
            if prev is None:
                cur, prev = [ln], ln
                continue

            gap = ln.box[1] - prev.box[3]
            prev_open = not _ends_sentence(prev.text)
            overlap_x = min(prev.box[2], ln.box[2]) - max(prev.box[0], ln.box[0])
            prev_near_right = (right_edge - prev.box[2]) < med_w * 0.18
            same_indent = abs(ln.box[0] - cur[0].box[0]) < med_h * 1.2
            same_size = (_size_class(ln.height, med_h)
                         == _size_class(prev.height, med_h))

            blank_line = gap > blank_gap
            structural = (
                _is_list_item(ln.text) != _is_list_item(prev.text)
                or _looks_like_table_row(ln) != _looks_like_table_row(prev)
                or (ln.box[0] - cur[0].box[0] > med_h * 2.2
                    and not _is_list_item(ln.text))
            )
            wrapped = (overlap_x > 0 and same_indent and same_size
                       and (prev_open or prev_near_right)
                       and gap <= blank_gap * 0.8)
            soft_split = (not same_size
                          or (not prev_open and gap > med_h * 0.55))

            if blank_line or structural or (soft_split and not wrapped):
                paras.append(cur)
                cur = [ln]
            else:
                cur.append(ln)
            prev = ln
    if cur:
        paras.append(cur)
    return paras


# ------------------------------------------------------------------ 分类
#: 句读标点：这些**不算**代码符号。
#: 把它们算进去会让 "Save As..." 的符号密度达到 1.63（3 个点 / 11 字符 × 6），
#: 直接被判成代码块 —— 实测就是这个渲染图暴露的。
_SENTENCE_PUNCT = set(".,;:!?…’'\"“”‘’·-")


def _code_score(text: str) -> float:
    """代码/命令的启发式打分（>0.6 判为代码）。

    只用**真正的代码符号**计数，并排除句读标点；同时要求至少出现一个
    强特征（花括号/等号/函数调用形态），否则纯靠密度会把大量英文句子误判。
    """
    if not text:
        return 0.0
    sym = sum(1 for c in text if c in "{}();=<>[]|&*_%$#@" and c not in _SENTENCE_PUNCT)
    density = sym / max(1, len(text)) * 6
    strong = 1.0 if _CODE_HINT.search(text) else 0.0
    indent = sum(1 for ln in text.split("\n") if ln.startswith(("  ", "\t"))) * 0.15
    # 强特征缺失时最多给 0.55：宁可漏判代码，不可把正文判成代码
    if strong == 0.0:
        return min(0.55, density * 0.5 + indent)
    return min(1.0, density + 0.25 + indent)


def _table_grid(paragraph: list[OcrLine]) -> bool:
    """通过"每字符占宽 / 行高"的比值判断行内是否被切成多个远距离单元格。

    ⚠ 这个比值**必须除以行高**。曾经写死 `width/len(text) > 1.5`，
    而 18px 字号的正常正文每字符就占 ~9px，于是**所有普通段落都被判成表格**，
    连带让标题分类失效（表格优先级高于标题）。归一化后：
      正常正文  char_w/height ≈ 0.45~0.60
      稀疏表格行（"销售额    增长率    备注"）≈ 1.5~4.0
    分界取 1.15，留足余量。
    """
    hits = 0
    for ln in paragraph:
        if len(ln.text.split()) < 3 or ln.height <= 0:
            continue
        char_w = ln.width / max(1, len(ln.text))
        if char_w / ln.height > 1.15:
            hits += 1
    return hits >= max(2, len(paragraph) // 2)


def _size_class(height: float, med_h: float) -> int:
    """字号相对全文中位数分三档：0 正文、+1 大、-1 小。

    为什么与**中位数**比而不是与前一行比：pairwise 比较在"标题 26px → 正文 18px"
    时比值是 0.69，落在容差内就不切段，标题被并进正文（实测踩过）。
    用中位数分档后同一文档里同类行永远同类。

    阈值 1.20/0.83：真实标题字号约为正文的 1.3~1.5 倍，
    同段内不同行的检测框高度差一般 <8%（上下标、纯数字行除外）。
    1.30 的旧阈值会把 1.3 倍的标题漏判成正文（渲染图实测）。
    """
    if med_h <= 0:
        return 0
    r = height / med_h
    if r > 1.20:
        return 1
    if r < 0.83:
        return -1
    return 0


def classify(paragraph: list[OcrLine], med_h: float) -> BlockKind:
    joined = " ".join(l.text for l in paragraph)
    if _looks_like_table_row(paragraph[0]) or (len(paragraph) >= 2 and _table_grid(paragraph)):
        return BlockKind.TABLE
    h0 = paragraph[0].height
    # 标题：独占一段 + 明显大于正文 + 不太长（长句即使字大也是正文）
    if len(paragraph) == 1 and _size_class(h0, med_h) > 0 and len(joined) < 90:
        return BlockKind.HEADING
    if _code_score(joined) > 0.6:
        return BlockKind.CODE
    return BlockKind.TEXT


# ------------------------------------------------------------------ 主入口
def build_blocks(lines: list[OcrLine]) -> list[OcrBlock]:
    """OCR 行 -> 有序段落块。**这是双语对照质量的关键一步。**"""
    if not lines:
        return []
    bands = group_into_bands(lines)
    column_groups = detect_columns(bands)
    med_h = _median([ln.height for ln in lines]) or 12.0

    blocks: list[OcrBlock] = []
    order = 0
    for group in column_groups:
        for para in merge_paragraphs(group):
            if not para:
                continue
            box = (min(l.box[0] for l in para), min(l.box[1] for l in para),
                   max(l.box[2] for l in para), max(l.box[3] for l in para))
            blocks.append(OcrBlock(kind=kind_of(para, med_h), lines=para,
                                   box=box, reading_order=order))
            order += 1
    log.info("版面分析：%d 行 -> %d 块（%d 栏，类型 %s）", len(lines), len(blocks),
             len(column_groups), sorted({b.kind.value for b in blocks}))
    return blocks


#: 对外别名（tools/verify_pipeline.py 与测试用）
kind_of = classify


def block_to_units(block: OcrBlock) -> list[OcrBlock]:
    """表格/代码块按行切成可独立翻译的 unit；正文块原样返回。

    表格整块翻译会把"销售额 增长率 备注"糊成一句；按行切后每行单独译，
    UI 里保持行对齐，至少读得通。
    """
    if block.kind in (BlockKind.CODE, BlockKind.TABLE):
        return [OcrBlock(kind=block.kind, lines=[ln], box=ln.box,
                         reading_order=block.reading_order * 100 + i)
                for i, ln in enumerate(block.lines)]
    return [block]
