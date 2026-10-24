"""由 results/mutation.json 与 mutants/triage_reasons.py 生成 mutants/triage.md：每个漏网突变体一行判定（等价 / 已补判据 / 待补）。

    python3.12 qdexsim/mutants/triage.py            # 重新生成 triage.md
    python3.12 qdexsim/mutants/triage.py --check    # 校验：每个漏网都有判定、没有多余判定、triage.md 与生成结果一致 → 否则退出码 1
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
REPO = os.path.dirname(PKG)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from qdexsim.mutants import triage_reasons as R      # noqa: E402

RESULTS = os.path.join(PKG, "results", "mutation.json")
OUT = os.path.join(HERE, "triage.md")
KINDS = {"equivalent": "等价", "fixed": "已补判据（重跑已抓住）", "pending": "待补"}


def load(path=RESULTS):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def render(data):
    s, meta = dict(data["summary"]), data["meta"]
    s["timeout"] = sum(m["timeout"] for m in s["modules"].values())
    survivors = sorted((m for m in data["mutants"] if m["status"] == "survived"), key=lambda m: (m["module"], m["line"]))
    lines = ["# qdexsim 突变漏网逐条判定（由 mutants/triage.py 生成）", "",
             f"命令 `{meta['command']}`；每模块 {meta['per_module']} 个、共 {s['total']} 个突变体，抓住 {s['caught']}（超时 {s['timeout']}），"
             f"漏网 {s['survived']}，抓住率 {s['kill_rate']:.1f}%；基线 {meta['baseline_seconds']} s，总耗时 {meta.get('elapsed_seconds', '?')} s。", "",
             "| 模块 | 总数 | 抓住 | 漏网 | 抓住率 |", "|---|---|---|---|---|"]
    for mod, m in sorted(s["modules"].items()):
        lines.append(f"| {mod} | {m['total']} | {m['killed'] + m['timeout']} | {m['survived']} | {m['kill_rate']:.1f}% |")
    lines += ["", "## 漏网逐条", "", "| id | 行 | 变化 | 判定 | 理由 |", "|---|---|---|---|---|"]
    counts = {k: 0 for k in KINDS}
    for m in survivors:
        kind, why = R.REASONS.get(m["id"], ("pending", "（没有判定）"))
        counts[kind] += 1
        lines.append(f"| `{m['id']}` | {m['line']} | `{m['before']}` → `{m['after']}` | {KINDS[kind]} | {why} |")
    lines += ["", f"等价 {counts['equivalent']}，已补判据 {counts['fixed']}，待补 {counts['pending']}。"]
    return "\n".join(lines) + "\n", survivors, counts


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    data = load()
    text, survivors, counts = render(data)
    ids = {m["id"] for m in survivors}
    missing = sorted(ids - set(R.REASONS))
    extra = sorted(set(R.REASONS) - {m["id"] for m in data["mutants"]})
    if args.check:
        with open(OUT, encoding="utf-8") as f:
            same = f.read() == text
        bad = missing or extra or not same or counts["pending"]
        print(f"survivors {len(ids)}, reasons {len(R.REASONS)}, missing {missing}, extra {extra}, pending {counts['pending']}, up-to-date {same}")
        return 1 if bad else 0
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"wrote {OUT}: survivors {len(ids)}, missing reasons {missing}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
