"""由 qdperf/tools/report_template.md + qdperf/results/*.json 生成 qdperf/REPORT.md（报告里的数字全部取自结果文件）。

    python3.12 qdperf/tools/build_report.py            # 写 qdperf/REPORT.md
    python3.12 qdperf/tools/build_report.py --check    # 只比较，不一致退出码 1（测试里用，防止报告与结果文件脱节）

占位符：``{{文件:点路径|格式}}``，例如 ``{{g1_synth_verify.json:summary.synth_seconds|.1f}}``；格式缺省为原样，
``,`` 为千分位，``%`` 为百分比（×100 带一位小数），``ms`` 为秒转毫秒（两位小数），``len`` 为列表长度，
``list`` 为列表逐项加反引号，其余按 format() 规格。
``{{@名字}}`` 为生成的表格 / 段落（见 BLOCKS）；``{{?名字}}`` 为达成判定（见 CHECKS），由结果文件算出 ✅ / ❌。
"""
import argparse
import os
import re
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from qdperf import common              # noqa: E402

RESULTS = os.path.join(REPO, "qdperf", "results")
TEMPLATE = os.path.join(REPO, "qdperf", "tools", "report_template.md")
OUT = os.path.join(REPO, "qdperf", "REPORT.md")
_PH = re.compile(r"\{\{([^{}]+)\}\}")


def _load(name, cache):
    if name not in cache:
        cache[name] = common.read_json(os.path.join(RESULTS, name))
    return cache[name]


def _get(obj, path):
    for part in path.split("."):
        obj = obj[int(part)] if isinstance(obj, list) else obj[part]
    return obj


def _fmt(value, spec):
    if spec is None:
        return str(value)
    if value is None:
        return "-"
    if spec == ",":
        return f"{value:,}"
    if spec == "len":
        return str(len(value))
    if spec == "list":
        return "、".join(f"`{x}`" for x in value) or "无"
    if spec == "%":
        return f"{value * 100:.1f}%"
    if spec == "ms":
        return f"{value * 1000:.2f}"
    return format(value, spec)


def _machine_line(m):
    return (f"{m['platform']}，Python {m['python']}，{m['cpu_model']} × {m['cpu_count']}，SQLite {m['sqlite_version']}"
            f"，提交 {m.get('git_commit') or '-'}，{m['timestamp']}")


# ---- 生成的块 ------------------------------------------------------------------------------
def block_machines(cache):
    rows = ["| 结果文件 | 生成机器 / Python / SQLite / 提交 / 时间 |", "|---|---|"]
    for name, path in (("g1_synth_verify.json", "machine"), ("g1_doctor.json", "machine"), ("g2_profile_gate.json", "machine"),
                       ("g2_noise_probe.json", "machine"), ("g3_tick_jitter.json", "machine"), ("g4_http_load.json", "machine"),
                       ("selftest_summary.json", "machine")):
        rows.append(f"| `results/{name}` | {_machine_line(_get(_load(name, cache), path))} |")
    return "\n".join(rows)


def block_g1_top(cache):
    rep = _load("g1_doctor.json", cache)
    by = {s["id"]: s for s in rep["statements"]}
    rows = ["| # | 语句 | 进程 | 次/秒 | 判定 | 行/次（估） | 频次×行数（行/秒） | 实测 ms（best-of-3） | 占库 ms/秒 |",
            "|---:|---|---|---:|---|---:|---:|---:|---:|"]
    for k, sid in enumerate(rep["top"][:12]):
        s = by[sid]
        rows.append(f"| {k + 1} | `{sid}` | {s['process']} | {s['per_second']:g} | {s['verdict']} | {s['rows_est']:,.0f} | "
                    f"{s['cost']:,.0f} | {s['measured']['ms_best']:.3f} | {s['db_ms_per_second']:,.1f} |")
    return "\n".join(rows)


def block_g1_suggestions(cache):
    rep = _load("g1_doctor.json", cache)
    amp = rep["indexes"]["write_amplification"]["per_suggestion_extra_btree_writes_per_second"]
    rows = ["| 索引（列顺序 = 等值 → 范围 → 排序） | 服务的语句 | 建索引+ANALYZE 后不再扫描 | 估算 MB | 实测 MB | 构建 s | +B-tree 写/秒 |",
            "|---|---|---|---:|---:|---:|---:|"]
    for s in rep["indexes"]["suggestions"]:
        ok = sum(c["ok"] for c in s["check_analyzed"].values())
        rows.append(f"| `{s['sql'][:-1]}` | {', '.join(s['serves'])} | {ok}/{len(s['check_analyzed'])} | "
                    f"{s['size_est_bytes'] / 1e6:.2f} | {s['size_bytes'] / 1e6:.2f} | {s['build_seconds']:.2f} | {amp[s['name']]:g} |")
    return "\n".join(rows)


def block_g1_contention(cache):
    rep = _load("g1_doctor.json", cache)
    rows = ["| 场景 | 写者 | 实际次/秒 | 提交 | 等待 p50 ms | 等待 p99 ms | 最大 ms | SQLITE_BUSY | 持锁 p99 ms |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for sc in rep["contention"]["scenarios"]:
        for w in sc["writers"]:
            rows.append(f"| {sc['name']} | {w['name']} | {w['achieved_rate']:.1f} | {w['committed']} | {w['wait_ms']['p50']:.3f} | "
                        f"{w['wait_ms']['p99']:.3f} | {w['wait_ms']['max']:.3f} | {w['busy']} | {w['hold_ms']['p99']:.3f} |")
    rows.append("")
    rows += [f"- **{sc['name']}**：{sc['starvation']['text']}" + ("；根因：" + "；".join(sc["root_cause"]) if sc.get("root_cause")
                                                                  else "") for sc in rep["contention"]["scenarios"]]
    return "\n".join(rows)


def block_g1_wal(cache):
    rep = _load("g1_doctor.json", cache)
    rows = ["| autocheckpoint（页） | 长读事务 | WAL 文件峰值 MB | 有效帧峰值 | 重置次数 | 提交 p99 ms | 有效帧随时间（每 0.1 s） |",
            "|---:|---|---:|---:|---:|---:|---|"]
    for w in rep["wal"]["scenarios"]:
        rows.append(f"| {w['config']['autocheckpoint']} | {'有' if w['config']['long_reader'] else '无'} | {w['max_bytes'] / 1e6:.1f} | "
                    f"{w['max_live_frames']} | {w['resets']} | {w['commit_ms']['p99']:.2f} | "
                    f"`{common.sparkline([c[2] for c in w['curve']], 40)}` |")
    from qdperf import sqlite_doctor
    rows += ["", sqlite_doctor.wal_conclusion(rep["wal"])]
    return "\n".join(rows)


def block_g2_slow(cache):
    s = _load("g2_profile_gate.json", cache)["summary"]
    rows = ["| 档位 | 无意义循环空转时长 | 实测热墙钟变化（3 遍中位数） | 闸（缺省容差）stdout 首行 | 闸（容差只取 2×噪声） | 新进前 10 的函数 |",
            "|---|---:|---:|---|---|---|"]
    raw = _load("g2_profile_gate.json", cache)["slow"]
    for k, v in s["slow"].items():
        rows.append(f"| 放慢 {v['intended'] * 100:.0f}% | {raw[k]['pct']:g} × 本次调用时长 | {v['achieved_hot'] * 100:+.1f}% | "
                    f"`{v['first_line']}` | `{v['first_line_noise_only']}` | {', '.join(f'`{n}`' for n in v['new_top10']) or '无'} |")
    return "\n".join(rows)


def block_g2_noise(cache):
    b = _load("g2_profile_gate.json", cache)["baseline"]["noise"]["per_run"]
    rows = ["| 基线第几遍 | 热墙钟 s（5 进程最快） | 冷墙钟 s（5 进程最快） | tracemalloc 峰值字节 |", "|---:|---:|---:|---:|"]
    for k, r in enumerate(b):
        rows.append(f"| {k + 1} | {r['hot_wall_s']:.4f} | {r['cold_wall_s']:.4f} | {r['peak_bytes']:,} |")
    return "\n".join(rows)


def block_g3_table(cache):
    from qdperf import tick_jitter
    return "```\n" + tick_jitter.knee_table(_load("g3_tick_jitter.json", cache)) + "\n```"


def block_g3_mitigations(cache):
    res = _load("g3_tick_jitter.json", cache)
    rows = ["| 缓解 | tick 间隔 p50 | p99 | 最大 | 超过 2T 的 tick | 面板实际轮询 / 秒 | 面板查询 p50 ms | 有效（p99 ≤ 2T） |",
            "|---|---:|---:|---:|---:|---:|---:|---|"]
    for r in res["mitigations"]:
        it, p = r["interval_T"], r.get("panel", {})
        rows.append(f"| {r['config']['mitigation']} | {it['p50']:.2f}T | {it['p99']:.2f}T | {it['max']:.2f}T | {r['over_2T']}/{r['ticks'] - 1} | "
                    f"{p.get('achieved_q', 0):.1f} | {p.get('query_ms', {}).get('p50') or 0:.0f} | {'是' if not r['starved'] else '否'} |")
    return "\n".join(rows)


def block_g3_gil(cache):
    res = _load("g3_tick_jitter.json", cache)
    rows = ["| 算段 | 引擎进程里的忙线程 | 算段耗时 p50 ms | 算段 p99 ms | tick 间隔 p99 |", "|---|---:|---:|---:|---:|"]
    for r in res["gil"]:
        seg = r["segments_ms"]["compute"]
        rows.append(f"| {r['config']['compute']} | {r['config']['bg_threads']} | {seg['p50']:.1f} | {seg['p99']:.1f} | "
                    f"{r['interval_T']['p99']:.2f}T |")
    return "\n".join(rows)


def block_g4_knee(cache):
    k = _load("g4_http_load.json", cache)["knee"]
    rows = ["| 并发 C | 成功/秒 | p50 ms | p95 ms | p99 ms | 错误率 |", "|---:|---:|---:|---:|---:|---:|"]
    for s in k["steps"]:
        t = s["total"]
        rows.append(f"| {s['c']} | {t['ok_rps']:.1f} | {t['latency_ms']['p50']:.1f} | {t['latency_ms']['p95']:.1f} | "
                    f"{t['latency_ms']['p99']:.1f} | {t['error_rate'] * 100:.2f}% |")
    return "\n".join(rows)


def block_g4_ab(cache):
    res = _load("g4_http_load.json", cache)
    rows = ["| 对比 | 路径 | 分位 | A（两遍均值）ms | B ms | 变化 | 容差 | 判定 |", "|---|---|---|---:|---:|---:|---:|---|"]
    for key, label in (("ab_slow", "A vs B（B 慢 30%）"), ("ab_same", "A vs A'（同配置）")):
        for r in res[key]["rows"]:
            rows.append(f"| {label} | {r['target']} | {r['q']} | {r['a']:.2f} | {r['b']:.2f} | {r['change'] * 100:+.1f}% | "
                        f"{r['tolerance'] * 100:.1f}% | {'红' if r['red'] else '绿'} |")
    return "\n".join(rows)


def block_coverage(cache):
    cov = _load("coverage.json", cache)
    rows = ["| 文件 | 语句 | 未覆盖 | 覆盖率 |", "|---|---:|---:|---:|"]
    for rel, r in sorted(cov["library"]["files"].items()):
        rows.append(f"| `{rel}` | {r['statements']} | {r['statements'] - r['covered']} | {r['percent']:.1f}% |")
    lib = cov["library"]
    rows.append(f"| **合计（计入判据）** | {lib['total_statements']} | {lib['total_statements'] - lib['total_covered']} | "
                f"**{lib['total_percent']:.2f}%** |")
    return "\n".join(rows)


def block_mutation(cache):
    """最终（补判据 / 改库后重跑）逐模块结果，旁边列第 3 轮首跑（抓住须确认、跑的过程中不改文件）的抓住率作对照。"""
    mut, r3 = _load("mutation.json", cache), _load("mutation_round3.json", cache)
    rows = ["| 模块 | 候选 | 突变体 | 第 3 轮首跑抓住率 | 最终抓住 | 超时 | 最终漏网（均已判定） | 最终抓住率 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|"]
    cands = mut["meta"].get("candidates", {})
    resampled = {m for e in mut["meta"].get("reruns", []) if "--module" in e["command"]
                 for m in e["command"].split("--module ")[1].split(" --")[0].split()}
    for name, m in sorted(mut["summary"]["modules"].items()):
        first = r3["summary"]["modules"][name]["kill_rate"]
        rows.append(f"| `{name}.py`{'（改库后重抽样）' if name in resampled else ''} | {cands.get(name, '')} | {m['total']} | "
                    f"{first:.1f}% | {m['killed']} | {m['timeout']} | {m['survived']} | {m['kill_rate']:.1f}% |")
    s = mut["summary"]
    timeouts = sum(m["timeout"] for m in s["modules"].values())
    rows.append(f"| **合计** | | {s['total']} | {r3['summary']['kill_rate']:.1f}% | {s['caught'] - timeouts} | {timeouts} | "
                f"{s['survived']} | **{s['kill_rate']:.1f}%** |")
    return "\n".join(rows)


def block_g3_panel_T(cache):
    """行内：面板长查询单线程实测耗时折成 T（标定目标与是否封顶也照写）。"""
    d = _load("g3_tick_jitter.json", cache)
    cal, t = d["panel_calibration"], d["summary"]["T"]
    capped = "，行数已封顶为全表" if cal["capped"] else ""
    return f"{cal['seconds'] / t:.1f}T（标定目标 {cal['target_seconds'] / t:.1f}T{capped}）"


def block_g3_knee_cfg(cache):
    k = _sum("g3_tick_jitter.json", cache)["knee"]
    return f"WAL {'开' if k['wal'] else '关'}、busy {k['busy']:g} s、{k['compute']}"


def block_g2_slow5_note(cache):
    s = _sum("g2_profile_gate.json", cache)
    tol, verdict = s["tolerance_hot_default"], s["slow"]["5pct"]["verdict_default"]
    rule = "放慢 5% 落在容差内，按设计应判绿" if tol > 0.05 else "放慢 5% 超出容差，按设计应判红"
    return f"容差 {tol * 100:.1f}% {'>' if tol > 0.05 else '≤'} 5% → {rule}；实际 {verdict}"


def block_g2_noise_probe(cache):
    """§2.3：进程快慢与两种 best-of-N 的三遍基线相对极差（结论句按实测数字写，不预设）。"""
    d = _load("g2_noise_probe.json", cache)
    s, n = d["summary"], d["n"]
    pct = lambda x: f"{x * 100:.1f}%"                                  # noqa: E731
    text = (f"同一段纯 Python 空转（`qdperf/tools/noise_probe.py`，{d['loops']:,} 次循环）在 {d['procs']} 个全新进程里"
            f"各自取最快，折合 {s['per_process_ns_min']:.1f}–{s['per_process_ns_max']:.1f} ns/次（进程之间相差 "
            f"{pct(s['per_process_spread'])}）：每个进程有自己的“快慢”，一个进程内的 N 个样本跳不出它。三遍基线的相对极差"
            f"（{d['trials']} 次重复的中位数 / 最大）：进程内 best-of-{n} 为 {pct(s['inproc_spread_median'])} / "
            f"{pct(s['inproc_spread_max'])}，跨进程 best-of-{n}（每遍 {n} 个全新进程各测一次取最快）为 "
            f"{pct(s['cross_spread_median'])} / {pct(s['cross_spread_max'])}。")
    if s["cross_more_stable"]:
        text += "所以回归闸缺省按进程取样；"
    else:
        text += "这次测量里跨进程取样并没有更稳（机器安静时会这样），回归闸仍按进程取样，以免被单个进程的快慢带偏；"
    return text + f"`pair` 模式（基线与本次交替测）再把分钟级的慢漂移同时落到两边。复现：`{d['command']}`。"


def block_g2_per_round(cache):
    lines = _sum("g2_profile_gate.json", cache)["slow"]["12pct"]["per_round_single"]
    red = sum(1 for x in lines if x.startswith("REGRESSION"))
    return f"{red}/{len(lines)} 轮判红（" + "；".join(f"`{x}`" for x in lines) + "）"


def block_mutation_first(cache):
    """行内：第 3 轮首跑（补判据之前）的抓住率与漏网数。"""
    r3 = _load("mutation_round3.json", cache)["summary"]
    return (f"第 3 轮首跑 {r3['kill_rate']:.1f}%（{r3['caught']}/{r3['total']}，抓住须确认），{r3['survived']} 条漏网逐条判定、"
            f"补判据并修 3 个真问题后重跑 → ")


BLOCKS = {"machines": block_machines, "g1_top": block_g1_top, "g1_suggestions": block_g1_suggestions,
          "g1_contention": block_g1_contention, "g1_wal": block_g1_wal, "g2_slow": block_g2_slow, "g2_noise": block_g2_noise,
          "g3_table": block_g3_table, "g3_mitigations": block_g3_mitigations, "g3_gil": block_g3_gil,
          "g4_knee": block_g4_knee, "g4_ab": block_g4_ab, "coverage": block_coverage, "mutation": block_mutation,
          "mutation_first": block_mutation_first, "g3_panel_T": block_g3_panel_T, "g3_knee_cfg": block_g3_knee_cfg,
          "g2_slow5_note": block_g2_slow5_note, "g2_noise_probe": block_g2_noise_probe,
          "g2_per_round": block_g2_per_round}


# ---- 达成判定：表 1 的“达成”列由结果文件算出（不手写 ✅），判据与需求原文一一对应 -----------------------------
def _sum(name, cache):
    return _load(name, cache)["summary"]


def _step(cache, name):
    return next(r for r in _load("selftest_summary.json", cache)["steps"] if r["step"] == name)


def _g1_rows(c):
    from qdperf import synth_db
    sv = _load("g1_synth_verify.json", c)["synth"]
    return (sv["scale"] == 1.0 and sv["rows"] == synth_db.FULL_ROWS and sv["seconds"] <= 180
            and sv["determinism"]["identical_fingerprint"])


def _g1_file(c):
    f = _load("g1_doctor.json", c)["file"]
    return (f["pragmas"]["page_size"] > 0 and f["fragmentation"] is not None and f["no_primary_key"] == ["settings"]
            and len(f["fk_without_index"]) > 0 and any(x["object"] == "idx_orders_user" for x in f["stats_stale"]))


def _g2_names(c):
    s = _sum("g2_profile_gate.json", c)
    return s["names_have_no_lineno_or_tmp"] and s["names_identical_rerun_top10"] and s["names_moved_copy_top10_equal"]


def _g2_tol(c):
    s = _sum("g2_profile_gate.json", c)
    return abs(s["tolerance_hot_default"] - max(0.10, 2 * s["noise"]["hot_wall_rel"])) < 1e-9 and s["runs"] >= 3


def _g3_mitig(c):
    m = _sum("g3_tick_jitter.json", c)["mitigations"]
    good = [k for k, v in m.items() if v["effective"] and v["improvement_x"] > 1.0]
    return len(good) >= 2


def _mutation_ok(c):
    """≥ 85%、每模块 ≥ 40 个，并且每条最终漏网都在 mutants/triage.md 里有判定（按 id 查）。"""
    mut = _load("mutation.json", c)
    s = mut["summary"]
    with open(os.path.join(REPO, "qdperf", "mutants", "triage.md"), encoding="utf-8") as f:
        triage = f.read()
    judged = all(f"`{r['id']}`" in triage for r in mut["mutants"] if r["status"] == "survived")
    return s["kill_rate"] >= 85.0 and all(m["total"] >= 40 for m in s["modules"].values()) and judged


CHECKS = {
    "g1_synth": _g1_rows,
    "g1_plans": lambda c: _sum("g1_synth_verify.json", c)["statements"] == len(_load("g1_doctor.json", c)["statements"]) > 0
    and len(_load("g1_doctor.json", c)["top"]) == min(20, _sum("g1_synth_verify.json", c)["statements"]),
    "g1_index": lambda c: (_sum("g1_synth_verify.json", c)["suggestions"] > 0
                           and _sum("g1_synth_verify.json", c)["suggestions_verified_independently"]
                           and set(_sum("g1_synth_verify.json", c)["still_scanning_after_apply"])
                           <= set(_sum("g1_synth_verify.json", c)["unsolvable"])),
    "g1_contention": lambda c: (_sum("g1_synth_verify.json", c)["contention_3x50"]["p99_ms"] <= 200.0
                                or bool(_sum("g1_synth_verify.json", c)["contention_3x50"]["root_cause"]))
    and any("BUSY_SNAPSHOT" in x for x in _sum("g1_synth_verify.json", c)["deferred_demo_root_cause"]),
    "g1_wal": lambda c: _sum("g1_synth_verify.json", c)["wal_free_capped"] and _sum("g1_synth_verify.json", c)[
        "wal_long_reader_never_reset"],
    "g1_file": _g1_file,
    "g1_untouched": lambda c: _sum("g1_synth_verify.json", c)["original_untouched"] is True,
    "g2_measure": lambda c: _sum("g2_profile_gate.json", c)["repeat"] >= 5 and len(
        _load("g2_profile_gate.json", c)["baseline"]["profile"]["top"]) == 30,
    "g2_gate": _g2_tol,
    "g2_slow": lambda c: (_sum("g2_profile_gate.json", c)["slow12_red"]
                          and _sum("g2_profile_gate.json", c)["slow"]["5pct"]["verdict_default"]
                          == _sum("g2_profile_gate.json", c)["slow5_expected_by_noise"]
                          and _sum("g2_profile_gate.json", c)["rerun_verdict"] == "OK"),
    "g2_names": _g2_names,
    "g3_scan": lambda c: _sum("g3_tick_jitter.json", c)["configs"] >= 32 and not _load("g3_tick_jitter.json", c)["quick"],
    "g3_q0": lambda c: _sum("g3_tick_jitter.json", c)["q0_p99_T_max"] <= 1.2,
    "g3_knee": lambda c: _sum("g3_tick_jitter.json", c)["knee_found"] and _sum("g3_tick_jitter.json", c)["knee"][
        "knee_p99_T"] > 2.0,
    "g3_mitig": _g3_mitig,
    "g4_p99": lambda c: 108.0 <= _sum("g4_http_load.json", c)["p99_measured_ms"] <= 132.0,
    "g4_knee": lambda c: 6 <= (_sum("g4_http_load.json", c)["knee_measured"] or 0) <= 10,
    "g4_ab": lambda c: _sum("g4_http_load.json", c)["ab_slow_verdict"] == "REGRESSION"
    and _sum("g4_http_load.json", c)["ab_same_verdict"] == "OK",
    "g5_selftest": lambda c: _load("selftest_summary.json", c)["ok"] and len(_load("selftest_summary.json", c)["steps"]) == 8,
    "g5_coverage": lambda c: _load("coverage.json", c)["library"]["total_percent"] >= 90.0
    and _load("coverage.json", c)["tests_failed"] == 0,
    "g5_mutation": _mutation_ok,
    "g5_constraints": lambda c: _step(c, "constraints")["ok"]
    and set(_step(c, "constraints")["detail"].values()) == {"ok"},
}


def render(template_text):
    cache = {}

    def sub(m):
        key = m.group(1).strip()
        if key.startswith("@"):
            return BLOCKS[key[1:]](cache)
        if key.startswith("?"):
            return "✅" if CHECKS[key[1:]](cache) else "❌"
        ref, _, spec = key.partition("|")
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
        print("REPORT.md up to date" if same else "REPORT.md differs from results/*.json — run build_report.py")
        return 0 if same else 1
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
