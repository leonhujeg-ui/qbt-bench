"""一条命令跑全部自检：unittest + 覆盖率 + 向量复现 / 契约校验 + 指标性质 + 模糊器自检 + 账本自检 + harness 自检。

    python3.12 -m qdcheck.selftest            # 全量（约 3 分钟，单核）；任何一项失败 -> 退出码 1
    python3.12 -m qdcheck.selftest --quick    # 约 1 分钟：unittest 用 QDCHECK_FAST=1，跳过覆盖率，账本 20 条流 x 1000 根
    python3.12 -m qdcheck.selftest --refresh  # 同全量，但把结果写回 qdcheck/results/（REPORT.md 引用的快照）并重建 REPORT.md

写文件：缺省只写 qdcheck/results/selftest/（本次运行的各项 JSON）与 qdcheck/results/selftest.json（总表）；
--refresh 时写 qdcheck/results/*.json 并重建 qdcheck/REPORT.md。不联网，不写仓库之外。
"""

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import time

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
RESULTS = os.path.join(REPO, "qdcheck", "results")


def _machine():
    return {"python": sys.version.split()[0], "implementation": platform.python_implementation(),
            "platform": platform.platform(), "machine": platform.machine(), "cpu_count": os.cpu_count()}


def _write(out_dir, name, obj):
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, sort_keys=True, ensure_ascii=False)


# ---- 各步 ------------------------------------------------------------------------------
def step_unittest(quick, out_dir):
    env = dict(os.environ, QDCHECK_FAST="1") if quick else dict(os.environ)
    env.pop("QDCHECK_COVERAGE", None)
    proc = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "qdcheck/tests", "-t", "."],
                          cwd=REPO, env=env, capture_output=True, text=True)
    ran = re.search(r"Ran (\d+) tests", proc.stderr)
    skipped = re.search(r"skipped=(\d+)", proc.stderr)
    n = int(ran.group(1)) if ran else 0
    ok = proc.returncode == 0 and n > 0
    tail = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else ""
    return {"scale": f"{n} tests" + (" (QDCHECK_FAST=1)" if quick else ""), "ok": ok,
            "summary": f"{tail}; skipped {int(skipped.group(1)) if skipped else 0}",
            "detail": {"tests_run": n, "returncode": proc.returncode, "stderr_tail": proc.stderr[-3000:]}}


def step_coverage(quick, out_dir):
    if quick:
        return {"scale": "-", "ok": True, "summary": "skipped (--quick)", "detail": {"skipped": True}}
    path = os.path.join(out_dir, "coverage.json")
    os.makedirs(out_dir, exist_ok=True)
    proc = subprocess.run([sys.executable, "qdcheck/tools/cov.py", "--fail-under", "90", "--json", path],
                          cwd=REPO, capture_output=True, text=True)
    cov = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
    lib = cov.get("library", {})
    pct = lib.get("total_percent", 0.0)
    return {"scale": f"{lib.get('total_statements', 0)} statements, {cov.get('tests_run', 0)} tests",
            "ok": proc.returncode == 0 and pct >= 90.0,
            "summary": f"{pct:.2f}% (>= 90% required); tests failed {cov.get('tests_failed', '?')}",
            "detail": {"total_percent": pct, "returncode": proc.returncode}}


def step_vectors(quick, out_dir):
    from qdcheck import gen_vectors, vector_schema
    chk = gen_vectors.check_all()
    schema = vector_schema.check_directory()
    _write(out_dir, "vectors_check.json", chk)
    ok = chk["ok"] and schema["ok"]
    return {"scale": f"{chk['vectors']} indicator vectors + {chk['ledger']['vectors']} ledger vectors, {schema['checked']} files",
            "ok": ok, "summary": f"regenerated: mismatches {len(chk['mismatches'])}, max_rel_err {chk['max_rel_err']:.1e}; "
                                 f"schema: {'all valid' if schema['ok'] else 'INVALID'}",
            "detail": {"mismatches": chk["mismatches"][:10], "schema_problems": {k: v for k, v in schema["files"].items() if v}}}


def step_indicators(quick, out_dir):
    from qdcheck.tools import selfcheck
    res = selfcheck.g1()
    _write(out_dir, "g1_selfcheck.json", res)
    fails = sum(v["failures"] for v in res["summary"].values())
    checks = sum(v["checks"] for v in res["summary"].values())
    return {"scale": f"{checks} property checks", "ok": fails == 0 and res["vectors"]["ok"],
            "summary": f"failures {fails}; ref vs alt max tol ratio {max(res['cross_check_max_tol_ratio'].values()):.2f}",
            "detail": res["summary"]}


def step_fuzz(quick, out_dir):
    from qdcheck.tools import selfcheck
    res = selfcheck.g2()
    _write(out_dir, "g2_selfcheck.json", res)
    s = res["summary"]
    per = s["per_validate"]
    ok = (s["coverage_ge_500"] and s["good_validate_le_20s"] and per["validate_good"]["no_bare"]
          and all(v["caught"] and v["ratio_le_30pct"] for v in per.values())
          and not res["runs"]["validate_good"]["report"]["fields_under_4_kinds"])
    return {"scale": "4 validates x 20000 cases", "ok": ok,
            "summary": (f"good: bare {per['validate_good']['outcomes']['bare']}, {s['good_validate_seconds']:.2f}s; caught "
                        + ", ".join(f"{k.split('_')[1]} x{sum(v['bare_by_type'].values())}" for k, v in per.items() if k != "validate_good")
                        + f"; nan/inf/missing/type {s['coverage_cases']['nan']}/{s['coverage_cases']['inf']}/"
                          f"{s['coverage_cases']['missing_key']}/{s['coverage_cases']['type_wrong']}"),
            "detail": s}


def step_ledger(quick, out_dir):
    from qdcheck import stream
    n_streams, n_bars = (20, 1000) if quick else (200, 3000)
    res = stream.run_many(n_streams, n_bars)
    res["command"] = f"python3.12 -m qdcheck.stream --streams {n_streams} --bars {n_bars}"
    _write(out_dir, "g3_streams.json", res)
    ok = res["identity_violations"] == 0 and res["alt_issues"] == 0 and res["row_mismatches"] == 0
    return {"scale": f"{n_streams} streams x {n_bars} bars, {res['total_records']} records", "ok": ok,
            "summary": (f"identity violations {res['identity_violations']}, alt issues {res['alt_issues']}, "
                        f"row mismatches {res['row_mismatches']} ({res['values_compared']} values); "
                        f"liquidations {res['counters']['liquidation']}"),
            "detail": {k: res[k] for k in ("identity_violations", "alt_issues", "row_mismatches", "counters", "seconds")}}


def step_harness(quick, out_dir):
    from qdcheck.tools import selfcheck
    res = selfcheck.g4()
    _write(out_dir, "g4_selfcheck.json", res)
    s = res["summary"]
    ok = s["as_expected"] == s["cases"] and s["set_order_engine_caught"] and s["deterministic_engine_passed"]
    return {"scale": f"{s['cases']} cases (4 fake engines + {s['qbt_cases']} qbt backtests)", "ok": ok,
            "summary": f"as expected {s['as_expected']}/{s['cases']}; set-order engine caught: {s['set_order_engine_caught']}; "
                       f"qbt identical {s['qbt_cases_identical']}/{s['qbt_cases']}",
            "detail": s}


STEPS = [("unittest", step_unittest), ("coverage", step_coverage), ("vectors", step_vectors),
         ("indicators", step_indicators), ("fuzz", step_fuzz), ("ledger", step_ledger), ("harness", step_harness)]


def run_all(quick=False, refresh=False):
    out_dir = RESULTS if refresh else os.path.join(RESULTS, "selftest")
    order = STEPS
    if refresh:          # 先刷新结果文件 -> 覆盖率 -> 重建 REPORT -> 最后 unittest（它检查 REPORT 与结果文件一致）
        names = ["vectors", "indicators", "fuzz", "ledger", "harness", "coverage", "unittest"]
        order = [next(s for s in STEPS if s[0] == n) for n in names]
    rows = []
    t_all = time.perf_counter()
    for name, fn in order:
        t0 = time.perf_counter()
        try:
            row = fn(quick, out_dir)
        except Exception as exc:                       # noqa: BLE001 —— 自检本身崩溃也要算失败并继续
            row = {"scale": "-", "ok": False, "summary": f"CRASH {type(exc).__name__}: {exc}"[:200], "detail": {}}
        row.update(step=name, seconds=round(time.perf_counter() - t0, 1))
        rows.append(row)
        print(_fmt_row(row), flush=True)
        if refresh and name == "coverage":
            from qdcheck.tools import build_report
            build_report.main([])
    total = round(time.perf_counter() - t_all, 1)
    ok = all(r["ok"] for r in rows)
    summary = {"command": "python3.12 -m qdcheck.selftest" + (" --quick" if quick else "") + (" --refresh" if refresh else ""),
               "machine": _machine(), "ok": ok, "seconds": total, "steps": rows}
    _write(RESULTS, "selftest.json", summary)
    return summary


def _fmt_row(r):
    return f"{r['step']:11s} {r['scale']:52s} {r['seconds']:7.1f}s  {'OK  ' if r['ok'] else 'FAIL'}  {r['summary']}"


def main(argv=None):
    ap = argparse.ArgumentParser(description="qdcheck 一键自检")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--refresh", action="store_true", help="写回 qdcheck/results/ 并重建 REPORT.md")
    args = ap.parse_args(argv)
    print(f"qdcheck selftest  {_machine()['platform']}  Python {_machine()['python']}")
    print(f"{'step':11s} {'scale':52s} {'time':>8s}  {'result':4s}  key numbers")
    summary = run_all(args.quick, args.refresh)
    print(f"{'TOTAL':11s} {'':52s} {summary['seconds']:7.1f}s  {'OK  ' if summary['ok'] else 'FAIL'}  "
          f"{sum(r['ok'] for r in summary['steps'])}/{len(summary['steps'])} steps passed; "
          f"written qdcheck/results/selftest.json")
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
