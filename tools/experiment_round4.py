"""第四轮：修正行高估计器后，验证"自适应预处理"是否真的优于固定策略。

round3 的 adaptive* 是在估计器**极性 bug**（深底白字估成 130~230px）之下测的，
结论无效；固定策略（none/lanczos2.0/nearest2.0）不受影响，仍可作为基线。
本脚本只跑 4 个候选 × 16 单元格，30 秒内出结果。
"""

from __future__ import annotations

import statistics as st
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.experiment_preprocess import LINES, make_sample  # noqa: E402
from ocrtranslator.ocr import preprocess as pp  # noqa: E402

import numpy as np  # noqa: E402

CANDIDATES = {
    "none":       lambda est: (1.0, "none"),
    "lanczos2.0": lambda est: (2.0, "lanczos"),
    "fixed2.0":   lambda est: (2.0, "nearest") if est < 13 else (2.0, "lanczos"),
    "adaptive":   lambda est: (1.0, "none") if est < 13 else ((1.5, "lanczos") if est < 20 else (2.0, "lanczos")),
}


def resize(img, scale, how):
    if scale == 1.0:
        return img
    import cv2
    interp = {"lanczos": cv2.INTER_LANCZOS4, "nearest": cv2.INTER_NEAREST}[how]
    return cv2.resize(img, (int(img.shape[1] * scale), int(img.shape[0] * scale)),
                      interpolation=interp)


def main() -> int:
    from rapidocr_onnxruntime import RapidOCR
    eng = RapidOCR(print_verbose=False, intra_op_num_threads=4, inter_op_num_threads=1)

    print("行高估计器（修正后）逐单元格结果：")
    print(f"{'模式':<15}{'px':>4}{'估计':>7}  " + "  ".join(f"{k:<12}" for k in CANDIDATES))
    tally = {k: {"exact": 0, "loose": 0, "n": 0, "ms": []} for k in CANDIDATES}
    for contrast in ("black_on_white", "white_on_dark", "gray_on_light"):
        for px in (10, 12, 14, 16, 18, 22, 26):
            base = np.array(make_sample(px, contrast))
            est = pp.estimate_text_height(base)
            cells = []
            for name, fn in CANDIDATES.items():
                scale, how = fn(est)
                arr = resize(base, scale, how)
                t0 = time.perf_counter()
                res, _ = eng(arr)
                ms = int((time.perf_counter() - t0) * 1000)
                rec = [r[1] for r in (res or [])]
                exact = sum(1 for t in LINES if t in rec)
                joined = " ".join(rec)
                loose = sum(1 for t in LINES if t.replace(" ", "") in joined.replace(" ", ""))
                tally[name]["exact"] += exact
                tally[name]["loose"] += loose
                tally[name]["n"] += 1
                tally[name]["ms"].append(ms)
                cells.append(f"{exact}/4 {loose}/4 {ms:>4}ms")
            print(f"{contrast:<15}{px:>4}{est:>7.1f}  " + "  ".join(f"{c:<12}" for c in cells))

    print("\n总计（n=%d 单元格）:" % tally["none"]["n"])
    for name, d in sorted(tally.items(), key=lambda kv: -kv[1]["exact"]):
        print(f"  {name:<11} exact {d['exact']:>3}/{d['n'] * 4} = {d['exact'] / d['n']:.2f}/4   "
              f"loose {d['loose'] / d['n']:.2f}/4   median {st.median(d['ms']):.0f}ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
