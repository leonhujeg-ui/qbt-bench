"""qdtext 的语句覆盖率：复用本仓 tools/cov.py 的统计口径（sys.monitoring 语句覆盖，纯标准库）。

    python3.12 qdtext/tools/cov.py [--fail-under 90] [--json qdtext/results/coverage.json]

（以脚本方式运行；若用 -m 运行，qdtext/__init__.py 会在开始监控之前被导入而被误记为未覆盖。）
口径：计入判据的是库模块 qdtext/*.py（selftest.py 除外：它只调度各步，自己跑 unittest 会递归）；qdtext/tools/*.py、
qdtext/mutants/*.py 与 selftest.py 单独列出、不计入；测试文件不统计。只统计主进程：mobile_layout 起的 Chromium 不是 Python，
tests/_audit_flow.py 子进程里执行的行不计入（只会让覆盖率偏低）。
"""
import argparse
import json
import os
import sys
import unittest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tools import cov as base        # noqa: E402  本仓已有的覆盖率工具（import 时会 chdir 到仓库根）

PKG = os.path.join(REPO, "qdtext")
ORCHESTRATORS = ("selftest.py",)


def library_files():
    return sorted(os.path.join(PKG, f) for f in os.listdir(PKG) if f.endswith(".py") and f not in ORCHESTRATORS)


def other_files():
    out = [os.path.join(PKG, f) for f in ORCHESTRATORS]
    for sub in ("tools", "mutants"):
        d = os.path.join(PKG, sub)
        out += sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".py"))
    return out


def summarize(hits, paths):
    """与 tools/cov.py 的 summarize 相同的计数规则（装饰器行区间任一行命中即算 def 行），作用于显式文件清单。"""
    files, tot, cov = {}, 0, 0
    for path in paths:
        lines, deco = base.executable_lines(path)
        hit = hits.get(path, set())
        covered = {ln for ln in lines if any(x in hit for x in range(deco.get(ln, ln), ln + 1))}
        rel = os.path.relpath(path, REPO)
        files[rel] = {"statements": len(lines), "covered": len(covered), "missed": sorted(lines - covered),
                      "percent": (100.0 * len(covered) / len(lines)) if lines else 100.0}
        tot += len(lines)
        cov += len(covered)
    return {"files": files, "total_statements": tot, "total_covered": cov, "total_percent": (100.0 * cov / tot) if tot else 100.0}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--fail-under", type=float, default=90.0)
    ap.add_argument("--json", default=os.path.join(PKG, "results", "coverage.json"))
    ap.add_argument("--pattern", default="test_*.py")
    args = ap.parse_args(argv)
    os.environ["QDTEXT_COVERAGE"] = "1"
    collector = base.Collector([PKG])
    collector.start()
    try:
        suite = unittest.defaultTestLoader.discover(os.path.join(PKG, "tests"), pattern=args.pattern, top_level_dir=REPO)
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    finally:
        collector.stop()
    lib, other = summarize(collector.hits, library_files()), summarize(collector.hits, other_files())
    print("== library (counts toward the gate) ==")
    base.print_report(lib)
    print("== orchestrator / tools / mutants (listed, not counted) ==")
    base.print_report(other)
    doc = {"library": lib, "other": other, "tests_run": result.testsRun, "tests_failed": len(result.failures) + len(result.errors),
           "tests_skipped": len(result.skipped), "fail_under": args.fail_under}
    os.makedirs(os.path.dirname(args.json), exist_ok=True)
    with open(args.json, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1, sort_keys=True)
    ok = result.wasSuccessful() and lib["total_percent"] >= args.fail_under
    print(f"qdtext library coverage {lib['total_percent']:.2f}% ({'OK' if ok else 'FAIL'}; required {args.fail_under}%)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
