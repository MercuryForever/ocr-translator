"""把依赖以 wheel 形式直接抓取并解包到 vendor/，不经过 pip 的临时目录。

为什么需要这个脚本（而不是 `pip install -r requirements.txt`）：
  1. 受限环境 / 部分杀软下，pip 因无法删除临时目录而报 Errno 13，
     wheel 本身就是 zip，用 zipfile 解包只需要"写"权限；
  2. 内网离线部署：希望把依赖固化成 vendor 目录，运行时注入 sys.path，
     目标机器不装任何包也能跑（配合 build_exe.bat 打单文件）。

用法::

    python tools/fetch_wheels.py                     # 默认依赖集 -> vendor/
    python tools/fetch_wheels.py --only onnxruntime  # 只抓某个包
    python tools/fetch_wheels.py --download-only     # 只下载不解包（留给 pip 离线装）
"""

from __future__ import annotations

import argparse
import platform
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

DEFAULT_PKGS = [
    "PyQt6", "PyQt6-Qt6", "PyQt6-sip",
    "rapidocr-onnxruntime", "onnxruntime",
    "pyclipper", "shapely", "python-docx",
]

MIRRORS = [
    "https://pypi.tuna.tsinghua.edu.cn/simple",
    "https://mirrors.aliyun.com/pypi/simple",
    "https://pypi.org/simple",
]

UA = {"User-Agent": "socrans-fetchwheels/1.0"}
_ROOT = Path(__file__).resolve().parent.parent


def _platform_tags() -> list[str]:
    if sys.platform == "win32":
        return ["win_amd64", "win32"]
    if sys.platform == "darwin":
        return ["macosx_11_0_arm64", "macosx_10_12_universal2", "macosx_10_12_x86_64"]
    return [
        "manylinux_2_17_x86_64.manylinux2014_x86_64",
        "manylinux_2_28_x86_64",
        "manylinux2014_x86_64",
        "manylinux_2_17_aarch64.manylinux2014_aarch64",
        "musllinux_1_2_x86_64",
        "linux_x86_64",
    ]


def _acceptable_tags() -> list[str]:
    """从最优到最宽的可接受 wheel tag（`{py}-{abi}-{plat}`），列表位置即优先级。

    wheel 文件名是 `name-[build-]ver-py-abi-plat.whl`，plat 内部可用 `.` 连接多个
    复合 tag（如 `manylinux_2_17_x86_64.manylinux2014_x86_64`），所以必须整段取
    后 3 个字段，再与这张表精确比对。
    """
    plats = _platform_tags()
    out: list[str] = []
    # abi3 (stable ABI) 向后兼容：cp37-abi3 的 wheel 在 cp312 上可用，
    # 因此必须一直枚举到 3.7，否则会漏掉 opencv-python 这类只发 abi3 的包。
    for minor in range(sys.version_info.minor, 6, -1):
        cp = f"cp3{minor}"
        for plat in plats:
            out.append(f"{cp}-abi3-{plat}")
    for minor in range(sys.version_info.minor, 9, -1):
        cp = f"cp3{minor}"
        for plat in plats:
            out.append(f"{cp}-{cp}-{plat}")          # 精确解释器 tag
        for plat in plats:
            out.append(f"{cp}-none-{plat}")
    for plat in (*plats, "any"):
        out.append(f"py3-none-{plat}")               # 纯 Python 包
    out.append("py2.py3-none-any")
    return out


def _split_filename(label: str) -> tuple[str, str] | None:
    """从 wheel 文件名取 (version, tag)；不符合 PEP425 的返回 None。"""
    fields = label[:-4].split("-")
    if len(fields) < 5:
        return None
    return fields[1], "-".join(fields[-3:])


def _verkey(v: str) -> tuple:
    nums = [int(x) for x in re.findall(r"\d+", v)]
    return tuple(nums) if nums else (0,)


def _is_prerelease(v: str) -> bool:
    """`2.2.0rc1` 的数字 key 会大于 `2.2.0`，必须显式排除预发布。"""
    return bool(re.search(r"(rc|dev|alpha|beta|b\d|a\d)", v, re.I))


def _links(html: str):
    for m in re.finditer(r'<a\s+href="([^"]+?)"[^>]*>([^<]*)</a>', html):
        yield m.group(1), m.group(2).strip() or m.group(1).rsplit("/", 1)[-1]


def resolve(name: str) -> tuple[str, str]:
    """返回 (wheel 文件名, 下载 URL)。

    只接受与当前解释器/平台匹配的 wheel。**刻意不回落 sdist**：
    回落会在编译失败时给出误导性的"下载成功"，而且镜像上历史 sdist 版本
    往往远低于当前版（例如 Shapely 1.0.1），静默装上后运行期才炸更难排查。
    """
    norm = re.sub(r"[-_.]+", "-", name).lower()
    order = _acceptable_tags()
    err: Exception | None = None
    wheels_seen = 0
    for base in MIRRORS:
        try:
            req = urllib.request.Request(f"{base}/{norm}/", headers=UA)
            html = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            err = exc
            continue
        cands: list[tuple[int, tuple, str, str]] = []
        for href, _label in _links(html):
            frag = urllib.parse.urldefrag(href)[0].split("/")[-1]
            if not frag.endswith(".whl"):
                continue
            wheels_seen += 1
            parsed = _split_filename(frag)
            if not parsed:
                continue
            ver, tag = parsed
            if _is_prerelease(ver):
                continue
            rank = next((i for i, t in enumerate(order) if tag == t), 10_000)
            if rank >= 10_000:
                continue
            # 镜像索引里 href 是 "../../packages/<hh>/<hash>/<file>.whl#sha256=..."，
            # 必须相对**页面 URL** 解析；直接用文件名会得到 simple/<pkg>/<file> → 404
            url = href if href.startswith("http") else \
                urllib.parse.urljoin(f"{base}/{norm}/", urllib.parse.urldefrag(href)[0])
            cands.append((rank, _verkey(ver), frag, url))
        if not cands:
            continue
        # tag 兼容性优先（rank 小者优），同级里取**版本号最大**者
        cands.sort(key=lambda c: (c[0], tuple(-x for x in c[1])))
        _, _, label, url = cands[0]
        return label, url
    hint = "" if wheels_seen else "（镜像上该包一个 wheel 都没有，只能用 pip 源码编译）"
    raise RuntimeError(f"没有匹配 Python {sys.version.split()[0]} / {platform.system()} 的 wheel{hint}: {err}")


def fetch(pkgs: list[str], dest: Path, wheel_cache: Path, download_only: bool) -> int:
    wheel_cache.mkdir(parents=True, exist_ok=True)
    if not download_only:
        dest.mkdir(parents=True, exist_ok=True)
    failed: list[str] = []
    for pkg in pkgs:
        try:
            label, url = resolve(pkg)
        except RuntimeError as exc:
            print(f"[FAIL] {pkg}: {exc}")
            failed.append(pkg)
            continue
        local = wheel_cache / label
        if local.exists() and local.stat().st_size > 0:
            print(f"[hit ] {label}  ({local.stat().st_size / 1e6:.1f} MB cached)")
        else:
            print(f"[get ] {label} ...", end=" ", flush=True)
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=180) as r:
                    data = r.read()
                local.write_bytes(data)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                print(f"下载失败: {exc}")
                failed.append(pkg)
                continue
            print(f"{len(data) / 1e6:.1f} MB")
        if download_only:
            continue
        if label.endswith(".whl"):
            with zipfile.ZipFile(local) as z:
                z.extractall(dest)
            print(f"[ext ] -> {dest}")
        else:
            print(f"[skip] {label} 是源码包，需编译；请用 pip install --find-links {wheel_cache}")
    print()
    print(f"wheel 缓存目录: {wheel_cache}")
    print(f"离线安装: pip install --no-index --find-links \"{wheel_cache}\" -r requirements.txt")
    if failed:
        print(f"未成功: {', '.join(failed)}")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dest", default=str(_ROOT / "vendor"))
    ap.add_argument("--wheel-cache", default=str(_ROOT / ".wheels"))
    ap.add_argument("--only", action="append", default=None)
    ap.add_argument("--requirements", help="从 requirements 文件解析包名")
    ap.add_argument("--download-only", action="store_true")
    ns = ap.parse_args()

    pkgs = ns.only or list(DEFAULT_PKGS)
    if ns.requirements:
        pkgs = []
        for line in Path(ns.requirements).read_text(encoding="utf-8").splitlines():
            line = line.split("#")[0].strip()
            if line and not line.startswith("-"):
                pkgs.append(re.split(r"[<>=!~\[\s;]", line)[0])
    print(f"Python {sys.version.split()[0]} on {platform.system()} -> {len(pkgs)} 个包")
    return fetch(pkgs, Path(ns.dest), Path(ns.wheel_cache), ns.download_only)


if __name__ == "__main__":
    sys.exit(main())
