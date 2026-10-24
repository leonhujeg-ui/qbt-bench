"""一条命令跑全部自检：unittest + 工程约束 + 覆盖率 + 突变结果核对 + 四个工具的判据对拍（合成语言包 / 合成站）+ 总表。

    python3.12 -m qdtext.selftest            # 缺省：全部步骤；任何一项 FAIL → 退出码 1（SKIP 不算失败，但会在总表里写明原因）
    python3.12 -m qdtext.selftest --quick    # 跳过覆盖率（最慢的一步）

写文件：qdtext/results/selftest/（各步 JSON + summary.json）与 qdtext/results/g*.json；中间文件全在 tempfile；只连 127.0.0.1。
突变测试本身很慢（分钟级），不在这里跑：这一步读 results/mutation.json，核对它记录的源码哈希与当前代码一致、每模块 ≥ 30 个
突变体、总抓住率 ≥ 85%、漏网逐条都有判定（mutants/triage.py --check）；任何一条不满足 → FAIL（不许拿旧结果冒充）。
"""
import argparse
import os
import platform
import re
import subprocess
import sys
import tempfile
import time

from qdtext import cdp
from qdtext import common as C

RESULTS = C.RESULTS
REQUIRED_CONSTRAINTS = ("test_stdlib_only", "test_static", "test_writes_and_connects")
MUTATION_MIN_PER_MODULE, MUTATION_MIN_KILL, COVERAGE_MIN = 30, 85.0, 90.0


def _row(scale, ok, summary, detail=None, skipped=False):
    return {"scale": scale, "ok": bool(ok), "skipped": skipped, "summary": summary, "detail": detail or {}}


def scratch_dirs():
    tmp = tempfile.gettempdir()
    return {n for n in os.listdir(tmp) if n.startswith("qdtext-")}


# ---- 各步 ------------------------------------------------------------------------------
def step_unittest(mode, out_dir, state):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    env.pop("QDTEXT_AUDIT_LOG", None)
    before = scratch_dirs()
    proc = subprocess.run([sys.executable, "-m", "unittest", "discover", "-v", "-s", "qdtext/tests", "-t", "."],
                          cwd=C.REPO, env=env, capture_output=True, text=True)
    leaked = sorted(scratch_dirs() - before)
    ran = re.search(r"Ran (\d+) tests? in ([\d.]+)s", proc.stderr)
    skipped = re.search(r"skipped=(\d+)", proc.stderr)
    state["unittest_log"] = proc.stderr
    n = int(ran.group(1)) if ran else 0
    tail = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else ""
    return _row(f"{n} tests", proc.returncode == 0 and n > 0 and not leaked,
                f"{tail}; skipped {int(skipped.group(1)) if skipped else 0}; tmp dirs left {len(leaked)}",
                {"tests_run": n, "returncode": proc.returncode, "leaked_tmp_dirs": leaked, "stderr_tail": proc.stderr[-3000:]})


def step_constraints(mode, out_dir, state):
    log = state.get("unittest_log", "")
    found = {}
    for name in REQUIRED_CONSTRAINTS:
        m = re.search(rf"^{name} \(qdtext\.tests\.test_constraints\.\w+\.{name}\)\n?[^\n]*?\.\.\. (\w+)", log, re.M)
        found[name] = m.group(1) if m else "missing"
    ok = all(v == "ok" for v in found.values())
    return _row("3 tests (deps / network / writes)", ok,
                "stdlib-only {test_stdlib_only}; loopback-only {test_static}; writes+connects audit {test_writes_and_connects}".format(**found), found)


def step_coverage(mode, out_dir, state):
    if mode == "quick":
        return _row("-", True, "skipped (--quick)", {"skipped": True}, skipped=True)
    path = os.path.join(out_dir, "coverage.json")
    proc = subprocess.run([sys.executable, "qdtext/tools/cov.py", "--fail-under", str(COVERAGE_MIN), "--json", path],
                          cwd=C.REPO, capture_output=True, text=True)
    cov = C.read_json(path) if os.path.exists(path) else {}
    lib = cov.get("library", {})
    pct = lib.get("total_percent", 0.0)
    worst = min(lib.get("files", {}).items(), key=lambda kv: kv[1]["percent"], default=("-", {"percent": 0}))
    return _row(f"{lib.get('total_statements', 0)} statements, {cov.get('tests_run', 0)} tests", proc.returncode == 0 and pct >= COVERAGE_MIN,
                f"{pct:.2f}% (>= {COVERAGE_MIN:g}% required); lowest file {os.path.basename(worst[0])} {worst[1]['percent']:.1f}%",
                {"total_percent": pct, "returncode": proc.returncode, "stdout_tail": proc.stdout[-1500:]})


def step_mutation(mode, out_dir, state):
    from qdtext.mutants import run as M
    from qdtext.mutants import triage
    path = os.path.join(RESULTS, "mutation.json")
    if not os.path.exists(path):
        return _row("-", False, "没跑：qdtext/results/mutation.json 不存在（python3.12 qdtext/mutants/run.py）")
    doc = C.read_json(path)
    s, meta = doc["summary"], doc["meta"]
    stale = sorted(m for m in M.modules() if meta.get("source_sha256", {}).get(m) != M.source_hash(m))
    few = sorted(m for m in M.modules() if s["modules"].get(m, {}).get("total", 0) < MUTATION_MIN_PER_MODULE)
    problems = triage.check(doc)
    ok = not stale and not few and not problems and s["kill_rate"] >= MUTATION_MIN_KILL
    note = []
    if stale:
        note.append(f"源码已变、结果过期: {stale}")
    if few:
        note.append(f"不足 {MUTATION_MIN_PER_MODULE} 个突变体: {few}")
    if problems:
        note.append(f"漏网判定缺失 {len(problems)}")
    return _row(f"{len(s['modules'])} modules, {s['total']} mutants (results @ {meta.get('finished_at')})", ok,
                f"kill {s['kill_rate']:.1f}% (>= {MUTATION_MIN_KILL:g}%); survived {s['survived']}; min per-module "
                f"{min((m['total'] for m in s['modules'].values()), default=0)}; " + ("; ".join(note) if note else "sources unchanged; triage complete"),
                {"kill_rate": s["kill_rate"], "stale": stale, "few": few, "triage_problems": problems})


def step_g1(mode, out_dir, state):
    from qdtext.tools import selfcheck as S
    s = S.g1(out_dir=RESULTS)["summary"]
    return _row(f"synth {s['keys']} keys x 10 langs, {s['planted']} planted", S.g1_ok(s),
                f"recall {s['recall_pct']:g}% ({s['caught']}/{s['planted']}); false positives {s['false_positives']}; "
                f"by-design over-budget on wordy keys {s['by_design_over_budget']}; clean pack red {s['clean_red']} non-wordy yellow {s['clean_yellow_non_wordy']}", s)


def step_g2(mode, out_dir, state):
    from qdtext.tools import selfcheck as S
    s = S.g2(out_dir=RESULTS)["summary"]
    return _row(f"{s['wordy_planted']} wordy + {s['good_planted']} good, {s['dup_groups']} dup screens + {s['good_screens']} good screens", S.g2_ok(s),
                f"wordy recall {s['wordy_recall_pct']:g}%, good flagged {len(s['good_flagged'])} ({s['good_false_positive_pct']:g}%); "
                f"dup recall {s['dup_recall_pct']:g}% via {s['dup_methods']}; good-screen pairs flagged {len(s['good_screens_flagged'])}/{s['good_screen_pairs']}", s)


def step_g3(mode, out_dir, state):
    from qdtext.tools import selfcheck as S
    s = S.g3(out_dir=RESULTS)["summary"]
    return _row(f"{s['typos_planted']} typos + {s['paraphrases_planted']} paraphrases; table {s['chars']} chars / {s['words']} words", S.g3_ok(s),
                f"typo recall {s['typo_recall_pct']:g}% ({s['typos_caught']}/{s['typos_planted']}); paraphrases red {len(s['paraphrases_red'])} "
                f"(info {s['paraphrases_info']}, silent ok {s['paraphrases_silent_ok']}); other red {len(s['other_red'])}; clean pack red {s['clean_red']} info {s['clean_info']}", s)


def step_g4(mode, out_dir, state):
    from qdtext.tools import selfcheck as S
    exe = cdp.find_chrome()
    if not exe:
        return _row("fixtures 3 pages x 2 sizes", True, "SKIP: no chromium/chrome executable found (set QDTEXT_CHROME); judged offline only",
                    {"chrome": None}, skipped=True)
    s = S.g4(out_dir=RESULTS)["summary"]
    bad = [k for k, v in s["checks"].items() if not v["ok"]]
    return _row(f"fixtures {s['pages']} pages x {len(s['sizes'])} sizes, {s['browser']}", S.g4_ok(s),
                f"{sum(v['ok'] for v in s['checks'].values())}/{len(s['checks'])} page checks ok; normal page findings {s['normal_page_findings']}; "
                f"{s['seconds']}s" + (f"; failed {bad}" if bad else ""), s)


def step_g4_offline(mode, out_dir, state):
    from qdtext.tools import selfcheck as S
    snap = os.path.join(C.FIXTURES, "snapshots", "site.json")
    s = S.g4(out_dir=RESULTS, snapshot=snap)["summary"]
    return _row(f"snapshot {os.path.basename(snap)}, {len(s['checks'])} page x size", S.g4_ok(s),
                f"{sum(v['ok'] for v in s['checks'].values())}/{len(s['checks'])} checks ok; findings {s['findings']}; normal page {s['normal_page_findings']}", s)


STEPS = [("unittest", step_unittest), ("constraints", step_constraints), ("coverage", step_coverage), ("mutation", step_mutation),
         ("g1_locale", step_g1), ("g2_wordy", step_g2), ("g3_zhpair", step_g3), ("g4_mobile", step_g4), ("g4_offline", step_g4_offline)]


def header(mode):
    exe = cdp.find_chrome()
    return (f"qdtext selftest ({mode})  {platform.platform()}  Python {platform.python_version()}  "
            f"{'Chromium ' + os.path.basename(exe) if exe else 'no Chromium'}  {os.cpu_count()} CPU")


def table(rows, total_s):
    lines = [f"{'step':12s}{'scale':62s}{'time':>8s}  {'result':6s}  key numbers"]
    for name, r in rows:
        res = "SKIP" if r["skipped"] else ("OK" if r["ok"] else "FAIL")
        lines.append(f"{name:12s}{r['scale'][:61]:62s}{r['seconds']:7.1f}s  {res:6s}  {r['summary']}")
    passed = sum(1 for _n, r in rows if r["ok"] and not r["skipped"])
    skipped = sum(1 for _n, r in rows if r["skipped"])
    failed = sum(1 for _n, r in rows if not r["ok"])
    verdict = "FAIL" if failed else "OK"
    lines.append(f"{'TOTAL':12s}{'':62s}{total_s:7.1f}s  {verdict:6s}  {passed}/{len(rows)} steps passed, {skipped} skipped, {failed} failed")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--only", nargs="*", default=None, help="只跑这些步（调试用；总表仍打印）")
    args = ap.parse_args(argv)
    mode = "quick" if args.quick else "default"
    out_dir = os.path.join(RESULTS, "selftest")
    os.makedirs(out_dir, exist_ok=True)
    print(header(mode), flush=True)
    rows, state, t0 = [], {}, time.perf_counter()
    for name, fn in STEPS:
        if args.only and name not in args.only:
            continue
        t1 = time.perf_counter()
        try:
            r = fn(mode, out_dir, state)
        except Exception as e:                                   # noqa: BLE001  任何异常都是这一步 FAIL，不吞
            r = _row("-", False, f"exception: {type(e).__name__}: {str(e)[:200]}")
        r["seconds"] = round(time.perf_counter() - t1, 1)
        rows.append((name, r))
        print(f"  {name:12s} {'SKIP' if r['skipped'] else ('OK' if r['ok'] else 'FAIL'):4s} {r['seconds']:6.1f}s  {r['summary'][:110]}", flush=True)
        C.write_json(os.path.join(out_dir, f"{name}.json"), r)
    total_s = time.perf_counter() - t0
    text = table(rows, total_s)
    print()
    print(header(mode))
    print(text)
    C.write_json(os.path.join(out_dir, "summary.json"), {"mode": mode, "header": header(mode), "seconds": round(total_s, 1),
                                                         "steps": {n: {k: v for k, v in r.items() if k != "detail"} for n, r in rows},
                                                         "ok": all(r["ok"] for _n, r in rows)})
    C.write_text(os.path.join(out_dir, "summary.txt"), header(mode) + "\n" + text + "\n")
    return 0 if all(r["ok"] for _n, r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
