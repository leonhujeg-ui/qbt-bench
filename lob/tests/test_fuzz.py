"""A reduced fuzz run as a unit test (full 200k run: python -m lob.fuzz.fuzz)."""
import unittest

from lob import events as E
from lob.common import BookConfig
from lob.fuzz.fuzz import FuzzGen, run


class FuzzTest(unittest.TestCase):
    def test_20k_no_crash_no_violation_no_divergence(self):
        res = run(n=20000, seed=7, bad_rate=0.3, full_every=50)
        self.assertEqual(res['crashes'], [])
        self.assertEqual(res['invariant_violations'], [])
        self.assertEqual(res['n_divergences'], 0)
        self.assertEqual(res['full_checks'], 20000 // 50 + 1)
        self.assertGreater(res['status_counts'].get('FILLED', 0), 0)
        self.assertGreater(res['trades_checked'], 0)
        for rej in ('REJ_QTY', 'REJ_PRICE', 'REJ_BAND', 'REJ_UNKNOWN', 'REJ_SIDE',
                    'REJ_KIND', 'REJ_FLAGS', 'REJ_DUP', 'REJ_DISPLAY', 'REJ_FOK',
                    'REJ_POST_ONLY'):
            self.assertGreater(res['status_counts'].get(rej, 0), 0, rej)

    def test_generator_is_reproducible_and_covers_classes(self):
        cfg = BookConfig(tick_size=5, lot_size=10, min_qty=10, ref_price=10000, band=400)
        ga, gb = FuzzGen(cfg, 3), FuzzGen(cfg, 3)
        a = [ga.next_event() for _ in range(2000)]
        b = [gb.next_event() for _ in range(2000)]
        self.assertEqual(a, b)
        recs = [E.RECORD.unpack(x) for x in a]
        self.assertTrue(any(r[9] < 0 for r in recs))                 # negative price
        self.assertTrue(any(r[10] <= 0 for r in recs))                # non-positive qty
        self.assertTrue(any(r[3] not in (1, 2, 3) for r in recs))     # bad kind
        self.assertTrue(any(r[4] not in (1, 2) for r in recs))        # bad side
        ts = [r[1] for r in recs]
        self.assertTrue(any(y < x for x, y in zip(ts, ts[1:])))       # regression

    def test_cancel_new_policy_and_no_lockstep(self):
        cfg = BookConfig(tick_size=1, lot_size=1, min_qty=1, max_qty=1000, band=50,
                         ref_price=500, stp='cancel_new')
        res = run(n=5000, seed=11, bad_rate=0.5, full_every=10, cfg=cfg, lockstep=False)
        self.assertEqual(res['crashes'], [])
        self.assertEqual(res['invariant_violations'], [])
