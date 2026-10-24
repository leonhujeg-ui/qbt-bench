"""由 results/mutation.json + mutants/triage_reasons.py 生成 mutants/triage.md；--check 只校验（selftest 用）。

    python3.12 qdtext/mutants/triage.py [--check]
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from qdtext.mutants.triage_reasons import REASONS      # noqa: E402

RESULTS = os.path.join(HERE, "..", "results", "mutation.json")


def load(path=RESULTS):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def check(doc):
    """→ 问题列表（空 = 一致）。"""
    survivors = {r["id"]: r for r in doc["mutants"] if r["status"] == "survived"}
    problems = [f"漏网未判定: {i}" for i in sorted(survivors) if i not in REASONS]
    problems += [f"判为 fixed 却仍漏网: {i}" for i, (kind, _why) in REASONS.items() if kind == "fixed" and i in survivors]
    problems += [f"判定引用了不存在的突变体: {i}" for i in REASONS if i not in {r["id"] for r in doc["mutants"]}]
    return problems


def render(doc):
    s, meta = doc["summary"], doc["meta"]
    lines = ["# qdtext 突变测试逐条判定（自动生成：`python3.12 qdtext/mutants/triage.py`）", "",
             f"命令：`{meta['command']}`；每模块抽样 {meta['per_module']}；基线 {meta['baseline_seconds']} s；完成于 {meta.get('finished_at')}", "",
             f"总数 {s['total']}，抓住 {s['caught']}（含超时 {sum(m['timeout'] for m in s['modules'].values())}），漏网 {s['survived']}，"
             f"抓住率 {s['kill_rate']:.1f}%", "", "| 模块 | 候选 | 抽样 | 抓住 | 漏网 | 抓住率 |", "|---|---|---|---|---|---|"]
    for m, v in sorted(s["modules"].items()):
        lines.append(f"| {m} | {meta['candidates'].get(m, '-')} | {v['total']} | {v['killed'] + v['timeout']} | {v['survived']} | {v['kill_rate']:.1f}% |")
    lines += ["", "## 漏网逐条", ""]
    survivors = [r for r in doc["mutants"] if r["status"] == "survived"]
    if not survivors:
        lines.append("无。")
    for r in sorted(survivors, key=lambda r: (r["module"], r["line"])):
        kind, why = REASONS.get(r["id"], ("?", "未判定"))
        lines.append(f"- `{r['id']}` 行 {r['line']} [{r['op']}] `{r['before'][:60]}` → `{r['after'][:60]}`：**{kind}** — {why}")
    fixed = [(i, why) for i, (kind, why) in REASONS.items() if kind == "fixed"]
    if fixed:
        lines += ["", "## 首轮漏网、补判据后抓住", ""]
        by_id = {r["id"]: r for r in doc["mutants"]}
        for i, why in fixed:
            r = by_id.get(i, {})
            lines.append(f"- `{i}` 行 {r.get('line', '?')}：首轮 {r.get('first_run', 'survived')} → 现在 {r.get('status', '?')} — {why}")
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--results", default=RESULTS)
    args = ap.parse_args(argv)
    if not os.path.exists(args.results):
        print("no mutation results yet: run qdtext/mutants/run.py first")
        return 1
    doc = load(args.results)
    problems = check(doc)
    for p in problems:
        print(p)
    if not args.check:
        with open(os.path.join(HERE, "triage.md"), "w", encoding="utf-8") as f:
            f.write(render(doc))
        print(f"wrote {os.path.join(HERE, 'triage.md')}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
