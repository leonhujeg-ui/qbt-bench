"""Stage 1+: run the hand-written cases against every engine."""
import unittest

from lob.reference import ReferenceBook
from lob.tests import cases
from lob.tests.harness import run_events

ENGINES = [ReferenceBook]
try:
    from lob.book import Book
    ENGINES.append(Book)
except ImportError:  # stage 1: the fast engine does not exist yet
    pass


class HandWrittenCasesTest(unittest.TestCase):
    def test_at_least_eight_cases_per_order_type(self):
        for name, group in cases.GROUPS.items():
            self.assertGreaterEqual(len(group), 8, name)
        self.assertGreaterEqual(len(cases.ALL_CASES), 100)
        names = [c.name for c in cases.ALL_CASES]
        self.assertEqual(len(names), len(set(names)))

    def _check(self, engine, case):
        st, trades, bids, asks, book = run_events(engine, case.config(), case.events)
        label = '%s / %s' % (engine.__name__, case.name)
        self.assertEqual(st, case.statuses, label + ' statuses')
        self.assertEqual(trades, case.trades, label + ' trades')
        self.assertEqual(bids, case.bids, label + ' bids')
        self.assertEqual(asks, case.asks, label + ' asks')
        self.assertEqual(book.submitted,
                         book.filled + book.resting + book.cancelled + book.rejected,
                         label + ' conservation')
        self.assertEqual(book.filled, 2 * sum(t[3] for t in trades), label + ' filled')
        self.assertEqual(book.resting, sum(o.rem for o in book.live_orders()),
                         label + ' resting')


def _make(engine, case):
    def test(self):
        self._check(engine, case)
    return test


for _engine in ENGINES:
    for _i, _case in enumerate(cases.ALL_CASES):
        _name = 'test_%s_%03d_%s' % (_engine.__name__, _i,
                                      ''.join(ch if ch.isalnum() else '_'
                                              for ch in _case.name))
        setattr(HandWrittenCasesTest, _name, _make(_engine, _case))


class CounterCasesTest(unittest.TestCase):
    """Counters that the level/trade expectations cannot see."""

    def _run(self, engine, evs, **cfg):
        case = cases.Case('tmp', evs, [], cfg=cfg)
        return run_events(engine, case.config(), evs)[4]

    def test_counters(self):
        N, C, M = cases.N, cases.C, cases.M
        for engine in ENGINES:
            b = self._run(engine, [N(1, 2, 101, 3), N(2, 1, 101, 5, cases.IOC)])
            self.assertEqual((b.filled, b.cancelled, b.resting), (6, 2, 0))
            b = self._run(engine, [N(1, 2, 101, 10, cases.ICE, 1, 3), C(1)])
            self.assertEqual((b.cancelled, b.resting), (10, 0))
            b = self._run(engine, [N(1, 2, 101, 5, 0, 3), N(2, 1, 101, 5, 0, 3)],
                          stp='cancel_old')
            self.assertEqual((b.stp_cancels, b.cancelled, b.resting), (1, 5, 5))
            b = self._run(engine, [N(1, 2, 101, 5, 0, 3), N(2, 1, 101, 5, 0, 3)])
            self.assertEqual((b.stp_cancels, b.cancelled, b.resting), (0, 5, 5))
            b = self._run(engine, [N(1, 2, 101, 5), M(1, 2), M(1, 9)])
            self.assertEqual((b.submitted, b.cancelled, b.resting), (12, 3, 9))
            b = self._run(engine, [N(1, 2, 101, 0), N(2, 1, 100, 5, cases.FOK)])
            self.assertEqual((b.rejected, b.submitted), (5, 5))
            b = self._run(engine, [N(1, 2, 101, 2), N(2, 1, 101, 2)])
            self.assertEqual(b.ref, 101)
            self.assertEqual(b.n_events, 2)
