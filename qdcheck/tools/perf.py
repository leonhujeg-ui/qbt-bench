"""性能表：每块的规模 / 墙钟 / 峰值内存（ru_maxrss，各块在独立子进程里测，互不影响）。

    python3.12 -m qdcheck.tools.perf [--json qdcheck/results/perf.json]

峰值内存 = 子进程自己的 resource.getrusage(RUSAGE_SELF).ru_maxrss（Linux 为 KB），含解释器基线（约 10 MB）；
determinism / 覆盖率 / 突变会再起子进程，它们的子进程内存不计入（注明）。
"""

import argparse
import json
import os
import subprocess
import sys
import time

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
RESULTS = os.path.join(REPO, "qdcheck", "results")

# (名字, 规模, 目标, 在子进程里执行的代码)
BLOCKS = [
    ("vectors_regen", "1260 indicator vectors + 7 ledger vectors regenerated and compared", "无目标",
     "from qdcheck import gen_vectors as G; r = G.check_all(); assert r['ok']"),
    ("indicator_props", "2884 causality + 630 closed-form + 1476 ref/alt cross-checks", "无目标",
     "from qdcheck.tools import selfcheck as S; r = S.g1(); assert all(v['failures'] == 0 for v in r['summary'].values())"),
    ("fuzz_20k", "validate_good x 20000 cases (generation + call + reference check, no shrinking)", "<= 20 s",
     "from qdcheck import fuzz as F, examples as E; r = F.run(E.validate_good, E.BACKTEST_SCHEMA, n=20000, seed=0, "
     "is_controlled=E.ValidationError); assert r.ok"),
    ("fuzz_broken_3", "3 broken validates x 20000 cases incl. shrinking", "无目标",
     "from qdcheck import fuzz as F, examples as E; [F.run(v, E.BACKTEST_SCHEMA, n=20000, seed=0, is_controlled=E.ValidationError) "
     "for v in (E.validate_keyerror, E.validate_zerodiv, E.validate_recursion)]"),
    ("streams_200x3000", "200 streams x 3000 bars: generate + ledger + ledger_alt + bar-by-bar compare", "<= 180 s",
     "from qdcheck import stream as S; r = S.run_many(200, 3000); assert r['identity_violations'] == 0 == r['row_mismatches']"),
    ("qbt_ledger_54", "qbt: 54 backtests x 20000 bars reconciled against the reference ledger", "无目标",
     "from qdcheck.tools import qbt_ledger_check as Q; rows = Q.run(20000); assert all(r['ok'] for r in rows)"),
    ("harness_16", "determinism: 16 cases, each = in-process + 2 subprocesses (subprocess memory not included)", "无目标",
     "from qdcheck.tools import selfcheck as S; r = S.g4(); assert r['summary']['as_expected'] == r['summary']['cases']"),
    ("unittest", "python -m unittest discover -s qdcheck/tests, except test_report (it reads perf.json itself); "
                 "peak memory = max over the test process and its determinism subprocesses", "无目标", None),
]
WRAPPER = """
import json, resource, sys, time
t0 = time.perf_counter()
{code}
rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print(json.dumps({{"seconds": round(time.perf_counter() - t0, 2), "max_rss_kb": rss}}))
"""


def run_block(code):
    proc = subprocess.run([sys.executable, "-c", WRAPPER.format(code=code)], cwd=REPO, capture_output=True, text=True,
                          env=dict(os.environ, PYTHONPATH=REPO))
    if proc.returncode != 0:
        return {"error": proc.stderr[-800:]}
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _unittest_peak():
    """unittest 块的峰值内存：测它启动的子进程（RUSAGE_CHILDREN 里是全部子进程的最大值）。"""
    code = ("import json, resource, subprocess, sys, time; t0 = time.perf_counter(); "
            "p = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'qdcheck/tests', '-t', '.', '-p', 'test_[!r]*.py'], "
            "capture_output=True, text=True); assert p.returncode == 0, p.stderr[-500:]; "
            "print(json.dumps({'seconds': round(time.perf_counter() - t0, 2), "
            "'max_rss_kb': resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss}))")
    proc = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True)
    return json.loads(proc.stdout.strip().splitlines()[-1]) if proc.returncode == 0 else {"error": proc.stderr[-800:]}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=os.path.join(RESULTS, "perf.json"))
    args = ap.parse_args(argv)
    rows = []
    for name, scale, target, code in BLOCKS:
        t0 = time.perf_counter()
        res = _unittest_peak() if name == "unittest" else run_block(code)
        wall = round(time.perf_counter() - t0, 2)
        row = {"block": name, "scale": scale, "target": target, "wall_seconds": wall,
               "inner_seconds": res.get("seconds"), "max_rss_mb": round(res["max_rss_kb"] / 1024, 1) if "max_rss_kb" in res else None,
               "error": res.get("error")}
        rows.append(row)
        print(f"{name:18s} {wall:8.1f}s  {row['max_rss_mb'] if row['max_rss_mb'] is not None else '?':>8} MB  {target:10s} {scale}", flush=True)
    mut_path = os.path.join(RESULTS, "mutation.json")
    if os.path.exists(mut_path):
        with open(mut_path, encoding="utf-8") as f:
            mut = json.load(f)
        meta, total = mut["meta"], mut["summary"]["total"]
        rows.append({"block": f"mutation_{total}", "scale": f"{total} mutants x fast test suite, 4 workers (from results/mutation.json)",
                     "target": "无目标", "wall_seconds": meta.get("elapsed_seconds"), "inner_seconds": None, "max_rss_mb": None,
                     "error": None, "note": "多进程，内存未测"})
    out = {"command": "python3.12 -m qdcheck.tools.perf", "python": sys.version.split()[0], "cpu_count": os.cpu_count(),
           "rows": rows}
    with open(args.json, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, sort_keys=True, ensure_ascii=False)
    print(f"wrote {os.path.relpath(args.json, REPO)}")
    return 0 if all(r["error"] is None for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
