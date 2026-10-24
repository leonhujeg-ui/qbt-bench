"""一条命令跑全部自检：unittest + 工程约束 + 覆盖率 + 各方言签名 / 撮合 / 重启自检 + 混沌可复现 + 两个参考客户端跑全部场景 +
对账自检 + 三刀，最后打印总表；任何一项失败 → 退出码 1。

    python3.12 -m qdexsim.selftest            # 缺省：全部步骤；结果写 qdexsim/results/selftest/（各步 JSON + summary.json）
    python3.12 -m qdexsim.selftest --quick    # 跳过覆盖率与三刀
    python3.12 -m qdexsim.selftest --full     # 同缺省，但各步结果写回 qdexsim/results/*.json（REPORT.md 引用的快照）

中间文件全在 tempfile。只连 / 只听 127.0.0.1。
"""
import argparse
import os
import re
import subprocess
import sys
import tempfile
import time

from qdexsim import common

RESULTS = common.RESULTS
REQUIRED_CONSTRAINTS = ("test_stdlib_only", "test_static", "test_writes_binds_and_connects")


def _row(scale, ok, summary, detail=None):
    return {"scale": scale, "ok": bool(ok), "summary": summary, "detail": detail or {}}


def scratch_dirs():
    tmp = tempfile.gettempdir()
    return {n for n in os.listdir(tmp) if n.startswith("qdexsim-")}


def step_unittest(mode, out_dir, state):
    env = dict(os.environ)
    for k in ("QDEXSIM_COVERAGE", "QDEXSIM_COV_DIR"):
        env.pop(k, None)
    before = scratch_dirs()
    proc = subprocess.run([sys.executable, "-W", "ignore::ResourceWarning", "-m", "unittest", "discover", "-v", "-s", "qdexsim/tests", "-t", "."],
                          cwd=common.REPO, env=env, capture_output=True, text=True)
    leaked = sorted(scratch_dirs() - before)
    ran = re.search(r"Ran (\d+) tests? in ([\d.]+)s", proc.stderr)
    state["unittest_log"] = proc.stderr
    n = int(ran.group(1)) if ran else 0
    tail = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else ""
    return _row(f"{n} tests", proc.returncode == 0 and n > 0 and not leaked, f"{tail}; tmp dirs left {len(leaked)}",
                {"tests_run": n, "returncode": proc.returncode, "leaked_tmp_dirs": leaked, "stderr_tail": proc.stderr[-3000:]})


def step_constraints(mode, out_dir, state):
    log = state.get("unittest_log", "")
    found = {}
    for name in REQUIRED_CONSTRAINTS:
        m = re.search(rf"^{name} \(qdexsim\.tests\.test_constraints\.\w+\.{name}\)\n?[^\n]*?\.\.\. (\w+)", log, re.M)
        found[name] = m.group(1) if m else "missing"
    ok = all(v == "ok" for v in found.values())
    return _row("3 tests (deps / network / writes)", ok, "stdlib-only {test_stdlib_only}; no-network {test_static}; "
                "writes+binds+connects audit {test_writes_binds_and_connects}".format(**found), found)


def step_coverage(mode, out_dir, state):
    if mode == "quick":
        return _row("-", True, "skipped (--quick)", {"skipped": True})
    path = os.path.join(out_dir, "coverage.json")
    proc = subprocess.run([sys.executable, "-W", "ignore::ResourceWarning", "qdexsim/tools/cov.py", "--fail-under", "90", "--json", path],
                          cwd=common.REPO, capture_output=True, text=True)
    cov = common.read_json(path) if os.path.exists(path) else {}
    lib = cov.get("library", {})
    pct = lib.get("total_percent", 0.0)
    return _row(f"{lib.get('total_statements', 0)} statements, {cov.get('tests_run', 0)} tests", proc.returncode == 0 and pct >= 90.0,
                f"{pct:.2f}% (>= 90% required); child processes merged {cov.get('child_processes_merged', 0)}",
                {"total_percent": pct, "returncode": proc.returncode, "stdout_tail": proc.stdout[-1500:]})


def step_g1(mode, out_dir, state):
    from qdexsim.tools import selfcheck as S
    s = S.g1(out_dir)
    codes = ", ".join(f"{n}:{d['bad_sign_code']}/{d['bad_ts_code']}" for n, d in s["dialects"].items())
    r = s["restart"]
    return _row("5 dialects x (bad sign, expired ts, place, partial fill, cancel) + process restart", s["ok"],
                f"sign/ts codes {codes}; partial 0.05/0.06 x5; restart orders {r['orders']} fills {r['fills']} equal={r['state_equal']} "
                f"dup-after-restart={r['dup_cid_after_restart']}", s)


def step_g2(mode, out_dir, state):
    from qdexsim.tools import selfcheck as S
    s = S.g2(out_dir)
    return _row("11 injection kinds, same seed x2 + other seed", s["ok"],
                f"events {s['events_seed0_run1']} identical={s['identical']}; other seed differs={s['differs_with_other_seed']}; "
                f"kinds logged {len(s['kinds_logged'])}/11", s)


def step_g3(mode, out_dir, state):
    from qdexsim.tools import selfcheck as S
    s = S.g3(out_dir)
    return _row(f"{s['scenarios']} scenarios x 2 clients", s["ok"],
                f"correct green {s['correct']['green']}/{s['scenarios']}; naive red {s['naive']['red']}/{s['scenarios']}; "
                f"naive must-red {s['naive_must_red_scenarios']} ok={s['naive_must_red_ok']}", s)


def step_g4(mode, out_dir, state):
    from qdexsim.tools import selfcheck as S
    s = S.g4(out_dir)
    return _row("synthetic 200 orders, 50 planted diffs", s["ok"],
                f"found {s['found']}/{s['planted']}, missed {len(s['missed'])}, false positives {len(s['false_positives'])}, "
                f"tz offset {s['ts_offset_ms']} ms", s)


def step_knives(mode, out_dir, state):
    if mode == "quick":
        return _row("-", True, "skipped (--quick)", {"skipped": True})
    from qdexsim.tools import selfcheck as S
    s = S.knives(out_dir)
    return _row("3 hand-made mutants", s["ok"], "; ".join(f"{k['knife']} red={k['red']}" for k in s["knives"]), s)


STEPS = [("unittest", step_unittest), ("constraints", step_constraints), ("coverage", step_coverage), ("g1_dialects", step_g1),
         ("g2_chaos", step_g2), ("g3_judges", step_g3), ("g4_recon", step_g4), ("knives", step_knives)]


def main(argv=None):
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--quick", action="store_true")
    g.add_argument("--full", action="store_true")
    args = ap.parse_args(argv)
    mode = "quick" if args.quick else "full" if args.full else "default"
    out_dir = RESULTS if mode == "full" else os.path.join(RESULTS, "selftest")
    os.makedirs(out_dir, exist_ok=True)
    m = common.machine()
    print(f"qdexsim selftest ({mode})  {m['platform']}  Python {m['python']}  SQLite {m['sqlite_version']}  {m['cpu_count']} CPU  "
          f"commit {m['git_commit']}")
    print(f"{'step':12s} {'scale':70s} {'time':>7s}  result  key numbers")
    rows, state, t_all = {}, {}, time.perf_counter()
    for name, fn in STEPS:
        t0 = time.perf_counter()
        try:
            row = fn(mode, out_dir, state)
        except Exception as e:                      # noqa: BLE001  一步崩了也要出总表
            row = _row("-", False, f"exception: {e!r}")
        row["seconds"] = round(time.perf_counter() - t0, 1)
        rows[name] = row
        print(f"{name:12s} {row['scale'][:70]:70s} {row['seconds']:6.1f}s  {'OK' if row['ok'] else 'FAIL':6s}  {row['summary']}", flush=True)
    total = round(time.perf_counter() - t_all, 1)
    ok = all(r["ok"] for r in rows.values())
    n_ok = sum(1 for r in rows.values() if r["ok"])
    print(f"{'TOTAL':12s} {'':70s} {total:6.1f}s  {'OK' if ok else 'FAIL':6s}  {n_ok}/{len(rows)} steps passed")
    common.write_json(os.path.join(out_dir, "selftest_summary.json"), {"mode": mode, "ok": ok, "seconds": total, "steps": rows, "machine": m,
                                                                      "command": "python3.12 -m qdexsim.selftest" + ("" if mode == "default" else f" --{mode}")})
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
