"""qdperf 的突变测试：复用本仓 mutants/run_mutations.py 的突变算子与候选枚举 / 校验 / 汇总
（enumerate_candidates、apply_mutation、_validate、summarize、_first_failure：运算符翻转、比较边界、条件取反、
布尔运算互换、常量 ±1 / 翻转、语句删除、return 置空、break/continue 互换），只是把被测包换成 qdperf，
按模块给出“最能抓”的测试顺序（-f 失败即停）。

    python3.12 qdperf/mutants/run.py --per-module 50 --jobs 4            # -> qdperf/results/mutation.json
    python3.12 qdperf/mutants/run.py --ids sqlplan:123:compare ...       # 补判据后只重跑指定突变体（并入 --out）

每个突变体：在 tempfile 下的独立工作目录里复制 qdperf/（被突变的模块换成突变源码；results/ 不复制），仓库其余顶层包
（qbt / strategies / tools / mutants）软链进去，子进程跑 QDPERF_FAST=1 QDPERF_NO_TIMING=1 python -m unittest -f
<模块顺序>；失败且经确认 = 抓住，超时 = 抓住（单列），通过 = 漏网。确认：报失败的那个测试在同一突变体上单独重跑仍失败才算；
单独重跑通过的失败是并行负载下的抖动，记进 unconfirmed 并整套再跑一遍（见 run_one）。
QDPERF_NO_TIMING=1 让依赖绝对时长的断言跳过（4 个突变体并行时机器忙，计时断言会误杀，虚增抓住率）；开跑前先在
同样的并行度下跑 jobs 份未突变基线，全部通过才开始；基线只能说明“通常不抖”，长时间满载下的偶发失败由上面的确认挡住。
"""
import argparse
import ast
import concurrent.futures
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from mutants import run_mutations as base      # noqa: E402  本仓已有的突变器

PKG = os.path.join(REPO, "qdperf")
RESULTS = os.path.join(PKG, "results")
T = "qdperf.tests."
ALL_TESTS = ["test_sqlplan", "test_index_advisor", "test_synth_db", "test_contract", "test_common", "test_doctor", "test_profile_gate",
             "test_constraints", "test_http_load", "test_load", "test_tick_jitter"]     # 快的在前；test_report 只查文档，不算
FIRST = {
    "common": ["test_common", "test_constraints"], "synth_db": ["test_synth_db"], "sqlplan": ["test_sqlplan", "test_doctor"],
    "index_advisor": ["test_index_advisor", "test_doctor"], "sqlite_doctor": ["test_doctor", "test_load"], "sqlite_load": ["test_load"],
    "profile_gate": ["test_profile_gate", "test_contract"], "tick_jitter": ["test_tick_jitter"],
    "http_load": ["test_http_load", "test_contract"],
    "fake_backend": ["test_http_load"],
}
EXCLUDED = ("__init__.py", "selftest.py")


def modules():
    return sorted(f[:-3] for f in os.listdir(PKG) if f.endswith(".py") and f not in EXCLUDED)


def test_order(module):
    first = FIRST.get(module, [])
    return [T + m for m in first + [m for m in ALL_TESTS if m not in first]]


def select(module, per_module, seed):
    """与 base.select_mutants 相同的抽样规则（按 seed:module 打乱候选，惰性校验），只是源文件在 qdperf/ 下。"""
    path = os.path.join(PKG, module + ".py")
    with open(path, encoding="utf-8") as f:
        source = f.read()
    cands = base.enumerate_candidates(source)
    nodes = list(ast.walk(ast.parse(source)))
    order = list(range(len(cands)))
    if per_module and len(cands) > per_module:
        random.Random(f"{seed}:{module}").shuffle(order)
    valid = []
    for i in order:
        if per_module and len(valid) >= per_module:
            break
        idx, op, extra = cands[i]
        rec = base._validate(source, path, idx, op, extra, nodes)
        if rec is not None:
            rec["id"] = f"{module}:{idx}:{op}" + (f":{extra}" if op == "const" else "")
            rec["module"] = module
            valid.append(rec)
    return source, sorted(valid, key=lambda m: m["idx"]), len(cands)


def prepare(workdir, module=None, mutated=None):
    if os.path.exists(workdir):
        shutil.rmtree(workdir)
    os.makedirs(workdir)
    shutil.copytree(PKG, os.path.join(workdir, "qdperf"), ignore=shutil.ignore_patterns("__pycache__", "results"))
    os.makedirs(os.path.join(workdir, "qdperf", "results"))
    if module is not None:
        with open(os.path.join(workdir, "qdperf", module + ".py"), "w", encoding="utf-8") as f:
            f.write(mutated)
    for name in ("qbt", "strategies", "tools", "mutants"):
        if os.path.isdir(os.path.join(REPO, name)):
            os.symlink(os.path.join(REPO, name), os.path.join(workdir, name))


def _env():
    env = dict(os.environ, PYTHONPATH="", QDPERF_FAST="1", QDPERF_NO_TIMING="1", PYTHONDONTWRITEBYTECODE="1")
    env.pop("QDPERF_COV_DIR", None)
    return env


FAILED_TEST = re.compile(r"^(?:FAIL|ERROR): \S+ \(([\w.]+)\)")


def _run(cmd, workdir, env, timeout):
    """→ (状态, 首个失败行)；状态 survived / killed / timeout。"""
    try:
        proc = subprocess.run(cmd, cwd=workdir, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "timeout", ""
    return ("survived" if proc.returncode == 0 else "killed"), base._first_failure(proc.stderr)


def run_one(mutant, source, timeout, work, attempts=2):
    """跑一个突变体。“抓住”要经确认：把报失败的那个测试在同一突变体上单独再跑一遍，仍失败才算（并行负载下偶发失败的
    测试不能把突变体“误杀”）；单独重跑通过 → 记为一次抖动，整套再跑一遍（最多 attempts 遍），都没有确认的失败 → 漏网。
    失败行里没有测试名（导入即崩、解释器报错）的属确定性失败，直接算抓住。"""
    mutated, _b, _a = base.apply_mutation(source, mutant["idx"], mutant["op"], mutant["extra"])
    workdir = os.path.join(work, mutant["id"].replace(":", "_"))
    prepare(workdir, mutant["module"], mutated)
    env = dict(_env(), QDPERF_MUTANT=mutant["id"])
    cmd = [sys.executable, "-m", "unittest", "-f"] + test_order(mutant["module"])
    t0 = time.perf_counter()
    flaky = []
    status, detail = "survived", "OK"
    for _ in range(attempts):
        status, detail = _run(cmd, workdir, env, timeout)
        if status != "killed":
            break
        m = FAILED_TEST.match(detail)
        if m is None or _run([sys.executable, "-m", "unittest", m.group(1)], workdir, env, timeout)[0] != "survived":
            break                                                  # 确认：单独重跑仍失败（或超时）
        flaky.append(detail)
        status, detail = "survived", "OK (unconfirmed failures only)"
    shutil.rmtree(workdir, ignore_errors=True)
    rec = {k: v for k, v in mutant.items() if k not in ("idx", "extra")}
    rec.update(status=status, detail=detail, seconds=round(time.perf_counter() - t0, 2))
    if flaky:
        rec["unconfirmed"] = flaky
    return rec


def baseline(work, copies, floor=120.0):
    """在突变体的目录布局里、按 copies 份并行跑未突变的全套测试：全部通过才继续（并行下判据不抖）。"""
    def one(k):
        wd = os.path.join(work, f"_baseline{k}")
        prepare(wd)
        t0 = time.perf_counter()
        proc = subprocess.run([sys.executable, "-m", "unittest"] + [T + m for m in ALL_TESTS], cwd=wd, env=_env(),
                              capture_output=True, text=True)
        shutil.rmtree(wd, ignore_errors=True)
        return proc.returncode, time.perf_counter() - t0, proc.stderr[-3000:]

    with concurrent.futures.ThreadPoolExecutor(max_workers=copies) as pool:
        res = list(pool.map(one, range(copies)))
    bad = [r for r in res if r[0] != 0]
    if bad:
        raise SystemExit(f"baseline failed in the mutant layout ({len(bad)}/{copies} copies):\n" + bad[0][2])
    elapsed = max(r[1] for r in res)
    return elapsed, max(floor, 4.0 * elapsed)


def write(records, meta, out_json, out_md):
    records = sorted(records, key=lambda r: (r["module"], r["line"], r["id"]))
    summary = base.summarize(records)
    for m in summary["modules"].values():
        m["kill_rate"] = 100.0 * (m["killed"] + m["timeout"]) / m["total"] if m["total"] else 0.0
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "meta": meta, "mutants": records}, f, indent=1, sort_keys=True, ensure_ascii=False)
    with open(out_md, "w", encoding="utf-8") as f:
        f.write("# qdperf 突变测试漏网清单（自动生成；逐条判定见 qdperf/mutants/triage.md）\n\n")
        f.write(f"总数 {summary['total']}，抓住 {summary['caught']}，漏网 {summary['survived']}，"
                f"抓住率 {summary['kill_rate']:.1f}%\n\n")
        for r in records:
            if r["status"] == "survived":
                f.write(f"- `{r['id']}` 行 {r['line']} [{r['op']}]: `{r['before']}` -> `{r['after']}`\n")
    return summary


def _previous(args, plan):
    """--ids / --module 时读回已有结果：→ (只剩要跑的 plan, 保留的旧记录, {id: 首轮状态}, 旧 meta)。"""
    done, prev_meta, first = {}, None, {}
    if os.path.exists(args.out) and (args.ids or args.module):
        with open(args.out, encoding="utf-8") as f:
            prev = json.load(f)
        done, prev_meta = {r["id"]: r for r in prev["mutants"]}, prev["meta"]
    if args.ids:
        wanted = set(args.ids)
        plan = {m: (s, [x for x in muts if x["id"] in wanted], n) for m, (s, muts, n) in plan.items()}
        missing = wanted - {x["id"] for _s, muts, _n in plan.values() for x in muts}
        if missing:
            raise SystemExit(f"unknown mutant ids: {sorted(missing)}")
        first = {k: v.get("first_run", v["status"]) for k, v in done.items() if k in wanted}
        done = {k: v for k, v in done.items() if k not in wanted}
    else:
        done = {k: v for k, v in done.items() if v["module"] not in plan}
    return plan, done, first, prev_meta


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--module", nargs="*", default=None)
    ap.add_argument("--per-module", type=int, default=50)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--timeout", type=float, default=0.0)
    ap.add_argument("--ids", nargs="*", default=None, help="只重跑这些 id，结果并入 --out")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--out", default=os.path.join(RESULTS, "mutation.json"))
    ap.add_argument("--md", default=os.path.join(RESULTS, "mutation_survivors.md"))
    args = ap.parse_args(argv)
    mods = args.module or modules()
    plan = {m: select(m, args.per_module, args.seed) for m in mods}
    if args.list:
        for m, (_s, muts, n) in plan.items():
            print(f"{m:16s} candidates {n:5d}  selected {len(muts)}")
        return 0
    work = tempfile.mkdtemp(prefix="qdperf-mutants-")
    base_s, timeout = baseline(work, args.jobs)
    timeout = args.timeout or timeout
    print(f"baseline {base_s:.1f}s x{args.jobs} parallel copies OK, timeout {timeout:.0f}s", flush=True)
    plan, done, first, prev_meta = _previous(args, plan)
    todo = [(m, src) for _mod, (src, muts, _n) in plan.items() for m in muts]
    meta = {"command": "python3.12 qdperf/mutants/run.py " + " ".join(argv if argv is not None else sys.argv[1:]),
            "per_module": args.per_module, "seed": args.seed, "jobs": args.jobs, "baseline_seconds": round(base_s, 1),
            "timeout_seconds": round(timeout, 1), "candidates": {m: n for m, (_s, _muts, n) in plan.items()}}
    if args.ids and prev_meta is not None:                 # 补判据后的重跑：保留首轮的 meta，重跑记进 reruns
        rerun = {"command": meta["command"], "ids": len(todo), "baseline_seconds": meta["baseline_seconds"]}
        meta = dict(prev_meta, reruns=prev_meta.get("reruns", []) + [rerun])
    records = list(done.values())
    t0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futs = [pool.submit(run_one, m, src, timeout, work) for m, src in todo]
        for k, fut in enumerate(concurrent.futures.as_completed(futs), 1):
            rec = fut.result()
            if rec["id"] in first:
                rec["first_run"] = first[rec["id"]]        # 首轮结果（survived → 补判据后 killed 的可追溯）
            records.append(rec)
            print(f"[{k}/{len(todo)}] {rec['status']:8s} {rec['id']} line {rec['line']} ({rec['seconds']}s) "
                  f"{rec['detail'][:70]}", flush=True)
            if k % 20 == 0:
                write(records, meta, args.out, args.md)
    (meta["reruns"][-1] if args.ids and prev_meta is not None else meta)["elapsed_seconds"] = \
        round(time.perf_counter() - t0, 1)
    shutil.rmtree(work, ignore_errors=True)
    summary = write(records, meta, args.out, args.md)
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
