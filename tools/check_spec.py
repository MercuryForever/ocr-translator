"""在不安装 PyInstaller 的前提下**执行** socrans.spec，验证它的 Python 逻辑。

为什么需要：spec 文件是用户拿到项目后**最后才执行**的一段代码，
一旦里面有 NameError / 参数拼错 / 路径猜错，用户要等 10 分钟编译完
才看到失败（或者更糟：编译"成功"但产物缺文件，双击没反应）。
本工具用桩替换 PyInstaller 的 API，把 spec 当普通脚本跑一遍：
  * 语法与名字解析（SPECPATH / Analysis / PYZ / EXE 是否用对）
  * collect_data_files / copy_metadata 的**参数是否真的能解析到东西**
    （这是最常见的静默失败：包名写错 → datas 为空 → 产物缺模型）
  * excludes 里引用的模块是否存在（写错名字不会报错，只是白写）

真实打包仍需 `build_exe.bat`，这里只保证 spec 本身不低级出错。

用法::

    python tools/check_spec.py            # 检查 socrans.spec
    python tools/check_spec.py --verbose  # 打印收集到的条目
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Artifact:
    """记录被"构建"出来的东西，供断言。"""

    def __init__(self) -> None:
        self.datas: list = []
        self.binaries: list = []
        self.hidden: list[str] = []
        self.excludes: list[str] = []
        self.exe: dict = {}
        self.entry: list = []


ART = Artifact()


# ---------------------------------------------------------------- 桩 API
def _stub_pyi():
    import types

    mod = types.ModuleType("PyInstaller")
    utils = types.ModuleType("PyInstaller.utils")
    hooks = types.ModuleType("PyInstaller.utils.hooks")
    building = types.ModuleType("PyInstaller.building")
    build_m = types.ModuleType("PyInstaller.building.build_main")

    missing: list[str] = []

    def collect_data_files(pkg, includes=None, **kw):
        spec = importlib.util.find_spec(pkg)
        if spec is None:
            missing.append(f"collect_data_files({pkg})：包不存在")
            return []
        base = Path(spec.origin or ".").parent
        out = []
        pats = includes or ["**/*"]
        for pat in pats:
            for p in base.glob(pat):
                if p.is_file() and p.suffix != ".py":
                    out.append((str(p), str(p.parent.relative_to(base.parent))))
        return out

    def collect_dynamic_libs(pkg, **kw):
        spec = importlib.util.find_spec(pkg)
        if spec is None:
            missing.append(f"collect_dynamic_libs({pkg})：包不存在")
            return []
        base = Path(spec.origin or ".").parent
        exts = {".dll", ".so", ".dylib"}
        return [(str(p), str(p.parent.relative_to(base.parent)))
                for p in base.rglob("*") if p.suffix in exts]

    def copy_metadata(pkg, **kw):
        from importlib.metadata import distribution, PackageNotFoundError

        try:
            d = distribution(pkg)
        except PackageNotFoundError:
            missing.append(f"copy_metadata({pkg})：发行包不存在（名字要用 pip 的名字）")
            return []
        dest = f"meta/{d.metadata['Name']}-{d.version}"
        return [(str(f) if hasattr(f, "locate") else str(f), dest)
                for f in (d.files or [])[:3]] or [(str(d._path), dest)]

    hooks.collect_data_files = collect_data_files
    hooks.collect_dynamic_libs = collect_dynamic_libs
    hooks.copy_metadata = copy_metadata

    class Analysis:
        def __init__(self, scripts, pathex=None, binaries=None, datas=None,
                     hiddenimports=None, hookspath=None, hooksconfig=None,
                     runtime_hooks=None, excludes=None, noarchive=False,
                     optimize=0, **kw):
            ART.entry = list(scripts)
            ART.datas = list(datas or [])
            ART.binaries = list(binaries or [])
            ART.hidden = list(hiddenimports or [])
            ART.excludes = list(excludes or [])
            self.datas, self.binaries, self.hiddenimports = datas, binaries, hiddenimports
            self.pure, self.scripts = [], list(scripts)
            for rh in (runtime_hooks or []):
                p = ROOT / rh
                if not p.is_file():
                    missing.append(f"runtime_hooks：{rh} 不存在")
            for s in scripts:
                if not (ROOT / s).is_file():
                    missing.append(f"入口脚本 {s} 不存在")

    class PYZ:
        def __init__(self, pure, *a, **kw):
            self.name = "pyz"

    class EXE:
        def __init__(self, *args, **kw):
            ART.exe = dict(kw)
            self.name = kw.get("name")

    class COLLECT:
        def __init__(self, *a, **kw):
            self.name = kw.get("name")

    build_m.Analysis = Analysis
    # 父子模块属性必须互相挂上：`import PyInstaller.utils.hooks` 走的是
    # 父模块的属性查找，只塞 sys.modules 不够（第一版就漏了这步，AttributeError）
    utils.hooks = hooks
    mod.utils = utils
    building.build_main = build_m
    mod.building = building
    mod.Analysis, mod.PYZ, mod.EXE, mod.COLLECT = Analysis, PYZ, EXE, COLLECT
    return mod, missing


def run_spec(spec_path: Path) -> tuple[list[str], Artifact]:
    src = spec_path.read_text(encoding="utf-8")
    mod, missing = _stub_pyi()
    for name, obj in (("PyInstaller", mod),
                      ("PyInstaller.utils", mod.utils),
                      ("PyInstaller.utils.hooks", mod.utils.hooks),
                      ("PyInstaller.building", mod.building),
                      ("PyInstaller.building.build_main", mod.building.build_main)):
        sys.modules[name] = obj
    g = {"__file__": str(spec_path), "__name__": "__spec__",
         "SPECPATH": str(spec_path.parent),
         "DISTPATH": str(ROOT / "dist"), "workpath": str(ROOT / "build"),
         "Analysis": mod.Analysis, "PYZ": mod.PYZ, "EXE": mod.EXE,
         "COLLECT": mod.COLLECT, "warn": lambda *a: None}
    try:
        exec(compile(src, str(spec_path), "exec"), g)
    except Exception as exc:
        missing.append(f"spec 执行失败：{type(exc).__name__}: {exc}")
    return missing, ART


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("spec", nargs="?", default="socrans.spec")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--no-vendor", action="store_true",
                    help="不注入 vendor（模拟『干净机器』，此时缺包会全部报错）")
    ns = ap.parse_args()

    if not ns.no_vendor:
        # 关键：检查器必须用**应用自己的**依赖解析方式。
        # 否则在"源码 + vendor 免安装"的机器上会把一切判成缺失，
        # 报出一屏假阳性，反而让人不敢信这个工具。
        sys.path.insert(0, str(ROOT))
        try:
            from ocrtranslator import bootstrap
            bootstrap.install()
        except Exception as exc:
            print(f"[warn] vendor 注入失败：{exc}")

    spec = Path(ns.spec)
    if not spec.is_absolute():
        spec = ROOT / spec
    if not spec.is_file():
        print(f"找不到 spec：{spec}")
        return 2

    print(f"校验 {spec.name}")
    problems, art = run_spec(spec)

    # ---- 结构性检查
    print(f"  入口脚本      : {art.entry}")
    print(f"  收集到的数据  : {len(art.datas)} 项")
    print(f"  收集到的动态库: {len(art.binaries)} 项")
    print(f"  hiddenimports : {len(art.hidden)} 项")
    print(f"  excludes      : {len(art.excludes)} 项")
    print(f"  EXE 参数      : console={art.exe.get('console')} "
          f"upx={art.exe.get('upx')} name={art.exe.get('name')}")

    onnx = [d for d in art.datas if str(d[0]).endswith(".onnx")]
    yaml = [d for d in art.datas if str(d[0]).endswith((".yaml", ".yml"))]
    dicts = [d for d in art.datas if "ppocr" in str(d[0]).lower()
             or "dict" in str(d[0]).lower() or str(d[0]).endswith(".txt")]
    print(f"\n  OCR 模型 .onnx : {len(onnx)}")
    print(f"  config.yaml    : {len(yaml)}")
    print(f"  字典/文本      : {len(dicts)}")

    # 关键断言：模型没被收集 = 打出的包必然运行失败
    if not onnx:
        problems.append("没有收集到任何 .onnx 模型：打包后 OCR 会报文件不存在")
    if not yaml:
        problems.append("没有收集到 config.yaml：RapidOCR 初始化会失败")
    if art.exe.get("console") is not True:
        problems.append("EXE(console=...) 不是 True：--file/--doctor 的 CLI 输出会全部丢失"
                        "（GUI 双击的黑窗由 main.py 的 _detach_console 收掉）")
    if art.exe.get("upx"):
        problems.append("开了 UPX：可能破坏 onnxruntime/opencv 的 DLL 导出表")

    # hiddenimports 指向的模块是否真的存在
    for name in art.hidden:
        top = name.split(".")[0]
        try:
            if importlib.util.find_spec(top) is None:
                problems.append(f"hiddenimports {name}：顶层包 {top} 未安装")
        except (ImportError, ValueError):
            problems.append(f"hiddenimports {name}：无法解析")

    if ns.verbose:
        for d in art.datas[:40]:
            print("   data:", d)
        for b in art.binaries[:20]:
            print("   bin :", b)

    print()
    if problems:
        print(f"发现 {len(problems)} 个问题：")
        for p in problems:
            print("  ✗", p)
        return 1
    print("spec 结构校验通过（真实打包请运行 build_exe.bat）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
