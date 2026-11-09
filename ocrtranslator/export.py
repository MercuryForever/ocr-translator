"""导出：剪贴板 / TXT / Markdown / DOCX / JSON / CSV / HTML。

设计约定：
  * 导出层**只吃 `BilingualDoc`**（中立的 title/meta/items 结构），
    不依赖 OCR 或翻译对象。这样"导入一个 TXT 做整篇批译"、"从历史记录导出"
    都能复用同一份代码，而不是和 PipelineResult 绑死。
  * 每种格式独立函数，失败只影响该格式；写文件一律**先写 .tmp 再 os.replace**，
    避免导出到一半崩溃留下半个文件（Word 打开半个 docx 会报"文件损坏"）。
  * DOCX 不假设用户装了 python-docx：缺依赖时降级为 RTF/HTML 并说明，
    而不是让"导出"这个动作整体失败。
"""

from __future__ import annotations

import csv
import html
import io
import json
import os
import re
import time
from pathlib import Path

from .logger import get_logger
from .models import BilingualDoc

log = get_logger("export")


class ExportError(RuntimeError):
    pass


FORMATS = ("txt", "md", "docx", "json", "csv", "html", "rtf")

_EXT = {"txt": ".txt", "md": ".md", "docx": ".docx",
        "json": ".json", "csv": ".csv", "html": ".html", "rtf": ".rtf"}


# ---------------------------------------------------------------- 文件名
_SAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_filename(stem: str, ext: str, folder: Path, max_len: int = 120,
                  overwrite: bool = False) -> Path:
    """清洗 + 去重。

    Windows 上非法字符比 POSIX 多（还要排除结尾空格与保留名 CON/PRN/NUL...），
    直接用原文当文件名会在"导出到桌面"这一步失败。
    重名时追加 (1)(2)：截图工具会频繁导出同名文件，静默覆盖会丢数据。
    `overwrite=True` 时保留清洗但跳过查重 —— 用户显式要求覆盖才覆盖。
    """
    stem = _SAFE.sub("_", (stem or "export").strip()).strip(". ")
    if not stem:
        stem = "export"
    # 保留名（不区分大小写，扩展名前后都可能）
    if stem.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL",
                                      *(f"COM{i}" for i in range(1, 10)),
                                      *(f"LPT{i}" for i in range(1, 10))}:
        stem = "_" + stem
    if len(stem) > max_len:
        stem = stem[:max_len].rstrip(". ")
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    cand = folder / f"{stem}{ext}"
    if overwrite:
        return cand
    n = 1
    while cand.exists():
        cand = folder / f"{stem}({n}){ext}"
        n += 1
    return cand


def pattern_filename(pattern: str, meta: dict[str, str]) -> str:
    """`ocr_{date}_{time}_{lang}` -> 实际名。未知占位符原样保留而不是报错。"""
    now = time.localtime()
    fields = {
        "date": time.strftime("%Y%m%d", now),
        "time": time.strftime("%H%M%S", now),
        "datetime": time.strftime("%Y%m%d-%H%M%S", now),
        "lang": meta.get("target_lang", "zh"),
        "srclang": meta.get("source_lang", "auto"),
        "engine": (meta.get("ocr", "") or "ocr").split()[0].lower(),
        "count": meta.get("count", "0"),
    }
    out = pattern
    for k, v in fields.items():
        out = out.replace("{" + k + "}", str(v))
    return out or "ocr_export"


# ---------------------------------------------------------------- 文本类
def to_txt(doc: BilingualDoc, numbered: bool = True) -> str:
    head = []
    if doc.title:
        head.append(doc.title)
    if doc.meta:
        head.append("  ".join(f"{k}: {v}" for k, v in doc.meta.items() if v))
        head.append("-" * 56)
    body = []
    for i, (src, tgt) in enumerate(doc.items, 1):
        if not src and not tgt:
            continue
        if numbered:
            body.append(f"[{i}] {src}")
            body.append(f"    {tgt}" if tgt else "    （无译文）")
        else:
            body.append(src)
            body.append(tgt)
        body.append("")
    return "\n".join([*head, *body]).rstrip() + "\n"


def to_markdown(doc: BilingualDoc) -> str:
    lines = [f"# {doc.title}" if doc.title else "# 双语对照", ""]
    if doc.meta:
        lines += ["| 项 | 值 |", "|---|---|"]
        lines += [f"| {k} | {v} |" for k, v in doc.meta.items() if v]
        lines.append("")
    # 表格形式在 Notion/Obsidian 里对齐得最好，所以 md 默认用表格
    if doc.items:
        lines += ["| # | 原文 | 译文 |", "|---:|---|---|"]
        for i, (s, t) in enumerate(doc.items, 1):
            lines.append(f"| {i} | {_md_cell(s)} | {_md_cell(t)} |")
    return "\n".join(lines) + "\n"


def _md_cell(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", "<br>")


def to_json(doc: BilingualDoc, indent: int = 2) -> str:
    payload = {"title": doc.title, "meta": doc.meta,
               "count": doc.count,
               "pairs": [{"source": s, "target": t} for s, t in doc.items]}
    return json.dumps(payload, ensure_ascii=False, indent=indent) + "\n"


def to_csv(doc: BilingualDoc) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(["#", "source", "target"])
    for i, (s, t) in enumerate(doc.items, 1):
        w.writerow([i, s, t])
    # utf-8-sig：Excel 读无 BOM 的 UTF-8 CSV 会把中文显示成乱码，
    # 这是"导出后打不开/乱码"投诉的头号原因。
    return "\ufeff" + buf.getvalue()


def to_html(doc: BilingualDoc) -> str:
    rows = []
    for i, (s, t) in enumerate(doc.items, 1):
        rows.append(f"<tr><td class='n'>{i}</td>"
                    f"<td>{html.escape(s or '')}</td>"
                    f"<td>{html.escape(t or '')}</td></tr>")
    meta = "".join(f"<li><b>{html.escape(k)}</b>：{html.escape(str(v))}</li>"
                   for k, v in doc.meta.items() if v)
    return f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>{html.escape(doc.title or '双语对照')}</title>
<style>
body{{font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;margin:24px;color:#1f2328}}
h1{{font-size:19px;margin:0 0 6px}} ul{{margin:0 0 16px;color:#57606a;font-size:12px}}
table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #d0d7de;padding:7px 9px;vertical-align:top;text-align:left}}
th{{background:#f6f8fa}} td.n{{color:#8b949e;width:34px;text-align:right}}
td:nth-child(3){{background:#fbfcfd}}
@media print{{table{{page-break-inside:auto}} tr{{page-break-inside:avoid}}}}
</style></head><body>
<h1>{html.escape(doc.title or '双语对照')}</h1><ul>{meta}</ul>
<table><thead><tr><th>#</th><th>原文</th><th>译文</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
</body></html>
"""


def _rtf_u(cp: int) -> str:
    """\\uN 需要一个"替代字符"紧随其后（老阅读器用它做降级显示），
    非 ASCII 码点必须补 '?'，否则部分阅读器会丢字。"""
    return f"\\u{cp if cp < 32768 else cp - 65536}?"


def to_rtf(doc: BilingualDoc) -> str:
    """python-docx 不可用时的降级路径。

    RTF 能被 Word/WPS/LibreOffice 直接打开，比让用户拿到一个 HTML 更贴近
    "我要 Word 文档" 的预期。中文必须 \\uN 转义（RTF 的 ANSI 码页不可靠）。
    """
    def esc(s: str) -> str:
        """逐字符转义。RTF 的 \\u 是**有符号 16 位**，且只能表达 BMP：
        emoji / CJK 扩展 B（>U+FFFF）必须拆成代理对写两次 \\u。
        早先写成 `ord(ch)-0x10000` 单发一次，会把 emoji 静默变成
        一个错误汉字 —— 用户看到"翻译里冒出乱码"却查不到原因。
        """
        out: list[str] = []
        for ch in s or "":
            if ch in "\\{}":
                out.append("\\" + ch)
            elif ord(ch) < 128:
                out.append(ch)
            else:
                cp = ord(ch)
                if cp <= 0xFFFF:
                    out.append(_rtf_u(cp))
                else:                                     # 代理对
                    v = cp - 0x10000
                    out.append(_rtf_u(0xD800 + (v >> 10)))
                    out.append(_rtf_u(0xDC00 + (v & 0x3FF)))
        return "".join(out)

    parts = [r"{\rtf1\ansi\ansicpg936\deff0",
             r"{\fonttbl{\f0 Microsoft YaHei;}{\f1 Consolas;}}",
             r"\f0\fs22"]
    if doc.title:
        parts.append(f"\\b \\fs32 {esc(doc.title)}\\b0\\par\\par ")
    for k, v in doc.meta.items():
        if v:
            parts.append(f"\\fs18\\gray120 {esc(k)}: {esc(str(v))}\\par ")
    parts.append("\\fs22\\par ")
    for i, (s, t) in enumerate(doc.items, 1):
        parts.append(f"\\b [{i}]\\b0  {esc(s)}\\par ")
        parts.append(f"\\i {esc(t)}\\i0\\par\\par ")
    parts.append("}")
    return "".join(parts)


# ---------------------------------------------------------------- 序列化
_WRITERS = {
    "txt": lambda d: to_txt(d),
    "md": lambda d: to_markdown(d),
    "json": lambda d: to_json(d),
    "csv": lambda d: to_csv(d),
    "html": lambda d: to_html(d),
    "docx": None,      # 二进制，单独处理
}


def export(doc: BilingualDoc, fmt: str, folder: Path | str,
           stem: str = "", *, overwrite: bool = False) -> Path:
    """导出一个文件，返回实际路径。失败抛 ExportError（带用户可读原因）。"""
    fmt = (fmt or "txt").lower().lstrip(".")
    if fmt not in FORMATS:
        raise ExportError(f"不支持的导出格式：{fmt}（可选 {', '.join(FORMATS)}）")
    folder = Path(folder)
    name = stem or pattern_filename("ocr_{date}_{time}", {**doc.meta,
                                                          "count": str(doc.count)})
    ext = _EXT[fmt]
    # overwrite 必须一路传到 safe_filename：只在下面加一个 `if overwrite` 分支
    # 是没用的（safe_filename 已经返回 same(1).txt 了，再"保留同一路径"等于没做）
    path = safe_filename(name, ext, folder, overwrite=overwrite)
    # 目标是已存在的**目录**时必须显式拒绝。
    # 不检查的话 Windows 上 os.replace 会把文件"移进"那个同名目录里，
    # 表面成功、实际找不到文件 —— 用户只会说"导出的文件不见了"。
    if path.is_dir():
        raise ExportError(f"目标位置是一个文件夹，无法写入：{path}")

    try:
        if fmt == "docx":
            _write_docx(doc, path)
        elif fmt == "rtf":
            _atomic(path, to_rtf(doc), encoding="ascii", errors="backslashreplace")
        else:
            text = _WRITERS[fmt](doc)
            # txt/md/json/csv 一律带 BOM 或不需要；html/csv 已在函数里处理
            _atomic(path, text, encoding="utf-8")
    except ExportError:
        raise
    except OSError as exc:
        raise ExportError(f"写入失败：{exc}"
                          + ("（目标可能是只读目录或占用中）" if fmt == "docx" else "")) from exc
    log.info("已导出 %s（%d 条，%.1f KB）", path.name, doc.count,
             path.stat().st_size / 1024)
    return path


def _atomic(path: Path, text: str, encoding: str = "utf-8", errors: str = "strict") -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding=encoding, errors=errors)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _write_docx(doc: BilingualDoc, path: Path) -> None:
    try:
        from docx import Document
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.oxml.ns import qn
        from docx.shared import Pt, RGBColor
    except ImportError as exc:
        # 降级到 RTF：同样是 Word 能开的文件，比直接失败好得多
        log.warning("python-docx 不可用（%s），改用 RTF 导出", exc)
        alt = path.with_suffix(".rtf")
        _atomic(alt, to_rtf(doc), encoding="ascii", errors="backslashreplace")
        raise ExportError(
            f"未安装 python-docx，已改为导出 {alt.name}（Word 可直接打开）。"
            "要真正的 .docx 请 pip install python-docx") from exc

    d = Document()
    # 中文字体必须同时设 ascii 与 eastAsia，否则 Word 里中文回落成宋体/方框
    style = d.styles["Normal"]
    style.font.name = "Segoe UI"
    style.font.size = Pt(10.5)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")

    if doc.title:
        h = d.add_heading(doc.title, level=1)
        h.alignment = WD_ALIGN_PARAGRAPH.LEFT
    if doc.meta:
        p = d.add_paragraph()
        for k, v in doc.meta.items():
            if not v:
                continue
            run = p.add_run(f"{k}：{v}   ")
            run.font.size = Pt(8.5)
            run.font.color.rgb = RGBColor(0x57, 0x60, 0x6A)

    table = d.add_table(rows=1, cols=3)
    table.style = "Table Grid"
    hdr = table.rows[0].cells
    for cell, label in zip(hdr, ("#", "原文", "译文")):
        run = cell.paragraphs[0].add_run(label)
        run.bold = True
    for i, (s, t) in enumerate(doc.items, 1):
        cells = table.add_row().cells
        cells[0].text = str(i)
        cells[1].text = s or ""
        cells[2].text = t or ""
        for c in cells:
            for par in c.paragraphs:
                for r in par.runs:
                    r.font.size = Pt(10)
                    r.font.name = "Segoe UI"
                    r.element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")
    tmp = path.with_name(path.name + ".tmp")
    d.save(str(tmp))
    os.replace(tmp, path)
