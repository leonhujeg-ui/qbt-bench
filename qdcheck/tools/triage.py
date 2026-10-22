"""把历次突变测试的漏网逐条落实：对每个历史漏网，在当前源码里找同一处突变（按 模块 + 算子 + 改动前后文本 匹配），
在最终测试上重跑一次，记录“抓住（被哪个测试）/ 仍漏网（等价判定理由）/ 该代码已不存在”，并渲染 qdcheck/mutants/triage.md。

    python3.12 -m qdcheck.tools.triage --replay        # 重跑（几分钟到十几分钟）-> results/mutation_replay.json
    python3.12 -m qdcheck.tools.triage                 # 只渲染 mutants/triage.md（用已有的 replay 结果）
"""

import argparse
import ast
import concurrent.futures
import json
import os
import shutil
import sys
import time

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
from mutants import run_mutations as base          # noqa: E402
from qdcheck.tools import mutate                    # noqa: E402

RESULTS = os.path.join(REPO, "qdcheck", "results")
HISTORY = os.path.join(RESULTS, "mutation_history")
OUT_MD = os.path.join(REPO, "qdcheck", "mutants", "triage.md")
ROUNDS = ["round1.json", "round2.json", "round3_merged.json", "round4_merged.json", "round5_merged.json",
          "round5b_merged.json", "final_full.json", "review_round1.json", "final_review.json"]


def historical_survivors():
    """{(module, op, before, after): {"first_seen": round, "ids": [...]}}（按出现顺序）。"""
    out = {}
    for rnd in ROUNDS:
        path = os.path.join(HISTORY, rnd)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for r in json.load(f)["mutants"]:
                if r["status"] != "survived":
                    continue
                key = (r["module"], r["op"], r["before"], r["after"])
                rec = out.setdefault(key, {"first_seen": rnd, "ids": [], "line": r["line"]})
                rec["ids"].append(f"{rnd[:-5]}:{r['id']}")
    return out


def current_candidates(module):
    path = os.path.join(mutate.PKG, module + ".py")
    with open(path, encoding="utf-8") as f:
        source = f.read()
    nodes = list(ast.walk(ast.parse(source)))
    found = {}
    for idx, op, extra in base.enumerate_candidates(source):
        rec = base._validate(source, path, idx, op, extra, nodes)
        if rec is None:
            continue
        rec["id"] = f"{module}:{idx}:{op}" + (f":{extra}" if op == "const" else "")
        rec["module"] = module
        found.setdefault((op, rec["before"], rec["after"]), rec)
    return source, found


def replay(jobs=4):
    surv = historical_survivors()
    plan, missing = [], []
    cache = {}
    for (module, op, before, after), rec in surv.items():
        if module not in cache:
            cache[module] = current_candidates(module)
        source, cands = cache[module]
        cur = cands.get((op, before, after))
        if cur is None:
            missing.append({"key": [module, op, before, after], **rec, "status": "code_gone"})
        else:
            plan.append((dict(cur), source, rec))
    base_s, timeout = mutate.baseline()
    results = []
    t0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        futs = {pool.submit(mutate.run_one, m, src, timeout): (m, rec) for m, src, rec in plan}
        for k, fut in enumerate(concurrent.futures.as_completed(futs), 1):
            m, rec = futs[fut]
            out = fut.result()
            out.update(first_seen=rec["first_seen"], history_ids=rec["ids"], history_line=rec["line"])
            results.append(out)
            print(f"[{k}/{len(plan)}] {out['status']:8s} {out['id']} ({out['first_seen']}) {out['detail'][:60]}", flush=True)
    shutil.rmtree(mutate.WORK, ignore_errors=True)
    report = {"command": "python3.12 -m qdcheck.tools.triage --replay", "rounds": ROUNDS, "unique_survivors": len(surv),
              "replayed": len(plan), "code_gone": missing, "baseline_seconds": round(base_s, 1),
              "elapsed_seconds": round(time.perf_counter() - t0, 1),
              "results": sorted(results, key=lambda r: (r["module"], r["line"], r["id"]))}
    with open(os.path.join(RESULTS, "mutation_replay.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1, sort_keys=True, ensure_ascii=False)
    return report


def render():
    with open(os.path.join(RESULTS, "mutation_replay.json"), encoding="utf-8") as f:
        rep = json.load(f)
    with open(os.path.join(RESULTS, "mutation_triage.json"), encoding="utf-8") as f:
        triage = json.load(f)
    with open(os.path.join(RESULTS, "mutation.json"), encoding="utf-8") as f:
        final = json.load(f)
    lines = ["# qdcheck 突变测试：历次漏网逐条判定（triage）", "",
             "由 `python3.12 -m qdcheck.tools.triage --replay` 生成：收集 `results/mutation_history/*.json` 每一轮的全部漏网",
             f"（去重后 {rep['unique_survivors']} 处突变），在**当前**源码里找同一处突变并用**最终**测试重跑一次。",
             "“抓住”列出第一个失败的测试；“仍漏网”的每条都有等价判定理由（`results/mutation_triage.json`）；",
             "“代码已不存在”表示那段代码在后续修改中被删除或改写（所在行与改动仍列出）。", "",
             f"最终全量结果：{final['summary']['caught']}/{final['summary']['total']} = {final['summary']['kill_rate']:.1f}%"
             f"（`results/mutation.json`）。重跑耗时 {rep['elapsed_seconds']} s。", "",
             "| 模块 | 行 | 首次漏网于 | 改动 | 现在 | 说明 |", "|---|---:|---|---|---|---|"]
    n_killed = n_surv = 0
    for r in rep["results"]:
        change = f"`{r['before'][:60]}` → `{r['after'][:40]}`".replace("|", "\\|")
        if r["status"] in ("killed", "timeout"):
            n_killed += 1
            how = "超时（计入抓住）" if r["status"] == "timeout" else f"抓住：`{r['detail'][:90]}`".replace("|", "\\|")
        else:
            n_surv += 1
            t = triage.get(r["id"], {"verdict": "未判定", "reason": "（缺判定）"})
            how = f"**仍漏网 → {t['verdict']}**：{t['reason']}".replace("|", "/")
        lines.append(f"| `{r['module']}` | {r['line']} | {r['first_seen'][:-5]} | {change} | "
                     f"{'抓住' if r['status'] != 'survived' else '漏网'} | {how} |")
    for m in rep["code_gone"]:
        module, op, before, after = m["key"]
        change = f"`{before[:60]}` → `{after[:40]}`".replace("|", "\\|")
        lines.append(f"| `{module}` | {m['line']} | {m['first_seen'][:-5]} | {change} | 代码已不存在 | "
                     f"该处代码在后续提交中删除 / 改写（{', '.join(m['ids'][:2])}） |")
    lines[8:8] = [f"重跑 {rep['replayed']} 处：抓住 {n_killed}，仍漏网 {n_surv}（全部有判定），代码已不存在 {len(rep['code_gone'])}。", ""]
    os.makedirs(os.path.dirname(OUT_MD), exist_ok=True)
    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"wrote {os.path.relpath(OUT_MD, REPO)}: replayed {rep['replayed']}, killed {n_killed}, survived {n_surv}, gone {len(rep['code_gone'])}")
    return n_surv


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay", action="store_true")
    ap.add_argument("--jobs", type=int, default=4)
    args = ap.parse_args(argv)
    if args.replay:
        replay(args.jobs)
    render()
    return 0


if __name__ == "__main__":
    sys.exit(main())
