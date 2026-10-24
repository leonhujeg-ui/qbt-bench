"""qdexsim 的语句覆盖率：复用本仓 tools/cov.py 的统计口径（sys.monitoring 语句覆盖，纯标准库），并合并子进程里执行的行。

    python3.12 qdexsim/tools/cov.py [--fail-under 90] [--json qdexsim/results/coverage.json]

（以脚本方式运行，与 tools/cov.py 一样：若用 -m 运行，qdexsim/__init__.py 会在开始监控之前被导入而被误记为未覆盖。）
口径：计入判据的是库模块 qdexsim/*.py 与 qdexsim/dialects/*.py（selftest.py 除外）；tools / mutants / scenarios/gen.py / selftest.py
单独列出、不计入；测试文件不统计。子进程（服务端子进程、审计流）由环境变量 QDEXSIM_COV_DIR 触发 common.child_init 记录并在
退出时写出，这里并进主进程命中（并集）。
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
import unittest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tools import cov as base        # noqa: E402  本仓已有的覆盖率工具（import 时会 chdir 到仓库根）

PKG = os.path.join(REPO, "qdexsim")
ORCHESTRATORS = ("selftest.py",)


def library_files():
    out = sorted(os.path.join(PKG, f) for f in os.listdir(PKG) if f.endswith(".py") and f not in ORCHESTRATORS)
    d = os.path.join(PKG, "dialects")
    return out + sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".py"))


def other_files():
    out = [os.path.join(PKG, f) for f in ORCHESTRATORS] + [os.path.join(PKG, "scenarios", "gen.py"), os.path.join(PKG, "scenarios", "__init__.py")]
    for sub in ("tools", "mutants"):
        d = os.path.join(PKG, sub)
        out += sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".py"))
    return out


def merge_children(hits, cov_dir):
    n = 0
    for name in sorted(os.listdir(cov_dir)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(cov_dir, name), encoding="utf-8") as f:
            data = json.load(f)
        for path, lines in data.items():
            hits.setdefault(path, set()).update(lines)
        n += 1
    return n


def summarize(hits, paths):
    files, tot, hit = {}, 0, 0
    for path in paths:
        lines, deco = base.executable_lines(path)
        seen = hits.get(path, set())
        covered = {ln for ln in lines if any(x in seen for x in range(deco.get(ln, ln), ln + 1))}
        files[os.path.relpath(path, REPO)] = {"statements": len(lines), "covered": len(covered), "missed": sorted(lines - covered),
                                              "percent": 100.0 * len(covered) / len(lines) if lines else 100.0}
        tot += len(lines)
        hit += len(covered)
    return {"files": files, "total_statements": tot, "total_covered": hit, "total_percent": 100.0 * hit / tot if tot else 100.0}


def _print(title, summ):
    print(f"-- {title}")
    print(f"{'file':44s} {'stmts':>6s} {'miss':>6s} {'cover':>7s}  missing")
    for rel, r in summ["files"].items():
        print(f"{rel:44s} {r['statements']:6d} {r['statements'] - r['covered']:6d} {r['percent']:6.1f}%  {base.format_ranges(r['missed'])[:60]}")
    print(f"{'TOTAL':44s} {summ['total_statements']:6d} {summ['total_statements'] - summ['total_covered']:6d} {summ['total_percent']:6.1f}%")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--fail-under", type=float, default=0.0)
    ap.add_argument("--json", default=None)
    ap.add_argument("--pattern", default="test_*.py")
    args = ap.parse_args(argv)
    cov_dir = tempfile.mkdtemp(prefix="qdexsim-cov-")
    os.environ["QDEXSIM_COVERAGE"] = "1"
    os.environ["QDEXSIM_COV_DIR"] = cov_dir
    collector = base.Collector([PKG])
    collector.start()
    try:
        suite = unittest.defaultTestLoader.discover(os.path.join(PKG, "tests"), pattern=args.pattern, top_level_dir=REPO)
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    finally:
        collector.stop()
    hits = {k: set(v) for k, v in collector.hits.items()}
    children = merge_children(hits, cov_dir)
    shutil.rmtree(cov_dir, ignore_errors=True)
    lib, other = summarize(hits, library_files()), summarize(hits, other_files())
    _print("library modules (counted)", lib)
    _print("tools / mutants / scenarios gen / selftest (reported, not counted)", other)
    report = {"command": "python3.12 qdexsim/tools/cov.py --fail-under 90", "library": lib, "other": other,
              "child_processes_merged": children, "tests_run": result.testsRun,
              "tests_failed": len(result.failures) + len(result.errors), "tests_skipped": len(result.skipped)}
    print(f"child processes merged: {children}")
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=1, sort_keys=True)
    return 0 if result.wasSuccessful() and lib["total_percent"] >= args.fail_under else 1


if __name__ == "__main__":
    sys.exit(main())
