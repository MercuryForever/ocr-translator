"""版面分析测试：段落合并、分栏、块分类。

重点覆盖两个**渲染图才暴露出来**的真实缺陷（单测原本全绿却仍有 bug）：
  1. `hard and not soft` 式判据在"空行 + 同缩进"时落到 else 而误合并 ——
     6 段文档只出 3 段，整页被当成一句话送翻译。
  2. `_code_score` 把句末点号算作代码符号，"Save As..." 被判成代码块。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ocrtranslator.layout import (build_blocks, classify, detect_columns,
                                  group_into_bands, _code_score, _size_class,
                                  _table_grid)
from ocrtranslator.models import BlockKind, OcrLine, join_lines


def line(text, x0, y0, x1, y1, conf=0.9) -> OcrLine:
    box = (x0, y0, x1, y1)
    return OcrLine(text=text, conf=conf, box=box,
                   poly=((x0, y0), (x1, y0), (x1, y1), (x0, y1)))


def para(lines_text, x0=40, y0=100, step=28, width=460, h=20):
    return [line(t, x0, y0 + i * step, x0 + width, y0 + i * step + h)
            for i, t in enumerate(lines_text)]


class TestParagraphMerge(unittest.TestCase):
    def test_blank_line_splits_even_with_same_indent(self):
        """核心回归：空行必须分段，即使两行左边缘完全对齐。

        旧实现写成 `hard and not soft`，而 soft 只要求"x 有重叠 + 同缩进"，
        于是同一段落风格的连续两段永远满足 soft -> 落到 else -> 合并。
        """
        a = para(["First paragraph line one", "first paragraph line two"])
        # 第三行与第一行之间留 3 倍行距（显式空行）
        b = [line("Second paragraph after a blank line", 40, 300, 500, 320)]
        blocks = build_blocks(a + b)
        self.assertEqual(len(blocks), 2, [x.source_inline for x in blocks])

    def test_wrapped_lines_stay_merged(self):
        """没有空行的连续断行必须合并成一段（否则逐行翻译毁掉语序）。"""
        rows = para(["This paper proposes a novel framework for",
                     "cross-modal representation learning. The key",
                     "idea is to align visual and textual features"])
        blocks = build_blocks(rows)
        self.assertEqual(len(blocks), 1)
        self.assertIn("novel framework", blocks[0].source_inline)

    def test_english_forced_wrap_after_period(self):
        """句末标点出现在行中（英文排版常见）不应导致误切段。"""
        rows = para(["We observed a 18% increase. This is consistent",
                     "with the prior study and suggests the effect",
                     "holds across all measured cohorts."])
        blocks = build_blocks(rows)
        self.assertEqual(len(blocks), 1, [b.source_inline for b in blocks])

    def test_size_change_splits(self):
        rows = para(["正文第一行", "正文第二行"])
        rows.append(line("标题字号明显更大", 40, 300, 300, 330))
        blocks = build_blocks(rows)
        self.assertGreaterEqual(len(blocks), 2)

    def test_cjk_indent_starts_new_paragraph(self):
        """中文首行缩进两字 = 新段落。"""
        rows = para(["前一段的最后一行文字", "前一段的第二行文字内容"])
        rows.append(line("　　缩进开始的新一段中文文字", 40 + 44, 220, 500, 240))
        blocks = build_blocks(rows)
        self.assertEqual(len(blocks), 2, [b.source_inline for b in blocks])

    def test_list_items_group_separately(self):
        rows = para(["普通正文的一行文字说明某件事"])
        rows += [line("- 列表项一", 40, 200, 200, 220),
                 line("- 列表项二", 40, 232, 200, 252)]
        blocks = build_blocks(rows)
        self.assertGreaterEqual(len(blocks), 2)

    def test_height_jitter_does_not_split(self):
        """同一行内检测框高度抖动 ±10% 不应被当成字号变化。"""
        import random
        rows = []
        for i in range(6):
            h = 20 + random.Random(i).randint(-2, 2)
            rows.append(line(f"line {i} of a normal paragraph with slight jitter here",
                             40, 100 + i * 28, 500, 100 + i * 28 + h))
        self.assertEqual(len(build_blocks(rows)), 1)

    def test_generous_line_leading_does_not_merge_everything(self):
        """核心回归：空行阈值必须相对本文档行距，不能相对行高。

        旧实现用 `gap > 1.9 × 行高`（行高 20 -> 阈值 38）。这张 fixture
        段内 gap=leading-20、段间 gap=leading+60：leading=40 时段内 gap=20
        尚可，但 leading 越大段内 gap 越接近阈值，最终每行都被切开。
        自适应阈值后四种排版都应得到 3 段。
        """
        for leading in (40, 44, 52, 60):
            rows = []
            y = 100
            for pi, group in enumerate([["段落一第一行文字内容", "段落一第二行文字内容"],
                                         ["段落二的第一行文字", "段落二的第二行文字"],
                                         ["段落三只有这一行文字"]]):
                for t in group:
                    rows.append(line(t, 40, y, 420, y + 20))
                    y += leading
                y += 80          # 段间额外空白
            blocks = build_blocks(rows)
            self.assertEqual(len(blocks), 3,
                             f"行距 {leading} 得到 {len(blocks)} 段："
                             + " | ".join(b.source_inline[:14] for b in blocks))

    def test_uniform_leading_single_paragraph_not_over_split(self):
        """间距完全均匀（一整段被切成 6 行）不应被误切开。"""
        rows = [line(f"wrapped sentence fragment number {i} continues here",
                     40, 100 + i * 30, 460, 120 + i * 30) for i in range(6)]
        self.assertEqual(len(build_blocks(rows)), 1)

    def test_empty_input(self):
        self.assertEqual(build_blocks([]), [])


class TestClassification(unittest.TestCase):
    def test_heading_detected(self):
        rows = [line("Financial Report Q3", 40, 30, 232, 56)]      # 高 26
        rows += para(["正文行一内容", "正文行二内容"], y0=100)
        blocks = build_blocks(rows)
        self.assertEqual(blocks[0].kind, BlockKind.HEADING)

    def test_ordinary_sentence_is_not_code(self):
        """核心回归：句末点号曾被算进代码符号密度，"Save As..." 判成代码块。"""
        for text in ("Save As...", "margin improved to 24.6 points.",
                     "Please press Ctrl+S to save the file."):
            self.assertLess(_code_score(text), 0.6, text)
            self.assertEqual(classify([line(text, 40, 100, 400, 120)], 20),
                             BlockKind.TEXT, text)

    def test_real_code_detected(self):
        rows = [line("def foo(a, b):", 40, 100, 300, 120),
                line("    return a + b  # sum", 40, 128, 400, 148),
                line("if x > 0 { do(); }", 40, 156, 400, 176)]
        self.assertEqual(classify(rows, 20), BlockKind.CODE)

    def test_table_row_detected(self):
        rows = [line("| 名称 | 数值 | 备注 |", 40, 100, 400, 120),
                line("| 甲   | 12   | ok   |", 40, 128, 400, 148),
                line("| 乙   | 30   | ok   |", 40, 156, 400, 176)]
        self.assertEqual(classify(rows, 20), BlockKind.TABLE)

    def test_table_grid_not_triggered_by_prose(self):
        """核心回归：char_w 必须除以行高，否则所有正文都被判成表格。"""
        rows = para(["Quarterly revenue grew 18 percent year over year,",
                     "driven mainly by the enterprise segment operating"])
        self.assertFalse(_table_grid(rows))
        self.assertEqual(classify(rows, 20), BlockKind.TEXT)

    def test_size_class_thresholds(self):
        self.assertEqual(_size_class(20, 20), 0)
        self.assertEqual(_size_class(30, 20), 1)      # 1.5x
        self.assertEqual(_size_class(26, 20), 1)      # 1.3x —— 标题常见倍数
        self.assertEqual(_size_class(12, 20), -1)     # 0.6x —— 脚注
        self.assertEqual(_size_class(21, 20), 0)      # 抖动
        self.assertEqual(_size_class(20, 0), 0)       # 无中位数不崩


class TestColumns(unittest.TestCase):
    def _two_col(self):
        left = [line(f"L{i} 左栏内容文字稍长一些填满", 40, 100 + i * 30, 300, 120 + i * 30)
                for i in range(4)]
        right = [line(f"R{i} 右栏内容文字稍长一些填满", 420, 100 + i * 30, 680, 120 + i * 30)
                 for i in range(4)]
        return left + right

    def test_two_column_detected_and_ordered(self):
        rows = self._two_col()
        blocks = build_blocks(rows)
        self.assertGreaterEqual(len(blocks), 2)
        # 阅读顺序：左栏全部先于右栏
        xs = [b.box[0] for b in blocks]
        self.assertTrue(all(x < 350 for x in xs[:len(xs) // 2]), xs)
        self.assertTrue(all(x > 350 for x in xs[len(xs) // 2:]), xs)

    def test_single_column_not_split(self):
        rows = para(["这是一段正常宽度的中文文字，占据整个版面宽度的一半左右",
                     "第二行同样从左边开始，右边留出大片空白但不构成第二栏",
                     "第三行继续，没有右侧内容，不应该被误判成分栏"], width=380)
        rows += para(["第四行仍然在左侧，右侧始终没有文字",
                      "第五行同上，所以整体是单栏",
                      "第六行结束这一段落"], width=380, y0=300)
        blocks = build_blocks(rows)
        self.assertLessEqual(len(blocks), 3)
        # 关键：不应出现"左块-右块-左块"交错
        xs = [b.box[0] for b in blocks]
        self.assertTrue(all(x == xs[0] for x in xs), xs)

    def test_wide_gap_between_two_blocks_not_columns(self):
        """左右两个独立文本块（如表格两列）之间空隙大但不是分栏。"""
        left = para(["左侧说明"], width=120)
        right = [line("右侧一个按钮", 600, 100, 700, 120)]
        self.assertEqual(len(detect_columns(group_into_bands(left + right))), 1)


class TestJoinLines(unittest.TestCase):
    def test_cjk_no_space(self):
        self.assertEqual(join_lines(["本季度营收", "同比增长 18%"]), "本季度营收同比增长 18%")

    def test_latin_adds_space(self):
        self.assertEqual(join_lines(["Quarterly revenue", "grew 18%"]), "Quarterly revenue grew 18%")

    def test_hyphen_join(self):
        self.assertEqual(join_lines(["repre-", "sentation"]), "representation")

    def test_fullwidth_punct_no_space(self):
        self.assertEqual(join_lines(["第一句，", "第二句"]), "第一句，第二句")

    def test_empty_and_blank(self):
        self.assertEqual(join_lines([]), "")
        self.assertEqual(join_lines(["", "  ", "x"]), "x")


if __name__ == "__main__":
    unittest.main(verbosity=2)
