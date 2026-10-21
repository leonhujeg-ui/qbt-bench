"""Stage 3: replay outputs, per-second snapshots, sharded determinism."""
import array
import filecmp
import json
import os
import tempfile
import unittest

from lob import events as E
from lob import replay as R


class ReplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.path = os.path.join(cls.tmp, 'ev.bin')
        cls.meta = E.generate(E.GenParams(n_events=100000, n_symbols=6, seed=77,
                                          withdraw_prob=0.002, shock_prob=0.002),
                              cls.path)
        cls.single = os.path.join(cls.tmp, 'single')
        cls.stats = R.replay(cls.path, cls.single, check_every=500)

    def test_single_and_sharded_byte_identical(self):
        out = os.path.join(self.tmp, 'sharded')
        stats = R.replay_sharded(self.path, out, processes=3, check_every=500)
        for name in ('trades.bin', 'status.bin', 'snapshots.jsonl'):
            self.assertTrue(filecmp.cmp(os.path.join(self.single, name),
                                        os.path.join(out, name), shallow=False), name)
        for key in ('events', 'trades', 'volume', 'status_counts', 'books', 'snapshots'):
            self.assertEqual(self.stats[key], stats[key], key)
        self.assertEqual(stats['mode'], 'sharded')
        self.assertEqual(sorted(sum(stats['shards'], [])), list(range(6)))

    def test_status_and_trade_files_consistent(self):
        n = self.meta['n_events']
        with open(os.path.join(self.single, 'status.bin'), 'rb') as fh:
            status = fh.read()
        self.assertEqual(len(status), n)
        self.assertEqual(sum(self.stats['status_counts'].values()), n)
        trades = list(R.iter_trades(os.path.join(self.single, 'trades.bin')))
        self.assertEqual(len(trades), self.stats['trades'])
        self.assertEqual(sum(t[7] for t in trades), self.stats['volume'])
        keys = [(t[0], t[1]) for t in trades]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len(keys), len(set(keys)))
        filled_legs = sum(b['filled'] for b in self.stats['books'].values())
        self.assertEqual(filled_legs, 2 * self.stats['volume'])
        for t in trades:
            self.assertGreater(t[7], 0)
            self.assertIn(t[3], (1, 2))
            self.assertEqual(status[t[0] - 1] in (1, 2, 3), True)

    def test_snapshots_sorted_complete_and_uncrossed(self):
        with open(os.path.join(self.single, 'snapshots.jsonl')) as fh:
            snaps = [json.loads(line) for line in fh]
        self.assertEqual(len(snaps), self.stats['snapshots'])
        keys = [(s['t'], s['sym']) for s in snaps]
        self.assertEqual(keys, sorted(keys))
        per_sym = {}
        for s in snaps:
            per_sym.setdefault(s['sym'], []).append(s['t'])
            bids, asks = s['bids'], s['asks']
            self.assertLessEqual(len(bids), 10)
            self.assertLessEqual(len(asks), 10)
            self.assertEqual([b[0] for b in bids], sorted((b[0] for b in bids), reverse=True))
            self.assertEqual([a[0] for a in asks], sorted(a[0] for a in asks))
            if bids and asks:
                self.assertLess(bids[0][0], asks[0][0])
            for _p, q in bids + asks:
                self.assertGreater(q, 0)
        for sym, ts in per_sym.items():
            self.assertEqual(ts, list(range(1, len(ts) + 1)), sym)
        self.assertEqual(set(per_sym), set(range(6)))

    def test_timing_array_and_limit(self):
        timing = array.array('Q')
        out = os.path.join(self.tmp, 'timed')
        stats = R.replay(self.path, out, check_every=0, limit=5000, timing=timing)
        self.assertEqual(stats['events'], 5000)
        self.assertEqual(len(timing), 5000)
        self.assertTrue(all(t > 0 for t in timing))
        with open(os.path.join(out, 'status.bin'), 'rb') as fh:
            self.assertEqual(len(fh.read()), 5000)

    def test_symbol_subset_replay(self):
        out = os.path.join(self.tmp, 'subset')
        stats = R.replay(self.path, out, symbols=[1, 4], check_every=100)
        self.assertEqual(set(stats['books']), {'1', '4'})
        self.assertEqual(stats['events'], self.stats['books']['1']['events']
                         + self.stats['books']['4']['events'])
        self.assertEqual(stats['books']['1'], self.stats['books']['1'])
