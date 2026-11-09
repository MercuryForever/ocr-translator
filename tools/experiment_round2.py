"""第二轮验证：(A) 按新策略预处理后，repair_spaces 到底值不值得留；(B) 线程数与延迟。

结论会直接写进 README 的"实测数据"表。
"""

from __future__ import annotations

import json
import statistics as st
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.experiment_preprocess import LINES, is_cjk, load_font, make_sample, respace  # noqa: E402

import numpy as np  # noqa: E402


def policy_resize(img, th):
    """落地版策略（来自 experiment_preprocess 的第一轮结论）。"""
    import cv2
    h, w = img.shape[:2]
    if th < 20:
        s = 2.0 if min(h, w) < 40 else 1.0
        interp = cv2.INTER_NEAREST
    elif th < 30:
        s, interp = 1.5, cv2.INTER_LANCZOS4
    else:
        s, interp = 2.0, cv2.INTER_LANCZOS4
    if s == 1.0:
        return img, 1.0
    return cv2.resize(img, (int(w * s), int(h * s)), interpolation=interp), s


def main() -> int:
    from rapidocr_onnxruntime import RapidOCR

    print("=" * 78)
    print("A. repair_spaces 的净收益（同一张图，修复前 vs 修复后 精确匹配数）")
    print("=" * 78)
    eng = RapidOCR(print_verbose=False)
    before = after = 0
    detail = []
    for px in (11, 13, 15, 18, 22, 26):
        for contrast in ("black_on_white", "white_on_dark"):
            base = make_sample(px, contrast)
            th = px
            arr = policy_resize(np.array(base), th)[0]
            res, _ = eng(arr, return_word_box=True)
            if not res:
                continue
            raw = [r[1] for r in res]
            fixed = []
            for r in res:
                text = r[1]
                if len(r) >= 5 and isinstance(r[3], list) and r[3]:
                    try:
                        chars = list(text)
                        # word_boxes 是"词"级，需要展平成与字符等长；这里用逐字符 rec_box
                        boxes = r[3]
                        if len(boxes) == len(chars):
                            text = respace(chars, boxes)
                    except Exception:
                        pass
                fixed.append(text)
            b = sum(1 for t in LINES if t in raw)
            a = sum(1 for t in LINES if t in fixed)
            before += b
            after += a
            detail.append({"px": px, "contrast": contrast, "before": b, "after": a,
                           "first_raw": raw[0], "first_fixed": fixed[0]})
            print(f"  px={px:<3}{contrast:<16} before={b}/4 after={a}/4   {raw[0][:34]:<36}-> {fixed[0][:34]}")
    print(f"\n  合计：修复前 {before} 处精确匹配 -> 修复后 {after} 处 "
          f"（净 {'+' if after >= before else ''}{after - before}）")

    print()
    print("=" * 78)
    print("B. intra_op_num_threads 与延迟（同图 3 次取中位数）")
    print("=" * 78)
    img = np.array(make_sample(18, "black_on_white"))
    for nthreads in (1, 2, 4, 8, -1):
        try:
            e = RapidOCR(print_verbose=False, intra_op_num_threads=nthreads,
                         inter_op_num_threads=1)
        except Exception as exc:
            print(f"  threads={nthreads:<3} 初始化失败 {exc}")
            continue
        e(np.array(img))  # 预热，排除首次 lazy 分配
        ts = []
        for _ in range(3):
            t0 = time.perf_counter()
            e(np.array(img), return_word_box=False)
            ts.append(time.perf_counter() - t0)
        print(f"  threads={nthreads:<3} median {st.median(ts) * 1000:7.0f} ms   "
              f"min {min(ts) * 1000:6.0f} max {max(ts) * 1000:6.0f}")

    print()
    print("=" * 78)
    print("C. 端到端：一次真实框选（含预处理）耗时分解")
    print("=" * 78)
    eng2 = RapidOCR(print_verbose=False, intra_op_num_threads=2, inter_op_num_threads=1)
    t_all = []
    for px in (13, 18, 26):
        arr = np.array(make_sample(px))
        t0 = time.perf_counter()
        out, sc = policy_resize(arr, px)
        t_pre = time.perf_counter() - t0
        res, elapsed = eng2(out, return_word_box=True)
        print(f"  px={px:<3} 预处理 {t_pre * 1000:5.1f}ms  引擎内部 {['%.0fms' % (e * 1000) for e in (elapsed or [])]}  "
              f"行={len(res or [])}")
        t_all.append(t_pre + sum(elapsed or []))
    print(f"  三次合计中位 {st.median(t_all) * 1000:.0f}ms")

    Path("experiment_round2.json").write_text(
        json.dumps({"respace": detail, "before": before, "after": after},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
