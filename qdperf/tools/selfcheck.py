"""四个工具的自检（产出 REPORT.md 引用的结果文件）。每个 gN() 返回 {"summary": {...判据...}, ...} 并写 JSON。

    python3.12 -m qdperf.tools.selfcheck g1 [--scale 1] [--out qdperf/results]     # 合成库 + 体检 + 独立复核
    python3.12 -m qdperf.tools.selfcheck g2 ...                                     # 见各函数说明

缺省写到 qdperf/results/（--out 可改）；中间文件全部在 tempfile 里，结束删除。
"""
import argparse
import os
import re
import shutil
import sqlite3
import sys
import time

from qdperf import common
from qdperf import fake_backend as B
from qdperf import http_load as H
from qdperf import index_advisor as A
from qdperf import profile_gate as G
from qdperf import sqlite_doctor as D
from qdperf import sqlplan as P
from qdperf import synth_db
from qdperf import tick_jitter as J
from qdperf.examples import qbt_case

WORKLOAD = os.path.join(common.PKG, "examples", "workload_trading.json")


# ---- G1 ------------------------------------------------------------------------------
def verify_suggestions(db, rep):
    """独立复核：把体检器输出的 SQL 原样在原库的一份新拷贝上执行，再对每条建议对应 / 实际服务的语句跑 EXPLAIN QUERY PLAN。"""
    work = common.scratch_dir("qdperf-verify-")
    try:
        dst = os.path.join(work, "patched.db")
        shutil.copyfile(db, dst)
        conn = sqlite3.connect(dst)
        t0 = time.perf_counter()
        conn.executescript("\n".join(rep["indexes"]["sql"]))
        apply_s = time.perf_counter() - t0
        wl = D.load_workload(WORKLOAD)
        by = {s["id"]: s for s in wl["statements"]}
        cols = {t: [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')] for t in rep["tables"]}
        checks = []
        for sug in rep["indexes"]["suggestions"]:
            for sid in sorted(set(sug["statements"]) | set(sug["serves"])):
                st = by[sid]
                nodes = A.resolve_aliases(D.explain(conn, st["sql"], st["param_samples"][0]),
                                          P.parse_statement(st["sql"], cols))
                scanned = sorted({n["table"] for n in nodes if n["op"] == "scan"})
                temp = P.classify(nodes)["temp_btree"]
                ok = sug["table"] not in scanned and (sug["kind"] != "sort" or not temp)
                checks.append({"index": sug["name"], "statement": sid, "ok": ok, "plan": [n["detail"] for n in nodes]})
        final = {}
        for sid, st in by.items():
            nodes = A.resolve_aliases(D.explain(conn, st["sql"], st["param_samples"][0]),
                                      P.parse_statement(st["sql"], cols))
            final[sid] = P.classify(nodes)["verdict"]
        conn.close()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return {"apply_seconds": round(apply_s, 2), "checks": checks, "all_ok": bool(checks) and all(c["ok"] for c in checks),
            "statements_still_scanning": sorted(k for k, v in final.items() if v == "full_scan"), "final_verdicts": final}


def g1(scale=1.0, out_dir=common.RESULTS, seed=synth_db.DEFAULT_SEED, doctor_kw=None):
    work = common.scratch_dir("qdperf-g1-")
    try:
        db = os.path.join(work, "synth.db")
        gen = synth_db.generate(db, seed=seed, scale=scale)
        t0 = time.perf_counter()
        gen["fingerprint"] = synth_db.fingerprint(db)
        gen["fingerprint_seconds"] = round(time.perf_counter() - t0, 2)
        db2 = os.path.join(work, "synth2.db")
        gen2 = synth_db.generate(db2, seed=seed, scale=scale)
        same = synth_db.fingerprint(db2) == gen["fingerprint"]
        os.remove(db2)
        gen["determinism"] = {"second_run_seconds": gen2["seconds"], "identical_fingerprint": same}
        gen["command"] = f"python3.12 -m qdperf.synth_db --scale {scale:g} --seed {seed} --fingerprint"
        rep = D.diagnose(db, WORKLOAD, deferred_demo=True, **(doctor_kw or {}))
        rep["command"] = (f"python3.12 -m qdperf.sqlite_doctor <synth.db> --workload qdperf/examples/workload_trading.json "
                          f"--deferred-demo")
        ver = verify_suggestions(db, rep)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    summary = g1_summary(gen, rep, ver, scale)
    out = {"summary": summary, "synth": gen, "verify": ver, "machine": common.machine()}
    if out_dir:
        common.write_json(os.path.join(out_dir, "g1_synth_verify.json"), out)
        common.write_json(os.path.join(out_dir, "g1_doctor.json"), rep)
    out["doctor"] = rep
    return out


def g1_summary(gen, rep, ver, scale):
    gate = [s for s in rep["contention"]["scenarios"] if s["gate"]]
    crit = gate[1]
    demo = [s for s in rep["contention"]["scenarios"] if not s["gate"]]
    wal = rep["wal"]["scenarios"]
    free = [w for w in wal if not w["config"]["long_reader"]]
    held = [w for w in wal if w["config"]["long_reader"]]
    p99 = crit["overall"]["wait_ms"]["p99"]
    met = p99 is not None and p99 <= 200.0 and crit["overall"]["busy"] == 0
    return {
        "scale": scale, "rows": sum(gen["rows_after_housekeeping"].values()), "synth_seconds": gen["seconds"],
        "synth_le_180s": gen["seconds"] <= 180.0, "synth_deterministic": gen["determinism"]["identical_fingerprint"],
        "statements": len(rep["statements"]), "full_scan": sum(s["verdict"] == "full_scan" for s in rep["statements"]),
        "temp_btree": sum("temp_btree" in s["flags"] for s in rep["statements"]),
        "suggestions": len(rep["indexes"]["suggestions"]), "rejected": len(rep["indexes"]["rejected"]),
        "suggestions_verified_independently": ver["all_ok"], "verify_checks": len(ver["checks"]),
        "still_scanning_after_apply": ver["statements_still_scanning"],
        "unsolvable": [u["statement"] for u in rep["indexes"]["unsolvable"]],
        "size_est_error_max_ge_1mb": max((abs(s["size_est_bytes"] - s["size_bytes"]) / s["size_bytes"]
                                          for s in rep["indexes"]["suggestions"] if s["size_bytes"] >= 1 << 20),
                                         default=None),
        "size_est_pages_off_max": max(abs(s["size_est_bytes"] - s["size_bytes"]) // 4096
                                      for s in rep["indexes"]["suggestions"]),
        "contention_3x50": {"p99_ms": p99, "max_ms": crit["overall"]["wait_ms"]["max"], "busy": crit["overall"]["busy"],
                            "met_200ms": met, "root_cause": crit.get("root_cause", []),
                            "starvation": crit["starvation"]["text"]},
        "contention_criterion_ok": met or bool(crit.get("root_cause")),
        "deferred_demo_root_cause": demo[0].get("root_cause", []) if demo else [],
        "deferred_demo_busy": demo[0]["overall"]["busy"] if demo else None,
        "wal_free_capped": all(w["max_live_frames"] <= 1.1 * w["config"]["autocheckpoint"] + 50 for w in free),
        "wal_long_reader_never_reset": all(w["resets"] == 0 for w in held),
        "wal_peaks_mb": {f"{w['config']['autocheckpoint']}/{'long' if w['config']['long_reader'] else 'none'}":
                         round(w["max_bytes"] / 1e6, 2) for w in wal},
        "original_untouched": rep["db"]["untouched"], "doctor_exit_code": 0 if rep["ok"] else 1,
        "doctor_seconds": rep["seconds"], "first_line": D.first_line(rep)}


def g1_ok(s):
    return (s["synth_deterministic"] and (s["scale"] < 1 or s["synth_le_180s"]) and s["suggestions"] > 0
            and s["suggestions_verified_independently"] and s["contention_criterion_ok"] and s["original_untouched"]
            and s["wal_free_capped"] and s["wal_long_reader_never_reset"] and s["doctor_exit_code"] == 1
            and any("BUSY_SNAPSHOT" in c for c in s["deferred_demo_root_cause"]))


# ---- G2 ------------------------------------------------------------------------------
QBT_TARGET = "qdperf.examples.qbt_case:backtest"


def copy_tree_for_names(dst):
    """把 qbt / strategies / qdperf（不含 results、tests）拷到临时目录：同一份代码换个路径测一遍，函数名集合应完全相同。"""
    for name in ("qbt", "strategies", "qdperf"):
        shutil.copytree(os.path.join(common.REPO, name), os.path.join(dst, name),
                        ignore=shutil.ignore_patterns("__pycache__", "results", "tests", "mutants"))


def g2(n=1_000_000, repeat=8, runs=3, out_dir=common.RESULTS, pcts=(0.05, 0.12), inner=2):
    """臂 = [基线 A, 放慢 5%, 放慢 12%] × runs + [原样重跑 R]，所有臂按进程轮转交替取样（每轮给每个臂起一个新进程），
    机器的慢漂移同时落在所有臂上；A / S 各 runs 遍取中位数，A 的 runs 遍相对极差 = 噪声幅度。
    R 是单遍（像平时“基线 3 遍 + 本次 1 遍”那样）做误报检查。"""
    args = {"n": n}
    keys = [f"{round(p * 100)}pct" for p in pcts]
    arm = {"target": QBT_TARGET, "args": args, "paths": (), "env": None}
    arms = []
    for _ in range(runs):
        arms += [arm] + [dict(arm, env={qbt_case.SLOW_ENV: str(p)}) for p in pcts]
    arms.append(arm)                                                      # 最后一个臂 = 原样重跑（单遍）
    docs = G.interleaved(arms, repeat=repeat, inner=inner)
    per = len(keys) + 1
    a_runs = docs[0:runs * per:per]
    s_runs = {k: docs[1 + j:runs * per:per] for j, k in enumerate(keys)}
    rerun = docs[-1]
    base = G.aggregate(a_runs)
    slow = {}
    for k in keys:
        doc = G.aggregate(s_runs[k])
        slow[k] = {"pct": pcts[keys.index(k)], "doc": doc, "gate_default": G.compare(base, doc),
                   "gate_noise_only": G.compare(base, doc, wall_tol=0.0),
                   "gate_single_run": G.compare(base, s_runs[k][0]),
                   "per_round_single": [G.compare(base, d)["lines"][0] for d in s_runs[k]]}
    work = common.scratch_dir("qdperf-g2-")
    try:
        copy_tree_for_names(work)
        moved = G.measure(QBT_TARGET, args, repeat=1, cold=False, paths=[work])
    finally:
        shutil.rmtree(work, ignore_errors=True)
    out = {"summary": g2_summary(base, slow, rerun, moved), "baseline": base, "rerun": rerun,
           "moved_copy_names": sorted(G.function_names(moved)), "moved_copy_top": moved["profile"]["top"], "order": "臂 = [A, S5pct, S12pct] × 3 + [R]，按进程轮转交替取样（G.interleaved）",
           "slow": slow, "machine": common.machine(),
           "commands": {"pair": f"python3.12 -m qdperf.profile_gate pair {QBT_TARGET} --args '{{\"n\": {n}}}' "
                                f"--repeat {repeat} --inner {inner} --env-b {qbt_case.SLOW_ENV}=0.12 --out base.json "
                                f"--out-b slow.json",
                        "baseline": f"python3.12 -m qdperf.profile_gate baseline {QBT_TARGET} --args '{{\"n\": {n}}}' "
                                    f"--repeat {repeat} --inner {inner} --out base.json",
                        "run": f"python3.12 -m qdperf.profile_gate run {QBT_TARGET} --args '{{\"n\": {n}}}' "
                               f"--repeat {repeat} --inner {inner} --out new.json",
                        "compare": "python3.12 -m qdperf.profile_gate compare base.json slow.json"}}
    if out_dir:
        common.write_json(os.path.join(out_dir, "g2_profile_gate.json"), out)
    return out


def names_found(base, rerun_top, moved_top):
    """名字可比：原样重跑、整份代码拷到临时目录再测，两份前 10 的名字都能在基线的前 30 里找到（名字没去掉行号 /
    临时路径时就找不到）。不要求前 10 的排位一致：第 10 名上下的函数累计耗时只差一两成，排位随噪声互换
    （前 10 是否完全相同照样记在 summary 里，只供参考）。"""
    names = G.function_names(base)
    return {t["name"] for t in rerun_top[:10]} <= names, {t["name"] for t in moved_top[:10]} <= names


def g2_summary(base, slow, rerun, moved):
    tol_hot = max(0.10, 2 * base["noise"]["hot_wall_rel"])
    rows = {}
    for key, v in slow.items():
        hot = next(m for m in v["gate_default"]["metrics"] if m["metric"] == "hot")
        rows[key] = {"intended": v["pct"], "achieved_hot": hot["change"], "verdict_default": v["gate_default"]["verdict"],
                     "first_line": v["gate_default"]["lines"][0], "verdict_noise_only": v["gate_noise_only"]["verdict"],
                     "first_line_noise_only": v["gate_noise_only"]["lines"][0],
                     "verdict_single_run": v["gate_single_run"]["verdict"],
                     "first_line_single_run": v["gate_single_run"]["lines"][0],
                     "per_round_single": v["per_round_single"],
                     "new_top10": [t["name"] for t in v["gate_default"]["new_top"]]}
    names_b, names_r, names_m = G.function_names(base), G.function_names(rerun), G.function_names(moved)
    found_r, found_m = names_found(base, rerun["profile"]["top"], moved["profile"]["top"])
    top10 = [t["name"] for t in base["profile"]["top"][:10]]
    return {"n": base["args"]["n"], "repeat": base["repeat"], "inner": base["inner"], "runs": base["runs"],
            "hot_wall_s": base["hot"]["wall_s"], "cold_wall_s": base["cold"]["wall_s"],
            "peak_bytes": base["memory"]["peak_bytes"], "noise": {k: v for k, v in base["noise"].items() if k != "per_run"},
            "tolerance_hot_default": tol_hot, "tolerance_hot_noise_only": 2 * base["noise"]["hot_wall_rel"],
            "slow": rows, "slow12_red": rows["12pct"]["verdict_default"] == "REGRESSION",
            "slow5_expected_by_noise": "REGRESSION" if 0.05 > tol_hot else "OK",
            "rerun_verdict": G.compare(base, rerun)["verdict"], "rerun_first_line": G.compare(base, rerun)["lines"][0],
            "names_rerun_top10_in_base": found_r, "names_moved_copy_top10_in_base": found_m,
            "names_identical_rerun_top10": set(top10) == {t["name"] for t in rerun["profile"]["top"][:10]},
            "names_jaccard_rerun": len(names_b & names_r) / len(names_b | names_r),
            "names_moved_copy_top10_equal": set(top10) == {t["name"] for t in moved["profile"]["top"][:10]},
            "names_moved_copy_jaccard": len(names_b & names_m) / len(names_b | names_m),
            "names_have_no_lineno_or_tmp": not any(re.search(r":\d+$|\(\d+\)|/tmp|qdperf-g2-", x)
                                                   for x in names_b | names_r | names_m),
            "base_top10": top10}


def g2_ok(s):
    return (s["slow12_red"] and s["slow"]["5pct"]["verdict_default"] == s["slow5_expected_by_noise"]
            and s["rerun_verdict"] == "OK" and s["names_have_no_lineno_or_tmp"]
            and s["names_rerun_top10_in_base"] and s["names_moved_copy_top10_in_base"])


# ---- G3 ------------------------------------------------------------------------------
def g3(quick=False, out_dir=common.RESULTS, log=print, seconds=None):
    """缺省每个配置 20 s（T = 0.05 s → 400 个 tick；p99 至少由 4 个样本决定，不被单个宿主机调度毛刺左右）；
    quick 时 10 s。"""
    secs = seconds or (10.0 if quick else 20.0)
    try:
        res = J.scan(quick=quick, base={"seconds": secs}, log=log)
        res["summary"] = g3_summary(res)
    except J.CalibrationError as exc:                    # 面板长查询标定达不到目标：不跑扫描，记下原因，判红
        res = {"format": J.FORMAT, "machine": common.machine(), "panel_calibration": exc.calibration, "quick": quick}
        res["summary"] = {"T": J.DEFAULTS["T"], "seconds_per_config": secs, "configs": 0, "panel_capped": True,
                          "panel_reason": str(exc), "q0_ok": False, "knee_found": False, "knee": None,
                          "effective_mitigations": [], "first_line": f"TICK_JITTER ERROR panel calibration: {exc}"}
    res["command"] = f"python3.12 -m qdperf.tick_jitter scan --seconds {secs:g}" + (" --quick" if quick else "")
    if out_dir:
        common.write_json(os.path.join(out_dir, "g3_tick_jitter.json"), res)
    return res


def _find(results, **kw):
    return next((r for r in results if all(r["config"][k] == v for k, v in kw.items())), None)


def g3_summary(res):
    rs = res["results"]
    q0 = [r for r in rs + res["gil"] if r["config"]["q"] == 0 and r["config"]["bg_threads"] == 0]
    knee = res["knee"]
    mit = {}
    for m in res["mitigations"]:
        p99 = m["interval_T"]["p99"]
        mit[m["config"]["mitigation"]] = {"p99_T": p99, "over_2T": m["over_2T"], "effective": p99 is not None and p99 <= 2.0,
                                          "improvement_x": (knee["knee_p99_T"] / p99) if knee and p99 else None,
                                          "panel_q": m.get("panel", {}).get("achieved_q"),
                                          "replica_refreshes": m.get("panel", {}).get("replica_refreshes")}
    busy = []
    for r in rs:
        c = r["config"]
        if c["busy"] == 0.5:
            o = _find(rs, wal=c["wal"], compute=c["compute"], q=c["q"], busy=5.0, mitigation="none", bg_threads=0)
            if o:
                busy.append({"wal": c["wal"], "compute": c["compute"], "q": c["q"], "p99_T_busy_0_5": r["interval_T"]["p99"],
                             "p99_T_busy_5": o["interval_T"]["p99"], "write_errors_busy_0_5": r["tick_errors"]["write"],
                             "write_errors_busy_5": o["tick_errors"]["write"]})
    gil = {f"{g['config']['compute']}/bg{g['config']['bg_threads']}": {
        "compute_p50_ms": g["segments_ms"]["compute"]["p50"], "p99_T": g["interval_T"]["p99"]} for g in res["gil"]}
    cal = res["panel_calibration"]
    return {"T": res["base"]["T"], "seconds_per_config": res["base"]["seconds"], "configs": len(rs),
            "panel_query_ms": cal["seconds"] * 1000, "panel_rows": cal["rows"], "panel_passes": cal.get("passes", 1),
            "panel_target_ms": cal["target_seconds"] * 1000, "panel_error": cal.get("error"),
            "panel_capped": cal["capped"], "panel_reason": cal.get("reason"),
            "q0_p99_T_max": max(r["interval_T"]["p99"] for r in q0), "q0_ok": all(r["interval_T"]["p99"] <= 1.2 for r in q0),
            "q0_runs": len(q0), "wake_late_ms_max": max((r["wake_late_ms"]["max"] or 0.0) for r in rs + res["gil"]),
            "knees": [{k: v for k, v in kn.items() if k != "p99_T_by_q"} for kn in res["knees"]],
            "knee": None if knee is None else {k: v for k, v in knee.items() if k != "p99_T_by_q"},
            "knee_found": knee is not None and knee["knee_p99_T"] > 2.0,
            "mitigations": mit, "effective_mitigations": sorted(k for k, v in mit.items() if v["effective"]),
            "busy_timeout_pairs": busy, "gil": gil, "first_line": J.first_line_scan(res)}


def g3_ok(s):
    """形状判据（拐点的 Q 与哪几条缓解有效随平台变，不卡具体值）：面板长查询标定到目标 ±20%；Q=0 时 p99 ≤ 1.2T；
    存在某个 Q 使 p99 > 2T；至少两条缓解把拐点配置的 p99 降回 ≤ 2T。"""
    return (not s["panel_capped"] and s["q0_ok"] and s["knee_found"] and len(s["effective_mitigations"]) >= 2)


# ---- G4 ------------------------------------------------------------------------------
def _backend(routes):
    return {"format": B.FORMAT, "seed": 7, "routes": routes}


def _manifest(paths):
    return {"format": H.FORMAT, "targets": [{"name": p.strip("/"), "path": p} for p in paths]}


def g4(out_dir=common.RESULTS, seconds=6.0, ramp_seconds=3.0, ab_seconds=4.0):
    out = {"machine": common.machine()}
    procs = []
    try:
        pa, ua = B.start(_backend({"/p99": {"latency": {"kind": "lognormal", "p50_ms": 30, "p99_ms": 120}},
                                   "/limited": {"latency": {"kind": "lognormal", "p50_ms": 50, "p99_ms": 60},
                                                "max_concurrency": 8},
                                   "/a1": {"latency": {"kind": "lognormal", "p50_ms": 20, "p99_ms": 60}},
                                   "/a2": {"latency": {"kind": "uniform", "lo_ms": 8, "hi_ms": 12}}}))
        procs.append(pa)
        pb, ub = B.start(_backend({"/a1": {"latency": {"kind": "lognormal", "p50_ms": 26, "p99_ms": 78}},
                                   "/a2": {"latency": {"kind": "uniform", "lo_ms": 10.4, "hi_ms": 15.6}}}))
        procs.append(pb)
        pc, uc = B.start(_backend({"/a1": {"latency": {"kind": "lognormal", "p50_ms": 20, "p99_ms": 60}},
                                   "/a2": {"latency": {"kind": "uniform", "lo_ms": 8, "hi_ms": 12}}}))
        procs.append(pc)
        out["p99"] = H.run_step(ua, _manifest(["/p99"]), c=8, seconds=seconds)
        out["knee"] = H.ramp(ua, _manifest(["/limited"]), cmax=32, seconds=ramp_seconds)
        out["ab_slow"] = H.ab(ua, ub, _manifest(["/a1", "/a2"]), c=8, seconds=ab_seconds)
        out["ab_same"] = H.ab(ua, uc, _manifest(["/a1", "/a2"]), c=8, seconds=ab_seconds)
    finally:
        for p in procs:
            B.stop(p)
    out["summary"] = g4_summary(out)
    if out_dir:
        common.write_json(os.path.join(out_dir, "g4_http_load.json"), out)
    return out


def g4_summary(out):
    p99 = out["p99"]["per_target"]["p99"]["latency_ms"]["p99"]
    knee = out["knee"]["knee"]
    return {"p99_injected_ms": 120.0, "p99_measured_ms": p99, "p99_error": (p99 - 120.0) / 120.0,
            "p99_in_108_132": p99 is not None and 108.0 <= p99 <= 132.0, "p99_requests": out["p99"]["total"]["requests"],
            "knee_injected": 8, "knee_measured": knee, "knee_in_6_10": knee is not None and 6 <= knee <= 10,
            "knee_rps_by_c": {str(st["c"]): round(st["total"]["ok_rps"], 1) for st in out["knee"]["steps"]},
            "ab_slow_verdict": out["ab_slow"]["verdict"], "ab_slow_first_line": out["ab_slow"]["first_line"],
            "ab_slow_red_rows": sum(r["red"] for r in out["ab_slow"]["rows"]), "ab_rows": len(out["ab_slow"]["rows"]),
            "ab_same_verdict": out["ab_same"]["verdict"], "ab_same_first_line": out["ab_same"]["first_line"]}


def g4_ok(s):
    return s["p99_in_108_132"] and s["knee_in_6_10"] and s["ab_slow_verdict"] == "REGRESSION" and s["ab_same_verdict"] == "OK"


def resummarize_g1(out_dir=common.RESULTS):
    """只用已入库的 g1 结果文件重算 summary（判据口径改了而实验不必重跑时用）。"""
    sv = common.read_json(os.path.join(out_dir, "g1_synth_verify.json"))
    rep = common.read_json(os.path.join(out_dir, "g1_doctor.json"))
    sv["summary"] = g1_summary(sv["synth"], rep, sv["verify"], sv["summary"]["scale"])
    common.write_json(os.path.join(out_dir, "g1_synth_verify.json"), sv)
    return sv["summary"]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3.12 -m qdperf.tools.selfcheck")
    ap.add_argument("which", choices=["g1", "g2", "g3", "g4"])
    ap.add_argument("--quick", action="store_true", help="g3：缩小扫描网格")
    ap.add_argument("--scale", type=float, default=1.0, help="g1：合成库比例")
    ap.add_argument("--n", type=int, default=1_000_000, help="g2：K 线根数")
    ap.add_argument("--repeat", type=int, default=8, help="g2：best-of-N（N 个进程）")
    ap.add_argument("--inner", type=int, default=2, help="g2：每个进程热调用次数")
    ap.add_argument("--out", default=common.RESULTS)
    args = ap.parse_args(argv)
    if args.which == "g2":
        s = g2(n=args.n, repeat=args.repeat, out_dir=args.out, inner=args.inner)["summary"]
        print(f"G2 {'OK' if g2_ok(s) else 'FAIL'} " + " ".join(f"{k}={v}" for k, v in s.items()
                                                              if not isinstance(v, (dict, list))))
        for k, v in s["slow"].items():
            print(f"  slow {k}: {v['first_line']} | noise-only: {v['first_line_noise_only']} | new top10: {v['new_top10']}")
        return 0 if g2_ok(s) else 1
    if args.which == "g3":
        s = g3(quick=args.quick, out_dir=args.out)["summary"]
        print(f"G3 {'OK' if g3_ok(s) else 'FAIL'} {s['first_line']} q0_ok={s['q0_ok']} knee_found={s['knee_found']} "
              f"effective={s['effective_mitigations']}")
        return 0 if g3_ok(s) else 1
    if args.which == "g4":
        s = g4(out_dir=args.out)["summary"]
        print(f"G4 {'OK' if g4_ok(s) else 'FAIL'} " + " ".join(f"{k}={v}" for k, v in s.items() if not isinstance(v, dict)))
        return 0 if g4_ok(s) else 1
    if args.which == "g1":
        res = g1(scale=args.scale, out_dir=args.out)
        s = res["summary"]
        print(f"G1 {'OK' if g1_ok(s) else 'FAIL'} " + " ".join(f"{k}={v}" for k, v in s.items()
                                                              if not isinstance(v, (dict, list))))
        return 0 if g1_ok(s) else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
