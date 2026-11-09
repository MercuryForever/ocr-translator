# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 配置。

三个必须显式处理的点，缺任何一个都会打出"双击没反应"的包：

1. **rapidocr 的 .onnx 模型是 package-data，不是代码**。
   PyInstaller 默认只收 .py，模型文件会被漏掉，运行时报
   "no such file: ch_PP-OCRv4_det_infer.onnx" —— 而且是在按下热键之后才报。
   用 collect_data_files 收全（含 config.yaml 与 dict 文本）。

2. **onnxruntime 的 C++ 扩展 DLL**。
   新版 PyInstaller 能自动跟随，但 opencv-python 与 onnxruntime 都带
   同名 DLL（如 libopenblas / msvcp140），必须 allow_libraries 去重，
   否则打包成功但启动时 "The procedure entry point ... could not be located"。

3. **控制台模式（console=True）**。
    这是一个"GUI 为主、CLI 可用"的混合程序：--file/--doctor/--selftest
    的输出必须在 pwsh 里可见，所以保留控制台；双击启动 GUI 时多余的黑窗
    由 main.py 的 _detach_console()（FreeConsole + 日志重定向）收掉。
    用 console=False 的话 CLI 模式 stdout 全部丢失，等于 --file 报废。

体积预期：单文件 ~180-220MB（Qt6 约 90MB + onnxruntime 15MB + OCR 模型 15MB + opencv 45MB）。
onedir 启动不用每次解包 100+MB（热键响应场景这是关键延迟），分发打包成 zip；
两种形态由环境变量切换：SOCRANS_ONEDIR=1 → 目录式，默认单文件。
"""

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, copy_metadata

ROOT = Path(SPECPATH).resolve()

# 依赖目录（源码运行时的 vendor 不影响打包，打包要求正常 pip 安装）
hidden = [
    # 本项目所有子模块都是运行期按需导入（OCR/翻译后端用 try-import 探测），
    # 静态分析容易漏，全部显式列出
    "ocrtranslator.capture.overlay",
    "ocrtranslator.capture.grab",
    "ocrtranslator.capture.hotkey",
    "ocrtranslator.ocr.rapid",
    "ocrtranslator.ocr.paddle",
    "ocrtranslator.ocr.tesseract",
    "ocrtranslator.translate.google_web",
    "ocrtranslator.translate.deepl",
    "ocrtranslator.translate.baidu",
    "ocrtranslator.translate.mymemory",
    "ocrtranslator.translate.opus",
    "ocrtranslator.ui.result_window",
    "ocrtranslator.ui.settings",
    "ocrtranslator.ui.tray",
    "ocrtranslator.ui.theme",
    # 默认 OCR 引擎：RapidOcrEngine.warmup() 里才 import，必须显式声明
    "rapidocr_onnxruntime",
    "onnxruntime",
    "cv2", "numpy", "PIL", "PIL.Image", "PIL.ImageDraw", "PIL.ImageFont",
    "docx", "yaml", "shapely", "pyclipper",
    # 注意：**不列 requests**。翻译层已改为纯 urllib（见 translate/http.py），
    # 列进来只会让包白白大 1.5MB，还会引入 urllib3/certifi 的版本冲突风险。
]

datas = []
datas += collect_data_files("rapidocr_onnxruntime")     # *.onnx + config.yaml + 字典文本
datas += copy_metadata("rapidocr-onnxruntime")          # importlib.metadata.version() 要读
datas += copy_metadata("onnxruntime")
# 注意：不要在这里 collect_data_files("PyQt6", ...)。
# Qt 插件（platforms/qwindows.dll）由 PyInstaller 自带的 PyQt6 hook 收集，
# 自己再收一遍会得到**两份**插件目录，运行时 Qt 选中错误的那份，
# 报 "Could not find the Qt platform plugin windows" —— 最难查的一类打包故障。

binaries = []
for pkg in ("onnxruntime", "cv2"):
    try:
        binaries += collect_dynamic_libs(pkg)
    except Exception as exc:
        print(f"[spec] collect_dynamic_libs({pkg}) 跳过：{exc}")

a = Analysis(
    ["main.py"],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=["tools/runtime_hook.py"],
    excludes=["tkinter", "matplotlib", "PyQt6.QtWebEngineCore", "PyQt6.QtQuick",
              "PyQt6.Qt3DCore", "PyQt6.QtCharts", "PyQt6.QtDataVisualization",
              "PyQt6.QtNetwork", "PyQt6.QtPdf", "PyQt6.QtPositioning",
              "paddle", "paddleocr", "torch", "transformers"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

onedir = os.environ.get("SOCRANS_ONEDIR") == "1"

if onedir:
    # 目录式：EXE 只装脚本，二进制/数据进 COLLECT 的目录（启动零解包）
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="截屏翻译",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=True,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=None,            # （待补充：图标 ico 文件，拿到后填路径即可）
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="截屏翻译",
    )
else:
    # 单文件：全部塞进 EXE，运行时解包到 %TEMP%（runtime_tmpdir=None）
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="截屏翻译",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,               # UPX 压 onnxruntime/opencv 的 DLL 会破坏其导出表
        upx_exclude=[],
        runtime_tmpdir=None,
        console=True,            # CLI 输出可见；GUI 双击黑窗由 _detach_console() 收掉
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=None,               # （待补充：图标 ico 文件，拿到后填路径即可）
    )
