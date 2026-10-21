"""Tooling: mutation generator/loader, coverage statement lines, lint, stress helpers."""
import os
import subprocess
import sys
import tempfile
import unittest

from lob import stress
from lob.mutants import run_mutations as RM
from lob.mutants import loader
from lob.tools import coverage as COV
from lob.tools import lint_limits as LL
from lob.tools import bench_structures as BS

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class MutationToolingTest(unittest.TestCase):
    def test_enough_mutants_and_all_compile(self):
        for target, (mod, path, _tests) in RM.TARGETS.items():
            ms = RM.make_mutants(path)
            self.assertGreaterEqual(len(ms), 80, target)
            ops = set(m['op'] for m in ms)
            self.assertTrue({'cmp', 'arith', 'const', 'negate', 'delete'} <= ops, ops)
            with open(path) as fh:
                original = fh.read()
            for m in ms[:40]:
                compile(m['source'], path, 'exec')
                self.assertNotEqual(m['source'], original)
            chosen = RM.sample_mutants(ms, 100, seed=1)
            self.assertEqual(len(chosen), min(100, len(ms)))
            self.assertEqual([m['id'] for m in chosen], sorted(m['id'] for m in chosen))
            self.assertEqual(RM.sample_mutants(ms, 10 ** 6, 1), ms)
            self.assertEqual(RM.sample_mutants(ms, 100, 1), RM.sample_mutants(ms, 100, 1))

    def test_apply_descriptions(self):
        import ast
        tree = ast.parse("def f(a, b):\n    if a < b and not a:\n        a += 1\n    return -a % 2\n")
        sites = RM._sites(tree)
        descs = [RM._apply(k, n, i) for k, n, i in sites if k not in ('brkcont', 'usub', 'delete')]
        self.assertIn('Lt -> LtE', descs)
        self.assertIn('And -> Or', descs)
        self.assertIn('aug Add -> Sub', descs)
        self.assertIn('negate condition', descs)
        self.assertIn('Mod -> FloorDiv', descs)

    def test_loader_installs_mutant_source(self):
        tmp = tempfile.mkdtemp()
        src = os.path.join(tmp, 'm.py')
        with open(src, 'w') as fh:
            fh.write('from lob.common import BookConfig\nMUTANT = 42\n')
        code = ('import lob.mutants.loader as L, sys\n'
                'L.install("lob.book", %r)\n'
                'import lob.book\nprint(lob.book.MUTANT, lob.book.BookConfig.__name__)\n' % src)
        out = subprocess.run([sys.executable, '-c', code], cwd=REPO, capture_output=True,
                             text=True)
        self.assertEqual(out.stdout.strip(), '42 BookConfig', out.stderr)
        with self.assertRaises(RuntimeError):
            loader.install('lob.common', src)        # already imported here

    def test_run_one_kills_an_obvious_mutant(self):
        path = RM.TARGETS['book'][1]
        ms = RM.make_mutants(path)
        broken = next(m for m in ms if m['op'] == 'const' and '2 * filled' in m['snippet'])
        RM.TARGETS['smoke'] = ('lob.book', path, ['lob.tests.test_cases'])
        try:
            r = RM.run_one('smoke', broken, timeout=300, tmpdir=tempfile.mkdtemp())
        finally:
            del RM.TARGETS['smoke']
        self.assertEqual(r['status'], 'killed', r)
        self.assertEqual(RM._by_op([r]), {'const': {'run': 1, 'killed': 1}})


class CoverageAndLintTest(unittest.TestCase):
    def test_statement_lines(self):
        tmp = tempfile.mkdtemp()
        p = os.path.join(tmp, 'x.py')
        with open(p, 'w') as fh:
            fh.write('"""doc"""\nimport os\n\ndef f(x):\n    """doc"""\n    global G\n'
                     '    if x:\n        return 1\n    else:\n        pass\n    try:\n'
                     '        y = 2\n    except ValueError:\n        y = 3\n    return y\n')
        self.assertEqual(COV.statement_lines(p), {2, 4, 7, 8, 10, 11, 12, 13, 14, 15})

    def test_lint_detects_long_function_and_file(self):
        tmp = tempfile.mkdtemp()
        p = os.path.join(tmp, 'long.py')
        with open(p, 'w') as fh:
            fh.write('def f():\n' + '    x = 1\n' * 61 + '\n' * 800)
        problems = LL.check_file(p)
        self.assertEqual(len(problems), 2)
        self.assertIn('f: 62 lines', problems[1])
        self.assertEqual(LL.check_file(os.path.join(LL.ROOT, 'common.py')), [])
        self.assertIn(os.path.join(LL.ROOT, 'book.py'), LL.python_files())

    def test_coverage_collector_records_lines(self):
        col = COV.Collector(root=COV.ROOT)
        col.start()
        try:
            from lob.common import flags_valid
            flags_valid(1)
        finally:
            col.stop()
        rows, total = col.report([os.path.join(COV.ROOT, 'common.py')])
        self.assertGreater(rows['common.py']['executed'], 0)
        self.assertGreaterEqual(total, 0.0)
        core, extra = COV.measured_files()
        self.assertIn(os.path.join(COV.ROOT, 'book.py'), core)


class StressHelpersTest(unittest.TestCase):
    def test_percentiles_and_best_of(self):
        p = stress.percentiles(list(range(1, 1001)))
        self.assertEqual((p['p50'], p['p99'], p['p99_9'], p['max']), (500, 990, 999, 1000))
        self.assertAlmostEqual(p['mean'], 500.5)
        self.assertEqual(stress.percentiles([7])['p99_9'], 7)
        calls = iter([{'events_per_second': 5}, {'events_per_second': 9}, {'events_per_second': 7}])
        best = stress.best_of(lambda: next(calls), 3)
        self.assertEqual((best['events_per_second'], best['runs']), (9, [5, 9, 7]))
        info = stress.machine_info()
        self.assertIn('python', info)

    def test_structure_benchmark_agreement(self):
        import random
        seed = 5
        ops = BS.workload(3000, 60, seed)
        structs = [BS.SortedList(), BS.LazyHeap(), BS.SkipList(), BS.DictScan()]
        for k in random.Random(seed).sample(range(100000, 100240), 60):
            for s in structs:
                s.insert(k)
        for op, k in ops:
            for s in structs:
                getattr(s, op)(k) if op in ('insert', 'remove') else getattr(s, op)()
            self.assertEqual(len(set(s.best() for s in structs)), 1, (op, k))
        res = BS.main(n_ops=2000, levels=(50,))
        self.assertIn('SortedList', res['ns_per_op']['50'])
