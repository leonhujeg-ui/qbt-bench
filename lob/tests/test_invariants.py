"""The checker must catch corrupted books (otherwise it proves nothing)."""
import unittest

from lob.book import Book, O_PRIO, O_VIS, O_SLOT, Q_DEAD
from lob.common import BookConfig
from lob.invariants import Checker, InvariantError
from lob.reference import ReferenceBook
from lob.tests.harness import N, C, run_events, to_record


class CheckerDetectsCorruptionTest(unittest.TestCase):
    def _book(self, engine=Book, evs=None):
        evs = evs or [N(1, 1, 99, 5), N(2, 1, 99, 7), N(3, 2, 101, 4), N(4, 2, 102, 3)]
        st, tr, b, a, book = run_events(engine, BookConfig(ref_price=100), evs)
        return book

    def test_clean_book_passes(self):
        for engine in (Book, ReferenceBook):
            book = self._book(engine)
            chk = Checker(full_every=1)
            chk.attach(book)
            chk.after_event(book, len(book.trades))
            chk.full_check(book)
            self.assertEqual(chk.full_checks, 2)

    def test_detects_crossed_book(self):
        book = self._book()
        book.bid_keys.append(105)
        book.bid_qty[105] = 1
        book.bid_queue[105] = [2, 0]
        chk = Checker()
        chk.attach(book)
        with self.assertRaises(InvariantError):
            chk.after_event(book, 0)

    def test_detects_conservation_break(self):
        for engine in (Book, ReferenceBook):
            book = self._book(engine)
            book.filled += 1
            chk = Checker()
            chk.attach(book)
            with self.assertRaises(InvariantError):
                chk.after_event(book, 0)

    def test_detects_level_sum_mismatch(self):
        book = self._book()
        book.bid_qty[99] += 1
        with self.assertRaises(InvariantError):
            Checker().full_check(book)

    def test_detects_priority_violation(self):
        book = self._book()
        o1, o2 = book.orders[1], book.orders[2]
        o1[O_PRIO], o2[O_PRIO] = o2[O_PRIO], o1[O_PRIO]
        with self.assertRaises(InvariantError):
            Checker().full_check(book)

    def test_detects_bad_slot_and_dead_count(self):
        book = self._book()
        book.orders[1][O_SLOT] = 7
        with self.assertRaises(InvariantError):
            Checker().full_check(book)
        book = self._book()
        book.bid_queue[99][Q_DEAD] = 3
        with self.assertRaises(InvariantError):
            Checker().full_check(book)

    def test_detects_unregistered_order_and_resting_counter(self):
        book = self._book()
        del book.orders[2]
        with self.assertRaises(InvariantError):
            Checker().full_check(book)
        book = self._book()
        book.resting += 1
        book.submitted += 1
        with self.assertRaises(InvariantError):
            Checker().full_check(book)

    def test_detects_unsorted_keys_and_bad_visible(self):
        book = self._book()
        book.ask_keys.reverse()
        with self.assertRaises(InvariantError):
            Checker().full_check(book)
        book = self._book()
        book.orders[3][O_VIS] = 0
        with self.assertRaises(InvariantError):
            Checker().full_check(book)

    def test_detects_self_trade_and_bad_trade_price(self):
        book = self._book()
        n = len(book.trades)
        chk = Checker()
        chk.attach(book)
        book.trades.append((5, 0, 0, 1, 9, 3, 101, 2, 7, 7))   # same trader
        with self.assertRaises(InvariantError):
            chk.after_event(book, n)
        book.trades[-1] = (5, 0, 0, 1, 9, 3, 100, 2, 7, 8)       # below best ask
        with self.assertRaises(InvariantError):
            chk.after_event(book, n)
        book.trades[-1] = (5, 0, 0, 2, 1, 9, 103, 2, 7, 8)       # sell above best bid
        with self.assertRaises(InvariantError):
            chk.after_event(book, n)
        book.trades[-1] = (5, 0, 0, 1, 9, 3, 101, 0, 7, 8)       # zero qty
        with self.assertRaises(InvariantError):
            chk.after_event(book, n)

    def test_detects_trade_above_limit_and_wrong_aggressor(self):
        book = self._book()
        n = len(book.trades)
        chk = Checker()
        chk.attach(book)
        rec = to_record(9, N(9, 1, 101, 2, 0, 7))
        book.trades.append((9, 0, 0, 1, 8, 3, 101, 2, 7, 8))     # aggressor id 8 != 9
        with self.assertRaises(InvariantError):
            chk.after_event(book, n, rec)
        book.trades[-1] = (9, 0, 0, 1, 9, 4, 102, 2, 7, 8)       # above limit 101
        with self.assertRaises(InvariantError):
            chk.after_event(book, n, rec)
        book.trades[-1] = (9, 0, 0, 1, 9, 3, 101, 2, 7, 8)       # fine
        chk.after_event(book, n, rec)
        self.assertEqual(chk.trades_checked, 1)

    def test_sweep_monotonicity(self):
        book = self._book()
        n = len(book.trades)
        chk = Checker()
        chk.attach(book)
        book.trades.append((9, 0, 0, 1, 9, 4, 102, 2, 7, 8))
        book.trades.append((9, 1, 0, 1, 9, 3, 101, 2, 7, 8))     # price went down
        with self.assertRaises(InvariantError):
            chk.after_event(book, n)

    def test_disabled_checker_is_silent(self):
        book = self._book()
        book.filled += 1
        chk = Checker(enabled=False)
        chk.attach(book)
        chk.after_event(book, 0)
        self.assertEqual(chk.events, 0)

    def test_reference_corruption_detected(self):
        book = self._book(ReferenceBook)
        book.rest[0].vis = 0
        with self.assertRaises(InvariantError):
            Checker().full_check(book)
        book = self._book(ReferenceBook)
        book.rest[0].prio = book.rest[1].prio
        with self.assertRaises(InvariantError):
            Checker().full_check(book)
        book = self._book(ReferenceBook)
        del book.by_id[1]
        with self.assertRaises(InvariantError):
            Checker().full_check(book)

    def test_checker_runs_through_cases_with_full_every_one(self):
        evs = [N(1, 1, 99, 5), N(2, 2, 101, 5), N(3, 1, 101, 3, 0, 7), C(1),
               N(4, 2, 0, 10, 1), N(5, 1, 100, 10, 16, 1, 3), N(6, 2, 100, 7)]
        for engine in (Book, ReferenceBook):
            chk = Checker(full_every=1)
            run_events(engine, BookConfig(ref_price=100), evs, checker=chk)
            self.assertEqual(chk.events, len(evs))
            self.assertGreater(chk.trades_checked, 0)
