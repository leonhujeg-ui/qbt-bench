"""纯标准库语句覆盖率工具（基于 Python 3.12 的 sys.monitoring，PEP 669）。

用法::

    python tools/cov.py [--include qbt strategies] [--fail-under 90] [--json out/coverage.json]
                        [--pattern 'test_*.py'] [--start tests]

统计口径（与 coverage.py 的“语句覆盖”对齐，差异已注明）：
- 可执行行 = 源文件 AST 中每个语句节点的起始行，排除：docstring、``global``/``nonlocal``、
  ``if __name__ == "__main__":`` 块、以及带 ``# pragma: no cover`` 注释的语句（含其子块）。
- 装饰器函数：装饰器首行到 ``def`` 行之间任何一行触发即视为 ``def`` 行已执行。
- 只统计主进程；子进程（进程池）里的执行不计入，因此优化器需有串行路径覆盖同一代码。
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sys
import unittest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
os.chdir(REPO)
TOOL_ID = sys.monitoring.COVERAGE_ID
_EXCLUDED_STMT = (ast.Global, ast.Nonlocal)


def executable_lines(path):
    """返回 (可执行行集合, {def 行: 装饰器起始行})。"""
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source, filename=path)
    pragma_lines = {i + 1 for i, line in enumerate(source.splitlines()) if "pragma: no cover" in line}
    lines, deco = set(), {}
    _collect(tree, lines, deco, pragma_lines, in_main_guard=False)
    return lines, deco


def _is_main_guard(node):
    if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
        return False
    t = node.test
    return (isinstance(t.left, ast.Name) and t.left.id == "__name__"
            and len(t.comparators) == 1 and isinstance(t.comparators[0], ast.Constant)
            and t.comparators[0].value == "__main__")


def _is_docstring(node, parent, index):
    return (index == 0 and isinstance(parent, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str))


def _collect(parent, lines, deco, pragma_lines, in_main_guard):
    for field in ("body", "orelse", "finalbody", "handlers", "cases"):
        for index, node in enumerate(getattr(parent, field, []) or []):
            _visit(node, parent, index, lines, deco, pragma_lines, in_main_guard)


def _visit(node, parent, index, lines, deco, pragma_lines, in_main_guard):
    if isinstance(node, ast.stmt):
        skip = isinstance(node, _EXCLUDED_STMT) or _is_docstring(node, parent, index)
        if node.lineno in pragma_lines:
            return                                   # 整个语句（含子块）排除
        if not skip and not in_main_guard:
            lines.add(node.lineno)
            dl = getattr(node, "decorator_list", None)
            if dl:
                deco[node.lineno] = dl[0].lineno
        in_main_guard = in_main_guard or _is_main_guard(node)
    _collect(node, lines, deco, pragma_lines, in_main_guard)
    if isinstance(node, ast.ExceptHandler) and not in_main_guard and node.lineno not in pragma_lines:
        lines.add(node.lineno)


class Collector:
    def __init__(self, roots):
        self.roots = tuple(os.path.abspath(r) + os.sep for r in roots)
        self.hits = {}

    def _on_line(self, code, line):
        fn = code.co_filename
        if fn.startswith(self.roots):
            s = self.hits.get(fn)
            if s is None:
                s = self.hits[fn] = set()
            s.add(line)
        return sys.monitoring.DISABLE

    def start(self):
        sys.monitoring.use_tool_id(TOOL_ID, "qbt-cov")
        sys.monitoring.register_callback(TOOL_ID, sys.monitoring.events.LINE, self._on_line)
        sys.monitoring.set_events(TOOL_ID, sys.monitoring.events.LINE)

    def stop(self):
        sys.monitoring.set_events(TOOL_ID, 0)
        sys.monitoring.free_tool_id(TOOL_ID)


def iter_py_files(roots):
    for root in roots:
        for dirpath, _dirs, files in os.walk(root):
            for name in sorted(files):
                if name.endswith(".py"):
                    yield os.path.abspath(os.path.join(dirpath, name))


def summarize(collector, roots):
    report, tot_exec, tot_hit = {}, 0, 0
    for path in iter_py_files(roots):
        lines, deco = executable_lines(path)
        hit = collector.hits.get(path, set())
        covered = set()
        for ln in lines:
            lo = deco.get(ln, ln)
            if any(x in hit for x in range(lo, ln + 1)):
                covered.add(ln)
        missed = sorted(lines - covered)
        rel = os.path.relpath(path)
        report[rel] = {"statements": len(lines), "covered": len(covered), "missed": missed,
                       "percent": (100.0 * len(covered) / len(lines)) if lines else 100.0}
        tot_exec += len(lines)
        tot_hit += len(covered)
    total = (100.0 * tot_hit / tot_exec) if tot_exec else 100.0
    return {"files": report, "total_statements": tot_exec, "total_covered": tot_hit, "total_percent": total}


def format_ranges(missed):
    out, start, prev = [], None, None
    for ln in missed + [None]:
        if start is None:
            start = prev = ln
        elif ln is not None and ln == prev + 1:
            prev = ln
        else:
            out.append(f"{start}" if start == prev else f"{start}-{prev}")
            start = prev = ln
    return ",".join(out)


def print_report(summary):
    print(f"{'file':40s} {'stmts':>6s} {'miss':>6s} {'cover':>7s}  missing")
    for rel, r in sorted(summary["files"].items()):
        miss = r["statements"] - r["covered"]
        print(f"{rel:40s} {r['statements']:6d} {miss:6d} {r['percent']:6.1f}%  {format_ranges(r['missed'])[:70]}")
    print(f"{'TOTAL':40s} {summary['total_statements']:6d} "
          f"{summary['total_statements'] - summary['total_covered']:6d} {summary['total_percent']:6.1f}%")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--include", nargs="+", default=["qbt", "strategies"])
    ap.add_argument("--start", default="tests")
    ap.add_argument("--pattern", default="test_*.py")
    ap.add_argument("--fail-under", type=float, default=0.0)
    ap.add_argument("--json", default="out/coverage.json")
    args = ap.parse_args(argv)
    os.environ.setdefault("QBT_COVERAGE", "1")          # 性能阈值测试据此跳过（监控下计时无意义）
    collector = Collector(args.include)
    collector.start()
    try:
        suite = unittest.defaultTestLoader.discover(args.start, pattern=args.pattern)
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    finally:
        collector.stop()
    summary = summarize(collector, args.include)
    summary["tests_run"] = result.testsRun
    summary["tests_failed"] = len(result.failures) + len(result.errors)
    summary["tests_skipped"] = len(result.skipped)
    print_report(summary)
    if args.json:
        os.makedirs(os.path.dirname(args.json) or ".", exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=1, sort_keys=True)
    ok = result.wasSuccessful() and summary["total_percent"] >= args.fail_under
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
