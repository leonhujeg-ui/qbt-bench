"""CLI commands end to end on tiny streams (stress / queue / fuzz / bench / demo)."""
import json
import os
import tempfile
import unittest

from lob import cli
from lob import events as E
from lob import stress


class CliTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.stream = os.path.join(cls.tmp, 'ev.bin')
        E.generate(E.GenParams(n_events=20000, n_symbols=2, seed=21), cls.stream)

    def test_stress_run_small(self):
        out = os.path.join(self.tmp, 'stress')
        res = stress.run(self.stream, out, check_every=500, engine_events=5000,
                         latency_events=3000, tracemalloc_events=2000, label='t', repeats=2)
        self.assertEqual(res['engine_only']['events'], 5000)
        self.assertEqual(len(res['engine_only']['runs']), 2)
        self.assertEqual(res['e2e']['events'], 20000)
        self.assertEqual(res['latency_ns']['events'], 3000)
        self.assertEqual(res['tracemalloc']['events'], 2000)
        self.assertGreater(res['snapshot']['cached_ns'], 0)
        self.assertTrue(os.path.exists(os.path.join(out, 'stress_t.json')))
        cli.main(['stress', '--stream', self.stream, '--out', out, '--engine-events', '2000',
                  '--label', 'c', '--json', os.path.join(self.tmp, 's.json')])
        with open(os.path.join(self.tmp, 's.json')) as fh:
            self.assertEqual(json.load(fh)['label'], 'c')

    def test_queue_collect_and_model_commands(self):
        probes = os.path.join(self.tmp, 'probes.csv')
        cli.main(['queue-collect', '--stream', self.stream, '--out', probes,
                  '--sample-every', '20', '--seed', '3'])
        self.assertTrue(os.path.exists(probes))
        qm = os.path.join(self.tmp, 'qm')
        cli.main(['queue-model', '--probes', probes, '--out', qm, '--horizons', '1', '2',
                  '--json', os.path.join(self.tmp, 'q.json')])
        with open(os.path.join(self.tmp, 'q.json')) as fh:
            res = json.load(fh)
        self.assertEqual(set(res['per_horizon']), {'1', '2'})
        self.assertTrue(os.path.exists(os.path.join(qm, 'reliability_h1.svg')))

    def test_fuzz_and_bench_commands(self):
        fz = os.path.join(self.tmp, 'fuzz.json')
        self.assertEqual(cli.main(['fuzz', '--n', '3000', '--seed', '5', '--json', fz]), 0)
        with open(fz) as fh:
            self.assertEqual(json.load(fh)['events'], 3000)
        be = os.path.join(self.tmp, 'bench.json')
        cli.main(['bench', '--ops', '2000', '--json', be])
        with open(be) as fh:
            self.assertIn('ns_per_op', json.load(fh))

    def test_demo_command(self):
        out = os.path.join(self.tmp, 'demo')
        cli.main(['demo', '--n', '20000', '--seed', '4', '--out', out])
        for name in ('events.bin', 'probes.csv', 'queue_model', 'replay', 'stress'):
            self.assertTrue(os.path.exists(os.path.join(out, name)), name)
