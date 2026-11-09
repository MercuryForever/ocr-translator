"""解释器选择器：被 run.bat 调用，决定"用哪个 python 跑 main.py"。

为什么需要它（而不是在 .bat 里直接 `python main.py`）：
  本机实测 `python` 解析到 Anaconda 3.13，而 PyQt6 在它下面报
  `DLL load failed while importing QtWidgets: 找不到指定的模块`
  —— Anaconda 把它自带的 Qt5/ICU DLL 放进搜索路径，抢在 PyQt6 的 Qt6 DLL 前面。
  同一份代码在 python.org 的 3.12 下完全正常。
  这类"解释器相关"的失败在 batch 里用 `where python` 判断不出来，
  **只有真的 import 一次才知道**。所以这里对每个候选解释器跑一次
  `main.py --doctor`，取第一个通过的。

同时解决 .bat 的两个固有问题：
  * 中文 + `chcp 65001` + LF 换行会让 cmd 解析器丢位置（症状是"双击没反应"）。
    所以所有给人看的提示都由这里用 UTF-8 打印，.bat 只留 ASCII 单行命令。
  * 没装 Python 的机器上 `python` 会命中 Microsoft Store 存根（返回 9009），
    这里把它和"真的没有 python"区分开。

退出码约定（run.bat 依赖它）：
    0  = 成功
    1  = 失败，但**完整诊断已经打印过并已 pause**，调用方不要再重试
  >=2  只可能来自"launcher 自己没跑起来"（解释器不存在/版本不够），
       此时 run.bat 才应该换下一个命令再试。

只用标准库，且**绝不 import 项目自身模块** —— 本文件要在任意解释器下都能启动。
"""

from __future__ import annotations

import glob
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIN = (3, 10)
DOCTOR_TIMEOUT = 45
EXE_OK_TIMEOUT = 10
#: 整轮探测的总预算。没有它的话，12 个候选 × (10+45)s 最坏要 11 分钟，
#: 用户双击后看到的是"窗口开着但一动不动"，比直接报错更让人以为程序死了。
TOTAL_BUDGET = 100.0


def _utf8_output() -> None:
    """stdout/stderr 固定 UTF-8、行缓冲、且永不因编码崩掉。

    两个都必须做：
    * 编码：输出含 ✓ ✗（U+2713/2717），它们**不在 GBK 里**。交互控制台走
      WriteConsoleW 没事，但被 `capture_output` 或重定向到文件时按 locale(cp936)
      编码，print 会抛 UnicodeEncodeError，把"launcher 自己崩了"伪装成
      "这个解释器不可用"，从而误杀一个本来能跑的环境。
    * 行缓冲：stdout 被管道化时默认是块缓冲，而子进程**继承同一个 fd 并直接写**，
      于是我们的"跳过 X / 选用 Y"会整块排到子进程输出之后 ——
      实测就看到过：先打印了子进程的 ✓ 结论，再打印"跳过 …"，
      用户会以为那句结论属于被跳过的那个解释器。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace",
                               line_buffering=True)
        except (AttributeError, ValueError, OSError):
            pass


def _pause() -> None:
    """失败时按回车再退出。

    双击运行失败后窗口立刻关闭是这类工具最劝退的体验：用户只看到"闪一下"，
    连错误信息长什么样都看不到。用 Python 而不是 `pause`，因为中文提示
    经 cmd 输出时编码不可靠。
    """
    try:
        input("\n按回车键退出…")
    except (EOFError, OSError):
        pass            # 非交互（CI / 管道）时不阻塞


def _exe_ok(exe: list[str], timeout: float = EXE_OK_TIMEOUT) -> bool:
    """该解释器能跑 Python 3.10+ 吗。用 -c 而不是 --version：后者在 Windows 上可能弹控制台。"""
    probe = "import sys; raise SystemExit(0 if sys.version_info >= %r else 1)" % (MIN,)
    try:
        return subprocess.run([*exe, "-c", probe], capture_output=True,
                              timeout=max(1.0, timeout),
                              stdin=subprocess.DEVNULL).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _parse_py_launcher(out: str) -> list[str]:
    """解析 `py -0p` 的输出。

    ⚠ 每行形如 ` -V:3.12 *  D:\\anaconda3\\python.exe`，**去掉前导空格后以 '-' 开头**。
    第一版用 `line.startswith("-")` 当"没有路径"来跳过，结果把所有候选全跳过了
    —— 这个分支在装了 Anaconda 但 py 启动器也在的机器上会静默失效，
    排查起来毫无痕迹（只是"少试了一个解释器"）。正确做法是按 token 找 .exe。
    """
    found = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        path = parts[-1]
        if path.lower().endswith(("python.exe", "pythonw.exe", "python3.exe")):
            if Path(path).is_file():
                found.append(path)
    return found


def candidates() -> tuple[list[list[str]], list[str]]:
    """返回 (按可信度排序的候选命令, 被忽略的显式配置)。"""
    out: list[list[str]] = []
    seen: set[str] = set()
    ignored: list[str] = []

    def add(cmd: list[str]) -> None:
        key = " ".join(cmd)
        if key not in seen:
            seen.add(key)
            out.append(cmd)

    # 1) 用户显式指定 —— 不存在时必须说出来，不能静默跳过
    env = os.environ.get("SOCRANS_PY", "").strip()
    if env:
        if Path(env).is_file():
            add([env])
        else:
            ignored.append(f"SOCRANS_PY={env}（文件不存在，已忽略）")

    # 2) 当前解释器（run.bat 就是用它启动的，最可能是对的）
    add([sys.executable])

    # 3) 项目内 venv —— 用户按 README 建的话优先于系统 Python
    for name in (".venv", ".venv-build", "venv"):
        for sub in (r"Scripts\python.exe", "bin/python"):
            p = ROOT / name / sub
            if p.is_file():
                add([str(p)])

    # 4) py 启动器枚举到的所有版本
    try:
        r = subprocess.run(["py", "-0p"], capture_output=True, text=True, timeout=15)
        for path in _parse_py_launcher(r.stdout or ""):
            add([path])
    except (OSError, subprocess.TimeoutExpired):
        pass

    # 5) PATH 上的所有 python.exe。必须扫 PATH：本机 Anaconda 装在 D:\anaconda3，
    #    任何"常见安装目录"硬编码列表都覆盖不了用户自定义盘符/绿色安装。
    for d in os.environ.get("Path", os.environ.get("PATH", "")).split(os.pathsep):
        if not d:
            continue
        for name in ("python.exe", "python3.exe"):
            p = Path(d) / name
            if p.is_file():
                add([str(p)])
                break

    # 6) conda 自己指的路（装了 conda 的机器上最准，即使它不在 PATH 里）
    for var in ("CONDA_PREFIX", "CondaPrefix", "CONDA_PYTHON_EXE"):
        v = os.environ.get(var, "").strip()
        if not v:
            continue
        cand = v if Path(v).name.lower().startswith("python") else \
            str(Path(v) / "python.exe")
        if Path(cand).is_file():
            add([cand])

    # 7) 常见安装位置（含工具链自带的运行时；本机可用的 3.12 就在这类目录里）
    local = os.environ.get("LOCALAPPDATA", "")
    home = str(Path.home())
    pats = [
        r"C:\Python3*\python.exe",
        os.path.join(local, r"Programs\Python\Python3*\python.exe") if local else "",
        os.path.join(home, "anaconda3", "python.exe"),
        os.path.join(home, "miniconda3", "python.exe"),
        r"C:\ProgramData\anaconda3\python.exe",
        r"C:\ProgramData\miniconda3\python.exe",
        os.path.join(home, ".dsh", "dsh-runtimes", "*", "dependencies", "python", "python.exe"),
        os.path.join(home, r".pyenv\versions\3*\python.exe"),
        os.path.join(home, ".pyenv/versions/3*/bin/python"),
        "/usr/bin/python3",
        "/usr/local/bin/python3",
    ]
    for pat in pats:
        if not pat:
            continue
        for p in sorted(glob.glob(pat), reverse=True):      # 版本号大的先试
            if Path(p).is_file():
                add([p])

    # 8) 裸名字（最后：`python` 可能是 Store 存根，或 PATH 扫描已覆盖过）
    for name in ("python", "python3", "py"):
        add([name])

    # 上限保护：极端 PATH（CI 里几十个 python）下逐个探测会很慢。
    # 每个候选最坏 25s(_exe_ok) + 90s(doctor)，截断到 12 个保证不会卡死。
    return out[:12], ignored


def doctor(exe: list[str], timeout: float = DOCTOR_TIMEOUT) -> tuple[bool, str]:
    """跑 `main.py --doctor`。返回 (可用, 一行失败摘要)。

    ⚠ `stdin=DEVNULL` 不是可选的。main.py 在启动失败时会"按回车退出"，
    而这里探测时 stdout 被捕获、**stdin 仍继承控制台**，
    `isatty()` 为真 → 每个坏解释器都会卡在等输入直到超时，
    用户看到的现象是"双击后一动不动"。
    DEVNULL 让 isatty() 返回 False，那条暂停分支自己就跳过了。
    """
    try:
        r = subprocess.run([*exe, "main.py", "--doctor"], cwd=str(ROOT),
                           capture_output=True, text=True,
                           timeout=max(1.0, timeout),
                           encoding="utf-8", errors="replace",
                           stdin=subprocess.DEVNULL)
    except OSError as exc:
        return False, f"无法执行：{exc}"
    except subprocess.TimeoutExpired:
        return False, f"检查超时（>{timeout:.0f}s）"
    if r.returncode == 9009:
        return False, "不是可执行的 Python（可能是 Microsoft Store 占位程序）"
    if r.returncode == 0:
        return True, ""
    tail = (r.stdout or "") + (r.stderr or "")
    for line in reversed(tail.splitlines()):
        s = line.strip()
        if s.startswith(("✗", "!")) or "Error" in s:
            return False, s[:160]
    return False, (tail.strip().splitlines()[-1][:160] if tail.strip()
                   else f"退出码 {r.returncode}")


def show_full_diagnosis(exe: list[str]) -> None:
    """用继承 stdio 的方式再跑一次 doctor，把**完整**中文诊断直接打到控制台。

    为什么不直接把捕获到的 stdout 打印出来：捕获时丢失了颜色无关的细节还好，
    更重要的是让子进程自己写控制台，输出与用户手动运行 `python main.py --doctor`
    完全一致，避免"launcher 里的版本"和"手动排查的版本"说两套话。
    """
    print("\n" + "=" * 62)
    print("下面是在该解释器下的完整检查结果：")
    print("=" * 62)
    try:
        subprocess.run([*exe, "main.py", "--doctor"], cwd=str(ROOT),
                       timeout=DOCTOR_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"（无法再次检查：{exc}）")


def main(argv: list[str]) -> int:
    _utf8_output()
    print(f"socrans 启动器（当前解释器：{sys.executable}）")
    cands, ignored = candidates()
    for note in ignored:
        print(f"! {note}")

    tried: list[tuple[list[str], str]] = []
    deadline = time.monotonic() + TOTAL_BUDGET
    untried = 0
    for idx, exe in enumerate(cands):
        label = " ".join(exe)
        left = deadline - time.monotonic()
        if left <= 0:
            # 用 enumerate 的下标计剩余，不能用 len(cands)-len(tried)：
            # tried 只记 doctor 失败，被 _exe_ok 淘汰的候选不在里面，
            # 那样算出来的数字会偏大，等于给用户一个错的"还有几个没试"。
            untried = len(cands) - idx
            break
        if not _exe_ok(exe, timeout=min(EXE_OK_TIMEOUT, left)):
            continue
        left = deadline - time.monotonic()
        if left <= 0:
            untried = len(cands) - idx - 1
            break
        ok, why = doctor(exe, timeout=min(DOCTOR_TIMEOUT, left))
        if ok:
            # flush 必须在起子进程之前：stdout 被重定向（管道/文件）时是块缓冲，
            # 而子进程**继承同一个 fd 并直接写**，于是子进程的输出会插到我们
            # 自己的"选用解释器"之前 —— 实测 run.bat 里就看到过这种顺序错乱，
            # 用户会以为诊断结论来自上一个解释器。
            print(f"选用解释器：{label}\n", flush=True)
            try:
                rc = subprocess.run([*exe, "main.py", *argv], cwd=str(ROOT)).returncode
            except KeyboardInterrupt:
                return 1
            except OSError as exc:
                print(f"✗ 启动失败：{exc}")
                _pause()
                return 1
            if rc != 0:
                # GUI 自己失败了。它内部有日志与错误提示，这里只补一句可操作的引导。
                print(f"\n程序以退出码 {rc} 结束。")
                print("  排查：python main.py --selftest   （逐项检查并真的识别/翻译一次）")
                _pause()
            return 0 if rc == 0 else 1
        tried.append((exe, why))
        print(f"  跳过 {label} —— {why}")

    print("\n✗ 没有可用的 Python 环境。")
    if untried:
        print(f"  （探测预算 {TOTAL_BUDGET:.0f}s 用尽，还有 {untried} 个候选没试到）")
        print("  想继续试：set SOCRANS_PY=C:\\path\\to\\python.exe 后重跑，"
              "或调大本文件的 TOTAL_BUDGET")
    if not tried:
        print("  连 Python 3.10+ 都没找到。请安装：https://www.python.org/downloads/")
        print("  安装时勾选 “Add python.exe to PATH”。")
        print("  装好后重跑本脚本即可（它会自动发现新版本）。")
    else:
        print(f"  已尝试 {len(tried)} 个解释器，全部未通过检查。")
        # 挑"最接近能用"的那个展示完整诊断：失败原因最短的通常是依赖缺失。
        # 必须用存下来的 exe 列表，不能用 label.split() ——
        # "C:\Program Files\Python312\python.exe" 里有空格，按空格拆会拆成三段，
        # 于是这次诊断必然失败，用户看到两句互相矛盾的结论。
        best_exe, best_why = min(tried, key=lambda t: len(t[1]))
        if not best_why.startswith(("无法执行", "检查超时", "不是可执行")):
            show_full_diagnosis(best_exe)
        print("\n  最常见原因是缺依赖，任选一种补齐：")
        print("    pip install -r requirements.txt")
        print(r"    python tools\fetch_wheels.py        （免 pip，解包到 vendor\）")
        print("  也可以手动指定解释器后重试：")
        print(r"    set SOCRANS_PY=C:\path\to\python.exe")
    _pause()
    return 1


if __name__ == "__main__":
    # 兜底：launcher 自己崩掉时也必须 pause。
    # 否则返回码 1 会被 run.bat 当成"已经打印诊断并暂停过"而立刻 exit /b 1，
    # 窗口一闪而过 —— 正是这次要修的那个症状，绝不能由修复工具自己重新引入。
    try:
        code = main(sys.argv[1:])
    except SystemExit:
        raise
    except BaseException:
        import traceback

        traceback.print_exc()
        _pause()
        code = 1
    raise SystemExit(code)
