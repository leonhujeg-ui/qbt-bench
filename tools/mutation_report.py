"""把 mutants/results.json + 人工 triage 表渲染成 REPORT.md 的突变测试一节（Markdown）。

用法：python tools/mutation_report.py [--results mutants/results.json] [--triage mutants/triage.json]
triage.json: {"<mutant id>": "等价：..." 或 "补判据：tests/...::test_x（已抓住）"}
"""
import argparse
import json
import os
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def render(results, triage):
    s = results["summary"]
    lines = [f"总数 **{s['total']}**（{len(s['modules'])} 个模块，每模块 80 个，抽样 seed=0），抓住 **{s['caught']}**"
             f"（其中 timeout {sum(m['timeout'] for m in s['modules'].values())} 个：死循环类突变体超过 4 倍基线时长判定），"
             f"漏网 **{s['survived']}**，抓住率 **{s['kill_rate']:.1f}%**。", "",
             "| 模块 | 总数 | 抓住 | 其中 timeout | 漏网 | 抓住率 |", "|---|---|---|---|---|---|"]
    for mod, m in sorted(s["modules"].items()):
        caught = m["killed"] + m["timeout"]
        lines.append(f"| `qbt/{mod}.py` | {m['total']} | {caught} | {m['timeout']} | {m['survived']} | {100.0 * caught / m['total']:.1f}% |")
    lines += ["", "突变算子：二元运算符互换、比较运算符边界互换（< ↔ <=、== ↔ !=、is ↔ is not、in ↔ not in）、and ↔ or、"
              "删除 not / 负号、常量 ±1 / ×1.5 / 翻转、if/while 条件取反、语句删除（替换为 pass）、return 置 None、break ↔ continue。",
              "运行方式：每个突变体复制整个 `qbt/` 到独立目录并软链其余包，子进程 `python -m unittest -f`（失败即停，先跑黄金文件与指标模块）。", "",
              "漏网清单（每个给出“为什么等价”或“补了哪条判据”）：", "",
              "| id | 行 | 变异 | 结论 |", "|---|---|---|---|"]
    for r in sorted(results["mutants"], key=lambda r: (r["module"], r["line"], r["id"])):
        if r["status"] != "survived":
            continue
        base = r["id"].rsplit(":", 1)[0] if r["op"] == "const" and r["id"].count(":") == 3 else r["id"]
        note = triage.get(r["id"]) or triage.get(base) or "（未 triage）"
        before, after = r["before"].replace("|", "\\|").replace("\n", " ")[:70], r["after"].replace("|", "\\|").replace("\n", " ")[:70]
        lines.append(f"| `{r['id']}` | {r['line']} | `{before}` → `{after}` | {note} |")
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=os.path.join(REPO, "mutants", "results.json"))
    ap.add_argument("--triage", default=os.path.join(REPO, "mutants", "triage.json"))
    args = ap.parse_args(argv)
    with open(args.results, encoding="utf-8") as f:
        results = json.load(f)
    triage = {}
    if os.path.exists(args.triage):
        with open(args.triage, encoding="utf-8") as f:
            triage = json.load(f)
    sys.stdout.write(render(results, triage))
    return 0


if __name__ == "__main__":
    sys.exit(main())
