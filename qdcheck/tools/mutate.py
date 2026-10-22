"""qdcheck 的突变测试：复用本仓 mutants/run_mutations.py 的突变算子与候选枚举 / 抽样 / 校验
（enumerate_candidates、apply_mutation、_validate、summarize、_first_failure：运算符翻转、比较边界、条件取反、
布尔运算互换、常量 ±1 / 翻转、语句删除、return 置空、break/continue 互换），只是把被测包换成 qdcheck，
并按模块给出“最能抓”的测试顺序（-f 失败即停）。

    python3.12 qdcheck/tools/mutate.py --per-module 60 --jobs 4            # -> qdcheck/results/mutation.json
    python3.12 qdcheck/tools/mutate.py --ids fuzz:1234:compare ... --merge  # 补判据后只重跑指定突变体

每个突变体：在 tempfile 下的独立工作目录里复制 qdcheck/（被突变的模块换成突变源码），vectors/ 与仓库其余顶层包软链进去，
子进程跑 QDCHECK_FAST=1 python -m unittest -f <模块顺序>；失败 = 抓住，超时 = 抓住（单列），通过 = 漏网。
"""

import argparse
import ast
import concurrent.futures
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from mutants import run_mutations as base      # noqa: E402  本仓已有的突变器

PKG = os.path.join(REPO, "qdcheck")
RESULTS = os.path.join(PKG, "results")
WORK = tempfile.mkdtemp(prefix="qdcheck-mutants-")             # 工作目录在 tempfile 下，不写仓库
T = "qdcheck.tests."
ALL_TESTS = ["test_compare", "test_synth", "test_ledger", "test_examples", "test_contract", "test_decimal", "test_stream",
             "test_determinism", "test_fuzz", "test_indicators", "test_vectors"]
FIRST = {   # 每个模块最先跑的测试（其余测试随后，保证漏网的突变体跑过整套）
    "compare": ["test_compare", "test_contract", "test_indicators"], "determinism": ["test_determinism"],
    "examples": ["test_examples", "test_fuzz", "test_determinism"], "fuzz": ["test_fuzz"], "gen_vectors": ["test_vectors"],
    "indicators_alt": ["test_indicators", "test_decimal"], "indicators_ref": ["test_indicators", "test_decimal"],
    "vector_schema": ["test_contract", "test_vectors"], "ledger": ["test_ledger", "test_stream"],
    "ledger_alt": ["test_ledger", "test_stream"], "props": ["test_indicators"],
    "stream": ["test_stream", "test_ledger", "test_vectors"], "synth": ["test_synth", "test_vectors"],
}


def modules():
    return sorted(f[:-3] for f in os.listdir(PKG) if f.endswith(".py") and f not in ("__init__.py", "selftest.py"))


def test_order(module):
    first = FIRST.get(module, [])
    return [T + m for m in first + [m for m in ALL_TESTS if m not in first]]


def select(module, per_module, seed):
    """与 base.select_mutants 相同的抽样规则（按 seed:module 打乱候选，惰性校验），只是源文件在 qdcheck/ 下。"""
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
    shutil.copytree(PKG, os.path.join(workdir, "qdcheck"),
                    ignore=shutil.ignore_patterns("__pycache__", "vectors", "results"))
    os.symlink(os.path.join(PKG, "vectors"), os.path.join(workdir, "qdcheck", "vectors"))
    os.makedirs(os.path.join(workdir, "qdcheck", "results"))
    if module is not None:
        with open(os.path.join(workdir, "qdcheck", module + ".py"), "w", encoding="utf-8") as f:
            f.write(mutated)
    for name in ("qbt", "strategies", "tools", "mutants", "tests"):
        if os.path.isdir(os.path.join(REPO, name)):
            os.symlink(os.path.join(REPO, name), os.path.join(workdir, name))


def _env():
    return dict(os.environ, PYTHONPATH="", QDCHECK_FAST="1", PYTHONDONTWRITEBYTECODE="1")


def run_one(mutant, source, timeout):
    mutated, _b, _a = base.apply_mutation(source, mutant["idx"], mutant["op"], mutant["extra"])
    workdir = os.path.join(WORK, mutant["id"].replace(":", "_"))
    prepare(workdir, mutant["module"], mutated)
    cmd = [sys.executable, "-m", "unittest", "-f"] + test_order(mutant["module"])
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(cmd, cwd=workdir, env=dict(_env(), QDCHECK_MUTANT=mutant["id"]),
                              capture_output=True, text=True, timeout=timeout)
        status = "survived" if proc.returncode == 0 else "killed"
        detail = base._first_failure(proc.stderr)
    except subprocess.TimeoutExpired:
        status, detail = "timeout", ""
    shutil.rmtree(workdir, ignore_errors=True)
    rec = {k: v for k, v in mutant.items() if k not in ("idx", "extra")}
    rec.update(status=status, detail=detail, seconds=round(time.perf_counter() - t0, 2))
    return rec


def baseline(timeout_floor=60.0):
    workdir = os.path.join(WORK, "_baseline")
    prepare(workdir)
    t0 = time.perf_counter()
    proc = subprocess.run([sys.executable, "-m", "unittest"] + [T + m for m in ALL_TESTS], cwd=workdir, env=_env(),
                          capture_output=True, text=True)
    elapsed = time.perf_counter() - t0
    shutil.rmtree(workdir, ignore_errors=True)
    if proc.returncode != 0:
        raise SystemExit("baseline failed in the mutant layout:\n" + proc.stderr[-3000:])
    return elapsed, max(timeout_floor, 4.0 * elapsed)


def write(records, meta, out_json, out_md):
    records = sorted(records, key=lambda r: (r["module"], r["line"], r["id"]))
    summary = base.summarize(records)
    for name, m in summary["modules"].items():
        m["kill_rate"] = 100.0 * (m["killed"] + m["timeout"]) / m["total"] if m["total"] else 0.0
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "meta": meta, "mutants": records}, f, indent=1, sort_keys=True,
                  ensure_ascii=False)
    with open(out_md, "w", encoding="utf-8") as f:
        f.write("# qdcheck 突变测试漏网清单\n\n")
        f.write(f"总数 {summary['total']}，抓住 {summary['caught']}，漏网 {summary['survived']}，"
                f"抓住率 {summary['kill_rate']:.1f}%\n\n")
        for r in records:
            if r["status"] == "survived":
                f.write(f"- `{r['id']}` 行 {r['line']} [{r['op']}]: `{r['before']}` -> `{r['after']}`\n")
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--module", nargs="*", default=None)
    ap.add_argument("--per-module", type=int, default=60)
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
    os.makedirs(WORK, exist_ok=True)
    base_s, timeout = baseline()
    timeout = args.timeout or timeout
    print(f"baseline {base_s:.1f}s, timeout {timeout:.0f}s", flush=True)
    done = {}
    if os.path.exists(args.out) and (args.ids or args.module):
        with open(args.out, encoding="utf-8") as f:
            done = {r["id"]: r for r in json.load(f)["mutants"]}
    if args.ids:
        wanted = set(args.ids)
        plan = {m: (s, [x for x in muts if x["id"] in wanted], n) for m, (s, muts, n) in plan.items()}
        missing = wanted - {x["id"] for _s, muts, _n in plan.values() for x in muts}
        if missing:
            raise SystemExit(f"unknown mutant ids: {sorted(missing)}")
    else:
        done = {k: v for k, v in done.items() if v["module"] not in plan}
    todo = [(m, src) for _mod, (src, muts, _n) in plan.items() for m in muts]
    meta = {"command": "python3.12 qdcheck/tools/mutate.py " + " ".join(argv if argv is not None else sys.argv[1:]),
            "per_module": args.per_module, "seed": args.seed, "baseline_seconds": round(base_s, 1),
            "timeout_seconds": round(timeout, 1),
            "candidates": {m: n for m, (_s, _muts, n) in plan.items()}}
    records = [r for r in done.values() if not (args.ids and r["id"] in set(args.ids))]
    t0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futs = [pool.submit(run_one, m, src, timeout) for m, src in todo]
        for k, fut in enumerate(concurrent.futures.as_completed(futs), 1):
            rec = fut.result()
            records.append(rec)
            print(f"[{k}/{len(todo)}] {rec['status']:8s} {rec['id']} line {rec['line']} ({rec['seconds']}s) "
                  f"{rec['detail'][:70]}", flush=True)
            if k % 20 == 0:
                write(records, meta, args.out, args.md)
    meta["elapsed_seconds"] = round(time.perf_counter() - t0, 1)
    shutil.rmtree(WORK, ignore_errors=True)
    summary = write(records, meta, args.out, args.md)
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
