"""第三轮：用**估计行高**（而非真实 px）选策略，扫参定阈值；并测翻译端对"丢空格"的容忍度。

A 部分回答"到底该在哪个行高切换放大策略"，B 部分回答"PP-OCR 丢空格这件事，
值不值得继续工程化修复" —— 如果翻译器本身对连写不敏感，就不该为它增加复杂度。
"""

from __future__ import annotations

import json
import statistics as st
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.experiment_preprocess import LINES, make_sample  # noqa: E402
from ocrtranslator.ocr import preprocess as pp  # noqa: E402

import numpy as np  # noqa: E402


def score(exact_key: str):
    def f(rec):
        joined = " ".join(rec)
        exact = sum(1 for t in LINES if t in rec)
        loose = sum(1 for t in LINES if t.replace(" ", "") in joined.replace(" ", ""))
        return exact, loose
    return f


CANDIDATES = {
    "none":        lambda th, est: (1.0, "none"),
    "lanczos1.5":  lambda th, est: (1.5, "lanczos"),
    "lanczos2.0":  lambda th, est: (2.0, "lanczos"),
    "nearest2.0":  lambda th, est: (2.0, "nearest"),
    "adaptiveA":   lambda th, est: (1.0, "none") if est < 20 else ((1.5, "lanczos") if est < 30 else (2.0, "lanczos")),
    "adaptiveB":   lambda th, est: (1.0, "none") if est < 13 else ((1.5, "lanczos") if est < 20 else (2.0, "lanczos")),
    "adaptiveC":   lambda th, est: (2.0, "nearest") if est < 13 else ((1.5, "lanczos") if est < 20 else (2.0, "lanczos")),
    "adaptiveD":   lambda th, est: (1.5, "lanczos") if est < 20 else (2.0, "lanczos"),
}


def resize(img, scale, how):
    if scale == 1.0:
        return img
    import cv2
    interp = {"lanczos": cv2.INTER_LANCZOS4, "nearest": cv2.INTER_NEAREST,
              "cubic": cv2.INTER_CUBIC}[how]
    return cv2.resize(img, (int(img.shape[1] * scale), int(img.shape[0] * scale)),
                      interpolation=interp)


def main() -> int:
    from rapidocr_onnxruntime import RapidOCR
    import os
    n = max(2, min(4, (os.cpu_count() or 4) // 2))
    eng = RapidOCR(print_verbose=False, intra_op_num_threads=n, inter_op_num_threads=1)

    print("=" * 92)
    print("A. 预处理策略 × 字号（指标 exact=逐字含空格命中/4，loose=内容命中/4）")
    print("=" * 92)
    sizes = (10, 12, 14, 16, 18, 20, 24, 28)
    tally = {k: {"exact": 0, "loose": 0, "n": 0, "ms": []} for k in CANDIDATES}
    for contrast in ("black_on_white", "white_on_dark"):
        for px in sizes:
            base = np.array(make_sample(px, contrast))
            est = pp.estimate_text_height(base)
            line = []
            for name, fn in CANDIDATES.items():
                scale, how = fn(px, est)
                arr = resize(base, scale, how)
                t0 = time.perf_counter()
                res, _ = eng(arr, return_word_box=False)
                ms = int((time.perf_counter() - t0) * 1000)
                rec = [r[1] for r in (res or [])]
                exact, loose = score("")(rec)
                tally[name]["exact"] += exact
                tally[name]["loose"] += loose
                tally[name]["n"] += 1
                tally[name]["ms"].append(ms)
                line.append(f"{name[:9]}={exact}{loose}")
            print(f"  {contrast[:9]} px={px:<3} 估计行高={est:5.1f}  " + " ".join(line))

    print("\n排名（exact 降序，同分看耗时）：")
    rank = sorted(tally.items(), key=lambda kv: (-kv[1]["exact"] / kv[1]["n"], st.median(kv[1]["ms"])))
    for name, d in rank:
        print(f"  {name:<11} exact {d['exact']}/{d['n'] * 4} = {d['exact'] / d['n']:.2f}/4   "
              f"loose {d['loose'] / d['n']:.2f}/4   median {st.median(d['ms']):.0f}ms")

    print()
    print("=" * 92)
    print("B. 翻译器对'PP-OCR 丢空格'的容忍度（同一句，标准空格 vs 全连写）")
    print("=" * 92)
    pairs = [
        ("Quarterly revenue grew 18 percent.", "Quarterlyrevenuegrew18percent."),
        ("Please press Ctrl+S to save the file.", "PleasepressCtrl+Stosavethefile."),
        ("Machine learning is not magic, but statistics.", "machinelearningisnotmagicbutstatistics."),
        ("This paper proposes a novel framework.", "Thispaperproposesanovelframework."),
    ]
    try:
        import requests
        sess = requests.Session()

        def g(text):
            r = sess.get("https://translate.googleapis.com/translate_a/single",
                        params={"client": "gtx", "sl": "auto", "tl": "zh-CN",
                                "dt": "t", "q": text}, timeout=12)
            r.raise_for_status()
            return "".join(seg[0] for seg in r.json()[0] if seg[0])
        ok = True
    except Exception as exc:
        print("  无法访问在线翻译端：", exc)
        ok = False
    if ok:
        same = 0
        for good, bad in pairs:
            try:
                tg, tb = g(good), g(bad)
            except Exception as exc:
                print("  请求失败：", exc)
                break
            flag = "同" if tg == tb else "异"
            same += tg == tb
            print(f"  [{flag}] 正常 -> {tg}\n         连写 -> {tb}")
        print(f"\n  结论：{same}/{len(pairs)} 句翻译结果一致"
              f" -> {'连写无影响，不值得为此增加预处理复杂度' if same >= len(pairs) - 1 else '连写确实损害译文，需在预处理或分词层修复'}")

    Path("experiment_round3.json").write_text(json.dumps(
        {k: {**v, "ms": st.median(v["ms"])} for k, v in tally.items()},
        ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
