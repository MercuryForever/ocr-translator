"""导出层测试：文件名清洗、各格式往返、DOCX 结构与降级。"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import sys
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ocrtranslator.export import (FORMATS, ExportError, export, pattern_filename,
                                  safe_filename, to_csv, to_html, to_json, to_markdown,
                                  to_rtf, to_txt)
from ocrtranslator.models import BilingualDoc

ITEMS = [("Quarterly revenue grew 18 percent.", "季度收入增长了 18%。"),
         ("本季度营收同比增长 18%。", "Revenue grew 18% YoY."),
         ('含 "引号" 与 | 竖线\n和换行', "quotes, pipe and newline")]


_ITEM_DIR = Path(__file__).resolve().parent.parent / ".tmp" / "testtmp"


@contextlib.contextmanager
def tmpdir():
    """扁平的临时目录，用完只删文件、不删目录。

    为什么不用 tempfile.TemporaryDirectory：
      1. Windows 上杀软/索引服务会在文件刚写完时短暂占用，清理常常失败；
      2. 本机工作区带 `Everyone Deny DeleteSubdirectoriesAndFiles` 这条 ACE，
         **删子目录必定 PermissionError**，而 CPython 的
         `_rmtree.onexc` 在处理错误时又先调 `_resetperms`（chmod），
         于是 `ignore_cleanup_errors=True` 也救不了 —— 异常从错误处理函数里抛出；
      3. 清理失败会把**通过的断言**报成 error，掩盖真实结果。
    所以这里复用同一个目录、只清文件。目录是扁平的（导出层也只写平铺文件）。
    """
    _ITEM_DIR.mkdir(parents=True, exist_ok=True)
    _clean(_ITEM_DIR)
    yield _ITEM_DIR
    _clean(_ITEM_DIR)


def _clean(d: Path) -> None:
    """删文件优先、删空目录尽力而为。

    顺序很重要：先递归删文件，再 rmdir 空目录。反过来在带
    `Deny DeleteSubdirectoriesAndFiles` 的 ACL 上更容易失败。
    失败一律忽略 —— 清理不属于断言，且下一个用例进来还会再清一次。
    """
    for f in sorted(d.iterdir()):
        if f.is_dir():
            _clean(f)
            try:
                f.rmdir()
                continue
            except OSError:
                pass
        elif f.is_file():
            try:
                f.unlink()
            except OSError:
                pass


def doc() -> BilingualDoc:
    return BilingualDoc(title="截图翻译 2026-11-08",
                        meta={"OCR": "RapidOCR 1.4.4", "翻译": "MyMemory",
                              "语向": "auto -> zh", "区域": "120,40 800x420",
                              "条数": "3"},
                        items=ITEMS)


class TestFilename(unittest.TestCase):
    def test_windows_illegal_chars(self):
        with tmpdir() as td:
            for bad in ['a/b\\c:d*e?f"g<h>i|j', "CON.txt", "NUL", "com1", "  ",
                        "a" * 300]:
                p = safe_filename(bad, ".txt", Path(td))
                self.assertTrue(p.name.endswith(".txt"))
                self.assertNotIn("/", p.name)
                self.assertNotIn("\\", p.name)
                self.assertLessEqual(len(p.name), 200)

    def test_reserved_names_prefixed(self):
        with tmpdir() as td:
            p = safe_filename("CON", ".txt", Path(td))
            self.assertNotEqual(p.name, "CON.txt")
            self.assertTrue(p.name.startswith("_"))

    def test_trailing_dot_stripped(self):
        with tmpdir() as td:
            p = safe_filename("a.", ".txt", Path(td))
            self.assertEqual(p.name, "a.txt")

    def test_dedup_not_overwrite(self):
        """同名导出第二次必须得到 (1)，否则用户会丢第一次的结果。"""
        with tmpdir() as td:
            d = doc()
            p1 = export(d, "txt", td, "same")
            p2 = export(d, "txt", td, "same")
            self.assertNotEqual(p1, p2)
            self.assertEqual(p2.name, "same(1).txt")
            self.assertTrue(p1.exists() and p2.exists())

    def test_overwrite_flag_allows_replace(self):
        with tmpdir() as td:
            p1 = export(doc(), "txt", td, "same", overwrite=True)
            p2 = export(doc(), "txt", td, "same", overwrite=True)
            self.assertEqual(p1, p2)

    def test_pattern_filename(self):
        name = pattern_filename("ocr_{date}_{time}_{lang}", {"target_lang": "zh"})
        self.assertTrue(name.startswith("ocr_"))
        self.assertIn("_zh", name)
        self.assertNotIn("{", name)
        # 未知占位符不报错、不吞掉
        self.assertIn("{unknown}", pattern_filename("{unknown}", {}))


class TestFormats(unittest.TestCase):
    def test_txt_structure(self):
        text = to_txt(doc())
        self.assertIn("[1]", text)
        self.assertIn("季度收入增长了 18%", text)
        self.assertIn("RapidOCR", text)          # meta 头
        self.assertEqual(len([l for l in text.splitlines() if l.startswith("[")]), 3)

    def test_txt_pairing_order(self):
        """原文与译文必须一一对应且顺序一致。"""
        for i, (s, t) in enumerate(ITEMS, 1):
            self.assertIn(s, to_txt(doc()))
            self.assertIn(t, to_txt(doc()))
        lines = [l for l in to_txt(doc(), numbered=True).splitlines() if l.startswith("[")]
        self.assertEqual(len(lines), len(ITEMS))

    def test_markdown_table_escaped(self):
        md = to_markdown(doc())
        self.assertIn("| # | 原文 | 译文 |", md)
        # 单元格里的竖线必须转义，否则表格列数错乱
        for line in md.splitlines():
            if line.startswith("| 3 |"):
                self.assertIn("\\|", line)

    def test_csv_roundtrip(self):
        text = to_csv(doc())
        self.assertTrue(text.startswith("\ufeff"))       # Excel 需要 BOM
        rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
        self.assertEqual(rows[0], ["#", "source", "target"])
        self.assertEqual(len(rows), 1 + len(ITEMS))
        for i, row in enumerate(rows[1:], 0):
            self.assertEqual(row[1], ITEMS[i][0])       # 引号/换行必须完好
            self.assertEqual(row[2], ITEMS[i][1])

    def test_json_roundtrip(self):
        data = json.loads(to_json(doc()))
        self.assertEqual(data["count"], 3)
        self.assertEqual(len(data["pairs"]), 3)
        for pair, (s, t) in zip(data["pairs"], ITEMS):
            self.assertEqual(pair["source"], s)
            self.assertEqual(pair["target"], t)

    def test_html_escapes(self):
        h = to_html(doc())
        self.assertIn("&quot;引号&quot;", h)
        self.assertNotIn("<script", h)
        self.assertEqual(h.count("<tr>"), len(ITEMS) + 1)

    def test_rtf_unicode_and_brace_escaping(self):
        r = to_rtf(doc())
        self.assertTrue(r.startswith(r"{\rtf1"))
        self.assertTrue(r.endswith("}"))
        self.assertIn("\\u", r)                        # 中文按 \uN 转义
        self.assertNotIn("本季度", r)                   # 输出里不应残留裸的非 ASCII

    def test_rtf_balances_braces(self):
        """内容里的花括号必须转义，否则 Word 认为文档结构破损（打开报错）。"""
        d = BilingualDoc("t", {}, [("dict = {k: v}", "代码块")])
        r = to_rtf(d)
        self.assertIn(r"\{", r)
        self.assertIn(r"\}", r)
        # 结构括号必须配平（转义后的 \{ \} 属于内容，不影响控制字解析）
        depth = 0
        i = 0
        while i < len(r):
            if r[i] == "\\":
                i += 2                                  # 跳过转义序列
                continue
            if r[i] == "{":
                depth += 1
            elif r[i] == "}":
                depth -= 1
                self.assertGreaterEqual(depth, 0, f"括号在第 {i} 字符提前闭合")
            i += 1
        self.assertEqual(depth, 0, "RTF 花括号不配平")

    def test_rtf_supplementary_plane_uses_surrogates(self):
        """emoji / CJK 扩展 B 在 BMP 之外：RTF 必须写成一对 \\u。

        这一类 bug 不抛异常，只会静默出乱码，所以专门测。
        U+1F600 -> 代理对 D83D DE00；RTF \\u 是有符号 16 位，
        故 D83D=55357 -> -10179，DE00=56832 -> -8704。
        """
        d = BilingualDoc("t", {}, [("报告 \U0001F600 完成", "emoji 测试")])
        r = to_rtf(d)
        self.assertNotIn("\U0001F600", r)
        self.assertIn("\\u-10179?", r)
        self.assertIn("\\u-8704?", r)

    def test_rtf_bmp_cjk_is_single_escape(self):
        """普通汉字必须只产生一次 \\u（不能被误当代理对拆成两次）。"""
        r = to_rtf(BilingualDoc("t", {}, [("中", "a")]))
        self.assertIn("\\u20013?", r)          # 0x4E2D = 20013
        self.assertEqual(r.count("\\u"), 1)

    def test_rtf_control_word_backslash_escaped(self):
        # 内容里的反斜线必须写成 \\：否则 \new 会被 Word 当控制字吞掉
        d = BilingualDoc("t", {}, [(r"C:\temp\newdir", "路径")])
        r = to_rtf(d)
        self.assertIn(r"C:\\temp", r)
        self.assertNotIn(r"temp\new", r)               # 未被转义的 \new 不应出现

    def test_docx_structure(self):
        with tmpdir() as td:
            p = export(doc(), "docx", td, "t")
            self.assertTrue(p.exists())
            # .docx 就是 zip：直接查 XML，不依赖 python-docx 读回来
            with zipfile.ZipFile(p) as z:
                names = z.namelist()
                self.assertIn("word/document.xml", names)
                xml = z.read("word/document.xml").decode("utf-8")
            self.assertIn("Quarterly revenue", xml)
            self.assertIn("季度收入增长了", xml)
            self.assertIn("<w:tbl>", xml)
            self.assertIn("微软雅黑", xml)              # eastAsia 字体必须显式设置

    def test_every_format_writes_nonempty(self):
        with tmpdir() as td:
            for fmt in FORMATS:
                p = export(doc(), fmt, td, f"all_{fmt}")
                self.assertTrue(p.exists(), fmt)
                self.assertGreater(p.stat().st_size, 20, f"{fmt} 内容过少")


class TestDegradation(unittest.TestCase):
    def test_docx_falls_back_to_rtf_without_python_docx(self):
        """缺 python-docx 时不能只是报错：要给出 Word 能打开的替代文件。"""
        import ocrtranslator.export as ex

        real = Path(__file__).resolve().parent.parent / "ocrtranslator"
        with tmpdir() as td:
            target = Path(td)
            # 制造 import 失败
            saved = sys.modules.get("docx")
            sys.modules["docx"] = None
            try:
                with self.assertRaises(ExportError) as cm:
                    ex.export(doc(), "docx", target, "degraded")
                self.assertIn("rtf", str(cm.exception).lower())
            finally:
                if saved is not None:
                    sys.modules["docx"] = saved
                else:
                    sys.modules.pop("docx", None)
            self.assertTrue((target / "degraded.rtf").exists(),
                            "降级应已写出同名 .rtf")

    def test_unknown_format(self):
        with tmpdir() as td:
            with self.assertRaises(ExportError):
                export(doc(), "pdf", td, "x")

    def test_write_failure_is_wrapped_readably(self):
        """写失败必须包成 ExportError 并给出人话，不能裸抛 OSError。

        不用 chmod 造只读目录：NTFS 上 chmod 基本是 no-op，
        权限由 ACL 决定，测试会"意外通过"或"意外失败"，与代码无关。
        直接注入失败更诚实 —— 这里考察的就是异常包装。
        """
        import ocrtranslator.export as ex

        real = ex._atomic
        try:
            def boom(*a, **kw):
                raise OSError(28, "No space left on device")
            ex._atomic = boom
            with tmpdir() as td:
                with self.assertRaises(ExportError) as cm:
                    ex.export(doc(), "txt", td, "x")
                self.assertIn("写入失败", str(cm.exception))
                self.assertIn("space", str(cm.exception))
        finally:
            ex._atomic = real

    def test_target_occupied_by_directory(self):
        """同名目录已存在时提前拒绝（导出目录选错成文件夹）。

        守卫在写入**之前**，所以文案是"目标位置是一个文件夹"，
        不是"写入失败" —— 两个错误路径要分别断言，别写成同一个。
        overwrite=False 时不会命中守卫：Path.exists() 对目录同样返回 True，
        于是 safe_filename 把名字让开成 conflict(1).txt —— 这也是对的
        （避开了目录且没丢数据），所以两种模式断言不同结果。
        """
        with tmpdir() as td:
            (Path(td) / "conflict.txt").mkdir()
            with self.assertRaises(ExportError) as cm:
                export(doc(), "txt", td, "conflict", overwrite=True)
            self.assertIn("文件夹", str(cm.exception))

            p = export(doc(), "txt", td, "conflict")      # 非覆盖：让开名字
            self.assertEqual(p.name, "conflict(1).txt")
            self.assertTrue(p.exists())


class TestDocAdapter(unittest.TestCase):
    """PipelineResult -> BilingualDoc：导出层与结果模型之间的唯一桥梁。"""

    def _result(self, pairs, engines=("MyMemory",), ocr="RapidOCR 1.4.4"):
        from ocrtranslator.models import (BlockKind, CaptureResult, OcrBlock,
                                          OcrLine, PipelineResult, TranslationUnit)

        units = []
        for i, (s, t, err) in enumerate(pairs):
            lines = [OcrLine(text=w, conf=0.9, box=(10, 10 * i, 200, 10 * i + 9))
                     for w in s.split("\n")]
            blk = OcrBlock(kind=BlockKind.TEXT, lines=lines, box=(10, 0, 200, 100),
                           reading_order=i)
            units.append(TranslationUnit(block=blk, target_text=t,
                                         engine=engines[0], error=err))
        return PipelineResult(capture=CaptureResult(200, 100, b""), units=units,
                              source_lang="en", target_lang="zh",
                              ocr_engine=ocr, translate_engine=engines[0],
                              elapsed_ms={"ocr": 480, "translate": 90},
                              warnings=["网络降级"])

    def test_to_document_keeps_every_row(self):
        """失败条目也必须保留（译文列留空），否则"3 条只导出 2 条"会被当成丢数据。"""
        res = self._result([("a", "甲", ""), ("b", "", "配额用尽"), ("c", "丙", "")])
        d = res.to_document()
        self.assertEqual(d.count, 3)
        self.assertEqual([s for s, _ in d.items], ["a", "b", "c"])
        self.assertEqual(d.items[1][1], "")

    def test_meta_fields(self):
        d = self._result([("a", "甲", "")]).to_document()
        self.assertIn("RapidOCR", d.meta["OCR"])
        self.assertEqual(d.meta["语向"], "en -> zh")
        self.assertIn("MyMemory", d.meta["翻译"])
        self.assertIn("480ms", d.meta["耗时"])

    def test_multiline_block_joined(self):
        res = self._result([("第一行\n第二行", "译", "")])
        d = res.to_document()
        self.assertEqual(d.items[0][0], "第一行第二行")   # join_lines 无空格拼接

    def test_plain_bilingual_includes_failure_reason(self):
        """剪贴板文本必须让失败可见 —— 只给原文会被误认为"翻译结果就是原文"。"""
        res = self._result([("hello", "你好", ""), ("x", "", "MyMemory 配额受限")])
        text = res.plain_bilingual
        self.assertIn("你好", text)
        self.assertIn("配额受限", text)
        self.assertIn("[1]", text)
        self.assertIn("[2]", text)

    def test_to_json_machine_format_keeps_geometry(self):
        """to_json 是给机器读的（含坐标/置信度），to_document 是给人读的。
        两者字段命名不同（ocr vs OCR）是刻意的，这里把契约钉住。"""
        res = self._result([("中", "文", "")])
        data = json.loads(res.to_json())
        self.assertEqual(data["pairs"][0]["source"], "中")
        self.assertEqual(data["meta"]["ocr"], "RapidOCR 1.4.4")
        self.assertEqual(data["meta"]["region"], [0, 0, 0, 0])
        self.assertIn("conf", data["pairs"][0])
        self.assertIn("box", data["pairs"][0])
        # 机器格式保留失败原因，人读格式不保留
        r2 = json.loads(self._result([("a", "", "配额")]).to_json())
        self.assertEqual(r2["pairs"][0]["error"], "配额")


if __name__ == "__main__":
    unittest.main(verbosity=2)
