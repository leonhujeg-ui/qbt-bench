"""AST-based mutation testing for book.py and queue_model.py.

Operators: comparison swap, arithmetic swap, augmented-assignment swap,
constant tweak, boolean and/or swap, condition negation, break/continue swap,
unary minus removal, statement deletion.  Every candidate site gets one
mutant; a deterministic (seeded) stratified sample of at most --max per file
is run (always >= 80).  A mutant is killed when the quick test suite fails,
errors, or times out.  Survivors are listed for manual classification.
"""
import ast
import concurrent.futures
import copy
import json
import os
import random
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(ROOT)
TARGETS = {
    'book': ('lob.book', os.path.join(ROOT, 'book.py'),
             ['lob.tests.test_cases', 'lob.tests.test_invariants', 'lob.tests.test_compare',
              'lob.tests.test_fuzz', 'lob.tests.test_replay', 'lob.tests.test_book',
              'lob.tests.test_boundaries', 'lob.tests.test_contract']),
    'queue_model': ('lob.queue_model', os.path.join(ROOT, 'queue_model.py'),
                    ['lob.tests.test_queue_model']),
}
CMP_SWAP = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt,
            ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Is: ast.IsNot, ast.IsNot: ast.Is}
ARITH_SWAP = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.FloorDiv,
              ast.FloorDiv: ast.Mult, ast.Mod: ast.FloorDiv}


def _in_functions(tree):
    inside = set()
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(fn):
                inside.add(id(node))
    return inside


def _sites(tree):
    """Enumerate (operator, node) mutation sites in AST walk order
    (only inside function bodies: module-level mutants are trivial)."""
    out = []
    inside = _in_functions(tree)
    for node in ast.walk(tree):
        if id(node) not in inside:
            continue
        if isinstance(node, ast.Compare):
            for i, op in enumerate(node.ops):
                if type(op) in CMP_SWAP:
                    out.append(('cmp', node, i))
        elif isinstance(node, ast.BinOp) and type(node.op) in ARITH_SWAP:
            out.append(('arith', node, 0))
        elif isinstance(node, ast.AugAssign) and type(node.op) in (ast.Add, ast.Sub):
            out.append(('aug', node, 0))
        elif isinstance(node, ast.Constant) and type(node.value) in (int, bool) \
                and not isinstance(node.value, bool) and 0 <= node.value < 1000:
            out.append(('const', node, 0))
        elif isinstance(node, ast.Constant) and isinstance(node.value, bool):
            out.append(('bool', node, 0))
        elif isinstance(node, ast.BoolOp):
            out.append(('boolop', node, 0))
        elif isinstance(node, (ast.If, ast.While)) and not _is_main_guard(node):
            out.append(('negate', node, 0))
        elif isinstance(node, (ast.Break, ast.Continue)):
            out.append(('brkcont', node, 0))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            out.append(('usub', node, 0))
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.Expr)) and _deletable(node):
            out.append(('delete', node, 0))
    return out


def _is_main_guard(node):
    return isinstance(node, ast.If) and isinstance(node.test, ast.Compare) and \
        isinstance(node.test.left, ast.Name) and node.test.left.id == '__name__'


def _deletable(node):
    if isinstance(node, ast.Expr):
        return isinstance(node.value, ast.Call)
    return True


def _apply(kind, node, idx):
    """Mutate `node` in place; return a short description."""
    if kind == 'cmp':
        old = node.ops[idx]
        node.ops[idx] = CMP_SWAP[type(old)]()
        return '%s -> %s' % (type(old).__name__, type(node.ops[idx]).__name__)
    if kind == 'arith':
        old = node.op
        node.op = ARITH_SWAP[type(old)]()
        return '%s -> %s' % (type(old).__name__, type(node.op).__name__)
    if kind == 'aug':
        old = node.op
        node.op = ast.Sub() if isinstance(old, ast.Add) else ast.Add()
        return 'aug %s -> %s' % (type(old).__name__, type(node.op).__name__)
    if kind == 'const':
        node.value = node.value + 1
        return 'const %d -> %d' % (node.value - 1, node.value)
    if kind == 'bool':
        node.value = not node.value
        return 'bool -> %s' % node.value
    if kind == 'boolop':
        old = node.op
        node.op = ast.Or() if isinstance(old, ast.And) else ast.And()
        return '%s -> %s' % (type(old).__name__, type(node.op).__name__)
    if kind == 'negate':
        node.test = ast.UnaryOp(op=ast.Not(), operand=node.test)
        return 'negate condition'
    if kind == 'brkcont':
        return 'break<->continue'
    if kind == 'usub':
        return 'drop unary minus'
    return 'delete statement'


class _Mutator(ast.NodeTransformer):
    def __init__(self, target_node, kind):
        self.target, self.kind = target_node, kind

    def visit(self, node):
        if node is self.target:
            if self.kind == 'brkcont':
                return ast.copy_location(ast.Continue() if isinstance(node, ast.Break)
                                         else ast.Break(), node)
            if self.kind == 'usub':
                return node.operand
            if self.kind == 'delete':
                return ast.copy_location(ast.Pass(), node)
        return self.generic_visit(node)


def make_mutants(path):
    with open(path) as fh:
        src = fh.read()
    base = ast.parse(src, path)
    sites = _sites(base)
    mutants = []
    for n, (kind, node, idx) in enumerate(sites):
        tree = copy.deepcopy(base)
        # locate the corresponding node in the copy by walk order
        node_c = [m for m in ast.walk(tree)][[m for m in ast.walk(base)].index(node)]
        if kind in ('brkcont', 'usub', 'delete'):
            desc = _apply(kind, node_c, idx)
            tree = _Mutator(node_c, kind).visit(tree)
        else:
            desc = _apply(kind, node_c, idx)
        ast.fix_missing_locations(tree)
        try:
            text = ast.unparse(tree)
            compile(text, path, 'exec')
        except (SyntaxError, ValueError, RecursionError):
            continue
        snippet = src.splitlines()[node.lineno - 1].strip()
        mutants.append({'id': n, 'op': kind, 'line': node.lineno, 'desc': desc,
                        'snippet': snippet[:100], 'source': text})
    return mutants


def sample_mutants(mutants, max_per_file, seed):
    if len(mutants) <= max_per_file:
        return mutants
    rng = random.Random(seed)
    by_op = {}
    for m in mutants:
        by_op.setdefault(m['op'], []).append(m)
    chosen = []
    quota = max(1, max_per_file // len(by_op))
    for op, ms in sorted(by_op.items()):
        chosen.extend(ms if len(ms) <= quota else rng.sample(ms, quota))
    rest = [m for m in mutants if m not in chosen]
    if len(chosen) < max_per_file and rest:
        chosen.extend(rng.sample(rest, min(len(rest), max_per_file - len(chosen))))
    elif len(chosen) > max_per_file:            # more operators than the budget
        chosen = rng.sample(chosen, max_per_file)
    return sorted(chosen, key=lambda m: m['id'])


def run_one(target, mutant, timeout, tmpdir):
    mod_name, _path, tests = TARGETS[target]
    src = os.path.join(tmpdir, 'mut_%s_%d.py' % (target, mutant['id']))
    with open(src, 'w') as fh:
        fh.write(mutant['source'])
    env = dict(os.environ, LOB_MUTANT_MODULE=mod_name, LOB_MUTANT_SRC=src,
               LOB_TEST_SCALE='quick', PYTHONDONTWRITEBYTECODE='1')
    t0 = time.perf_counter()
    try:
        proc = subprocess.run([sys.executable, '-m', 'lob.mutants.loader'] + tests, cwd=REPO,
                              env=env, capture_output=True, timeout=timeout)
        status = 'survived' if proc.returncode == 0 else 'killed'
        tail = proc.stderr.decode(errors='replace')[-300:]
    except subprocess.TimeoutExpired:
        status, tail = 'timeout', ''
    return {k: mutant[k] for k in ('id', 'op', 'line', 'desc', 'snippet')} | {
        'status': status, 'seconds': round(time.perf_counter() - t0, 1), 'tail': tail}


def run_target(target, max_per_file=120, seed=1, workers=4, timeout=300, out=None, ids=None, merge=False):
    """ids: re-run exactly these mutant ids (from a results JSON) instead of sampling."""
    _mod, path, _tests = TARGETS[target]
    mutants = make_mutants(path)
    if ids:
        by_id = {m['id']: m for m in mutants}
        missing = [i for i in ids if i not in by_id]
        if missing:
            raise SystemExit('unknown mutant ids for %s: %s' % (target, missing))
        chosen = [by_id[i] for i in ids]
    else:
        chosen = sample_mutants(mutants, max_per_file, seed)
    tmpdir = tempfile.mkdtemp(prefix='lobmut_')
    t0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(workers) as ex:
        results = list(ex.map(lambda m: run_one(target, m, timeout, tmpdir), chosen))
    killed = sum(r['status'] != 'survived' for r in results)
    summary = {'target': target, 'file': os.path.relpath(path, REPO), 'candidates': len(mutants),
               'seed': None if ids else seed, 'ids': list(ids) if ids else None,
               'tests': list(_tests), 'machine_note': 'seconds are wall-clock on the running machine',
               'run': len(results), 'killed': killed, 'survived': len(results) - killed,
               'timeouts': sum(r['status'] == 'timeout' for r in results),
               'kill_rate': round(100.0 * killed / len(results), 1) if results else 0.0,
               'seconds': round(time.perf_counter() - t0, 1),
               'by_operator': _by_op(results),
               'survivors': [r for r in results if r['status'] == 'survived'],
               'results': results}
    if out and ids and merge and os.path.exists(out):
        summary = merge_into(out, summary)
    if out:
        with open(out, 'w') as fh:
            json.dump(summary, fh, indent=1, sort_keys=True)
    return summary


def merge_into(out, part):
    """--ids --merge: replace the re-run mutants inside an existing results JSON and
    recompute its counters (the sampled set, seed and candidates stay those of the base run)."""
    with open(out) as fh:
        base = json.load(fh)
    new = {r['id']: r for r in part['results']}
    results = [new.get(r['id'], r) for r in base['results']]
    killed = sum(r['status'] != 'survived' for r in results)
    base.update({'results': results, 'killed': killed, 'survived': len(results) - killed,
                 'timeouts': sum(r['status'] == 'timeout' for r in results),
                 'kill_rate': round(100.0 * killed / len(results), 1) if results else 0.0,
                 'by_operator': _by_op(results), 'survivors': [r for r in results if r['status'] == 'survived'],
                 'reruns': (base.get('reruns') or []) + [{'ids': part['ids'], 'seconds': part['seconds'],
                                                          'tests': part['tests']}]})
    return base


def _by_op(results):
    out = {}
    for r in results:
        d = out.setdefault(r['op'], {'run': 0, 'killed': 0})
        d['run'] += 1
        d['killed'] += r['status'] != 'survived'
    return out


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--target', choices=sorted(TARGETS), default='book')
    ap.add_argument('--max', type=int, default=120)
    ap.add_argument('--seed', type=int, default=1)
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--timeout', type=int, default=300)
    ap.add_argument('--json', default=None)
    ap.add_argument('--ids', type=int, nargs='*', default=None,
                    help='re-run exactly these mutant ids (as listed in a results JSON) instead of sampling')
    ap.add_argument('--merge', action='store_true',
                    help='with --ids: replace those mutants inside the existing --json file and recompute its counters')
    args = ap.parse_args(argv)
    s = run_target(args.target, args.max, args.seed, args.workers, args.timeout, args.json, ids=args.ids,
                   merge=args.merge)
    print(json.dumps({k: v for k, v in s.items() if k != 'results'}, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
