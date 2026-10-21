"""自写突变测试器（纯标准库）。

对 ``qbt/`` 下每个模块用 AST 自动生成突变体（运算符翻转、比较边界、条件取反、
布尔运算互换、常量 ±1 / 翻转、语句删除、return 置空、break/continue 互换），
把突变后的包复制到独立工作目录里，用子进程跑测试套件；测试失败 = 抓住。

用法::

    python mutants/run_mutations.py --list                  # 只枚举每个模块的候选数
    python mutants/run_mutations.py --per-module 80 --jobs 4 # 跑满并输出 mutants/results.json
    python mutants/run_mutations.py --module broker --resume

判定：子进程退出码非 0 => killed；超时 => timeout（单列，默认也计入“抓住”，报告中单独注明）；
退出码 0 => survived（漏网），需要逐个在 REPORT 中说明“等价”或“补了哪条判据”。
"""
from __future__ import annotations

import argparse
import ast
import concurrent.futures
import json
import os
import random
import shutil
import subprocess
import sys
import time

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PACKAGE = "qbt"

_BINOP_SWAP = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.Div, ast.Div: ast.Mult,
               ast.FloorDiv: ast.Div, ast.Mod: ast.Mult, ast.Pow: ast.Mult}
_CMP_SWAP = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt,
             ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Is: ast.IsNot, ast.IsNot: ast.Is,
             ast.In: ast.NotIn, ast.NotIn: ast.In}
_DELETABLE = (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Expr, ast.Raise, ast.Continue,
              ast.Break, ast.Delete)


# ---- 候选枚举 -----------------------------------------------------------------
def _in_main_guard_or_slots(node_parents):
    for p in node_parents:
        if isinstance(p, ast.If) and isinstance(p.test, ast.Compare):
            left = p.test.left
            if isinstance(left, ast.Name) and left.id == "__name__":
                return True
        if isinstance(p, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__slots__" for t in p.targets):
            return True
    return False


def _walk_with_parents(tree):
    """与 ast.walk 相同的 BFS 顺序，同时给出父链，保证候选编号确定。"""
    from collections import deque
    todo = deque([(tree, ())])
    while todo:
        node, parents = todo.popleft()
        yield node, parents
        for child in ast.iter_child_nodes(node):
            todo.append((child, parents + (node,)))


def enumerate_candidates(source):
    """返回 [(walk_index, op, extra)]；op 为字符串，extra 为可 JSON 的附加信息。"""
    tree = ast.parse(source)
    lines = source.splitlines()
    cands = []
    for idx, (node, parents) in enumerate(_walk_with_parents(tree)):
        lineno = getattr(node, "lineno", None)
        if lineno is None or _in_main_guard_or_slots(parents):
            continue
        if "no mutate" in lines[lineno - 1]:
            continue
        cands.extend((idx, op, extra) for op, extra in _ops_for(node, parents))
    return cands


def _ops_for(node, parents):
    out = []
    if isinstance(node, (ast.BinOp, ast.AugAssign)) and type(node.op) in _BINOP_SWAP:
        out.append(("binop", _BINOP_SWAP[type(node.op)].__name__))
    if isinstance(node, ast.Compare):
        for k, op in enumerate(node.ops):
            if type(op) in _CMP_SWAP:
                out.append(("compare", [k, _CMP_SWAP[type(op)].__name__]))
    if isinstance(node, ast.BoolOp):
        out.append(("boolop", "Or" if isinstance(node.op, ast.And) else "And"))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.Not, ast.USub)):
        out.append(("unary_drop", None))
    if isinstance(node, ast.Constant) and not isinstance(parents[-1], ast.Expr):
        out.extend(_const_ops(node, parents))
    if isinstance(node, (ast.If, ast.While)) and not isinstance(node.test, ast.Constant):
        out.append(("negate_cond", None))
    if isinstance(node, _DELETABLE) and not _is_docstring(node):
        out.append(("delete_stmt", None))
    if isinstance(node, ast.Return) and node.value is not None and not isinstance(node.value, ast.Constant):
        out.append(("return_none", None))
    if isinstance(node, (ast.Break, ast.Continue)):
        out.append(("break_continue", None))
    return out


def _is_docstring(node):
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)


def _const_ops(node, parents):
    v = node.value
    if isinstance(v, bool):
        return [("const", not v)]
    if isinstance(v, int):
        return [("const", v + 1), ("const", v - 1)]
    if isinstance(v, float):
        return [("const", 1.0 if v == 0.0 else v * 1.5)]
    return []


# ---- 应用突变 -----------------------------------------------------------------
def apply_mutation(source, idx, op, extra):
    """返回 (mutated_source, before_snippet, after_snippet)。"""
    tree = ast.parse(source)
    nodes = list(ast.walk(tree))
    node = nodes[idx]
    before = ast.unparse(node)
    if op == "binop":
        node.op = getattr(ast, extra)()
    elif op == "compare":
        node.ops[extra[0]] = getattr(ast, extra[1])()
    elif op == "boolop":
        node.op = getattr(ast, extra)()
    elif op == "unary_drop":
        _replace(tree, node, node.operand)
    elif op == "const":
        node.value = extra
    elif op == "negate_cond":
        node.test = ast.UnaryOp(op=ast.Not(), operand=node.test)
    elif op == "delete_stmt":
        _replace(tree, node, ast.Pass())
    elif op == "return_none":
        node.value = ast.Constant(value=None)
    elif op == "break_continue":
        _replace(tree, node, ast.Continue() if isinstance(node, ast.Break) else ast.Break())
    else:
        raise ValueError(op)
    ast.fix_missing_locations(tree)
    after = ast.unparse(nodes[idx]) if op not in ("unary_drop", "delete_stmt", "break_continue") else _after_snippet(op, node)
    return ast.unparse(tree), before, after


def _after_snippet(op, node):
    if op == "unary_drop":
        return ast.unparse(node.operand)
    if op == "delete_stmt":
        return "pass"
    return "continue" if isinstance(node, ast.Break) else "break"


def _replace(tree, old, new):
    for parent in ast.walk(tree):
        for field, value in ast.iter_fields(parent):
            if value is old:
                setattr(parent, field, new)
                return
            if isinstance(value, list):
                for i, item in enumerate(value):
                    if item is old:
                        value[i] = new
                        return
    raise RuntimeError("node not found")


# ---- 选择与执行 ---------------------------------------------------------------
def list_modules(only=None):
    pkg = os.path.join(REPO, PACKAGE)
    names = sorted(f[:-3] for f in os.listdir(pkg) if f.endswith(".py") and f != "__init__.py")
    return [n for n in names if only is None or n in only]


def select_mutants(module, per_module, seed):
    path = os.path.join(REPO, PACKAGE, module + ".py")
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()
    cands = enumerate_candidates(source)
    valid = []
    for idx, op, extra in cands:
        try:
            mutated, before, after = apply_mutation(source, idx, op, extra)
            compile(mutated, path, "exec")
        except (SyntaxError, ValueError, RuntimeError):
            continue
        if mutated == ast.unparse(ast.parse(source)):
            continue
        lineno = list(ast.walk(ast.parse(source)))[idx].lineno
        valid.append({"id": f"{module}:{idx}:{op}", "module": module, "line": lineno, "op": op,
                      "before": before[:120], "after": after[:120], "idx": idx, "extra": extra})
    if per_module and len(valid) > per_module:
        rng = random.Random(f"{seed}:{module}")
        valid = sorted(rng.sample(valid, per_module), key=lambda m: m["idx"])
    return source, valid


def _prepare_workdir(workdir, module, mutated_source):
    if os.path.exists(workdir):
        shutil.rmtree(workdir)
    os.makedirs(workdir)
    shutil.copytree(os.path.join(REPO, PACKAGE), os.path.join(workdir, PACKAGE),
                    ignore=shutil.ignore_patterns("__pycache__"))
    with open(os.path.join(workdir, PACKAGE, module + ".py"), "w", encoding="utf-8") as f:
        f.write(mutated_source)
    for name in ("tests", "strategies"):
        os.symlink(os.path.join(REPO, name), os.path.join(workdir, name))


def run_one(mutant, source, workroot, timeout, test_args):
    mutated, _b, _a = apply_mutation(source, mutant["idx"], mutant["op"], mutant["extra"])
    workdir = os.path.join(workroot, mutant["id"].replace(":", "_"))
    _prepare_workdir(workdir, mutant["module"], mutated)
    env = dict(os.environ, PYTHONPATH="", QBT_FAST_TESTS="1", QBT_MUTANT=mutant["id"],
               PYTHONDONTWRITEBYTECODE="1")
    cmd = [sys.executable, "-m", "unittest"] + test_args
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(cmd, cwd=workdir, env=env, capture_output=True, text=True, timeout=timeout)
        status = "survived" if proc.returncode == 0 else "killed"
        detail = _first_failure(proc.stderr)
    except subprocess.TimeoutExpired:
        status, detail = "timeout", ""
    shutil.rmtree(workdir, ignore_errors=True)
    rec = dict(mutant)
    rec.update({"status": status, "detail": detail, "seconds": round(time.perf_counter() - t0, 2)})
    rec.pop("idx", None)
    rec.pop("extra", None)
    return rec


def baseline_timeout(test_args):
    """跑一次未突变的测试套件计时；死循环突变体超过 4 倍基线即判 timeout（计入抓住并单列）。"""
    env = dict(os.environ, PYTHONPATH="", QBT_FAST_TESTS="1", PYTHONDONTWRITEBYTECODE="1")
    t0 = time.perf_counter()
    proc = subprocess.run([sys.executable, "-m", "unittest"] + test_args, cwd=REPO, env=env,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit("baseline test run failed; fix the suite before mutating:\n" + proc.stderr[-2000:])
    return max(30.0, 4.0 * (time.perf_counter() - t0))


def _first_failure(stderr):
    for line in stderr.splitlines():
        if line.startswith(("FAIL: ", "ERROR: ")):
            return line[:160]
    tail = stderr.strip().splitlines()
    return tail[-1][:160] if tail else ""


def summarize(records):
    by_mod = {}
    for r in records:
        m = by_mod.setdefault(r["module"], {"total": 0, "killed": 0, "timeout": 0, "survived": 0})
        m["total"] += 1
        m[r["status"]] += 1
    total = sum(m["total"] for m in by_mod.values())
    caught = sum(m["killed"] + m["timeout"] for m in by_mod.values())
    return {"modules": by_mod, "total": total, "caught": caught,
            "survived": total - caught, "kill_rate": (100.0 * caught / total) if total else 0.0}


def write_outputs(records, out_json, out_md):
    records = sorted(records, key=lambda r: (r["module"], r["line"], r["id"]))
    summary = summarize(records)
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "mutants": records}, f, indent=1, sort_keys=True, ensure_ascii=False)
    with open(out_md, "w", encoding="utf-8") as f:
        f.write("# 突变测试漏网清单（survivors）\n\n")
        f.write(f"总数 {summary['total']}，抓住 {summary['caught']}，漏网 {summary['survived']}，"
                f"抓住率 {summary['kill_rate']:.1f}%\n\n")
        for r in records:
            if r["status"] == "survived":
                f.write(f"- `{r['id']}` 行 {r['line']} [{r['op']}]: `{r['before']}` -> `{r['after']}`\n")
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--module", nargs="*", default=None)
    ap.add_argument("--per-module", type=int, default=80)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    ap.add_argument("--timeout", type=float, default=0.0,
                    help="单个突变体的测试超时（秒）；0 = 自动：先跑一次未突变基线，取 max(30, 4*基线)")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--out", default=os.path.join(REPO, "mutants", "results.json"))
    ap.add_argument("--md", default=os.path.join(REPO, "mutants", "survivors.md"))
    ap.add_argument("--tests", nargs="*", default=["discover", "-s", "tests", "-p", "test_*.py"])
    args = ap.parse_args(argv)
    plan = {m: select_mutants(m, args.per_module, args.seed) for m in list_modules(args.module)}
    if args.list:
        for m, (_src, muts) in plan.items():
            print(f"{m:12s} candidates(valid, capped)={len(muts)}")
        return 0
    if args.timeout <= 0:
        args.timeout = baseline_timeout(args.tests)
        print(f"auto timeout = {args.timeout:.0f}s", flush=True)
    done = {}
    if args.resume and os.path.exists(args.out):
        with open(args.out, "r", encoding="utf-8") as f:
            done = {r["id"]: r for r in json.load(f)["mutants"]}
    records = list(done.values())
    todo = [(m, src) for _mod, (src, muts) in plan.items() for m in muts if m["id"] not in done]
    workroot = os.path.join(REPO, "mutants", "_work")
    os.makedirs(workroot, exist_ok=True)
    t0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(run_one, m, src, workroot, args.timeout, args.tests) for m, src in todo]
        for k, fut in enumerate(concurrent.futures.as_completed(futures), 1):
            rec = fut.result()
            records.append(rec)
            print(f"[{k}/{len(todo)}] {rec['status']:8s} {rec['id']} line {rec['line']} ({rec['seconds']}s) {rec['detail'][:60]}", flush=True)
            if k % 10 == 0:
                write_outputs(records, args.out, args.md)
    summary = write_outputs(records, args.out, args.md)
    print(json.dumps(summary, indent=1))
    print(f"elapsed {time.perf_counter() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
