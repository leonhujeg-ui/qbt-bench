"""由 qdcheck/tools/report_template.md + qdcheck/results/*.json 生成 qdcheck/REPORT.md（报告里的数字全部取自结果文件）。

    python3.12 qdcheck/tools/build_report.py            # 写 qdcheck/REPORT.md
    python3.12 qdcheck/tools/build_report.py --check    # 只比较，不一致退出码 1（测试里用，防止报告与结果文件脱节）

占位符：``{{文件:点路径|格式}}``，例如 ``{{g3_streams.json:seconds|.1f}}``、``{{coverage.json:library.total_percent|.2f}}``；
格式缺省为原样，``,`` 为千分位，其余按 format() 规格。``{{@名字}}`` 为生成的表格（见 BLOCKS）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from qdcheck import indicators_ref as R      # noqa: E402
from qdcheck import props as P               # noqa: E402

RESULTS = os.path.join(REPO, "qdcheck", "results")
TEMPLATE = os.path.join(REPO, "qdcheck", "tools", "report_template.md")
OUT = os.path.join(REPO, "qdcheck", "REPORT.md")
_PH = re.compile(r"\{\{([^{}]+)\}\}")


def _load(name, cache):
    if name not in cache:
        with open(os.path.join(RESULTS, name), encoding="utf-8") as f:
            cache[name] = json.load(f)
    return cache[name]


def _get(obj, path):
    for part in path.split("."):
        obj = obj[int(part)] if isinstance(obj, list) else obj[part]
    return obj


def _fmt(value, spec):
    if spec is None:
        return str(value)
    if spec == ",":
        return f"{value:,}"
    return format(value, spec)


def variant_table():
    rows = ["| 指标 | 输出线 | variant（全部） | 默认参数 | 预热期（前导无值个数，非退化数据） |", "|---|---|---|---|---|"]
    for ind in R.INDICATORS:
        for k, var in enumerate(R.VARIANTS[ind]):
            warm = P.expected_warmup(ind, var)
            w = ", ".join(f"{line}={n}" for line, n in warm.items())
            rows.append(f"| {ind if k == 0 else ''} | {', '.join(R.LINES[ind]) if k == 0 else ''} | `{var}` | "
                        f"{json.dumps(R.DEFAULT_PARAMS[ind], ensure_ascii=False) if k == 0 else ''} | {w} |")
    return "\n".join(rows)


def coverage_table(cache):
    cov = _load("coverage.json", cache)
    rows = ["| 文件 | 语句 | 未覆盖 | 覆盖率 |", "|---|---:|---:|---:|"]
    for rel, r in sorted(cov["library"]["files"].items()):
        rows.append(f"| `{rel}` | {r['statements']} | {r['statements'] - r['covered']} | {r['percent']:.1f}% |")
    lib = cov["library"]
    rows.append(f"| **合计（计入判据）** | {lib['total_statements']} | {lib['total_statements'] - lib['total_covered']} "
                f"| **{lib['total_percent']:.2f}%** |")
    tools = cov["tools"]
    rows.append(f"| qdcheck/tools/*（不计入） | {tools['total_statements']} | "
                f"{tools['total_statements'] - tools['total_covered']} | {tools['total_percent']:.1f}% |")
    return "\n".join(rows)


def mutation_table(cache):
    mut = _load("mutation.json", cache)
    rows = ["| 模块 | 候选 | 突变体 | 抓住 | 超时 | 漏网 | 抓住率 |", "|---|---:|---:|---:|---:|---:|---:|"]
    cands = mut["meta"].get("candidates", {})
    for name, m in sorted(mut["summary"]["modules"].items()):
        rows.append(f"| `{name}.py` | {cands.get(name, '')} | {m['total']} | {m['killed']} | {m['timeout']} | "
                    f"{m['survived']} | {m['kill_rate']:.1f}% |")
    s = mut["summary"]
    rows.append(f"| **合计** | | {s['total']} | | | {s['survived']} | **{s['kill_rate']:.1f}%** |")
    return "\n".join(rows)


def triage_table(cache):
    mut = _load("mutation.json", cache)
    tri = _load("mutation_triage.json", cache)
    rows = ["| 突变 id | 行 | 改动 | 判定 | 理由 |", "|---|---:|---|---|---|"]
    for r in mut["mutants"]:
        if r["status"] != "survived":
            continue
        t = tri.get(r["id"], {"verdict": "未判定", "reason": ""})
        change = f"`{r['before'][:50]}` → `{r['after'][:50]}`".replace("|", "\\|")
        rows.append(f"| `{r['id']}` | {r['line']} | {change} | {t['verdict']} | {t['reason'].replace('|', '/')} |")
    if len(rows) == 2:
        rows.append("| （无） | | | | |")
    return "\n".join(rows)


BLOCKS = {"variant_table": lambda c: variant_table(), "coverage_table": coverage_table,
          "mutation_table": mutation_table, "triage_table": triage_table,
          "n_variants": lambda c: str(sum(len(v) for v in R.VARIANTS.values())),
          "n_variant_lines": lambda c: str(sum(len(v) * len(R.LINES[k]) for k, v in R.VARIANTS.items()))}


def render(template_text):
    cache = {}

    def sub(m):
        expr = m.group(1).strip()
        if expr.startswith("@"):
            return BLOCKS[expr[1:]](cache)
        ref, _, spec = expr.partition("|")
        name, _, path = ref.partition(":")
        return _fmt(_get(_load(name, cache), path), spec or None)
    return _PH.sub(sub, template_text)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    with open(TEMPLATE, encoding="utf-8") as f:
        text = render(f.read())
    if args.check:
        with open(OUT, encoding="utf-8") as f:
            same = f.read() == text
        print("REPORT.md is up to date" if same else "REPORT.md is stale: run python3.12 qdcheck/tools/build_report.py")
        return 0 if same else 1
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"wrote {os.path.relpath(OUT, REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
