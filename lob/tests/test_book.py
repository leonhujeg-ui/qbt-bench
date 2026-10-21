"""Book internals: snapshot caching, compaction, digests, workers, CLI."""
import json
import os
import random
import tempfile
import unittest

from lob import cli
from lob import compare as CMP
from lob import events as E
from lob import replay as R
from lob.book import Book, O_VIS, O_PRIO, Q_HEAD, Q_DEAD, Q_FIRST, COMPACT_MIN_DEAD, COMPACT_MIN_HEAD
from lob.common import BookConfig
from lob.invariants import Checker
from lob.tests.harness import N, C, M, to_record, run_events


def expected_top(book, depth=10):
    bids, asks = {}, {}
    for o in book.orders.values():
        d = bids if o[1] == 1 else asks
        d[o[2]] = d.get(o[2], 0) + o[O_VIS]
    bp = sorted(bids, reverse=True)[:depth]
    ap = sorted(asks)[:depth]
    return (bp, [bids[p] for p in bp], ap, [asks[p] for p in ap])


class SnapshotTest(unittest.TestCase):
    def test_top_matches_recomputation_after_every_event(self):
        rng = random.Random(4)
        book = Book(BookConfig(ref_price=1000, band=100))
        chk = Checker(full_every=1)
        chk.attach(book)
        oid = 1
        live = []
        for seq in range(1, 4001):
            r = rng.random()
            if r < 0.55 or not live:
                side = rng.choice((1, 2))
                price = 1000 + (rng.randint(-8, 0) if side == 1 else rng.randint(1, 9))
                if rng.random() < 0.15:
                    price = 1000 + (rng.randint(1, 4) if side == 1 else rng.randint(-3, 0))
                flags = rng.choice((0, 0, 0, 2, 4, 8, 16, 1))
                qty = rng.randint(1, 20)
                aux = max(1, qty // 3) if flags == 16 else 0
                ev = N(oid, side, 0 if flags == 1 else price, qty, flags, rng.randint(1, 4), aux)
                live.append(oid)
                oid += 1
            elif r < 0.85:
                ev = C(live.pop(rng.randrange(len(live))))
            else:
                ev = M(rng.choice(live), rng.randint(1, 25))
            n0 = len(book.trades)
            book.process(to_record(seq, ev))
            chk.after_event(book, n0)
            d = rng.choice((10, 10, 3, 1))
            self.assertEqual(book.top(d), expected_top(book, d), seq)
            self.assertEqual(book.top(d), book.top(d))          # cached path
            self.assertEqual(book.levels(), (list(zip(*expected_top(book, 10 ** 6)[:2])),
                                             list(zip(*expected_top(book, 10 ** 6)[2:]))))
        self.assertGreater(len(book.trades), 0)

    def test_cached_snapshot_is_same_object_until_mutation(self):
        _st, _tr, _b, _a, book = run_events(Book, BookConfig(ref_price=100),
                                            [N(1, 1, 99, 5), N(2, 2, 101, 7)])
        s1 = book.top(10)
        self.assertIs(book.top(10), s1)
        book.process(to_record(3, M(1, 3)))
        s2 = book.top(10)
        self.assertIsNot(s2, s1)
        self.assertEqual(s2, ([99], [3], [101], [7]))
        self.assertEqual(book.top(0), ([], [], [], []))
        self.assertEqual(book.levels(1), ([(99, 3)], [(101, 7)]))

    def test_empty_book_helpers(self):
        book = Book(BookConfig())
        self.assertIsNone(book.best_bid())
        self.assertIsNone(book.best_ask())
        self.assertEqual(book.levels(), ([], []))
        self.assertEqual(book.live_orders(), [])
        self.assertEqual(book.resting_check(), 0)
        self.assertEqual(book.top(), ([], [], [], []))


class CompactionTest(unittest.TestCase):
    def test_cancel_compaction_keeps_fifo_and_slots(self):
        book = Book(BookConfig(ref_price=100))
        chk = Checker(full_every=1)
        chk.attach(book)
        n = 3 * COMPACT_MIN_DEAD
        for i in range(1, n + 1):
            book.process(to_record(i, N(i, 2, 101, 1)))
        queue = book.ask_queue[-101]
        self.assertEqual(len(queue), Q_FIRST + n)
        seq = n + 1
        for i in range(1, n + 1, 2):                  # cancel every other order
            book.process(to_record(seq, C(i)))
            chk.after_event(book, len(book.trades))
            seq += 1
        self.assertLess(len(queue), Q_FIRST + n)        # compacted at least once
        survivors = [o[0] for o in queue[queue[Q_HEAD]:] if o is not None]
        self.assertEqual(survivors, list(range(2, n + 1, 2)))
        for i, o in enumerate(queue[Q_FIRST:], Q_FIRST):
            if o is not None:
                self.assertEqual(o[8], i)
        book.process(to_record(seq, N(seq, 1, 101, 3)))
        chk.after_event(book, 0)
        self.assertEqual([t[5] for t in book.trades], [2, 4, 6])

    def _step(self, book, chk, seq, ev):
        n0 = len(book.trades)
        st = book.process(to_record(seq, ev))
        chk.after_event(book, n0)
        return st

    def test_head_compaction_after_many_fills(self):
        book = Book(BookConfig(ref_price=100))
        chk = Checker(full_every=1)
        chk.attach(book)
        seq = 1
        for i in range(COMPACT_MIN_HEAD + 10):
            self._step(book, chk, seq, N(seq, 2, 101, 1))
            seq += 1
        queue = book.ask_queue[-101]
        for i in range(COMPACT_MIN_HEAD + 5):
            self._step(book, chk, seq, N(seq, 1, 101, 1))
            seq += 1
        self.assertLess(queue[Q_HEAD], COMPACT_MIN_HEAD)
        self.assertEqual(len(book.orders), 5)
        self.assertEqual(len(book.trades), COMPACT_MIN_HEAD + 5)

    def test_iceberg_refresh_then_compaction(self):
        book = Book(BookConfig(ref_price=100))
        chk = Checker(full_every=1)
        chk.attach(book)
        seq = 1
        self._step(book, chk, seq, N(1, 2, 101, 500, 16, 7, 2))
        seq += 1
        for _ in range(COMPACT_MIN_HEAD + 5):
            self._step(book, chk, seq, N(seq, 1, 101, 2, 0, 9))
            seq += 1
        self.assertEqual(book.orders[1][3], 500 - 2 * (COMPACT_MIN_HEAD + 5))
        self.assertEqual(book.top(1), ([], [], [101], [2]))
        self.assertLess(book.ask_queue[-101][Q_HEAD], COMPACT_MIN_HEAD)


class CompactionPolicyTest(unittest.TestCase):
    """Compaction must happen exactly at the documented thresholds."""

    def _asks(self, n):
        book = Book(BookConfig(ref_price=100))
        for i in range(1, n + 1):
            book.process(to_record(i, N(i, 2, 101, 1)))
        return book, book.ask_queue[-101], n + 1

    def test_no_premature_head_compaction(self):
        book, queue, seq = self._asks(70)
        for _ in range(5):
            book.process(to_record(seq, N(seq, 1, 101, 1)))
            seq += 1
        self.assertEqual(queue[Q_HEAD], Q_FIRST + 5)
        self.assertEqual(len(queue), Q_FIRST + 70)

    def test_unlink_compaction_thresholds(self):
        # (i) small level: two increases of the same order, no compaction
        book, queue, seq = self._asks(2)
        for q in (3, 4):
            book.process(to_record(seq, M(1, q)))
            seq += 1
        self.assertEqual((queue[Q_DEAD], len(queue)), (2, Q_FIRST + 4))
        # (ii) exactly COMPACT_MIN_DEAD dead slots and dead*2 == entries -> compaction
        book, queue, seq = self._asks(COMPACT_MIN_DEAD)
        for i in range(1, COMPACT_MIN_DEAD + 1):
            book.process(to_record(seq, M(i, 2)))
            seq += 1
            if i < COMPACT_MIN_DEAD:
                self.assertEqual(queue[Q_DEAD], i)
        self.assertEqual(queue[Q_DEAD], 0)
        self.assertNotIn(None, queue[Q_FIRST:])
        self.assertEqual([o[0] for o in queue[Q_FIRST:]], list(range(1, COMPACT_MIN_DEAD + 1)))
        # (iii) dead*2 below the entry count: no compaction yet
        book, queue, seq = self._asks(100)
        for i in range(1, 51):
            book.process(to_record(seq, M(i, 2)))
            seq += 1
        self.assertEqual((queue[Q_DEAD], len(queue)), (50, Q_FIRST + 150))
        Checker().full_check(book)


class MiscBookTest(unittest.TestCase):
    def test_initial_state(self):
        book = Book(BookConfig())
        self.assertEqual((book.seq, book.n_events, book.prio, book.ref), (0, 0, 0, 1000))
        self.assertTrue(book.dirty and book.keys_dirty)
        self.assertEqual(book.top(0), ([], [], [], []))
        self.assertEqual(book.top(10), ([], [], [], []))
        self.assertEqual(book.top(0), ([], [], [], []))
        self.assertEqual((book.submitted, book.filled, book.cancelled, book.rejected,
                          book.resting, book.stp_cancels, book.ts_regressions), (0,) * 7)


    def test_digest_and_ts_regressions(self):
        evs = [N(1, 1, 99, 5), N(2, 2, 101, 7), N(3, 1, 101, 2)]
        b1 = run_events(Book, BookConfig(ref_price=100), evs)[4]
        b2 = run_events(Book, BookConfig(ref_price=100), evs)[4]
        self.assertEqual(b1.digest(), b2.digest())
        b2.process(to_record(4, C(1)))
        self.assertNotEqual(b1.digest(), b2.digest())
        rec = list(to_record(5, N(9, 1, 98, 1)))
        rec[1] = 0
        b2.process(tuple(rec))
        self.assertEqual(b2.ts_regressions, 1)
        self.assertEqual(b2.n_events, 5)
        self.assertEqual([o[0] for o in b2.live_orders()], [2, 9])

    def test_config_validation(self):
        for kw in ({'tick_size': 0}, {'lot_size': 0}, {'min_qty': 0}, {'band': -1},
                   {'lot_size': 10, 'min_qty': 5}, {'max_qty': 0}, {'ref_price': 0},
                   {'stp': 'nope'}):
            with self.assertRaises(ValueError, msg=kw):
                BookConfig(**kw)
        cfg = BookConfig(tick_size=5, symbol=3)
        self.assertEqual(BookConfig.from_dict(cfg.to_dict(), symbol=3).to_dict(), cfg.to_dict())


class WorkersAndCliTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.path = os.path.join(cls.tmp, 'ev.bin')
        cls.meta = E.generate(E.GenParams(n_events=20000, n_symbols=3, seed=31), cls.path)

    def test_shard_worker_in_process(self):
        import multiprocessing
        R._SHARED['status'] = multiprocessing.RawArray('B', self.meta['n_events'])
        try:
            part = R._shard_worker((self.path, [1], self.tmp, 0, 500, E.NS, 10, None))
        finally:
            R._SHARED.clear()
        self.assertEqual(part['symbols'], [1])
        self.assertTrue(os.path.exists(part['trades_path']))
        self.assertEqual(set(part['books']), {'1'})
        os.remove(part['trades_path'])

    def test_compare_worker_in_process(self):
        s = CMP._worker((self.path, [2], 100, 100, 5000))
        self.assertIsNone(s['first_divergence'])
        self.assertGreater(s['events'], 0)

    def test_divergence_is_reported(self):
        class Broken(CMP.Book):
            def process(self, rec):
                st = super().process(rec)
                return 99 if self.n_events == 7 else st
        orig = CMP.Book
        CMP.Book = Broken
        try:
            s = CMP.compare_stream(self.path, symbols=[0], limit=200)
        finally:
            CMP.Book = orig
        self.assertEqual(s['first_divergence']['what'], 'status')

    def test_cli_commands(self):
        out = os.path.join(self.tmp, 'cli_ev.bin')
        self.assertFalse(cli.main(['gen', '--n', '3000', '--symbols', '2', '--seed', '5',
                                   '--out', out]))
        self.assertTrue(os.path.exists(E.meta_path(out)))
        rep = os.path.join(self.tmp, 'rep')
        cli.main(['replay', '--stream', out, '--out', rep, '--check', '100',
                  '--json', os.path.join(self.tmp, 'rep.json')])
        with open(os.path.join(self.tmp, 'rep.json')) as fh:
            self.assertEqual(json.load(fh)['events'], 3000)
        cli.main(['replay', '--stream', out, '--out', rep + '2', '--processes', '2'])
        self.assertEqual(cli.main(['compare', '--stream', out, '--check', '100']), 0)
        self.assertEqual(cli.main(['compare', '--stream', out, '--processes', '2',
                                   '--limit', '1000']), 0)
