"""生成 qdperf/mutants/triage.md（突变漏网逐条判定）。数字全部从入库的结果文件算，判定与理由来自
qdperf/mutants/triage_reasons.py（人写的部分）：

    python3.12 qdperf/mutants/triage.py            # 写 qdperf/mutants/triage.md
    python3.12 qdperf/mutants/triage.py --check    # 只比较，不一致退出码 1

结果文件：results/mutation_round1.json、mutation_round2_partial.json、mutation_round3.json（第 3 轮首跑）、
mutation_resample1.json / mutation_resample2.json（改库重抽样后的两个中间快照）、mutation.json（最终）。

突变体 id 里的节点序号依赖那一轮所用的源码版本；改了库代码的模块重抽样后，“同一处突变”按 算子 + 前后文本 +
所在源码行 认，行文本用 git 取那一轮的源码（eb20721 / 1a03d29 / ad5d3e9 / d63f970，浅克隆先 git fetch --unshallow）。
每张判定表里的每一条都必须在 triage.md 里出现（或与表 A 的某行是同一处突变），每条漏网都必须有判定，否则报错退出码 2。
"""
import argparse
import collections
import json
import os
import re
import subprocess
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from qdperf.mutants import triage_reasons as TR      # noqa: E402

RESULTS = os.path.join(REPO, "qdperf", "results")
OUT = os.path.join(REPO, "qdperf", "mutants", "triage.md")
EQ, FIX = TR.EQ, TR.FIX
SRC3, SRC_R1 = "eb20721", "1a03d29"                 # 第 3 轮 / 第一次整模块重抽样所用源码
TABLES = ("ROUND1", "ROUND2", "ROUND3", "RESAMPLE1", "RESAMPLE2", "RESAMPLE3")
EARLY = ("ROUND1", "ROUND2", "ROUND3", "RESAMPLE1")    # 第 3 轮与第一次重抽样快照里的记录按 id 查这几张表
# 每次重抽样的样本：(标题, 结果文件, 模块, 源码提交, 判定表, 是否已被下一次重抽样替换)
SAMPLES = [
    ("fake_backend 第 1 次重抽样（修 (b) 之后，`--module fake_backend sqlite_doctor`）", "mutation_resample1.json",
     "fake_backend", SRC_R1, "RESAMPLE1", True),
    ("sqlite_doctor 重抽样（修 (a) 之后，同一次 `--module fake_backend sqlite_doctor`）", "mutation.json",
     "sqlite_doctor", SRC_R1, "RESAMPLE1", False),
    ("fake_backend 第 2 次重抽样（修 (c) 之后）", "mutation_resample2.json", "fake_backend", "ad5d3e9", "RESAMPLE2", True),
    ("fake_backend 第 3 次重抽样（修 (d) 之后）", "mutation.json", "fake_backend", "d63f970", "RESAMPLE3", False),
]
COMMITS = sorted({SRC3} | {s[3] for s in SAMPLES})       # 生成时要从 git 历史里取源码的提交
# 第 1 轮里“抓住”一个突变体的测试若不在这里，就是与被突变模块无关的测试（并行负载下的抖动）
OWN = {"common": None, "synth_db": None, "sqlplan": {"test_sqlplan", "test_doctor", "test_load", "test_index_advisor"},
       "index_advisor": {"test_doctor", "test_index_advisor"}, "sqlite_doctor": {"test_doctor", "test_load", "test_contract"},
       "sqlite_load": {"test_load", "test_doctor"}, "profile_gate": {"test_profile_gate", "test_contract"},
       "tick_jitter": {"test_tick_jitter", "test_contract"}, "http_load": {"test_http_load", "test_contract"},
       "fake_backend": {"test_http_load"}}
HDR = "| id | 行 | 突变 | 判定 | 理由 | {} |\n|---|---|---|---|---|---|"


class TriageError(Exception):
    pass


def first(r):
    return r.get("first_run", r["status"])


def caught(status):
    return status in ("killed", "timeout")


def same_mutation(a, b):
    return (a["op"], a["before"], a["after"]) == (b["op"], b["before"], b["after"])


def doubt_kind(r):
    """第 1 轮的一个“抓住”为什么不可信：length（函数长度判据）/ quote（按引号匹配源码文本）/ flaky（无关测试）。"""
    m = re.match(r"^(?:FAIL|ERROR): (\S+) \(qdperf\.tests\.(\w+)\.", r["detail"])
    mod, name = (m.group(2), m.group(1)) if m else (None, None)
    if name == "test_file_and_function_length":
        return "length"
    if name in ("test_static", "test_enumerations_documented"):
        return "quote"
    own = OWN[r["module"]]
    if mod and mod != "test_constraints" and own is not None and mod not in own:
        return "flaky"
    return None


def status_text(f):
    if f["status"] == "survived":
        return "漏网"
    if f["status"] == "timeout":
        return "超时"
    return "抓住：" + f["detail"].split(" (")[0].replace("|", "/")


def row(r, v, why, status):
    def flat(x):                                              # 多行的 if 块压成一行，表格不断
        return " ".join(x.split()).replace("|", "\\|")[:60]
    return f"| `{r['id']}` | {r['line']} | `{flat(r['before'])}` → `{flat(r['after'])}` | **{v}** | {why} | {status} |"


def short_command(c):
    if " --ids " not in c:
        return c
    head, rest = c.split(" --ids ", 1)
    ids, _, tail = rest.partition(" --")
    return f"{head} --ids <{len(ids.split())} 个 id>" + (f" --{tail}" if tail else "")


def tables():
    """六张判定表；同一个 id 不得出现在两张表里（id 的含义随源码版本变，重复了就分不清是哪一轮的判定）。"""
    seen = {}
    for name in TABLES:
        for k in getattr(TR, name):
            if k in seen:
                raise TriageError(f"id {k} 同时出现在 {seen[k]} 与 {name}")
            seen[k] = name
    return {name: getattr(TR, name) for name in TABLES}


class Triage:
    def __init__(self, results=RESULTS, repo=REPO):
        self.repo = repo

        def load(name):
            with open(os.path.join(results, name), encoding="utf-8") as f:
                return json.load(f)
        self.r1, self.r2, self.r3 = load("mutation_round1.json"), load("mutation_round2_partial.json"), load("mutation_round3.json")
        self.files = {n: load(n) for n in ("mutation.json", "mutation_resample1.json", "mutation_resample2.json")}
        self.fin = self.files["mutation.json"]
        self.t = tables()
        self.early = {k: v for name in EARLY for k, v in self.t[name].items()}
        self.used = collections.defaultdict(set)
        self._src = {}
        self.resampled = sorted({m for e in self.fin["meta"].get("reruns", []) if "--module" in e["command"]
                                 for m in e["command"].split("--module ")[1].split(" --")[0].split()})
        self.recf = {r["id"]: r for r in self.fin["mutants"]}
        self.rec3 = {r["id"]: r for r in self.r3["mutants"]}
        self.sigreason = {}
        for recs, commit in ((self.r3["mutants"], SRC3), (self.files["mutation_resample1.json"]["mutants"], SRC_R1)):
            for r in recs:
                if r["id"] in self.early and r["status"] == "survived":
                    self.sigreason.setdefault(self.sig(r, commit), (self.early[r["id"]], r["id"]))
        self.latest = {mod: (table, commit) for _t, _f, mod, commit, table, old in SAMPLES if not old}
        self.by_sig = collections.defaultdict(list)
        for r in self.fin["mutants"]:
            if r["module"] in self.latest:
                self.by_sig[self.sig(r, self.latest[r["module"]][1])].append(r)
        self.surv3 = sorted((r for r in self.r3["mutants"] if r["status"] == "survived"),
                            key=lambda r: (r["module"], r["line"], r["id"]))
        self.sig3 = {self.sig(o, SRC3) for o in self.surv3}
        self.survf = sorted((r for r in self.fin["mutants"] if r["status"] == "survived"),
                            key=lambda r: (r["module"], r["line"], r["id"]))

    # ---- 查判定 ------------------------------------------------------------------------
    def src_line(self, commit, module, line):
        if (commit, module) not in self._src:
            proc = subprocess.run(["git", "-C", self.repo, "show", f"{commit}:qdperf/{module}.py"], capture_output=True, text=True)
            if proc.returncode != 0:
                raise TriageError(f"git 里取不到 {commit}:qdperf/{module}.py（需要完整历史：git fetch --unshallow）")
            self._src[(commit, module)] = proc.stdout.splitlines()
        return self._src[(commit, module)][line - 1].strip()

    def sig(self, r, commit):
        """“同一处突变”：算子 + 前后文本 + 所在源码行（按那一轮所用的源码版本取行文本）。"""
        return r["module"], r["op"], r["before"], r["after"], self.src_line(commit, r["module"], r["line"])

    def verdict(self, table, rid):
        if rid not in self.t[table]:
            raise TriageError(f"{rid} 在 {table} 里没有判定")
        self.used[table].add(rid)
        return self.t[table][rid]

    def early_verdict(self, r):
        """第 3 轮的漏网：本轮新判的在 ROUND3；与第 1 / 2 轮同 id 的沿用那一轮的判定，但必须是同一处突变。"""
        for name, recs in (("ROUND3", None), ("ROUND1", self.r1["mutants"]), ("ROUND2", self.r2["mutants"])):
            if r["id"] in self.t[name]:
                old = next((o for o in recs if o["id"] == r["id"]), None) if recs else r
                if old is None or not same_mutation(old, r):
                    raise TriageError(f"{r['id']}：{name} 里同编号的不是同一处突变")
                return self.verdict(name, r["id"])
        raise TriageError(f"第 3 轮漏网未判定：{r['id']}")

    def sample_reason(self, r, table, commit):
        """重抽样样本里的一条：先按本样本的 id 查，再按“同一处突变”对回第 3 轮 / 第一次重抽样的判定。"""
        if r["id"] in self.t[table]:
            return self.verdict(table, r["id"])
        hit = self.sigreason.get(self.sig(r, commit))
        if hit is None:
            raise TriageError(f"重抽样漏网未判定：{r['id']}（{table}）")
        (v, why), old_id = hit
        return v, why + f"（与 `{old_id}` 是同一处突变，改库后重抽样换了编号）"

    def final_reason(self, r):
        """最终漏网的判定：未改库的模块按 id；改库重抽样的模块按它最后一次样本的判定表 / “同一处突变”。"""
        if r["module"] not in self.resampled:
            return self.early_verdict(r)
        table, commit = self.latest[r["module"]]
        return self.sample_reason(r, table, commit)

    def final_status(self, r):
        """第 3 轮的一条在最终结果里的状态；重抽样模块按“同一处突变”找对应的新突变体。"""
        f = self.recf.get(r["id"])
        prefix = ""
        if r["module"] in self.resampled:
            cands = self.by_sig.get(self.sig(r, SRC3), [])
            if not cands:
                return "模块改库后重抽样，这一处没再抽中（或该行已改写）"
            f = min(cands, key=lambda c: abs(c["line"] - r["line"]))
            prefix = f"（重抽样后为 `{f['id']}`）"
        elif f is None or not same_mutation(f, r):
            raise TriageError(f"{r['id']}：未改库的模块在最终结果里找不到同一处突变")
        return prefix + status_text(f)

    def round3_status(self, r):
        """附录里第 1 / 2 轮的一条在第 3 轮的状态（同编号且同一处突变才算同一个）。"""
        s = self.rec3.get(r["id"])
        if s is None:
            return "不在第 3 轮样本里（源码已改）"
        if not same_mutation(s, r):
            return "第 3 轮同编号是另一处突变（源码已改）"
        return "漏网" if s["status"] == "survived" else "抓住"

    # ---- 各节 --------------------------------------------------------------------------
    def counts(self):
        r1 = self.r1["mutants"]
        c = {"n1": len(r1), "c1": sum(1 for r in r1 if caught(first(r))), "n2": len(self.r2["mutants"])}
        c["doubt"] = collections.Counter(doubt_kind(r) for r in r1 if first(r) == "killed" and "first_run" not in r)
        c["doubt"].pop(None, None)
        c["quote2"] = sum(1 for r in self.r2["mutants"] if r["status"] == "killed" and doubt_kind(r) == "quote")
        v3 = [self.early_verdict(r)[0] for r in self.surv3]
        c["eq3"], c["fix3"] = v3.count(EQ), v3.count(FIX)
        vf = [self.final_reason(r)[0] for r in self.survf]
        c["eqf"], c["fixf"] = vf.count(EQ), vf.count(FIX)
        c["unconf3"] = [r for r in self.r3["mutants"] if r.get("unconfirmed")]
        return c

    def intro(self, c):
        s3, sf, d = self.r3["summary"], self.fin["summary"], c["doubt"]
        w = []
        w.append("# qdperf 突变漏网逐条判定\n")
        w.append("本文件由 `python3.12 qdperf/mutants/triage.py` 从 `qdperf/results/mutation*.json` 与 "
                 "`qdperf/mutants/triage_reasons.py`（人写的逐条判定）生成；`--check` 只比较。\n")
        w.append("判据：突变抓住率 ≥ 85%，每模块 ≥ 40 个突变体（这里每模块 50 个、seed 0，10 个库模块共 500 个），活下来的逐条判“等价”或补判据。"
                 "跑法与口径见 `qdperf/mutants/run.py` 文档串。\n")
        w.append(f"**结论：第 3 轮（抓住须确认、跑的过程中不改任何文件）首跑抓住率 {s3['kill_rate']:.1f}%（{s3['caught']}/{s3['total']}），"
                 f"漏网 {s3['survived']} 条，逐条判定为等价 {c['eq3']}、补判据 {c['fix3']}；补判据并修掉追查中发现的 3 个真问题后重跑，"
                 f"最终 {sf['kill_rate']:.1f}%（{sf['caught']}/{sf['total']}，`results/mutation.json`；另 1 个真问题由自检的临时目录检查发现，"
                 f"见经过第 7 条），剩下的 {sf['survived']} 条漏网"
                 + ("全部判为等价" if not c["fixf"] else f"中等价 {c['eqf']}、仍待补判据 {c['fixf']}") + "（见表 A / A2）。**\n")
        w.append("## 经过\n")
        w.append(f"1. **第 1 轮**（`results/mutation_round1.json`）：{c['n1']} 个中“抓住” {c['c1']}（{100.0 * c['c1'] / c['n1']:.1f}%）。"
                 f"事后查明其中 {sum(d.values())} 个不可信：{d['length']} 个死在“函数 ≤ 60 行”判据上（我在跑到一半时改长了 "
                 "`qdperf/mutants/run.py:main`，之后开跑的突变体复制到的都是超长版本）、"
                 f"{d['quote']} 个死在按源码文本、按双引号匹配的判据上（突变器写回的是 `ast.unparse` 的单引号输出）、{d['flaky']} 个死在与被突变模块"
                 "毫无关系的测试上（4 个突变体并行、机器满载时的偶发失败）。整轮作废；它的 40 条漏网已逐条补判据 / 判等价（附录 C）。")
        w.append("2. **修正一**：三个负载敏感的测试改为不依赖负载的推断；跑器加“抓住须确认”（报失败的测试在同一突变体上单独重跑仍失败才算，"
                 "否则记 `unconfirmed`、整套再跑）；跑的过程中不再改仓库里任何文件。")
        w.append(f"3. **第 2 轮**（`results/mutation_round2_partial.json`）：跑到 {c['n2']}/500 时因后台时限中断；暴露了 {c['quote2']} 个"
                 "仍按引号文本判死的误杀，以及第 1 轮被误杀遮住的一批漏网（附录 B）。**修正二**：源码文本判据改成与排版无关"
                 "（AST 字符串常量 / `ast.unparse` 规范化文本）；修掉 `sqlite_load` 的 tempfile 目录泄漏（测试与突变跑了几千次后 /tmp 堆了 1.5 GB）。")
        w.append(f"4. **第 3 轮**（`results/mutation_round3.json`，`{self.r3['meta']['command']}`）：同 seed、500 个突变体一次跑完，"
                 f"耗时 {self.r3['meta'].get('elapsed_seconds', 0) / 60:.0f} 分钟，首跑抓住率 **{s3['kill_rate']:.1f}%**，漏网 {s3['survived']}；"
                 f"未确认的抖动失败 {len(c['unconf3'])} 次（均未算抓住，列在文末）。")
        w.append(f"5. **逐条判定这 {s3['survived']} 条**：等价 {c['eq3']}、补判据 {c['fix3']}（表 A）。追查漏网时发现并修了 3 个真问题："
                 "(a) 体检工具遇到写错的 SQL 会跑到一半抛 Traceback（违反“输入错误退出码 2”的契约）→ 先在原库只读连接上把每条语句 EXPLAIN 一遍；"
                 "(b) `fake_backend.start()` 起不来时异常里没有原因（原因打在子进程 stdout、却被丢进 DEVNULL）→ 收集子进程输出；"
                 "(c) `fake_backend` 补齐响应体时差 1～8 字节会补出负数个 x、体反而比 `body_bytes` 长（缺省 64 字节时 POST 的第 1000 个请求起就会），"
                 "`body_bytes` / `schedule` 也不校验 → 差不到 9 字节就不补、两项都校验。")
        w.append("6. **重跑**：" + "；".join(f"`{short_command(e['command'])}`（{e['mutants']} 个，{e.get('elapsed_seconds', 0) / 60:.0f} 分钟）"
                                       for e in self.fin["meta"].get("reruns", [])) +
                 "。改了库代码的模块（fake_backend 改了三次、sqlite_doctor 一次）源码变了、突变体按新源码重新抽样，所以整模块重跑，"
                 "每次重抽样的首跑漏网逐条判定（表 A2；与第 3 轮同一处突变——算子 + 前后文本 + 所在源码行——的对回表 A 的判定），"
                 "补了判据的再按 id 重跑；其余模块只按 id 重跑第 3 轮的漏网（记 `first_run`）。")
        w.append("7. **第 4 个真问题（自检发现，不是突变）**：`selftest --full` 跑完 /tmp 里留下 4 个 `qdperf-backend-*` 目录——"
                 "(d) `fake_backend.start()` 起不来时抛错前没删自己的 scratch 目录（两个“起不来”测试 × unittest / coverage 两步）。"
                 "修掉后两个测试都断言目录已删；selftest 的 unittest 步加了“测试跑完 tempfile 下不许多出 `qdperf-*` 目录”的检查；fake_backend 第 3 次重抽样。\n")
        return w

    def module_table(self, c):
        s3, sf = self.r3["summary"], self.fin["summary"]
        w = ["| 模块 | 突变体 | 第 1 轮“抓住”（作废） | 第 3 轮首跑抓住 | 第 3 轮首跑抓住率 | 最终抓住（含超时） | 最终漏网 | 最终抓住率 |\n"
             "|---|---:|---:|---:|---:|---:|---:|---:|"]
        m1 = collections.Counter(r["module"] for r in self.r1["mutants"] if caught(first(r)))
        for m in sorted(sf["modules"]):
            a, b = s3["modules"][m], sf["modules"][m]
            w.append(f"| {m}{'（重抽样）' if m in self.resampled else ''} | {b['total']} | {m1[m]} | {a['killed'] + a['timeout']} | "
                     f"{a['kill_rate']:.0f}% | {b['killed'] + b['timeout']} | {b['survived']} | {b['kill_rate']:.0f}% |")
        w.append(f"| **合计** | {sf['total']} | {c['c1']} | {s3['caught']} | {s3['kill_rate']:.1f}% | {sf['caught']} | {sf['survived']} | "
                 f"**{sf['kill_rate']:.1f}%** |\n")
        w.append("复现：`python3.12 qdperf/mutants/run.py --per-module 50 --jobs 4`（约 70 分钟，4 核）；只看某几条：`--ids <id>...`；"
                 "某模块改了库代码后：`--module <模块>`（都并入结果文件，原 meta 保留、重跑记进 `reruns`）。\n")
        return w

    def table_a(self):
        w = [f"\n## A. 第 3 轮的 {len(self.surv3)} 条漏网（最终判定）\n\n“等价”给出为什么任何合法输入下都观测不到差别"
             "（或差别只落在没有消费者的字段 / 不可达的缺省值上）；“补判据”写明新断言在哪个测试、断言什么。最后一栏是最终结果文件里的状态。\n"]
        cur = None
        for r in self.surv3:
            if r["module"] != cur:
                cur = r["module"]
                w.append(f"\n### {cur}\n\n" + HDR.format("最终"))
            v, why = self.early_verdict(r)
            w.append(row(r, v, why, self.final_status(r)))
        return w

    def table_a2(self):
        w = ["\n## A2. 改库后重抽样的样本里的首跑漏网\n\n每次重抽样的样本单列一节；与第 3 轮同一处突变的不重复列"
             "（在表 A，最后一栏是重抽样后的编号与状态）。样本已被下一次重抽样替换的，最后一栏是替换前的状态。\n"]
        for title, fname, mod, commit, table, old in SAMPLES:
            rows = []
            for r in self.files[fname]["mutants"]:
                if r["module"] != mod or first(r) != "survived":
                    continue
                if self.sig(r, commit) in self.sig3:          # 与表 A 的某行是同一处突变：不重复列，本表若有判定也算用过
                    if r["id"] in self.t[table]:
                        self.used[table].add(r["id"])
                    continue
                rows.append(r)
            rows.sort(key=lambda r: (r["line"], r["id"]))
            w.append(f"\n### {title}：{len(rows)} 条\n\n" + HDR.format("替换前" if old else "最终"))
            pending = 0
            for r in rows:
                v, why = self.sample_reason(r, table, commit)
                w.append(row(r, v, why, status_text(r)))
                pending += old and v == FIX and r["status"] == "survived"
            if pending:
                w.append(f"\n其中 {pending} 条补了判据、还没按 id 重跑，这个样本就因下一次修库被整模块重抽样替换了，所以“替换前”一栏仍是漏网；"
                         "新判据留在测试里（之后两次整模块重跑都带着它们），但这几个突变体本身没有再跑过——它们改的是旧版源码，"
                         "放到修库之后的源码上已不是同一个突变。")
        return w

    def appendices(self):
        w = ["\n## B. 第 2 轮显形的漏网（第 1 轮被误杀遮住；第 3 轮前已补判据）\n\n" + HDR.format("第 3 轮")]
        for r in sorted((r for r in self.r2["mutants"] if r["status"] == "survived" and r["id"] not in self.t["ROUND1"]),
                        key=lambda r: (r["module"], r["line"])):
            w.append(row(r, *self.verdict("ROUND2", r["id"]), self.round3_status(r)))
        w.append("\n## C. 第 1 轮的 40 条漏网（第 2 轮前已补判据）\n\n" + HDR.format("第 3 轮"))
        for r in sorted((r for r in self.r1["mutants"] if first(r) == "survived"), key=lambda r: (r["module"], r["line"])):
            w.append(row(r, *self.verdict("ROUND1", r["id"]), self.round3_status(r)))
        rest = [r for r in self.r1["mutants"] if r["id"] in self.t["ROUND2"] and r["id"] not in self.used["ROUND2"]]
        w.append(f"\n## D. 第 1 轮的误杀里单独补了判据 / 判等价的 {len(rest)} 条\n\n第 1 轮把它们记成“抓住”，抓住它们的却是抖动或排版判据"
                 "（见经过第 1 条）；第 2 轮没跑到（或跑到时已被新判据抓住），判定当时就记下了。\n\n" + HDR.format("第 3 轮"))
        for r in sorted(rest, key=lambda r: (r["module"], r["line"])):
            w.append(row(r, *self.verdict("ROUND2", r["id"]), self.round3_status(r)))
        w.append("\n## 超时（计入抓住）\n")
        for r in self.fin["mutants"]:
            if r["status"] == "timeout":
                w.append(f"- `{r['id']}` 行 {r['line']}：`{r['before'][:60]}` → `{r['after'][:60]}`（解析 / 调度死循环）")
        w.append("\n## 未确认的失败（抖动，没有算抓住）\n")
        unconf3 = [r for r in self.r3["mutants"] if r.get("unconfirmed")]
        seen = {u["id"] for u in unconf3}
        for r in unconf3 + [x for x in self.fin["mutants"] if x.get("unconfirmed") and x["id"] not in seen]:
            w.append(f"- `{r['id']}`：" + "；".join(f"`{d}`" for d in r["unconfirmed"]))
        return w

    def render(self):
        c = self.counts()
        out = self.intro(c) + self.module_table(c) + self.table_a() + self.table_a2() + self.appendices()
        unused = {name: sorted(set(self.t[name]) - self.used[name]) for name in TABLES}
        unused = {k: v for k, v in unused.items() if v}
        if unused:
            raise TriageError(f"判定表里有没在 triage.md 出现的条目：{unused}")
        return "\n".join(out) + "\n", c


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3.12 qdperf/mutants/triage.py", description="生成 qdperf/mutants/triage.md")
    ap.add_argument("--check", action="store_true", help="只比较，不一致退出码 1")
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args(argv)
    try:
        text, c = Triage().render()
    except TriageError as exc:
        print(f"TRIAGE ERROR {exc}")
        return 2
    if a.check:
        with open(a.out, encoding="utf-8") as f:
            same = f.read() == text
        print("triage.md up to date" if same else "triage.md differs: run python3.12 qdperf/mutants/triage.py")
        return 0 if same else 1
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"wrote {a.out}: round 3 equivalent {c['eq3']} / new assertions {c['fix3']}; "
          f"final survivors equivalent {c['eqf']} / new assertions {c['fixf']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
