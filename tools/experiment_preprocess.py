"""实测脚本：预处理策略对 PP-OCR 精度的影响（README 的数据来源）。

跑法::

    python tools/experiment_preprocess.py            # 全量矩阵
    python tools/experiment_preprocess.py --quick    # 只跑关键单元格

两个结论直接写进了 ocr/preprocess.py：
  1. 放大倍率不是越大越好，存在 ~15px 的交叉点；
  2. Lanczos 在极小字上会因振铃（ringing）破坏笔画，改用 INTER_CUBIC + 锐化更稳。
"""

from __future__ import annotations

import argparse
import json
import statistics as st
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ocrtranslator import bootstrap  # noqa: E402  把 vendor/ 挂上 sys.path

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

LINES = [
    "Quarterly revenue grew 18 percent.",
    "Please press Ctrl+S to save the file.",
    "本季度营收同比增长 18%，达 42 亿元。",
    "Machine learning is not magic, but statistics.",
]

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]


def load_font(size: int) -> ImageFont.FreeTypeFont:
    for p in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()  # type: ignore[return-value]


def make_sample(px: int, contrast: str = "black_on_white") -> Image.Image:
    """构造"真实截图"风格的样本：有页边距、多行、非纯黑字。"""
    f = load_font(px)
    lh = int(px * 1.9)
    w = max(int(f.getbbox(t)[2]) for t in LINES) + 56
    h = lh * len(LINES) + 56
    bg = {"black_on_white": "white", "gray_on_light": "#F2F2F2",
          "white_on_dark": "#202124"}[contrast]
    fg = {"black_on_white": "#111111", "gray_on_light": "#3C4043",
          "white_on_dark": "#E8EAED"}[contrast]
    im = Image.new("RGB", (w, h), bg)
    d = ImageDraw.Draw(im)
    for i, t in enumerate(LINES):
        d.text((28, 28 + i * lh), t, fill=fg, font=f)
    return im


def resize(im: Image.Image, scale: float, how: str) -> Image.Image:
    if scale == 1.0:
        return im
    size = (int(im.width * scale), int(im.height * scale))
    if how == "lanczos":
        return im.resize(size, Image.LANCZOS)
    if how == "bicubic":
        return im.resize(size, Image.BICUBIC)
    if how == "nearest":
        return im.resize(size, Image.NEAREST)
    import cv2
    interp = {"cv_cubic": cv2.INTER_CUBIC, "cv_linear": cv2.INTER_LINEAR,
              "cv_lanczos": cv2.INTER_LANCZOS4}[how]
    a = cv2.resize(np.array(im), size, interpolation=interp)
    out = Image.fromarray(a)
    if how == "cv_cubic":
        # 3x3 轻度锐化核对冲放大的模糊
        k = np.array([[0, -0.25, 0], [-0.25, 2.0, -0.25], [0, -0.25, 0]])
        out = Image.fromarray(np.clip(cv2.filter2D(a, -1, k), 0, 255).astype(np.uint8))
    return out


def is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (0x3040 <= o <= 0x30FF or 0x3400 <= o <= 0x4DBF or 0x4E00 <= o <= 0x9FFF
            or 0xF900 <= o <= 0xFAFF or 0xFF00 <= o <= 0xFF65)


def respace(chars: list[str], boxes: list) -> str | None:
    """用相邻字符的水平间隙重建空格 —— 修 PP-OCR 丢空格问题。

    阈值取"字符推进宽度中位数 × 1.6"：小于它视为字距，大于它视为词间隔。
    只在两侧都是非 CJK 时插空格，因为中文词内本就没有空格。
    """
    if not chars or len(chars) != len(boxes):
        return None

    def edges(box):
        xs = [p[0] for p in box] if isinstance(box[0], (list, tuple)) else list(box)
        return min(xs), max(xs)

    spans = [edges(b) for b in boxes]
    adv = [r - l for l, r in spans if r > l]
    if not adv:
        return None
    med = st.median(adv)
    out = [chars[0]]
    for i in range(len(chars) - 1):
        gap = spans[i + 1][0] - spans[i][1]
        if (gap > med * 1.6 and not is_cjk(chars[i]) and not is_cjk(chars[i + 1])
                and chars[i] != " " and chars[i + 1] != " "):
            out.append(" ")
        out.append(chars[i + 1])
    return "".join(out)


def score(recognized: list[str]) -> tuple[int, int, int]:
    """返回 (逐字精确命中含空格, 去空格后命中, 词序完整命中)。"""
    joined = " ".join(recognized)
    exact = sum(1 for t in LINES if t in recognized)
    loose = sum(1 for t in LINES if t.replace(" ", "") in joined.replace(" ", ""))
    return exact, loose, len(recognized)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--out", default="experiment_result.json")
    ns = ap.parse_args()

    from rapidocr_onnxruntime import RapidOCR

    eng = RapidOCR()
    px_list = [11, 13, 15, 18, 22] if not ns.quick else [11, 15, 22]
    scales = [1.0, 1.5, 2.0] if not ns.quick else [1.0, 2.0]
    methods = ["lanczos", "cv_cubic", "nearest"] if not ns.quick else ["lanczos", "cv_cubic"]
    contrasts = ["black_on_white", "white_on_dark"] if not ns.quick else ["black_on_white"]

    rows = []
    print(f"{'px':>4} {'scale':>6} {'method':<10} {'contrast':<15} "
          f"{'exact':>6} {'loose':>6} {'lines':>6} {'ms':>6}   识别首行")
    for contrast in contrasts:
        for px in px_list:
            base = make_sample(px, contrast)
            for scale in scales:
                for method in (["none"] if scale == 1.0 else [method for method in methods]):
                    im = base if scale == 1.0 else resize(base, scale, method)
                    t0 = time.perf_counter()
                    res, _ = eng(np.array(im), return_word_box=True)
                    ms = int((time.perf_counter() - t0) * 1000)
                    rec = [r[1] for r in (res or [])]
                    exact, loose, nlines = score(rec)
                    fixed = None
                    if res:
                        row0 = res[0]
                        if len(row0) >= 5 and isinstance(row0[3], list) and row0[3]:
                            fixed = respace(list(row0[4]), list(row0[3]))
                    rows.append({"px": px, "scale": scale, "method": method,
                                 "contrast": contrast, "exact": exact, "loose": loose,
                                 "lines": nlines, "ms": ms, "first": rec[0] if rec else "",
                                 "respaced": fixed})
                    print(f"{px:>4} {scale:>6.1f} {method:<10} {contrast:<15} "
                          f"{exact:>6} {loose:>6} {nlines:>6} {ms:>6}   "
                          f"{(rec[0] if rec else '<无文字>')[:40]}"
                          + (f"   ->修复: {fixed[:40]}" if fixed and fixed != rec[0] else ""))

    # 汇总：哪种预处理组合的 (exact, -ms) 最优
    best = max(rows, key=lambda r: (r["exact"], r["px"], -r["ms"]))
    print(f"\n最优单项: px={best['px']} scale={best['scale']} method={best['method']} "
          f"exact={best['exact']}/4 {best['ms']}ms")
    Path(ns.out).write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"明细 -> {ns.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
