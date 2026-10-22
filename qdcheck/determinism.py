"""跨进程 / 跨哈希种子一致性 harness。

给一个可 JSON 序列化的任务描述和一个 ``模块:函数``：本进程跑一遍，再起两个子进程（PYTHONHASHSEED 分别为
0 和 4242）各跑一遍，三份结果两两结构化比较：

- 整数 / 字符串 / 布尔 / None / list 顺序 / dict 键集合必须完全相等（int 与 float 不能互换）；
- 浮点按相对容差 rel_tol（默认 1e-9）比较，NaN 视为相等，inf 必须同号；
- 第一处差异按确定的前序遍历（dict 键按字典序）报告路径，例如 ``trades[17].price``；
- 不做任何哈希摘要或逐字节比较；dict 键顺序默认不比（JSON 语义），``dict_order=True`` 时也比。
另外用 hash("qdcheck-hash-probe") 确认两个子进程的哈希种子确实不同（否则这次检查无效）。

    from qdcheck import determinism
    rep = determinism.check({"n": 5000, "seed": 1}, "mypkg.engine:run_backtest")
    print(rep["ok"], rep["pairs"][0]["first_diff"])

    python3.12 -m qdcheck.determinism qdcheck.examples:set_order_engine --task '{"n_bars": 60}' [--json out.json]

函数的返回值必须能被 json.dumps 序列化（tuple 视为 list；set / bytes / 自定义对象直接报错，
因为把 set 排序后再比会掩盖的恰恰就是迭代顺序问题）。
"""
from __future__ import annotations

import argparse
import importlib
import json
import math
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBE = "qdcheck-hash-probe"
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class HarnessError(RuntimeError):
    """任务 / 结果不可序列化，或子进程失败。"""


def load_target(spec):
    mod, sep, name = spec.partition(":")
    if not sep or not mod or not name:
        raise HarnessError(f"target must look like 'package.module:function', got {spec!r}")
    obj = importlib.import_module(mod)
    for part in name.split("."):
        obj = getattr(obj, part)
    if not callable(obj):
        raise HarnessError(f"{spec} is not callable")
    return obj


def to_json_text(obj, what):
    try:
        return json.dumps(obj, allow_nan=True)
    except (TypeError, ValueError) as exc:
        raise HarnessError(f"{what} is not JSON-serialisable: {exc}") from exc


def run_target(target, task):
    """在当前进程里跑一次，返回 JSON 往返后的结果（与子进程看到的形态一致）。"""
    fn = load_target(target)
    result = fn(json.loads(to_json_text(task, "task")))
    return json.loads(to_json_text(result, "result"))


def _child_main():
    """子进程入口：stdin 读 {"target","task"}，stdout 写 {"result","probe","seed_env"}。"""
    req = json.loads(sys.stdin.read())
    out = {"result": run_target(req["target"], req["task"]), "probe": hash(PROBE),
           "seed_env": os.environ.get("PYTHONHASHSEED"), "python": sys.version.split()[0]}
    sys.stdout.write(to_json_text(out, "child output"))


def run_child(target, task, hash_seed, timeout=600.0, python=None, paths=()):
    env = dict(os.environ, PYTHONHASHSEED=str(hash_seed),
               PYTHONPATH=os.pathsep.join([ROOT, *paths]))
    code = "from qdcheck.determinism import _child_main; _child_main()"
    payload = to_json_text({"target": target, "task": task}, "task")
    t0 = time.perf_counter()
    try:
        proc = subprocess.run([python or sys.executable, "-c", code], input=payload, env=env, cwd=ROOT,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise HarnessError(f"child with PYTHONHASHSEED={hash_seed} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        raise HarnessError(f"child with PYTHONHASHSEED={hash_seed} failed (exit {proc.returncode}):\n"
                           + proc.stderr[-3000:])
    out = json.loads(proc.stdout)
    out["seconds"] = round(time.perf_counter() - t0, 3)
    return out


# ---- 结构化比较 -------------------------------------------------------------------
def _kind(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "str"
    if isinstance(v, (list, tuple)):
        return "list"
    if isinstance(v, dict):
        return "dict"
    return type(v).__name__


def format_path(tokens):
    if not tokens:
        return "<root>"
    out = []
    for k, tok in enumerate(tokens):
        if isinstance(tok, int):
            out.append(f"[{tok}]")
        elif _IDENT.match(tok):
            out.append(tok if k == 0 else f".{tok}")
        else:
            out.append(f"[{json.dumps(tok, ensure_ascii=False)}]")
    return "".join(out)


def _short(v):
    text = repr(v)
    return v if len(text) <= 200 and not isinstance(v, (list, dict)) else text[:200]


def floats_equal(a, b, rel_tol, abs_tol=0.0):
    if math.isnan(a) or math.isnan(b):
        return math.isnan(a) and math.isnan(b)
    if math.isinf(a) or math.isinf(b):
        return a == b
    return abs(a - b) <= max(rel_tol * max(abs(a), abs(b)), abs_tol)


def diff_all(a, b, rel_tol=1e-9, abs_tol=0.0, dict_order=False, limit=10):
    """返回 (差异列表（前序遍历顺序，最多 limit 条，不进入已判不同的子树）, 比较过的叶子数)。
    差异 = {"path", "reason", "a", "b"}。迭代实现。"""
    stack, leaves, found = [((), a, b)], 0, []
    while stack and len(found) < limit:
        item = stack.pop()
        if item[0] == "LEN":
            _tag, path, la, lb = item
            found.append({"path": format_path(path), "reason": f"length {la} != {lb}", "a": la, "b": lb})
            continue
        path, x, y = item
        kx, ky = _kind(x), _kind(y)
        if kx != ky:
            found.append({"path": format_path(path), "reason": f"type {kx} != {ky}", "a": _short(x), "b": _short(y)})
        elif kx == "dict":
            keys_x, keys_y = set(x), set(y)
            if keys_x != keys_y:
                k = sorted(keys_x ^ keys_y)[0]
                found.append({"path": format_path(path + (k,)), "reason": f"key only in {'a' if k in keys_x else 'b'}",
                              "a": _short(x.get(k)), "b": _short(y.get(k))})
                continue
            if dict_order and list(x) != list(y):
                found.append({"path": format_path(path), "reason": "dict key order differs", "a": list(x)[:20],
                              "b": list(y)[:20]})
                continue
            stack.extend((path + (k,), x[k], y[k]) for k in sorted(x, reverse=True))
        elif kx == "list":
            if len(x) != len(y):
                stack.append(("LEN", path, len(x), len(y)))
            stack.extend((path + (i,), x[i], y[i]) for i in reversed(range(min(len(x), len(y)))))
        else:
            leaves += 1
            same = floats_equal(x, y, rel_tol, abs_tol) if kx == "float" else x == y
            if not same:
                found.append({"path": format_path(path), "reason": "value", "a": _short(x), "b": _short(y)})
    return found, leaves


def diff(a, b, rel_tol=1e-9, abs_tol=0.0, dict_order=False):
    """返回 (第一处差异 | None, 比较过的叶子数)。"""
    found, leaves = diff_all(a, b, rel_tol, abs_tol, dict_order, limit=1)
    return (found[0] if found else None), leaves


def check(task, target, hash_seeds=(0, 4242), rel_tol=1e-9, abs_tol=0.0, dict_order=False, timeout=600.0,
          python=None, paths=(), in_process=True):
    """本进程 + 每个 hash_seed 一个子进程，两两比较。返回报告 dict（ok = 全部两两一致且哈希种子确实生效）。"""
    to_json_text(task, "task")
    runs = []
    if in_process:
        t0 = time.perf_counter()
        runs.append({"name": "in_process", "hash_seed": os.environ.get("PYTHONHASHSEED", "random"),
                     "result": run_target(target, task), "probe": hash(PROBE),
                     "seconds": round(time.perf_counter() - t0, 3)})
    for seed in hash_seeds:
        out = run_child(target, task, seed, timeout, python, paths)
        runs.append({"name": f"PYTHONHASHSEED={seed}", "hash_seed": str(seed), "result": out["result"],
                     "probe": out["probe"], "seconds": out["seconds"], "seed_env": out["seed_env"]})
    pairs = []
    for i in range(len(runs)):
        for j in range(i + 1, len(runs)):
            found, _n = diff_all(runs[i]["result"], runs[j]["result"], rel_tol, abs_tol, dict_order, limit=10)
            _first, leaves = diff(runs[i]["result"], runs[j]["result"], rel_tol, abs_tol, dict_order)
            pairs.append({"a": runs[i]["name"], "b": runs[j]["name"], "ok": not found,
                          "first_diff": found[0] if found else None, "diffs": found, "leaves": leaves})
    child_probes = {r["probe"] for r in runs if r["name"] != "in_process"}
    effective = len(child_probes) == len(hash_seeds)
    return {"ok": effective and all(p["ok"] for p in pairs), "target": target, "task": task,
            "rel_tol": rel_tol, "abs_tol": abs_tol, "dict_order": dict_order, "hash_seeds_effective": effective,
            "runs": [{k: v for k, v in r.items() if k != "result"} for r in runs], "pairs": pairs}


def format_report(rep):
    lines = [f"{'OK' if rep['ok'] else 'DIFFERENT'}: {rep['target']}  (hash seeds effective: "
             f"{rep['hash_seeds_effective']})"]
    for p in rep["pairs"]:
        if p["ok"]:
            lines.append(f"  {p['a']} vs {p['b']}: identical ({p['leaves']} leaves)")
        else:
            d = p["first_diff"]
            lines.append(f"  {p['a']} vs {p['b']}: first difference at {d['path']} ({d['reason']}): "
                         f"{d['a']!r} vs {d['b']!r}")
            for extra in p["diffs"][1:4]:
                lines.append(f"      also {extra['path']} ({extra['reason']}): {extra['a']!r} vs {extra['b']!r}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="跨进程 / 跨哈希种子一致性检查")
    ap.add_argument("target", help="模块:函数")
    ap.add_argument("--task", default="{}", help="任务 JSON 字符串，或 @文件路径")
    ap.add_argument("--seeds", type=int, nargs="+", default=[0, 4242])
    ap.add_argument("--rel", type=float, default=1e-9)
    ap.add_argument("--dict-order", action="store_true")
    ap.add_argument("--json", default=None)
    args = ap.parse_args(argv)
    try:
        if args.task.startswith("@"):
            with open(args.task[1:], encoding="utf-8") as f:
                task = json.load(f)
        else:
            task = json.loads(args.task)
        rep = check(task, args.target, tuple(args.seeds), args.rel, dict_order=args.dict_order)
    except (HarnessError, ValueError, OSError, ImportError, AttributeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(format_report(rep))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=1, sort_keys=True)
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
