"""图像预处理。

## 最终策略（4 轮实测确定，见 tools/experiment_round{1..4}*，数据在 README）

    估计行高 est        倍率 × 插值           说明
    ──────────────────────────────────────────────────────────────
    est < 13px          2.0x NEAREST           小字：阶跃重采样保住笔画边缘与词间空隙
    est ≥ 13px          2.0x LANCZOS           正常/大字：平滑插值提供更丰富细节
    max(边) > 2500px    等比缩小 INTER_AREA     超过 PP-OCR det 内部阈值，自己缩更可控
    任一边 < 40px       3.0x 内 NEAREST         误框/单字符，det 否则直接判空

n=21 单元格（3 种明暗极性 × 7 个字号）的汇总：

    策略                        exact        loose       median
    ───────────────────────────────────────────────────────────
    fixed2.0（本文件）          2.90/4  ✓    3.43/4      453ms ✓
    lanczos2.0 恒放大           2.33/4       3.52/4      876ms
    adaptive（<13px 不放大）    1.86/4       3.57/4      494ms
    none（完全不放大）          1.29/4       3.57/4      952ms

三个结论，其中两个推翻了我自己前面的判断：

1. **"小字不要放大"是错的。** round1 我读到过"px<20 放大为负收益"，
   但那是在拿 lanczos/cubic 做放大候选的比较里得出的；同一张表里
   `2.0x nearest` 在小字上是**最优**（2.50 vs none 2.00 vs lanczos 0.50~0.75）。
   我在 round2 写策略时把"别用 lanczos 放大"误写成了"别放大"。
   round4 用修正后的估计器复核，确认放大 2x + 正确插值才是对的。

2. **真正的分野是插值方式，不是放不放大。** Lanczos/Cubic 的负瓣在细笔画边缘
   产生振铃，把只有 1~2px 宽的词间空隙糊成实心；NEAREST 无振铃。
   exact 2.90 vs 2.33 的差主要来自这里。

3. **丢空格只影响 exact，几乎不影响内容命中（全策略 loose 3.43~3.57）。**
   即 `Quarterlyrevenuegrew18percent.` 的"内容"是对的。
   因此不用空格修复作为默认路径：round2 实测 `repair_spaces` 净收益 **+0**
   （PP-OCR 的 `cal_rec_boxes` 把字符框在 det 框内**等距铺开**，
   框间隙不含真实空白信息，所以 gap-based 检测根本无从判断）。
   代码保留但默认关闭（`ocr.restore_spaces`），因为对某些 rec 模型
   返回真实字符框时它仍然有效。

⚠ 关于耗时数字：单元格之间波动很大（同一策略 437~1364ms），
受 CPU 频率调度影响明显，所以上表的 median 只用于**相对比较**，
不要当作 SLA 承诺。真正稳定有效的提速是限制推理线程数（见 rapid.py 的 warmup：
`intra_op_num_threads=4` 相对默认 `-1` 实测 1661ms -> 484ms，约 3.4x）。
"""

from __future__ import annotations

import statistics as st
from dataclasses import dataclass

import numpy as np

from ..logger import get_logger

log = get_logger("ocr.preprocess")

_MAX_SIDE = 2500
_MIN_SIDE_TARGET = 40
_DEFAULT_SCALE = 2.0
_SMALL_INTERP_THRESHOLD = 13.0     # 低于此估计行高改用 NEAREST


@dataclass(slots=True)
class Prepared:
    image: np.ndarray           # uint8, HxWx3 (BGR)
    scale: float                # 相对原图的缩放系数
    resized: bool
    text_height: float = 0.0
    reason: str = ""

    def map_back(self, v: float) -> float:
        """处理后的坐标 -> 原图物理像素坐标。"""
        return v / self.scale if self.scale else v


# ----------------------------------------------------------------- 行高估计
def estimate_text_height(img: np.ndarray) -> float:
    """用行投影剖面估计主体文字行高（不依赖 OCR，所以可用于决定预处理）。

    **极性是关键**：Otsu 把像素分成两类，"文字"永远是**少数类**（背景占面积绝大部分）。
    白底黑字时少数类是暗像素，暗色模式截图里少数类是亮像素。
    只按 `gray < thresh` 取暗像素，会在深色主题上把"背景带"当文字测 ——
    实测后果是把 10px 的字估成 130~230px，从而错误地大幅放大、精度腰斩
    （round3 第一次运行的 white_on_dark 全部行都中了这个坑）。

    做法：分别统计两类像素占比，取占比小的那类当墨迹，再做行投影取内容带高度中位数。
    """
    gray = _luma(img)
    if gray.size == 0:
        return 0.0
    hist = np.bincount(gray.ravel(), minlength=256)
    if int(hist.max()) >= gray.size:
        return 0.0                       # 纯色：没有任何文字
    t = _otsu(gray)
    ink_dark = gray < t
    ink_light = gray > t                  # 严格不等，跳过阈值本身
    dark_frac = float(ink_dark.mean())
    light_frac = float(ink_light.mean())
    if dark_frac <= light_frac:
        ink = ink_dark
    else:
        ink = ink_light
    if ink.mean() < 0.002:                # 墨迹极少（图标/噪点），不足以判断行高
        return 0.0

    proj = ink.sum(axis=1).astype(np.float64)
    mean = float(proj.mean())
    if mean < 0.5:
        return 0.0
    above = proj >= max(1.0, mean * 0.25)
    d = np.diff(above.astype(np.int8))
    starts = np.flatnonzero(d == 1) + 1
    ends = np.flatnonzero(d == -1) + 1
    if above[0]:
        starts = np.r_[0, starts]
    if above[-1]:
        ends = np.r_[ends, len(above)]
    runs = ends - starts
    # 只接受 5~60px 的行带：更长的一定是插图/色块，会把中位数拉爆
    # （上一版用 240 上限，导致 px=28 的深底样本估出 0.0，即"没文字"）
    runs = runs[(runs >= 5) & (runs <= 60)]
    return float(st.median(runs)) if len(runs) else 0.0


def _luma(img: np.ndarray) -> np.ndarray:
    if img.ndim == 3:
        w = np.array([0.114, 0.587, 0.299], dtype=np.float32)   # BGR 通道顺序
        return np.rint(img[:, :, :3].astype(np.float32) @ w).astype(np.uint8)
    return img


def _otsu(gray: np.ndarray) -> int:
    hist = np.bincount(gray.ravel(), minlength=256).astype(np.float64)
    total = float(gray.size)
    sum_all = float(np.dot(np.arange(256), hist))
    sum_b = w_b = 0.0
    best_var, best_t = -1.0, 127
    for t in range(256):
        w_b += hist[t]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += t * hist[t]
        var = w_b * w_f * ((sum_b / w_b) - ((sum_all - sum_b) / w_f)) ** 2
        if var > best_var:
            best_var, best_t = var, t
    return int(best_t)


# ----------------------------------------------------------------- 主入口
def prepare(img: np.ndarray, *, force_scale: float | None = None,
            enable: bool = True) -> Prepared:
    """按实测策略缩放。返回的 `Prepared.scale` 用于坐标回映，调用方必须使用。"""
    h, w = img.shape[:2]
    if cv2 is None:
        return Prepared(img, 1.0, False, reason="opencv 不可用，跳过预处理")
    if not enable:
        return Prepared(img, 1.0, False, reason="预处理关闭")

    # 1) 超大图先按长边收缩：PP-OCR det 内部也会缩到 2000，
    #    但 INTER_AREA 的降采样质量优于引擎默认插值，主动做更可控。
    if max(h, w) > _MAX_SIDE:
        s = _MAX_SIDE / max(h, w)
        out = _resize(img, s, cv2.INTER_AREA)
        log.debug("超大图缩小 %.3f (%dx%d)", s, w, h)
        return Prepared(out, s, True, reason=f"超过 {_MAX_SIDE}px，等比缩小")

    if force_scale:
        interp = cv2.INTER_NEAREST if force_scale >= 2.0 else cv2.INTER_LANCZOS4
        return Prepared(_resize(img, force_scale, interp), force_scale, True,
                        reason="用户指定倍率")

    th = estimate_text_height(img)

    # 2) 极小选区（误框/单字符）托底放大，否则 det 直接判空
    if min(h, w) < _MIN_SIDE_TARGET:
        s = min(3.0, _MIN_SIDE_TARGET / max(1, min(h, w)))
        return Prepared(_resize(img, s, cv2.INTER_NEAREST), s, True, th,
                        f"选区过小（{w}x{h}），{s:.1f}x NEAREST 托底")

    # 3) 主策略：一律 2.0x，插值按估计行高分流（见模块 docstring 结论 1、2）
    interp = cv2.INTER_NEAREST if th < _SMALL_INTERP_THRESHOLD else cv2.INTER_LANCZOS4
    out = _resize(img, _DEFAULT_SCALE, interp)
    how = "NEAREST" if interp == cv2.INTER_NEAREST else "LANCZOS"
    return Prepared(out, _DEFAULT_SCALE, True, th,
                    f"行高≈{th:.0f}px，{_DEFAULT_SCALE}x {how}")


def _resize(img: np.ndarray, scale: float, interp: int) -> np.ndarray:
    if scale == 1.0:
        return img
    new = (max(1, int(round(img.shape[1] * scale))), max(1, int(round(img.shape[0] * scale))))
    return cv2.resize(img, new, interpolation=interp)


try:
    import cv2  # type: ignore
except ImportError:  # pragma: no cover
    cv2 = None  # type: ignore[assignment]
    log.warning("opencv 不可用，预处理将退化为原图")


# ----------------------------------------------------------------- 空格修复（默认关闭）
def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (0x3040 <= o <= 0x30FF or 0x3400 <= o <= 0x4DBF or 0x4E00 <= o <= 0x9FFF
            or 0xF900 <= o <= 0xFAFF or 0xAC00 <= o <= 0xD7A3 or 0xFF00 <= o <= 0xFF65)


def repair_spaces(text: str, char_boxes: list | None, gap_ratio: float = 1.6) -> str:
    """用字符框间距补回丢失的拉丁空格。

    ⚠ 默认**不启用**。round2 实测净收益 +0：PP-OCR 的 `cal_rec_boxes`
    把识别字符框在 det 框内等距铺开，相邻框间隙等于"框宽分配"而非真实空白，
    gap 检测因此无从判断词边界。只有当 rec 侧提供**真实**字符框（例如换成
    PP-OCRv5 新版或自建模型）时这个函数才有意义，故保留并挂到配置开关上。

    仅在两侧都是非 CJK 字符时插入 —— 中日韩词内本无空格，插了必错。
    """
    if not text or not char_boxes or len(char_boxes) < 2:
        return text
    chars = list(text)
    if len(chars) != len(char_boxes):
        return text              # 词级框（数量少于字符）无法逐字符插，保持原样
    try:
        spans = [(min(float(p[0]) for p in b), max(float(p[0]) for p in b)) for b in char_boxes]
        adv = [r - l for l, r in spans if r > l]
        if not adv:
            return text
        med = st.median(adv)
        out = [chars[0]]
        for i in range(len(chars) - 1):
            gap = spans[i + 1][0] - spans[i][1]
            if (gap > med * gap_ratio and not _is_cjk(chars[i]) and not _is_cjk(chars[i + 1])
                    and chars[i] != " " and chars[i + 1] != " "):
                out.append(" ")
            out.append(chars[i + 1])
        return "".join(out)
    except (TypeError, ValueError, IndexError):
        log.debug("空格修复失败", exc_info=True)
        return text
