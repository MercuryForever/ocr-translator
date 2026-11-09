"""静态检查：**未绑定名字**（用了但按 Python 作用域规则根本取不到）。

为什么不用 pyflakes：内网/受限环境经常装不上额外工具（本机 pip 就因
目录 ACL 失败），而本项目反复踩的正是同一类错误 —— `Qt` 用了没导入、
`log` 在模块级用了没定义、函数里用了只在**别的函数**里 import 过的名字。
这类错误 py_compile 查不出（语法完全合法），只在运行到那一行时才 NameError，
往往要等 GUI 真跑起来才暴露，代价高。

⚠ 本工具**必须按作用域**判断，不能"全文件收集所有绑定名"。
第一版就是全文件收集，结果 `run_gui` 里 `from PyQt6.QtCore import Qt`
让同一文件的 `_install_shortcuts` 里的 `Qt` 被误判为已绑定 ——
检查器给出"0 处问题"，程序照样崩。假阴性的检查器比没有检查器更糟。

规则（与 CPython 一致）：
  * 函数作用域：函数内任何位置的赋值/导入/参数/推导式目标都算绑定，
    即使写在读取语句之后（Python 编译期就决定局部性）。
  * 类作用域：**不**是其方法的 enclosing scope。
        class C:
            X = 1
            def m(self): return X      # 运行时 NameError，本工具会报
  * `global`/`nonlocal` 声明的名字归到模块/外层作用域。
  * 嵌套函数可见外层函数的局部（闭包）。
  * 出现 `import *` 的文件跳过报告（无法知道导入了什么，硬判会误报）。

用法::

    python tools/check_bindings.py                 # 检查全部
    python tools/check_bindings.py --quiet         # 只打印有问题的文件
    python tools/check_bindings.py --self-test     # 正/负对照，验证检查器本身
"""

from __future__ import annotations

import argparse
import ast
import builtins
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILTIN = set(dir(builtins)) | {
    "__file__", "__name__", "__doc__", "__package__", "__spec__",
    "__loader__", "__builtins__", "__debug__", "__path__", "__class__",
}


# ------------------------------------------------------------------ 作用域
@dataclass
class Scope:
    kind: str                                  # module | function | class | lambda
    names: set[str] = field(default_factory=set)

    def bind(self, n: str) -> None:
        self.names.add(n)

    def contains(self, n: str) -> bool:
        return n in self.names


def _argnames(a: ast.arguments) -> set[str]:
    out = {x.arg for x in (*a.posonlyargs, *a.args, *a.kwonlyargs)}
    if a.vararg:
        out.add(a.vararg.arg)
    if a.kwarg:
        out.add(a.kwarg.arg)
    return out


def collect_bindings(node: ast.AST, *, stop_nested: bool) -> set[str]:
    """收集 node 子树内产生的绑定名。

    stop_nested=True 时不进入嵌套的 def/class 体（那些属于它们自己的作用域），
    但**要**收下嵌套 def/class 的**名字**本身（它们绑定在当前层）。
    """
    out: set[str] = set()

    def add_target(t: ast.expr) -> None:
        for sub in ast.walk(t):
            if isinstance(sub, ast.Name):
                out.add(sub.id)
            elif isinstance(sub, ast.Attribute):
                pass

    for child in ast.iter_child_nodes(node):
        if stop_nested and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                              ast.ClassDef)):
            out.add(child.name)
            continue
        _walk_collect(child, out, add_target, stop_nested)
    return out


def _walk_collect(node: ast.AST, out: set[str], add_target, stop_nested: bool) -> None:
    for n in ast.walk(node):
        if stop_nested and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                          ast.ClassDef)) and n is not node:
            out.add(n.name)
            # 装饰器与默认值在当前层求值，需要单独收
            for d in getattr(n, "decorator_list", []):
                _walk_collect(d, out, add_target, False)
            args = getattr(n, "args", None)
            if args:
                for base in (*args.defaults, *args.kw_defaults):
                    if base:
                        _walk_collect(base, out, add_target, False)
            continue
        if isinstance(n, (ast.Name,)) and isinstance(n.ctx, (ast.Store, ast.Del)):
            out.add(n.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                if a.name == "*":
                    out.add("*")
                else:
                    out.add(a.asname or a.name.split(".")[0])
        elif isinstance(n, (ast.ExceptHandler,)) and n.name:
            out.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            out.update(n.names)
        elif isinstance(n, ast.Lambda):
            out.update(_argnames(n.args))
        elif isinstance(n, ast.arg):
            out.add(n.arg)
        elif isinstance(n, (ast.MatchAs, ast.MatchStar)) and n.name:
            out.add(n.name)
        elif isinstance(n, ast.MatchMapping) and n.rest:
            out.add(n.rest)
        elif isinstance(n, (ast.TypeAlias,)):
            add_target(n.name)


# ------------------------------------------------------------------ 检查
class Checker:
    def __init__(self, path: Path, src: str):
        self.path = path
        self.src = src
        self.lines = src.splitlines()
        self.problems: list[tuple[int, str, str]] = []
        self.has_star = False
        self.stack: list[Scope] = []

    # ---- 作用域查询
    def known(self, name: str) -> bool:
        if name in BUILTIN:
            return True
        top = self.stack[-1]
        for i in range(len(self.stack) - 1, -1, -1):
            sc = self.stack[i]
            # 类作用域对其内部定义的函数不可见（CPython 语义）
            if sc.kind == "class" and sc is not top:
                continue
            if sc.contains(name):
                return True
        return False

    def push(self, sc: Scope) -> None:
        self.stack.append(sc)

    def pop(self) -> None:
        self.stack.pop()

    # ---- 遍历
    def run(self) -> list[tuple[int, str, str]]:
        tree = ast.parse(self.src, filename=str(self.path))
        module = Scope("module")
        module.names |= collect_bindings(tree, stop_nested=True)
        # 任何位置的 global 声明都算模块级绑定
        for n in ast.walk(tree):
            if isinstance(n, ast.Global):
                module.names.update(n.names)
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                for a in n.names:
                    if a.name == "*":
                        self.has_star = True
        self.push(module)
        self.visit_body(tree.body, module)
        self.pop()
        return self.problems

    def visit_body(self, body: list[ast.stmt], scope: Scope) -> None:
        for stmt in body:
            self.visit(stmt, scope)

    def visit(self, node: ast.AST, scope: Scope) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            inner = Scope("function")
            inner.names |= collect_bindings(node, stop_nested=True)
            inner.names |= _argnames(node.args)
            # 装饰器、默认值、返回值注解在**外层**作用域求值
            for d in node.decorator_list:
                self.visit(d, scope)
            args = node.args
            for base in (*args.defaults, *(x for x in args.kw_defaults if x)):
                self.visit(base, scope)
            for a in (*args.posonlyargs, *args.args, *args.kwonlyargs):
                if a.annotation:
                    self.visit(a.annotation, scope)
            if node.returns:
                self.visit(node.returns, scope)
            self.push(inner)
            self.visit_body(node.body, inner)
            self.pop()
            return
        if isinstance(node, ast.ClassDef):
            inner = Scope("class")
            inner.names |= collect_bindings(node, stop_nested=True)
            for d in node.decorator_list:
                self.visit(d, scope)
            for b in node.bases:
                self.visit(b, scope)
            for k in node.keywords:
                self.visit(k.value, scope)
            self.push(inner)
            self.visit_body(node.body, inner)
            self.pop()
            return
        if isinstance(node, ast.Lambda):
            inner = Scope("lambda")
            inner.names |= _argnames(node.args)
            inner.names |= collect_bindings(node, stop_nested=False)
            args = node.args
            for base in (*args.defaults, *(x for x in args.kw_defaults if x)):
                self.visit(base, scope)
            self.push(inner)
            self.visit(node.body, inner)
            self.pop()
            return
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load) and not self.known(node.id):
                line = self.lines[node.lineno - 1].strip() if node.lineno <= len(self.lines) else ""
                self.problems.append((node.lineno, node.id, line[:78]))
            return
        if isinstance(node, ast.arg):
            return
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            return
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return
        for child in ast.iter_child_nodes(node):
            self.visit(child, scope)


def check_file(path: Path) -> list[str]:
    src = path.read_text(encoding="utf-8", errors="replace")
    try:
        ck = Checker(path, src)
        problems = ck.run()
    except SyntaxError as exc:
        return [f"{path.name}:{exc.lineno}: 语法错误 {exc.msg}"]
    if ck.has_star:
        return []          # 通配导入无法判定，跳过而不是硬猜
    return [f"{path.name}:{ln}: 未绑定名字 {name!r}  |  {line}"
            for ln, name, line in problems]


# ------------------------------------------------------------------ 自检
CONTROL = '''\
import os
from typing import Any

CONST = 1

def good(a, b=2):
    for i in range(a):
        with open("x") as fh:
            pass
        y = [z * i for z in fh]
        w = (v := 5)
    try:
        pass
    except ValueError as e:
        log = str(e)
    return a, b, i, y, w, v, fh, log, os, CONST, Any

def bad_local():
    log.info("x")                      # 未导入
    return Qt.WindowType + undefined_name

def uses_other_func_import():
    return os                        # 只在 good 里没绑？os 是模块级导入 -> 合法

class C:
    X = 1
    def m(self):
        return X                       # 类作用域对方法不可见 -> 必须报
    def n(self):
        return self.X                  # 合法

def forward_ok():
    return later_bound
later_bound = 1                        # 模块级前向引用：合法（调用时才解析）

def closure_ok():
    outer = 1
    def inner():
        return outer                   # 闭包：合法
    return inner()

def global_decl():
    global NEW_GLOBAL
    NEW_GLOBAL = 2
'''

CONTROL_EXPECT = {"log", "Qt", "undefined_name", "X"}


def self_test() -> int:
    """验证检查器本身：既不能漏报（正对照），也不能误报（负对照）。

    第一版检查器就是因为没有这一节，给出"0 处问题"的假安心结论。
    """
    print("检查器自检")
    # 不用 TemporaryDirectory：本机工作区 ACL 禁止删除子目录，
    # CPython 的清理逻辑会在 __exit__ 抛 PermissionError 并**盖掉真实结果**。
    d = ROOT / ".tmp" / "ctl"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "ctl_selftest.py"
    p.write_text(CONTROL, encoding="utf-8")
    found = check_file(p)
    try:
        p.unlink()
    except OSError:
        pass
    names = {t.split("未绑定名字 ")[1].split(" ")[0].strip("'") for t in found}
    ok = True
    for expect in sorted(CONTROL_EXPECT):
        if expect in names:
            print(f"  [✓] 正对照：抓到未绑定的 {expect!r}")
        else:
            print(f"  [✗] 正对照漏报：{expect!r} 没被抓到")
            ok = False
    for t in found:
        print(f"      -> {t}")
    # 负对照：合法名字绝不能被报
    legal = {"os", "Any", "CONST", "later_bound", "outer", "NEW_GLOBAL",
             "a", "b", "i", "y", "w", "v", "fh", "e", "self"}
    bad = {n for n in names if n in legal}
    if bad:
        print(f"  [✗] 负对照误报：{sorted(bad)}")
        ok = False
    else:
        print("  [✓] 负对照：合法作用域（局部/闭包/前向/global）均未误报")
    print("自检结果：" + ("通过" if ok else "不通过"))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("targets", nargs="*")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--self-test", action="store_true", help="验证检查器本身")
    ns = ap.parse_args()
    if ns.self_test:
        return self_test()

    roots = [Path(t) for t in (ns.targets or ["ocrtranslator", "main.py",
                                              "tools", "tests"])]
    files: list[Path] = []
    for r in roots:
        r = r if r.is_absolute() else ROOT / r
        if r.is_dir():
            files += sorted(p for p in r.rglob("*.py")
                            if "vendor" not in p.parts
                            and "__pycache__" not in p.parts
                            and p.name != Path(__file__).name)
        elif r.is_file():
            files.append(r)
    total = 0
    for f in files:
        probs = check_file(f)
        if probs:
            total += len(probs)
            rel = f.relative_to(ROOT) if f.is_relative_to(ROOT) else f
            print(f"--- {rel}")
            for p in probs:
                print("   ", p)
        elif not ns.quiet:
            print(f"ok  {f.name}")
    print(f"\n检查 {len(files)} 个文件，发现 {total} 处未绑定名字")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
