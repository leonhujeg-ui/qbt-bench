"""Differential tests: Book vs ReferenceBook, three scales.

LOB_TEST_SCALE=quick    -> 10k only (used by the mutation runner)
LOB_TEST_SCALE=standard -> 10k + 1M (default)
LOB_TEST_SCALE=full     -> 10k + 1M + 10M (sharded over processes)
"""
import json
import os
import tempfile
import unittest

from lob import events as E
from lob.compare import compare_stream, compare_sharded

SCALE = os.environ.get('LOB_TEST_SCALE', 'standard')
RESULTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'results')
DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')


def stream(n, symbols, seed, name):
    """Canonical streams live in lob/data (regenerated if missing)."""
    os.makedirs(DATA, exist_ok=True)
    path = os.path.join(DATA, name)
    if not os.path.exists(path) or not os.path.exists(E.meta_path(path)):
        E.generate(E.GenParams(n_events=n, n_symbols=symbols, seed=seed), path)
    return path


def save(name, summary):
    os.makedirs(RESULTS, exist_ok=True)
    with open(os.path.join(RESULTS, name), 'w') as fh:
        json.dump(summary, fh, indent=1, sort_keys=True)


class Compare10kTest(unittest.TestCase):
    def test_10k_lockstep_full_invariants_every_event(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, 'ev10k.bin')
        E.generate(E.GenParams(n_events=10000, n_symbols=4, seed=101), path)
        s = compare_stream(path, full_every=1, digest_every=50)
        self.assertIsNone(s['first_divergence'], s)
        self.assertEqual(s['events'], 10000)
        self.assertEqual(s['full_checks'], 10000 + 4)
        self.assertGreater(s['trades'], 0)
        save('compare_10k.json', s)

    def test_10k_each_stp_policy_and_small_lots(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, 'ev10k_b.bin')
        E.generate(E.GenParams(n_events=10000, n_symbols=2, seed=202, tick_size=5,
                               lot_size=10, min_qty=10, qty_mean_lots=3,
                               withdraw_prob=0.01, shock_prob=0.01, band=600,
                               aggr_ratio=0.3, bad_rate=0.05), path)
        s = compare_stream(path, full_every=1, digest_every=20)
        self.assertIsNone(s['first_divergence'], s)
        self.assertGreater(s['trades'], 0)


class Compare1MTest(unittest.TestCase):
    def test_1m_lockstep(self):
        if SCALE not in ('standard', 'full'):
            self.skipTest('LOB_TEST_SCALE=%s' % SCALE)
        path = stream(1_000_000, 8, 12345, 'events_1m.bin')
        s = compare_sharded(path, processes=4, full_every=1000, digest_every=1000)
        self.assertIsNone(s['first_divergence'], s)
        self.assertEqual(s['events'], 1_000_000)
        save('compare_1m.json', s)


class Compare10MTest(unittest.TestCase):
    def test_10m_sharded(self):
        if SCALE != 'full':
            self.skipTest('LOB_TEST_SCALE=%s (needs full)' % SCALE)
        path = stream(10_000_000, 8, 12345, 'events_10m.bin')
        s = compare_sharded(path, processes=4, full_every=2000, digest_every=2000)
        self.assertIsNone(s['first_divergence'], s)
        self.assertEqual(s['events'], 10_000_000)
        save('compare_10m.json', s)
