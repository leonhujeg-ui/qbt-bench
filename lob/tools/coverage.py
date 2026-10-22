"""Statement coverage with sys.monitoring (Python 3.12+), no third-party code.

    python -m lob.tools.coverage [--json out.json] -- <unittest args...>

Statement lines are taken from the AST (every ast.stmt / except-handler
node's first line, minus docstrings and `global`/`nonlocal`, which produce
no line event).  Executed lines come from LINE events; each location
is disabled after its first hit so the overhead stays small.
"""
import ast
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = ('book.py', 'events.py', 'reference.py', 'replay.py', 'queue_model.py',
        'queue_collect.py', 'stress.py', 'invariants.py', 'compare.py',
        'common.py', 'cli.py', 'fuzz/fuzz.py', 'mutants/run_mutations.py')
SKIP_NODES = (ast.Global, ast.Nonlocal)


def statement_lines(path):
    with open(path) as fh:
        tree = ast.parse(fh.read(), path)
    lines = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.stmt, ast.ExceptHandler)) or isinstance(node, SKIP_NODES):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            continue
        lines.add(node.lineno)
    return lines


class Collector:
    def __init__(self, root=ROOT, tool_id=1):      # 1 = sys.monitoring.COVERAGE_ID
        self.root = root
        self.hits = {}
        self.TOOL = tool_id

    def _on_line(self, code, line):
        fn = code.co_filename
        if fn.startswith(self.root):
            self.hits.setdefault(fn, set()).add(line)
        return sys.monitoring.DISABLE

    def start(self):
        m = sys.monitoring
        m.use_tool_id(self.TOOL, 'lobcov')
        m.register_callback(self.TOOL, m.events.LINE, self._on_line)
        m.set_events(self.TOOL, m.events.LINE)

    def stop(self):
        m = sys.monitoring
        m.set_events(self.TOOL, 0)
        m.free_tool_id(self.TOOL)

    def report(self, files):
        rows = {}
        tot_s = tot_h = 0
        for path in files:
            stmts = statement_lines(path)
            hit = self.hits.get(path, set()) & stmts
            missed = sorted(stmts - hit)
            rel = os.path.relpath(path, self.root)
            rows[rel] = {'statements': len(stmts), 'executed': len(hit),
                         'percent': round(100.0 * len(hit) / len(stmts), 1) if stmts else 100.0,
                         'missed': missed}
            tot_s += len(stmts)
            tot_h += len(hit)
        return rows, (round(100.0 * tot_h / tot_s, 2) if tot_s else 100.0)


def measured_files():
    core = [os.path.join(ROOT, f) for f in CORE if os.path.exists(os.path.join(ROOT, f))]
    extra = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in ('data', '__pycache__', 'results', 'tests')]
        for f in filenames:
            p = os.path.join(dirpath, f)
            if f.endswith('.py') and p not in core and f != '__init__.py':
                extra.append(p)
    return core, sorted(extra)


def _forget_measured_modules():
    """`python -m lob.cli coverage` imports lob.events (and friends) before the collector
    starts, so their module-level statements would never be seen.  Drop every already
    imported measured module so the suite's imports re-execute them under monitoring."""
    core, extra = measured_files()
    paths = {os.path.abspath(p) for p in core + extra}
    for name, mod in list(sys.modules.items()):
        f = getattr(mod, '__file__', None)
        if f and os.path.abspath(f) in paths and name != __name__:
            del sys.modules[name]


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    out = None
    if argv and argv[0] == '--json':
        out = argv[1]
        argv = argv[2:]
    if argv and argv[0] == '--':
        argv = argv[1:]
    tmp_data = tempfile.mkdtemp(prefix='lobcov_data_')
    os.environ['LOB_DATA_DIR'] = tmp_data          # 流文件在临时目录重新生成：生成器路径每次都计入
    _forget_measured_modules()
    col = Collector()
    col.start()
    suite_argv = ['python -m unittest'] + (argv or ['discover', '-s', os.path.join(ROOT, 'tests'),
                                                    '-t', os.path.dirname(ROOT)])
    result = unittest.main(module=None, argv=suite_argv, exit=False).result
    col.stop()
    shutil.rmtree(tmp_data, ignore_errors=True)
    core, extra = measured_files()
    rows, total = col.report(core)
    rows_x, total_x = col.report(extra)
    print('\n%-32s %6s %6s %6s' % ('file', 'stmts', 'exec', '%'))
    for name, r in rows.items():
        print('%-32s %6d %6d %6.1f' % (name, r['statements'], r['executed'], r['percent']))
    print('%-32s %21.2f   <- core (target >= 90)' % ('TOTAL core', total))
    print('%-32s %21.2f   <- tools/extras' % ('TOTAL extras', total_x))
    summary = {'core_percent': total, 'extras_percent': total_x, 'files': rows,
               'extras': rows_x, 'tests_run': result.testsRun,
               'failures': len(result.failures), 'errors': len(result.errors),
               'skipped': len(result.skipped), 'argv': argv}
    if out:
        with open(out, 'w') as fh:
            json.dump(summary, fh, indent=1, sort_keys=True)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main())
