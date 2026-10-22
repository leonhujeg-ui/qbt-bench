"""热路径剖析 + 回归闸（G2）。

    python3.12 -m qdperf.profile_gate run      pkg.mod:func [--args JSON] [--repeat 5] [--path DIR] [--env K=V] --out new.json
    python3.12 -m qdperf.profile_gate baseline pkg.mod:func [--args JSON] [--repeat 5] [--runs 3] --out base.json
    python3.12 -m qdperf.profile_gate compare  base.json new.json [--wall-tol 0.10] [--mem-tol 0.20] [--gate hot,cold,mem]

    python3.12 -m qdperf.profile_gate pair     pkg.mod:func [--args JSON] --env-b K=V | --path-b DIR --out a.json --out-b b.json

测量（全部在全新解释器子进程里，和调用方进程状态无关；best-of-N 的 N 个样本来自 N 个进程）：
- 冷：每个进程计时“导入目标模块 + 第一次调用”（不含解释器启动）；热：同一进程紧接着再调（--inner 次取最快）；
  冷 / 热各取 N 个进程中最快者；墙钟 perf_counter、CPU process_time；
- 内存：另起一个进程，预热后 tracemalloc 峰值一次（只算这次调用期间新分配的；计时不受 tracemalloc 拖慢影响）；
- 剖析：同一进程再调一次，cProfile 按累计时间取前 30；函数名规范成 “相对路径:限定名”（去掉行号与 sys.path 前缀 /
  临时目录），两份 JSON 的函数名集合可直接比较。
pair：A、B 各 runs 遍按进程轮转交替取样（A1 B1 A2 B2 A3 B3 …），机器的慢漂移同时落在两边、也不进入基线噪声；
baseline：runs 遍依次连跑（之后另找时间 run 时，两次之间的漂移无从抵消，噪声幅度如实把连跑期间的漂移算进容差）。
基线：同一台机器连跑 runs（缺省 3）遍上述测量，各指标取中位数作基线值，噪声幅度 = (最大 - 最小) / 最小，写进 JSON。
回归闸：每个指标的容差 = max(手动容差, 2 × 基线噪声幅度)；本次 > 基线 × (1 + 容差) 即红。stdout 第一行：
    REGRESSION <指标>=<基线>→<本次> (+<百分比>)      （红；有多个红时取超出容差最多的那个）
    OK <指标>=<基线>→<本次> (<±百分比>) tol=<容差>    （全绿；指标为 hot.wall_s）
其后逐行列出每个指标与“新进前 10 的函数”（NEW_TOP10 <函数名> cum=<秒>）。退出码：0 绿，1 红，2 输入错误 / 两份 JSON 不可比。
"""
import argparse
import ast
import cProfile
import importlib
import json
import os
import pstats
import subprocess
import sys
import sysconfig
import tempfile
import time
import tracemalloc

from qdperf import common

FORMAT = "qdperf.profile.v1"
METRICS = {"hot": ("hot", "wall_s"), "cold": ("cold", "wall_s"), "mem": ("memory", "peak_bytes")}
DEFAULT_GATE = ("hot", "cold", "mem")
EPS = 1e-9            # 浮点比较余量：恰好等于容差（如 1.0→1.1 对 10%）不判红


class GateError(ValueError):
    """输入错误 / 目标不可导入或调用失败 / 两份 JSON 不可比；CLI 退出码 2。"""


# ---- 目标解析 ----------------------------------------------------------------------------
def resolve(target):
    """'pkg.mod:func' 或 'pkg.mod:Class.method' → 可调用对象。"""
    if not isinstance(target, str) or target.count(":") != 1:
        raise GateError(f"target must look like 'pkg.module:function', got {target!r}")
    mod_name, attr = target.split(":")
    if not mod_name or not attr:
        raise GateError(f"target must look like 'pkg.module:function', got {target!r}")
    try:
        obj = importlib.import_module(mod_name)
        for part in attr.split("."):
            obj = getattr(obj, part)
    except (ImportError, AttributeError) as exc:
        raise GateError(f"cannot resolve {target}: {exc}") from exc
    if not callable(obj):
        raise GateError(f"{target} is not callable")
    return obj


# ---- 函数名规范化 --------------------------------------------------------------------------
_QUAL_CACHE = {}


def qualnames(path):
    """{行号: 限定名}：def 行与第一个装饰器行都映射到同一个限定名（cProfile 的 co_firstlineno 指向装饰器行）。"""
    if path in _QUAL_CACHE:
        return _QUAL_CACHE[path]
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
        tree = None
    if tree is not None:
        _walk_defs(tree, "", out)
    _QUAL_CACHE[path] = out
    return out


def _walk_defs(node, prefix, out):
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            q = prefix + child.name
            if not isinstance(child, ast.ClassDef):
                out[child.lineno] = q
                for d in child.decorator_list:
                    out.setdefault(d.lineno, q)
            _walk_defs(child, q + ("." if isinstance(child, ast.ClassDef) else ".<locals>."), out)
        else:
            _walk_defs(child, prefix, out)


def path_roots():
    """规范化用的前缀：标准库目录记作 <stdlib>，其余 sys.path 目录按最长前缀剥掉。"""
    std = os.path.realpath(sysconfig.get_paths()["stdlib"])
    roots = [(std, "<stdlib>/")]
    for p in sys.path:
        if p and os.path.isdir(p):
            rp = os.path.realpath(p)
            if rp != std:
                roots.append((rp, ""))
    return sorted(roots, key=lambda r: -len(r[0]))


def func_name(key, roots):
    """pstats 键 (文件, 行号, 函数名) → '相对路径:限定名'；内建函数 → '<builtin>:名字'。"""
    filename, lineno, name = key
    if filename == "~" or filename.startswith("<"):
        return f"<builtin>:{name}"
    real = os.path.realpath(filename)
    rel = os.path.basename(real)
    for root, tag in roots:
        if real.startswith(root + os.sep):
            rel = tag + real[len(root) + 1:].replace(os.sep, "/")
            break
    qual = qualnames(real).get(lineno, name)
    return f"{rel}:{qual}"


def top_functions(profiler, n=30):
    """cProfile → 规范化函数名后按累计时间降序的前 n 个（同名合并：调用次数 / 自身时间 / 累计时间相加）。"""
    stats = pstats.Stats(profiler)
    roots = path_roots()
    merged = {}
    for key, (cc, nc, tt, ct, _callers) in stats.stats.items():
        if "_lsprof.Profiler" in key[2]:
            continue                                                   # 剖析器自己的 disable() 不算
        name = func_name(key, roots)
        m = merged.setdefault(name, {"name": name, "ncalls": 0, "primitive_calls": 0, "tottime": 0.0, "cumtime": 0.0})
        m["ncalls"] += nc
        m["primitive_calls"] += cc
        m["tottime"] += tt
        m["cumtime"] += ct
    ranked = sorted(merged.values(), key=lambda m: (-m["cumtime"], m["name"]))
    for m in ranked:
        m["tottime"] = round(m["tottime"], 6)
        m["cumtime"] = round(m["cumtime"], 6)
    return ranked[:n], len(merged)


# ---- 子进程工作者 ----------------------------------------------------------------------------
def worker(mode, target, args, inner, top):
    """在当前（全新）进程里测量。
    mode = sample：冷样本（导入目标模块 + 第一次调用）+ 热样本（之后 inner 次调用里最快的一次）；
    mode = deep：预热一次后，tracemalloc 峰值一次、cProfile 一次。返回可 JSON 的 dict。"""
    common.child_init()
    if mode not in ("sample", "deep"):
        raise GateError(f"unknown worker mode {mode!r}")
    if mode == "sample":
        t0, c0 = time.perf_counter(), time.process_time()
        fn = resolve(target)
        fn(**args)
        cold_wall, cold_cpu = time.perf_counter() - t0, time.process_time() - c0
        walls, cpus = [], []
        for _ in range(max(1, inner)):
            t1, c1 = time.perf_counter(), time.process_time()
            fn(**args)
            walls.append(time.perf_counter() - t1)
            cpus.append(time.process_time() - c1)
        return {"cold_wall": cold_wall, "cold_cpu": cold_cpu, "hot_wall": min(walls), "hot_cpu": min(cpus),
                "hot_inner_samples": walls}
    fn = resolve(target)
    fn(**args)                                                         # 预热
    tracemalloc.start()
    fn(**args)
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    prof = cProfile.Profile()
    prof.enable()
    fn(**args)
    prof.disable()
    ranked, total = top_functions(prof, top)
    return {"peak_bytes": peak, "current_bytes": current, "top": ranked, "functions": total}


def _spawn(mode, target, args, inner, top, paths, timeout, extra_env=None):
    fd, out = tempfile.mkstemp(prefix="qdperf-prof-", suffix=".json")
    os.close(fd)
    env = dict(os.environ, **(extra_env or {}))
    extra = [os.path.abspath(p) for p in paths] + [os.getcwd()]
    env["PYTHONPATH"] = os.pathsep.join(extra + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    cmd = [sys.executable, "-m", "qdperf.profile_gate", "_worker", mode, target, json.dumps(args), str(inner),
           str(top), out]
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
        with open(out, encoding="utf-8") as f:
            text = f.read()
    finally:
        os.remove(out)
    if proc.returncode != 0 or not text:
        raise GateError(f"{mode} worker failed (exit {proc.returncode}): {proc.stderr.strip()[-800:]}")
    res = json.loads(text)
    res["process_wall_s"] = time.perf_counter() - t0
    return res


# ---- 测量 / 基线 ----------------------------------------------------------------------------
def measure(target, args=None, repeat=5, cold=True, top=30, paths=(), timeout=3600, env=None, inner=1):
    """一遍完整测量 → JSON dict（字段见 CONTRACT.md §2.3）。
    best-of-N：起 repeat 个全新进程，每个进程先测一次冷调用（导入 + 第一次调用）、再测热调用（inner 次里取最快），
    冷 / 热各取 N 个进程里最快的那个；另起一个进程测 tracemalloc 峰值与 cProfile。
    （N 个样本来自 N 个进程而不是同一进程连跑 N 次：本机实测同一进程内的样本带着该进程自己的“快慢”，
    进程之间差到 10% 以上，只有跨进程取最快才能把噪声压下来，见 REPORT.md §2.3。）
    env：额外传给测量子进程的环境变量（例如功能开关；不进入 target/args，不影响两份 JSON 的可比性）。"""
    arm = {"target": target, "args": dict(args or {}), "paths": paths, "env": env}
    return interleaved([arm], repeat, cold, top, timeout, inner)[0]


def interleaved(arms, repeat=5, cold=True, top=30, timeout=3600, inner=1):
    """多个“臂”（各自的 target / args / paths / env）按进程轮转取样：第 r 轮依次给每个臂起一个新进程，
    共 repeat 轮；所以各臂的样本落在同一段时间里，机器的慢漂移对各臂一样。每个臂另起一个进程测内存与剖析。
    → 每个臂一份与 measure() 相同格式的 JSON。"""
    if repeat < 1:
        raise GateError("repeat must be >= 1")
    if not arms:
        raise GateError("no arms")
    samples = [[] for _ in arms]
    for _ in range(repeat):
        for k, a in enumerate(arms):
            samples[k].append(_spawn("sample", a["target"], a["args"], inner, top, a.get("paths", ()), timeout,
                                     a.get("env")))
    return [_arm_doc(a, samples[k], _spawn("deep", a["target"], a["args"], inner, top, a.get("paths", ()), timeout,
                                           a.get("env")), repeat, inner, cold) for k, a in enumerate(arms)]


def _arm_doc(arm, samples, deep, repeat, inner, cold):
    out = {"format": FORMAT, "target": arm["target"], "args": dict(arm["args"]), "repeat": repeat, "inner": inner,
           "machine": common.machine(),
           "hot": {"wall_s": min(x["hot_wall"] for x in samples), "cpu_s": min(x["hot_cpu"] for x in samples),
                   "wall_samples": [x["hot_wall"] for x in samples], "cpu_samples": [x["hot_cpu"] for x in samples],
                   "processes": repeat},
           "memory": {"peak_bytes": deep["peak_bytes"], "current_bytes": deep["current_bytes"], "mode": "hot, tracemalloc"},
           "profile": {"top": deep["top"], "functions": deep["functions"], "sort": "cumtime"},
           "runs": 1, "noise": None}
    if cold:
        out["cold"] = {"wall_s": min(x["cold_wall"] for x in samples), "cpu_s": min(x["cold_cpu"] for x in samples),
                       "wall_samples": [x["cold_wall"] for x in samples], "cpu_samples": [x["cold_cpu"] for x in samples],
                       "process_wall_samples": [x["process_wall_s"] for x in samples]}
    return out


def _median(xs):
    return common.percentile(xs, 0.5)


def aggregate(each):
    """多遍 measure 合成一份基线：各指标取中位数，噪声幅度 = 各遍之间的相对极差；剖析取热墙钟居中的那一遍。"""
    runs = len(each)
    if runs < 1:
        raise GateError("runs must be >= 1")
    hot = [m["hot"]["wall_s"] for m in each]
    mid = sorted(range(runs), key=lambda k: hot[k])[runs // 2]
    out = json.loads(json.dumps(each[mid]))
    out["runs"] = runs
    out["hot"]["wall_s"] = _median(hot)
    out["hot"]["cpu_s"] = _median([m["hot"]["cpu_s"] for m in each])
    out["memory"]["peak_bytes"] = int(_median([m["memory"]["peak_bytes"] for m in each]))
    noise = {"hot_wall_rel": common.rel_spread(hot), "peak_rel": common.rel_spread([m["memory"]["peak_bytes"] for m in each]),
             "per_run": [{"hot_wall_s": m["hot"]["wall_s"], "peak_bytes": m["memory"]["peak_bytes"],
                          "cold_wall_s": m.get("cold", {}).get("wall_s")} for m in each]}
    if all("cold" in m for m in each):
        cw = [m["cold"]["wall_s"] for m in each]
        out["cold"]["wall_s"] = _median(cw)
        out["cold"]["cpu_s"] = _median([m["cold"]["cpu_s"] for m in each])
        noise["cold_wall_rel"] = common.rel_spread(cw)
    out["noise"] = noise
    return out


def baseline(target, args=None, repeat=5, runs=3, cold=True, top=30, paths=(), env=None, inner=1):
    """同一台机器连跑 runs 遍 measure → aggregate。"""
    if runs < 1:
        raise GateError("runs must be >= 1")
    return aggregate([measure(target, args, repeat, cold, top, paths, env=env, inner=inner) for _ in range(runs)])


def pair(target, args=None, runs=3, repeat=5, top=30, paths_a=(), paths_b=(), env_a=None, env_b=None, inner=1):
    """A、B 各 runs 遍，按进程交替取样（A1 B1 A2 B2 A3 B3 轮转 repeat 轮，见 interleaved），各自 aggregate →
    (基线, 本次)。用于“发版前后”在同一时间窗里对比：机器的慢漂移同时落在两边，也不进入基线噪声。"""
    if runs < 1:
        raise GateError("runs must be >= 1")
    a = {"target": target, "args": dict(args or {}), "paths": paths_a, "env": env_a}
    b = {"target": target, "args": dict(args or {}), "paths": paths_b, "env": env_b}
    docs = interleaved([a, b] * runs, repeat, True, top, inner=inner)
    return aggregate(docs[0::2]), aggregate(docs[1::2])


# ---- 回归闸 ----------------------------------------------------------------------------
def _get(doc, metric):
    sec, field = METRICS[metric]
    return doc.get(sec, {}).get(field) if isinstance(doc.get(sec), dict) else None


def _noise(base, metric):
    n = base.get("noise") or {}
    return {"hot": n.get("hot_wall_rel"), "cold": n.get("cold_wall_rel"), "mem": n.get("peak_rel")}[metric] or 0.0


def comparable(base, new):
    for doc in (base, new):
        if not isinstance(doc, dict) or doc.get("format") != FORMAT:
            raise GateError(f"not a {FORMAT} document")
    if base["target"] != new["target"] or base["args"] != new["args"]:
        raise GateError(f"different target/args: {base['target']} {base['args']} vs {new['target']} {new['args']}")


def _fmt(metric, v):
    return str(int(v)) if metric == "mem" else f"{v:.4g}"


def compare(base, new, wall_tol=0.10, mem_tol=0.20, gate=DEFAULT_GATE, top_new=10):
    """→ {verdict: "OK"|"REGRESSION", lines: [...], metrics: [...], new_top: [...]}；输入不可比抛 GateError。"""
    comparable(base, new)
    if wall_tol < 0 or mem_tol < 0:
        raise GateError("tolerances must be >= 0")
    rows = []
    for metric in gate:
        if metric not in METRICS:
            raise GateError(f"unknown gate metric {metric!r}")
        b, n = _get(base, metric), _get(new, metric)
        if b is None or n is None:
            continue
        manual = mem_tol if metric == "mem" else wall_tol
        noise = _noise(base, metric)
        tol = max(manual, 2.0 * noise)
        change = common.rel_change(b, n)
        rows.append({"metric": metric, "name": ".".join(METRICS[metric]), "base": b, "new": n, "change": change,
                     "tolerance": tol, "manual_tolerance": manual, "noise": noise, "red": change > tol + EPS})
    if not rows:
        raise GateError("no comparable metric")
    new_top = _new_top(base, new, top_new)
    red = [r for r in rows if r["red"]]
    if red:
        w = max(red, key=lambda r: (r["change"] - r["tolerance"], r["name"]))
        first = f"REGRESSION {w['name']}={_fmt(w['metric'], w['base'])}→{_fmt(w['metric'], w['new'])} ({common.fmt_pct(w['change'])})"
    else:
        w = rows[0]
        first = (f"OK {w['name']}={_fmt(w['metric'], w['base'])}→{_fmt(w['metric'], w['new'])} "
                 f"({common.fmt_pct(w['change'])}) tol={w['tolerance'] * 100:.1f}%")
    lines = [first] + [f"{'RED' if r['red'] else 'ok '} {r['name']} {_fmt(r['metric'], r['base'])}→{_fmt(r['metric'], r['new'])} "
                       f"{common.fmt_pct(r['change'])} tol={r['tolerance'] * 100:.1f}% (manual {r['manual_tolerance'] * 100:.1f}%, "
                       f"2×noise {2 * r['noise'] * 100:.1f}%)" for r in rows]
    lines += [f"NEW_TOP{top_new} {t['name']} cum={t['cumtime']:.4g}s" for t in new_top]
    return {"verdict": "REGRESSION" if red else "OK", "lines": lines, "metrics": rows, "new_top": new_top}


def _new_top(base, new, k):
    old = {t["name"] for t in base["profile"]["top"][:k]}
    return [t for t in new["profile"]["top"][:k] if t["name"] not in old]


def function_names(doc):
    return {t["name"] for t in doc["profile"]["top"]}


# ---- CLI ------------------------------------------------------------------------------
def first_line_profile(doc):
    noise = doc.get("noise") or {}
    hn = noise.get("hot_wall_rel")
    cold = doc.get("cold", {}).get("wall_s")
    return (f"PROFILE target={doc['target']} hot_wall_s={doc['hot']['wall_s']:.4g} "
            f"cold_wall_s={'-' if cold is None else f'{cold:.4g}'} peak_bytes={doc['memory']['peak_bytes']} "
            f"runs={doc['runs']} noise_hot={'-' if hn is None else f'{hn * 100:.2f}%'}")


def _parser():
    ap = argparse.ArgumentParser(prog="python3.12 -m qdperf.profile_gate", description="热路径剖析 + 回归闸")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "baseline", "pair"):
        p = sub.add_parser(name)
        p.add_argument("target")
        p.add_argument("--args", default="{}", help="JSON 对象，作为关键字参数传给目标")
        p.add_argument("--repeat", type=int, default=5, help="best-of-N 的 N（N 个全新进程）")
        p.add_argument("--inner", type=int, default=1, help="每个进程里热调用的次数（取最快）；目标很短时调大")
        p.add_argument("--runs", type=int, default=1 if name == "run" else 3)
        p.add_argument("--no-cold", action="store_true")
        p.add_argument("--top", type=int, default=30)
        p.add_argument("--path", action="append", default=[], help="额外加入子进程 sys.path 的目录（可多次）")
        p.add_argument("--env", action="append", default=[], help="KEY=VALUE，传给测量子进程的环境变量（可多次）")
        p.add_argument("--out", required=True, help="pair 时为 A（基线）的输出")
        if name == "pair":
            p.add_argument("--out-b", required=True, help="B（本次）的输出")
            p.add_argument("--path-b", action="append", default=[], help="B 的额外 sys.path 目录（例如新版本的检出目录）")
            p.add_argument("--env-b", action="append", default=[], help="B 的环境变量 KEY=VALUE")
    c = sub.add_parser("compare")
    c.add_argument("base")
    c.add_argument("new")
    c.add_argument("--wall-tol", type=float, default=0.10)
    c.add_argument("--mem-tol", type=float, default=0.20)
    c.add_argument("--gate", default=",".join(DEFAULT_GATE))
    c.add_argument("--json", default=None)
    w = sub.add_parser("_worker")
    for a in ("mode", "target", "args", "inner", "top", "out"):
        w.add_argument(a)
    return ap


def _env_pairs(items):
    if any("=" not in e for e in items):
        raise GateError("--env must be KEY=VALUE")
    return dict(e.split("=", 1) for e in items)


def main(argv=None):
    args = _parser().parse_args(argv)
    if args.cmd == "_worker":
        res = worker(args.mode, args.target, json.loads(args.args), int(args.inner), int(args.top))
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(res, f)
        common.child_finish()
        return 0
    try:
        if args.cmd == "compare":
            res = compare(common.read_json(args.base), common.read_json(args.new), args.wall_tol, args.mem_tol,
                          tuple(g for g in args.gate.split(",") if g))
            print("\n".join(res["lines"]))
            if args.json:
                common.write_json(args.json, res)
            return 0 if res["verdict"] == "OK" else 1
        kw = json.loads(args.args)
        if not isinstance(kw, dict):
            raise GateError("--args must be a JSON object")
        env = _env_pairs(args.env)
        if args.cmd == "pair":
            doc, doc_b = pair(args.target, kw, args.runs, args.repeat, args.top, args.path, args.path_b, env,
                              _env_pairs(args.env_b), args.inner)
            common.write_json(args.out_b, doc_b)
            print(first_line_profile(doc_b).replace("PROFILE ", "PROFILE_B ", 1))
        elif args.runs > 1:
            doc = baseline(args.target, kw, args.repeat, args.runs, not args.no_cold, args.top, args.path, env, args.inner)
        else:
            doc = measure(args.target, kw, args.repeat, not args.no_cold, args.top, args.path, env=env, inner=args.inner)
    except (GateError, OSError, json.JSONDecodeError) as exc:
        print(f"PROFILE_GATE ERROR {type(exc).__name__}: {exc}")
        return 2
    common.write_json(args.out, doc)
    print(first_line_profile(doc))
    print(common.text_table(["累计s", "自身s", "调用", "函数"], [(f"{t['cumtime']:.4f}", f"{t['tottime']:.4f}", t["ncalls"],
                                                                   t["name"]) for t in doc["profile"]["top"][:15]]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
