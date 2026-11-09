"""统一日志：文件（滚动）+ 内存环形缓冲（供 UI 日志面板读取）。

设计要点：
* GUI 程序不能用 print 调试，任何异常都必须落盘，否则打包后无法定位；
* UI 需要一个"最近 N 条"的视图，因此加一个 in-memory handler。
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from collections import deque
from datetime import datetime

_MAX_RECORDS = 2000
_buffer: deque[logging.LogRecord] = deque(maxlen=_MAX_RECORDS)
LOGGER_NAME = "socrans"


class RingHandler(logging.Handler):
    """把日志留在内存中，主窗口日志面板直接读取，无需解析文件。"""

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            _buffer.append(record)
        except Exception:  # pragma: no cover - 日志器本身绝不能抛异常
            pass


def recent(limit: int = 200, level: int | None = None) -> list[str]:
    """返回最近的日志文本行（默认倒序不处理，保持时间正序）。"""
    out = []
    for rec in list(_buffer)[-limit:]:
        if level is not None and rec.levelno < level:
            continue
        out.append(f"{datetime.fromtimestamp(rec.created).strftime('%H:%M:%S')} "
                   f"[{rec.levelname[:1]}] {rec.name}:{rec.funcName} | {rec.getMessage()}")
    return out


def force_utf8(stream) -> None:
    """把流固定为 UTF-8 + 行缓冲，且绝不因编码问题抛异常。

    为什么必须做（本机实测踩过两次）：
      * 本项目的输出含 ✓ ✗ → ≈（U+2713/2717/2192/2248），**都不在 GBK 里**。
        交互式控制台走 WriteConsoleW 没事；一旦被 `capture_output` 或
        `> file` 重定向，Python 就按 locale(cp936) 编码，
        轻则花屏（run.bat 里看到过），重则 UnicodeEncodeError 让
        "检查器自己崩掉"被上层误读成"这个环境不可用"。
      * 行缓冲：stdout 被管道化时默认块缓冲，而子进程**继承同一个 fd 并直接写**，
        于是父进程整块日志会排到子进程输出之后，用户会把结论
        归给错误的解释器。

    对包内其它模块（doctor.py 等）统一调用这里，不再各写一份。
    唯一例外是 tools/launcher.py：它必须在**任意**解释器下都能跑起来
    （包括连本项目依赖都装不上的那些），因此刻意不 import 本包，自带一份。
    """
    try:
        stream.reconfigure(encoding="utf-8", errors="replace",
                           line_buffering=True)
    except (AttributeError, ValueError, OSError):
        pass        # 老版本 Python、或已被替换成非 TextIOWrapper 的 stream


def get_logger(name: str = "") -> logging.Logger:
    """获取子日志器：``get_logger('ocr')`` -> ``socrans.ocr``。"""
    base = logging.getLogger(LOGGER_NAME)
    return base if not name else base.getChild(name)


def setup(log_dir: str | None = None, verbose: bool = False) -> logging.Logger:
    """初始化根日志器。重复调用安全（幂等）。"""
    base = logging.getLogger(LOGGER_NAME)
    if getattr(base, "_socrans_configured", False):
        if verbose:
            base.setLevel(logging.DEBUG)
        return base

    base.setLevel(logging.DEBUG if verbose else logging.INFO)
    base.propagate = False
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-22s %(funcName)-18s %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )

    # 重定向到文件/管道时 sys.stderr 用 locale 编码（中文 Windows = cp936），
    # 而日志里有 ✓ ✗ →（都不在 GBK 里），实测 run.bat 的输出就花屏了。
    # 控制台本身走 WriteConsoleW 不受影响，所以统一改成 UTF-8 是安全的。
    force_utf8(sys.stderr)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    console.setLevel(logging.DEBUG if verbose else logging.WARNING)
    base.addHandler(console)
    base.addHandler(RingHandler())

    if log_dir:
        try:
            os.makedirs(log_dir, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                os.path.join(log_dir, "socrans.log"),
                maxBytes=2_000_000, backupCount=3, encoding="utf-8",
            )
            fh.setFormatter(fmt)
            fh.setLevel(logging.DEBUG)
            base.addHandler(fh)
        except OSError as exc:  # 日志目录不可写不应阻止程序启动
            base.warning("无法创建日志目录 %s: %s", log_dir, exc)

    # 第三方库噪声：Paddle/ONNX/urllib3 都会打到 root
    for noisy in ("onnxruntime", "urllib3", "filelock", "paddle", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    base._socrans_configured = True  # type: ignore[attr-defined]
    return base
