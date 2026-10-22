"""qdcheck 的语句覆盖率：直接复用本仓 tools/cov.py 的统计口径（executable_lines / Collector / format_ranges，
sys.monitoring 语句覆盖，纯标准库），只是把统计范围换成 qdcheck 的文件清单。

    python3.12 qdcheck/tools/cov.py [--fail-under 90] [--json qdcheck/results/coverage.json]

（以脚本方式运行，与 tools/cov.py 一样：若用 -m 运行，qdcheck/__init__.py 会在开始监控之前被导入而被误记为未覆盖。）

口径：
- 计入判据的是库模块 qdcheck/*.py（selftest.py 除外：它只调度各步）；qdcheck/tools/*.py 与 selftest.py 单独列出、不计入；测试文件不统计。
- 与 tools/cov.py 相同：只统计主进程，子进程（determinism 的 PYTHONHASHSEED 子进程等）里执行的行不计入。
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

PKG = os.path.join(REPO, "qdcheck")


ORCHESTRATORS = ("selftest.py",)          # 只调度各步、在子进程里跑测试的脚本，与 tools/ 一样单列不计入


def library_files():
    return sorted(os.path.join(PKG, f) for f in os.listdir(PKG) if f.endswith(".py") and f not in ORCHESTRATORS)


def tool_files():
    d = os.path.join(PKG, "tools")
    return sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".py")) + \
        [os.path.join(PKG, f) for f in ORCHESTRATORS]


def summarize(collector, paths):
    """与 tools/cov.py 的 summarize 相同的计数规则，但作用于显式文件清单。"""
    files, tot, hit = {}, 0, 0
    for path in paths:
        lines, deco = base.executable_lines(path)
        seen = collector.hits.get(path, set())
        covered = {ln for ln in lines if any(x in seen for x in range(deco.get(ln, ln), ln + 1))}
        missed = sorted(lines - covered)
        files[os.path.relpath(path, REPO)] = {"statements": len(lines), "covered": len(covered), "missed": missed,
                                              "percent": 100.0 * len(covered) / len(lines) if lines else 100.0}
        tot += len(lines)
        hit += len(covered)
    return {"files": files, "total_statements": tot, "total_covered": hit,
            "total_percent": 100.0 * hit / tot if tot else 100.0}


def _print(title, summ):
    print(f"-- {title}")
    print(f"{'file':42s} {'stmts':>6s} {'miss':>6s} {'cover':>7s}  missing")
    for rel, r in summ["files"].items():
        print(f"{rel:42s} {r['statements']:6d} {r['statements'] - r['covered']:6d} {r['percent']:6.1f}%  "
              f"{base.format_ranges(r['missed'])[:60]}")
    print(f"{'TOTAL':42s} {summ['total_statements']:6d} {summ['total_statements'] - summ['total_covered']:6d} "
          f"{summ['total_percent']:6.1f}%")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--fail-under", type=float, default=0.0)
    ap.add_argument("--json", default=None)
    args = ap.parse_args(argv)
    os.environ["QDCHECK_COVERAGE"] = "1"                 # 性能阈值测试据此跳过（监控下计时无意义）
    collector = base.Collector([PKG])
    collector.start()
    try:
        suite = unittest.defaultTestLoader.discover(os.path.join(PKG, "tests"), pattern="test_*.py", top_level_dir=REPO)
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    finally:
        collector.stop()
    lib, tools = summarize(collector, library_files()), summarize(collector, tool_files())
    _print("library modules (counted)", lib)
    _print("tools (reported, not counted)", tools)
    report = {"command": "python3.12 qdcheck/tools/cov.py --fail-under 90", "library": lib, "tools": tools,
              "tests_run": result.testsRun, "tests_failed": len(result.failures) + len(result.errors),
              "tests_skipped": len(result.skipped), "skipped": [str(t[0]) + ": " + t[1] for t in result.skipped]}
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=1, sort_keys=True)
    ok = result.wasSuccessful() and lib["total_percent"] >= args.fail_under
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
