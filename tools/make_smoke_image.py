"""渲染打包验证用的测试图片：混排中英文、多行、含标点。

用途：exe 的 --file 端到端验证需要一张"内容已知"的图，
跑完后人工比对 OCR+译文是否覆盖全部 4 行。
渲染参数（白底黑字、大字号、无边框装饰）是 OCR 最友好的形态，
专测打包链路而不是 OCR 质量——后者已有 70 单元格的预处理实验数据。
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "smoke_bilingual.png"

LINES = [
    ("msyh.ttc", 30, "Python OCR 翻译工具自检样本"),
    ("msyh.ttc", 24, "Hello world, this is line two."),
    ("msyh.ttc", 24, "第三行：中英 mixed text 123"),
    ("msyh.ttc", 20, "Last line with punctuation, ok!"),
]


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (760, 220), "white")
    d = ImageDraw.Draw(img)
    y = 24
    for name, size, text in LINES:
        font = ImageFont.truetype(f"C:/Windows/Fonts/{name}", size)
        d.text((30, y), text, font=font, fill="black")
        y += size + 18
    img.save(OUT)
    print(OUT)


if __name__ == "__main__":
    main()
