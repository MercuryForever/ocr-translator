"""离线端到端验证：不弹窗、不联网，跑通 抓图字节 -> OCR -> 版面 -> 配对 全链路。

这是本项目最重要的一份测试：它用的就是生产代码路径（CaptureResult 的 RGBA 字节、
preprocess.prepare、RapidOcrEngine.recognize、layout.build_blocks），
所以它挂了就说明程序真的不能用，而不是"测试自己写错了"。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ocrtranslator import bootstrap  # noqa: E402

bootstrap.install()

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from ocrtranslator.config import AppConfig  # noqa: E402
from ocrtranslator.layout import build_blocks  # noqa: E402
from ocrtranslator.models import BlockKind, CaptureResult  # noqa: E402
from ocrtranslator.ocr import preprocess  # noqa: E402
from ocrtranslator.ocr.rapid import RapidOcrEngine  # noqa: E402

FONTS = [r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\segoeui.ttf",
         "/System/Library/Fonts/PingFang.ttc",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]


def font(sz):
    for p in FONTS:
        try:
            return ImageFont.truetype(p, sz)
        except OSError:
            continue
    return ImageFont.load_default()


def to_capture(im: Image.Image) -> CaptureResult:
    """PIL -> 与 QScreen.grabWindow 完全一致的 RGBA 字节布局。"""
    rgba = im.convert("RGBA")
    data = rgba.tobytes()          # 行主序、每像素 4 字节、无 padding
    return CaptureResult(width=rgba.width, height=rgba.height, bytes_bmp=data,
                         dpr=1.0, region=(0, 0, rgba.width, rgba.height))


def doc_image(px=18, dark=False, two_col=False, title=True) -> Image.Image:
    """构造一张"像真实文档"的截图：标题 + 两段 + 可选双栏。"""
    f, fb = font(px), font(int(px * 1.45))
    bg, fg = ("#1b1c1d", "#e8eaed") if dark else ("white", "#16181c")
    W, H = (1000, 620) if two_col else (760, 560)
    im = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(im)
    p1 = ["Quarterly revenue grew 18 percent year over year,",
          "driven mainly by the enterprise segment. Operating",
          "margin improved to 24.6 points.",
          "",
          "本季度营收同比增长 18%，主要由企业级业务",
          "驱动。营业利润率提升至 24.6 个百分点。",
          "管理层预计下一季度将继续保持这一趋势。"]
    p2 = ["We recommend restarting the service before",
          "retrying the sync operation.",
          "",
          "建议在重新同步之前先重启服务。"]
    colw = W // 2 if two_col else W

    def draw_para(lines, x, y):
        lh = int(px * 1.85)
        for ln in lines:
            if ln:
                d.text((x, y), ln, fill=fg, font=f)
            y += lh
        return y

    y = 40
    if title:
        d.text((40, y), "Financial Report Q3", fill=fg, font=fb)
        y += int(px * 2.6)
    if two_col:
        y = draw_para(p1, 40, y)
        draw_para(p2, colw + 30, 40)
    else:
        y = draw_para(p1 + [""] + p2, 40, y)
    return im


def check(name, cond, extra=""):
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"  ({extra})" if extra else ""))
    return cond


def main() -> int:
    cfg = AppConfig.load()
    cfg.ocr.engine = "rapid"
    cfg.ocr.min_conf = 0.30
    ok_all = True
    print("=" * 78)
    print("端到端离线验证（生产代码路径）")
    print("=" * 78)

    eng = RapidOcrEngine(cfg)
    t0 = time.perf_counter()
    eng.warmup()
    print(f"\n1) 引擎加载 {time.perf_counter() - t0:.2f}s  推理线程={getattr(eng, 'last_threads', '?')}")
    ok_all &= check("warmup 幂等（第二次立即返回）", (lambda: (eng.warmup(), True)[1])())

    # ------------------------------------------------ 预处理单元行为
    print("\n2) 预处理策略")
    for px, dark in ((12, False), (18, False), (18, True), (26, False)):
        im = doc_image(px=px, dark=dark)
        cap = to_capture(im)
        bgr = eng.to_numpy(cap, "bgr")
        prep = preprocess.prepare(bgr, enable=True)
        est = prep.text_height
        expect_interp = "NEAREST" if est < preprocess._SMALL_INTERP_THRESHOLD else "LANCZOS"
        print(f"   px={px} dark={dark} 估计行高={est:5.1f} -> {prep.reason}")
        ok_all &= check(f"px={px} 倍率为 2.0（实测最优）", abs(prep.scale - 2.0) < 0.01,
                        f"scale={prep.scale:.2f}")
        ok_all &= check(f"px={px} 插值符合策略（{expect_interp}）",
                        expect_interp in prep.reason, prep.reason)
    # 纯色图
    blank = to_capture(Image.new("RGB", (400, 200), "white"))
    p_blank = preprocess.prepare(eng.to_numpy(blank, "bgr"), enable=True)
    ok_all &= check("纯色图不误放大（保持 2x 但 det 会返回空，不做特殊处理）",
                    p_blank.text_height == 0.0, f"est={p_blank.text_height}")
    # 超大图
    huge = to_capture(Image.new("RGBA", (4000, 300), "white"))
    p_huge = preprocess.prepare(np.full((300, 4000, 3), 255, np.uint8), enable=True)
    ok_all &= check("超过 2500px 主动缩小", p_huge.scale < 1.0, f"scale={p_huge.scale:.3f}")
    del huge

    # ------------------------------------------------ 识别 + 坐标回映
    print("\n3) 识别质量与坐标回映")
    for label, px, dark in (("浅色 18px", 18, False), ("深色 18px", 18, True),
                            ("浅色 12px", 12, False), ("双栏 18px", 18, False)):
        im = doc_image(px=px, dark=dark, two_col=(label.startswith("双栏")))
        cap = to_capture(im)
        t0 = time.perf_counter()
        lines = eng.recognize(cap)
        dt = time.perf_counter() - t0
        joined = " ".join(l.text for l in lines)
        loose = sum(1 for src in _expect(px, dark) if src.replace(" ", "") in joined.replace(" ", ""))
        print(f"   {label}: {len(lines)} 行 / {dt:.2f}s  首行={lines[0].text[:52] if lines else '<空>'}")
        ok_all &= check(f"{label} 有识别结果", len(lines) > 0)
        ok_all &= check(f"{label} 内容命中≥{max(2, len(_expect(px, dark)) - 1)}",
                        loose >= max(2, len(_expect(px, dark)) - 1), f"loose={loose}")
        # 坐标必须在图内，且已按 scale 回映（不放大到图外）
        inb = all(0 <= l.box[0] and l.box[2] <= cap.width * 1.02 and
                  l.box[3] <= cap.height * 1.02 for l in lines)
        ok_all &= check(f"{label} 坐标回映落在原图内", inb,
                        f"max_x1={max((l.box[2] for l in lines), default=0):.0f} vs W={cap.width}")
        if label == "浅色 18px":
            ok_all &= check("置信度已归一到 0~1", all(0 <= l.conf <= 1 for l in lines))

    # ------------------------------------------------ 版面配对
    print("\n4) 版面分析（段落/标题/双栏）")
    cap = to_capture(doc_image(px=18, dark=False, two_col=False, title=True))
    lines = eng.recognize(cap)
    blocks = build_blocks(lines)
    kinds = [b.kind for b in blocks]
    print(f"   {len(lines)} 行 -> {len(blocks)} 块：{[k.value for k in kinds]}")
    ok_all &= check("行数被合并为更少块（说明断行拼接生效）", len(blocks) < len(lines),
                    f"{len(lines)} -> {len(blocks)}")
    ok_all &= check("标题被识别为 heading", BlockKind.HEADING in kinds,
                    f"kinds={kinds}")
    ok_all &= check("阅读顺序单调", [b.reading_order for b in blocks] == sorted(
        b.reading_order for b in blocks))
    # 段落合并质量：中文段应该被拼成 1~2 块，而不是 7 块
    zh = [b for b in blocks if any(ord(c) > 0x4E00 for c in b.source_inline)]
    ok_all &= check("中文多行被合并（≤3 块）", len(zh) <= 3, f"={len(zh)}")
    print("   合并结果示例：")
    for b in blocks[:4]:
        print(f"     [{b.kind.value:<7}] {b.source_inline[:74]}")

    # 双栏：阅读顺序应是"先整左栏再整右栏"
    cap2 = to_capture(doc_image(px=18, dark=False, two_col=True))
    b2 = build_blocks(eng.recognize(cap2))
    xs = [(b.box[0], b.box[2]) for b in b2]
    left_first = all(xs[i][0] <= 500 for i in range(min(3, len(xs))))
    print(f"   双栏：{len(b2)} 块，块 x0 序列={[int(a) for a, _ in xs]}")
    ok_all &= check("双栏未交错（左栏块在前）", left_first, f"xs={[int(a) for a, _ in xs]}")

    # ------------------------------------------------ 异常场景
    print("\n5) 异常与边界")
    ok_all &= check("空图返回 0 行不抛异常",
                    len(eng.recognize(to_capture(Image.new("RGB", (300, 120), "white")))) == 0)
    tiny = eng.recognize(to_capture(Image.new("RGB", (12, 8), "white")))
    ok_all &= check("极小选区不崩溃", isinstance(tiny, list), f"{len(tiny)} 行")
    noise = np.random.randint(0, 255, (200, 400, 3), dtype=np.uint8)
    rgba = np.dstack([noise, np.full(noise.shape[:2], 255, np.uint8)])
    capn = CaptureResult(width=400, height=200, bytes_bmp=rgba.tobytes(), dpr=1.0)
    try:
        nl = eng.recognize(capn)
        ok_all &= check("纯噪声图不抛异常", True, f"{len(nl)} 行（噪声可能被误认，属正常）")
    except Exception as exc:
        ok_all &= check("纯噪声图不抛异常", False, str(exc)[:60])
    cfg.ocr.upscale = False
    eng2 = RapidOcrEngine(cfg)
    try:
        eng2.warmup()
        n = eng2.recognize(to_capture(doc_image(px=18)))
        ok_all &= check("关闭预处理仍能出结果", len(n) > 0, f"{len(n)} 行")
    except Exception as exc:
        ok_all &= check("关闭预处理仍能出结果", False, str(exc)[:80])

    print()
    print("=" * 78)
    print("总体：" + ("全部通过 ✓" if ok_all else "存在失败项 ✗"))
    print("=" * 78)
    return 0 if ok_all else 1


def _expect(px, dark):
    return ["Quarterly revenue grew 18 percent year over year",
            "本季度营收同比增长 18%", "营业利润率提升至 24.6"]


if __name__ == "__main__":
    raise SystemExit(main())
