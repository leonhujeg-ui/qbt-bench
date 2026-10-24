"""由 qdexsim/results/*.json 生成 REPORT.md（python3.12 qdexsim/tools/build_report.py [--check]）。
数字只从结果文件来：coverage.json、mutation.json、g1_dialects.json、g2_chaos.json、g3_scenarios.json、g4_recon.json、knives.json、
selftest_summary.json；文字段落（简化口径、待核清单、搬运建议）在 report_template.md 里，占位符 {{...}} 由这里填。"""
import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
REPO = os.path.dirname(PKG)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from qdexsim import common                       # noqa: E402
from qdexsim.mutants import triage as T          # noqa: E402

OUT = os.path.join(PKG, "REPORT.md")


def load(name):
    return common.read_json(os.path.join(common.RESULTS, name))


def table(rows, header):
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def g1_block(g1):
    rows = []
    for name, d in g1["dialects"].items():
        pf = d["partial_fill"]
        rows.append([name, f"{d['bad_sign_code']} (期望 {d['bad_sign_expected']})", f"{d['bad_ts_code']} (期望 {d['bad_ts_expected']})",
                     f"{pf['status']} {pf['filled']}/{pf['qty']}", d["cancel_status"], d["after_cancel"], "✅" if d["ok"] else "❌"])
    r = g1["restart"]
    restart = (f"进程 kill 后同库同端口重启：订单 {r['orders']}、成交 {r['fills']}、BTC 持仓 {r['position_btc']}，与重启前快照相等 = {r['state_equal']}，"
               f"同端口 = {r['same_port']}，重启后用旧 clientOrderId 再下单 → {r['dup_cid_after_restart']}；耗时 {g1['seconds']} s")
    return table(rows, ["方言", "错签名码", "过期时间戳码", "限价 0.06 被 0.05 流动性打成", "撤单", "撤后状态", "结果"]) + "\n\n" + restart


def g3_block(g3):
    teeth = sorted(g3["naive_red_judges"].items(), key=lambda kv: -kv[1])
    per_t = {}
    for r in g3["results"]:
        t = r["scenario"].split("/")[1]
        d = per_t.setdefault(t, {"naive_red": 0, "correct_green": 0, "naive_judges": set(), "seconds": 0.0})
        if r["client"] == "naive" and not r["ok"]:
            d["naive_red"] += 1
            d["naive_judges"] |= {v["judge"] for v in r["verdicts"] if not v["ok"]}
        if r["client"] == "correct" and r["ok"]:
            d["correct_green"] += 1
        d["seconds"] += r["seconds"]
    rows = [[t, f"{d['naive_red']}/5", f"{d['correct_green']}/5", ", ".join(sorted(d["naive_judges"])), f"{d['seconds']:.1f}"]
            for t, d in sorted(per_t.items())]
    return (f"{g3['scenarios']} 个场景（{len(g3['templates'])} 模板 × {len(g3['dialects'])} 方言），两个客户端各跑一遍，共 {g3['seconds']} s。"
            f"正确客户端绿 {g3['correct']['green']}/{g3['scenarios']}，红 {g3['correct']['red']}；天真客户端红 {g3['naive']['red']}/{g3['scenarios']}，"
            f"绿 {g3['naive']['green']}；要求必红的 {g3['naive_must_red_scenarios']} 个场景（unknown_timeout / unknown_drop / unknown_502 / client_restart）"
            f"全红 = {g3['naive_must_red_ok']}。\n\n天真客户端被哪些判据抓红（场景数）：" + "，".join(f"{k} {v}" for k, v in teeth) + "\n\n"
            + table(rows, ["模板", "天真红", "正确绿", "抓红天真客户端的判据", "秒（两客户端合计）"]))


def cov_block(cov):
    lib = cov["library"]
    rows = [[rel, r["statements"], r["statements"] - r["covered"], f"{r['percent']:.1f}%"] for rel, r in sorted(lib["files"].items())]
    rows.append(["**合计**", lib["total_statements"], lib["total_statements"] - lib["total_covered"], f"**{lib['total_percent']:.2f}%**"])
    return (f"{cov['tests_run']} 个测试，子进程覆盖合并 {cov['child_processes_merged']} 份；库模块合计 **{lib['total_percent']:.2f}%**（要求 ≥ 90%）。\n\n"
            + table(rows, ["文件", "语句", "未覆盖", "覆盖率"]))


def mut_block(mut, text, counts):
    s, meta = dict(mut["summary"]), mut["meta"]
    s["timeout"] = sum(m["timeout"] for m in s["modules"].values())
    rows = [[m, v["total"], v["killed"] + v["timeout"], v["survived"], f"{v['kill_rate']:.1f}%"] for m, v in sorted(s["modules"].items())]
    reruns = meta.get("reruns", [])
    first = sum(1 for m in mut["mutants"] if m.get("first_run") == "survived" and m["status"] != "survived")
    return (f"{len(s['modules'])} 个模块 × 每模块 {meta['per_module']} 个 = {s['total']} 个突变体（`{meta['command']}`，{meta['jobs']} 路并行，"
            f"基线 {meta['baseline_seconds']} s，首轮 {meta.get('elapsed_seconds', '?')} s）；抓住 {s['caught']}（其中超时 {s['timeout']}），漏网 {s['survived']}，"
            f"**抓住率 {s['kill_rate']:.1f}%**（要求 ≥ 85%）。首轮漏网、补判据后重跑（`--ids`，{len(reruns)} 轮）被抓住的 {first} 个；最终漏网逐条判定：等价 "
            f"{counts['equivalent']}、待补 {counts['pending']}（见 [mutants/triage.md](mutants/triage.md)）。\n\n"
            + table(rows, ["模块", "总数", "抓住", "漏网", "抓住率"]))


def knives_block(k):
    rows = [[x["knife"], "红 ✅" if x["red"] else "绿 ❌", x["caught_by"] or "-"] for x in k["knives"]]
    return table(rows, ["手工突变", "结果", "先抓住它的测试"])


def selftest_block(st):
    rows = [[n, r["scale"], f"{r['seconds']}", "OK" if r["ok"] else "FAIL", r["summary"]] for n, r in st["steps"].items()]
    return f"`{st['command']}`，{st['seconds']} s，{'全部通过' if st['ok'] else '有失败'}：\n\n" + table(rows, ["步", "规模", "秒", "结果", "关键数字"])


def shapes_counts():
    with open(os.path.join(PKG, "SHAPES.md"), encoding="utf-8") as f:
        text = f.read()
    secs = re.split(r"^## ", text, flags=re.M)[1:]
    rows = []
    for sec in secs:
        title = sec.splitlines()[0]
        if title[0] not in "12345":
            continue
        rows.append([title.split("（")[0], sec.count("**稿**") + sec.count("| 稿"), sec.count("待核"), sec.count("自定")])
    return table(rows, ["方言", "标「稿」", "标「待核」", "标「自定」"])


def build():
    g1, g2, g3, g4 = load("g1_dialects.json"), load("g2_chaos.json"), load("g3_scenarios.json"), load("g4_recon.json")
    cov, mut, kn, st = load("coverage.json"), load("mutation.json"), load("knives.json"), load("selftest_summary.json")
    text, _s, counts = T.render(mut)
    m = st["machine"]
    with open(os.path.join(HERE, "report_template.md"), encoding="utf-8") as f:
        tpl = f.read()
    fills = {
        "machine": f"{m['platform']}，Python {m['python']}，SQLite {m['sqlite_version']}，{m['cpu_count']} CPU，提交 `{m['git_commit']}`，{m['timestamp']}",
        "selftest": selftest_block(st), "g1": g1_block(g1),
        "g2": (f"用到全部 11 种注入的场景同种子跑两遍：注入事件 {g2['events_seed0_run1']} / {g2['events_seed0_run2']} 条，逐条相同 = {g2['identical']}"
               f"（请求数 {g2['requests']}）；换种子 99 → {g2['events_seed99']} 条，与种子 0 不同 = {g2['differs_with_other_seed']}；"
               f"事件日志里出现的注入种类 {len(g2['kinds_logged'])}/11（缺 {g2['kinds_missing'] or '无'}）；耗时 {g2['seconds']} s"),
        "g3": g3_block(g3),
        "g4": (f"合成 {g4['summary']['remote_orders']} 张服务端订单 / {g4['summary']['local_orders']} 条本地聚合后记录（含 10 张拆成两行的单），"
               f"埋 {g4['planted']} 条差异：找出 {g4['found']}，漏 {len(g4['missed'])}，误报 {len(g4['false_positives'])}；三类各 "
               f"{g4['summary']['missing_local']} / {g4['summary']['phantom']} / {g4['summary']['mismatch']}；本地时间戳整体 +8h 被识别为常数偏移 "
               f"{g4['ts_offset_ms']} ms、不算差异；耗时 {g4['seconds']} s"),
        "coverage": cov_block(cov), "mutation": mut_block(mut, text, counts), "knives": knives_block(kn), "shapes": shapes_counts(),
        "tests": str(cov["tests_run"]),
    }
    out = tpl
    for k, v in fills.items():
        out = out.replace("{{" + k + "}}", v)
    left = re.findall(r"\{\{\w+\}\}", out)
    if left:
        raise SystemExit(f"unfilled placeholders: {left}")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    text = build()
    if args.check:
        with open(OUT, encoding="utf-8") as f:
            same = f.read() == text
        print("REPORT.md up-to-date" if same else "REPORT.md stale")
        return 0 if same else 1
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
