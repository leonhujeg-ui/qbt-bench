"""一条命令跑全部自检：unittest + 覆盖率 + 四个工具的自检 + 性能自检 + 工程约束，最后打印总表。

    python3.12 -m qdperf.selftest            # 缺省：合成库 5%、G3 精简网格、性能自检用 20 万根；任何一项失败 → 退出码 1
    python3.12 -m qdperf.selftest --quick    # 更快：unittest 用 QDPERF_FAST=1、跳过覆盖率、合成库 2%、性能自检 10 万根
    python3.12 -m qdperf.selftest --full     # 全量：合成库 100%、G3 全网格、性能自检 100 万根；结果写回 qdperf/results/

写文件：缺省与 --quick 只写 qdperf/results/selftest/（各步 JSON + summary.json，不入库）；--full 写 qdperf/results/*.json
（REPORT.md 引用的快照）。中间文件全在 tempfile。只连 127.0.0.1。
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

from qdperf import common

RESULTS = common.RESULTS
REQUIRED_CONSTRAINTS = ("test_stdlib_only", "test_static", "test_writes_and_connects")


def _row(scale, ok, summary, detail=None):
    return {"scale": scale, "ok": bool(ok), "summary": summary, "detail": detail or {}}


def scratch_dirs():
    """tempfile 下所有 qdperf-* 目录（工具与测试的 scratch 都用这个前缀）。"""
    tmp = tempfile.gettempdir()
    return {n for n in os.listdir(tmp) if n.startswith("qdperf-")}


# ---- 各步 ------------------------------------------------------------------------------
def step_unittest(mode, out_dir, state):
    env = dict(os.environ)
    if mode == "quick":
        env["QDPERF_FAST"] = "1"
    for k in ("QDPERF_COVERAGE", "QDPERF_COV_DIR", "QDPERF_NO_TIMING"):
        env.pop(k, None)
    before = scratch_dirs()
    proc = subprocess.run([sys.executable, "-m", "unittest", "discover", "-v", "-s", "qdperf/tests", "-t", "."],
                          cwd=common.REPO, env=env, capture_output=True, text=True)
    leaked = sorted(scratch_dirs() - before)                 # 测试跑完还留在 tempfile 下的目录 = 清理漏了
    ran = re.search(r"Ran (\d+) tests? in ([\d.]+)s", proc.stderr)
    skipped = re.search(r"skipped=(\d+)", proc.stderr)
    state["unittest_log"] = proc.stderr
    n = int(ran.group(1)) if ran else 0
    tail = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else ""
    return _row(f"{n} tests" + (" (QDPERF_FAST=1)" if mode == "quick" else ""),
                proc.returncode == 0 and n > 0 and not leaked,
                f"{tail}; skipped {int(skipped.group(1)) if skipped else 0}; tmp dirs left {len(leaked)}",
                {"tests_run": n, "returncode": proc.returncode, "leaked_tmp_dirs": leaked,
                 "stderr_tail": proc.stderr[-3000:]})


def step_constraints(mode, out_dir, state):
    log = state.get("unittest_log", "")
    found = {}
    for name in REQUIRED_CONSTRAINTS:
        # 有 docstring 的测试，-v 会把 docstring 首行另起一行打在 "... ok" 前面
        m = re.search(rf"^{name} \(qdperf\.tests\.test_constraints\.\w+\.{name}\)\n?[^\n]*?\.\.\. (\w+)", log, re.M)
        found[name] = m.group(1) if m else "missing"
    ok = all(v == "ok" for v in found.values())
    return _row("3 tests (deps / network / writes)", ok,
                "stdlib-only {test_stdlib_only}; no-network {test_static}; writes+connects audit "
                "{test_writes_and_connects}".format(**found), found)


def step_coverage(mode, out_dir, state):
    if mode == "quick":
        return _row("-", True, "skipped (--quick)", {"skipped": True})
    path = os.path.join(out_dir, "coverage.json")
    proc = subprocess.run([sys.executable, "qdperf/tools/cov.py", "--fail-under", "90", "--json", path],
                          cwd=common.REPO, capture_output=True, text=True)
    cov = common.read_json(path) if os.path.exists(path) else {}
    lib = cov.get("library", {})
    pct = lib.get("total_percent", 0.0)
    return _row(f"{lib.get('total_statements', 0)} statements, {cov.get('tests_run', 0)} tests",
                proc.returncode == 0 and pct >= 90.0,
                f"{pct:.2f}% (>= 90% required); child processes merged {cov.get('child_processes_merged', 0)}",
                {"total_percent": pct, "returncode": proc.returncode, "stdout_tail": proc.stdout[-1500:]})


def step_g1(mode, out_dir, state):
    from qdperf.tools import selfcheck as S
    scale = {"quick": 0.02, "default": 0.05, "full": 1.0}[mode]
    kw = {} if mode == "full" else {"duration": 2.0, "wal_duration": 3.0}
    res = S.g1(scale=scale, out_dir=out_dir, doctor_kw=kw)
    s = res["summary"]
    return _row(f"synth {s['rows']:,} rows, {s['statements']} statements", S.g1_ok(s),
                f"synth {s['synth_seconds']}s deterministic={s['synth_deterministic']}; suggestions "
                f"{s['suggestions']} verified={s['suggestions_verified_independently']}; 3x50/s p99 "
                f"{s['contention_3x50']['p99_ms']:.2f} ms busy {s['contention_3x50']['busy']}; untouched="
                f"{s['original_untouched']}", s)


def step_g2(mode, out_dir, state):
    """回归闸自身：确定性小负载上，3 遍基线噪声、放慢 12% 必红、原样重跑必绿、换路径函数名集合相同。
    臂 = [基线 A, 放慢 S] × 3 + [原样重跑 R]，全部按进程轮转交替取样（G.interleaved，与 perf_qbt 同一做法），
    宿主机的慢漂移同时落在三条臂上；每次调用 ≈ 85 ms、每臂 8 个进程：16 ms × 5 个进程、重跑单独另测时，
    嘈杂的宿主机上 12% 偶尔量成 +8%、原样重跑偶尔量成 +10% 以上。"""
    from qdperf import profile_gate as G
    target, args = "qdperf.examples.demo_work:spin", {"n": 2_000_000}
    arm = {"target": target, "args": args, "paths": (), "env": None}
    docs = G.interleaved([arm, dict(arm, env={"QDPERF_DEMO_SLOW": "0.12"})] * 3 + [arm], repeat=8, inner=2)
    base, slow, rerun = G.aggregate(docs[0:6:2]), G.aggregate(docs[1:6:2]), docs[6]
    names = []
    for _ in range(2):
        d = tempfile.mkdtemp(prefix="qdperf-names-")
        try:
            from qdperf.examples import demo_work
            shutil.copyfile(demo_work.__file__, os.path.join(d, "tmpwork.py"))
            names.append(G.function_names(G.measure("tmpwork:spin", args, repeat=1, cold=False, paths=[d])))
        finally:
            shutil.rmtree(d, ignore_errors=True)
    red, green = G.compare(base, slow), G.compare(base, rerun)
    s = {"noise_hot": base["noise"]["hot_wall_rel"], "slow12": red["lines"][0], "rerun": green["lines"][0],
         "names_equal_across_paths": names[0] == names[1], "new_top": [t["name"] for t in red["new_top"]]}
    common.write_json(os.path.join(out_dir, "g2_selftest.json"), {"summary": s, "baseline": base, "slow": slow,
                                                                   "rerun": rerun})
    ok = red["verdict"] == "REGRESSION" and green["verdict"] == "OK" and s["names_equal_across_paths"]
    return _row("demo_work.spin: A/S x3 + R, interleaved x8", ok,
                f"noise {s['noise_hot'] * 100:.1f}%; {s['slow12']}; rerun {s['rerun']}; names equal {s['names_equal_across_paths']}",
                s)


def step_g3(mode, out_dir, state):
    from qdperf.tools import selfcheck as S
    res = S.g3(quick=mode != "full", out_dir=out_dir, log=None)
    s = res["summary"]
    scale = f"{s['configs']} configs x {s['seconds_per_config']:g}s, T={s['T']:g}s"
    if s["panel_capped"]:
        return _row(scale, False, f"panel calibration FAILED: {s['panel_reason']}", s)
    return _row(scale, S.g3_ok(s),
                f"panel {s['panel_query_ms']:.0f} ms ({s['panel_error'] * 100:+.0f}% of target, x{s['panel_passes']}); "
                f"Q=0 p99 max {s['q0_p99_T_max']:.2f}T; knee "
                f"{'none' if not s['knee'] else 'Q=' + format(s['knee']['knee_q'], 'g') + ' p99 ' + format(s['knee']['knee_p99_T'], '.2f') + 'T'}"
                f"; effective mitigations {s['effective_mitigations']}", s)


def step_g4(mode, out_dir, state):
    from qdperf.tools import selfcheck as S
    s = S.g4(out_dir=out_dir)["summary"]
    return _row(f"{s['p99_requests']} req p99 run + ramp 1..32 + 2 A/B", S.g4_ok(s),
                f"p99 injected 120 → {s['p99_measured_ms']:.1f} ms; knee injected 8 → {s['knee_measured']}; "
                f"B +30% → {s['ab_slow_verdict']}; A/A → {s['ab_same_verdict']}", s)


def step_perf(mode, out_dir, state):
    """性能自检：本仓 qbt 回测真实用例上跑回归闸（3 遍基线交替“放慢 5% / 12%”与原样重跑）。"""
    from qdperf.tools import selfcheck as S
    n = {"quick": 100_000, "default": 200_000, "full": 1_000_000}[mode]
    s = S.g2(n=n, out_dir=out_dir)["summary"]
    return _row(f"qbt {n:,} bars x ma_cross, best-of-{s['repeat']} procs, {s['runs']} runs", S.g2_ok(s),
                f"noise {s['noise']['hot_wall_rel'] * 100:.1f}%; 12%: {s['slow']['12pct']['first_line']}; "
                f"5%: {s['slow']['5pct']['verdict_default']} (expected {s['slow5_expected_by_noise']}); rerun {s['rerun_verdict']}",
                s)


STEPS = [("unittest", step_unittest), ("constraints", step_constraints), ("coverage", step_coverage),
         ("g1_sqlite", step_g1), ("g2_gate", step_g2), ("g3_tick", step_g3), ("g4_http", step_g4), ("perf_qbt", step_perf)]


def run_all(mode):
    out_dir = RESULTS if mode == "full" else os.path.join(RESULTS, "selftest")
    os.makedirs(out_dir, exist_ok=True)
    rows, state = [], {}
    t_all = time.perf_counter()
    for name, fn in STEPS:
        t0 = time.perf_counter()
        try:
            row = fn(mode, out_dir, state)
        except Exception as exc:                       # noqa: BLE001 —— 自检本身崩溃也要算失败并继续
            row = _row("-", False, f"CRASH {type(exc).__name__}: {exc}"[:200])
        row.update(step=name, seconds=round(time.perf_counter() - t0, 1))
        rows.append(row)
        print(fmt_row(row), flush=True)
    summary = {"command": "python3.12 -m qdperf.selftest" + ("" if mode == "default" else f" --{mode}"), "mode": mode,
               "machine": common.machine(), "ok": all(r["ok"] for r in rows),
               "seconds": round(time.perf_counter() - t_all, 1), "steps": rows}
    common.write_json(os.path.join(out_dir, "selftest_summary.json"), summary)
    return summary


def fmt_row(r):
    return f"{r['step']:11s} {r['scale']:44s} {r['seconds']:7.1f}s  {'OK  ' if r['ok'] else 'FAIL'}  {r['summary']}"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3.12 -m qdperf.selftest", description="qdperf 一键自检")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--quick", action="store_true")
    g.add_argument("--full", action="store_true")
    a = ap.parse_args(argv)
    mode = "quick" if a.quick else "full" if a.full else "default"
    m = common.machine()
    print(f"qdperf selftest ({mode})  {m['platform']}  Python {m['python']}  SQLite {m['sqlite_version']}  "
          f"{m['cpu_count']} CPU")
    print(f"{'step':11s} {'scale':44s} {'time':>8s}  {'result':4s}  key numbers")
    summary = run_all(mode)
    print(f"{'TOTAL':11s} {'':44s} {summary['seconds']:7.1f}s  {'OK  ' if summary['ok'] else 'FAIL'}  "
          f"{sum(r['ok'] for r in summary['steps'])}/{len(summary['steps'])} steps passed")
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
