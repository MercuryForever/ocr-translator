"""渲染结果窗口三种布局到 PNG（离屏，不需要真截图）。

这是唯一能验证"双语对照到底对不对齐"的方法 —— 逻辑测试过得去，
布局也可能把译文挤成一坨或者两列错位。所以这里真的生成图，人眼看。

    python tools/render_ui.py --out .tmp/ui
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ocrtranslator import bootstrap  # noqa: E402

bootstrap.bootstrap(headless=False)

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from ocrtranslator.config import AppConfig  # noqa: E402
from ocrtranslator.languages import AUTO  # noqa: E402
from ocrtranslator.layout import build_blocks  # noqa: E402
from ocrtranslator.models import (CaptureResult, OcrLine, PipelineResult,  # noqa: E402
                                  TranslationUnit, join_lines)
from ocrtranslator.ui.result_window import ResultWindow  # noqa: E402

#: (text, conf, box, 期望归属的段号)
#: 段号用来构造"明显空行"，否则 10 行会被 merge_paragraphs 并成 1~3 段，
#: 演示图里就看不到"失败卡片""低置信度卡片"这些状态。
LINES = [
    ("Financial Report Q3", 0.97, (40, 30, 232, 56), 0),
    ("Quarterly revenue grew 18 percent year over year,", 0.93, (40, 100, 470, 120), 1),
    ("driven mainly by the enterprise segment. Operating", 0.91, (40, 128, 474, 148), 1),
    ("margin improved to 24.6 points.", 0.95, (40, 156, 232, 176), 1),
    ("本季度营收同比增长 18%，主要由企业级业务驱动。", 0.88, (40, 240, 452, 260), 2),
    ("营业利润率提升至 24.6 个百分点。", 0.90, (40, 268, 300, 288), 2),
    ("We recommend restarting the service before", 0.62, (40, 360, 420, 380), 3),
    ("retrying the sync operation.", 0.55, (40, 388, 210, 408), 3),
    ("建议在重新同步之前先重启服务。", 0.28, (40, 480, 330, 500), 4),
    ("Save As...", 0.94, (40, 560, 118, 578), 5),
]

# 手工译文：模拟"混合来源"——第 4 段故意全失败（配额），第 5 段无译文
TRANS = {
    0: ("第三季度财务报告", "deepl"),
    1: ("季度收入同比增长 18%，", "mymemory"),
    2: ("主要由企业级业务推动。营业", "mymemory"),
    3: ("利润率提升至 24.6 个百分点。", "mymemory"),
    4: ("Quarterly revenue grew 18% YoY, driven mainly by enterprise.", "deepl"),
    5: ("Operating margin rose to 24.6 percentage points.", "deepl"),
    6: ("建议在重试同步操作之前先重启服务。", "mymemory"),
    7: ("Please restart before retrying.", "deepl"),
    8: ("", ""),          # 触发失败卡片
    9: ("另存为…", "mymemory"),
}


def make_result(cfg: AppConfig) -> PipelineResult:
    """单块演示（保留给 --debug 用）：只取第一个 block。"""
    return full_units(cfg)


def full_units(cfg) -> PipelineResult:
    """构造**与真实流水线一致**的结果：一个 block 一个 unit。

    踩过的坑：早先按"每行一个 unit"造数据，但每个 unit 都挂同一个 block，
    而 PairCard 的原文取 `unit.block.source_inline`（整段），
    于是渲染出来每一行都显示同一段全文 —— 看着像 UI 坏了，其实是数据错了。
    这条正好说明 unit 与 block 必须 1:1，导出层与 UI 层都依赖这个不变式。
    """
    lines = [OcrLine(text=t, conf=c, box=b,
                     poly=((b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])))
             for t, c, b, _para in LINES]
    blocks = build_blocks(lines)
    units = []
    for blk in blocks:
        parts, engines, missing = [], [], 0
        for ln in blk.lines:
            idx = next((i for i, (t, _, _, _) in enumerate(LINES) if t == ln.text), None)
            tgt, eng = TRANS.get(idx, ("", "")) if idx is not None else ("", "")
            if tgt:
                parts.append(tgt)
                engines.append(eng)
            else:
                missing += 1
        err = ""
        if not parts:
            err = "MyMemory 配额受限（HTTP 403），请稍后重试或改用 DeepL"
        joined = join_lines(parts) if parts else ""
        units.append(TranslationUnit(block=blk, target_text=joined,
                                     engine=engines[0] if engines else "",
                                     error=err))
    cap = CaptureResult(width=760, height=640, bytes_bmp=b"", dpr=1.0,
                        screen_name="DISPLAY1", region=(120, 40, 760, 640))
    return PipelineResult(capture=cap, units=units, source_lang=AUTO,
                          target_lang="zh", ocr_engine="RapidOCR 1.4.4",
                          translate_engine="mymemory",
                          elapsed_ms={"capture": 12, "ocr": 486, "translate": 940},
                          warnings=["Google 端点不可达，已降级到 MyMemory",
                                    "1 段置信度低于 60%，建议放大后重新框选"])


def render(app, cfg, result, mode, theme, out: Path, size=(1080, 720), conf=True,
           raw=False) -> Path:
    cfg.ui.layout_mode = mode
    cfg.ui.theme = theme
    cfg.ui.show_confidence = conf
    cfg.ui.show_raw_lines = raw
    w = ResultWindow(cfg, result)
    w.resize(*size)
    w.show()
    for _ in range(3):
        app.processEvents()
    pm = w.grab()
    path = out / f"result_{mode}_{theme}{'' if conf else '_noconf'}{'_raw' if raw else ''}.png"
    pm.save(str(path))
    w.close()
    w.deleteLater()
    print(f"  {path.name:44s} {pm.width()}x{pm.height()}  "
          f"{path.stat().st_size / 1024:.0f}KB")
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=".tmp/ui")
    ap.add_argument("--quick", action="store_true")
    ns = ap.parse_args()
    out = ROOT / ns.out
    out.mkdir(parents=True, exist_ok=True)

    app = QApplication.instance() or QApplication(sys.argv)
    cfg = AppConfig()
    res = full_units(cfg)
    print(f"unit 数={len(res.units)}  成功={sum(1 for u in res.units if u.ok)}")
    paths = []
    for theme in ("light", "dark"):
        for mode in (("side_by_side", "stacked") if ns.quick
                     else ("side_by_side", "stacked", "interleaved")):
            paths.append(render(app, cfg, res, mode, theme, out))
    # 变体：不显示置信度 / 显示原始行
    paths.append(render(app, cfg, res, "stacked", "dark", out, conf=False))
    paths.append(render(app, cfg, res, "stacked", "dark", out, raw=True))
    print("\n完成 ->", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
