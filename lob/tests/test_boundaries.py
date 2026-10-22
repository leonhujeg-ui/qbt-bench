"""Boundary criteria, one per entry point (CONTRACT.md section 7, rows E01..E35).

Rule: a raw exception is a failure.  The only controlled outcomes are a REJ_*
status, a ValueError listed in the error table, or an outcome the table spells
out (FileNotFoundError for a missing file, KeyError for an unknown GenParams
name, documented silence).  Every test carries a `# contract: Exx` tag;
test_contract checks the tags and the table agree one to one.
"""
import json
import os
import tempfile
import unittest

from lob import cli
from lob import compare as CMP
from lob import events as E
from lob import queue_model as Q
from lob import replay as R
from lob.book import Book
from lob.common import BookConfig, STATUS_NAMES
from lob.invariants import Checker, InvariantError
from lob.queue_collect import ProbeCollector, FIELDS
from lob.reference import ReferenceBook
from lob.tests.harness import N, C, M, RAW, to_record

B, S = 1, 2
MK, IOC, FOK, PO, ICE = 1, 2, 4, 8, 16
I64 = 2 ** 63 - 1
U64 = 2 ** 64 - 1
TS = E.EPOCH_NS


def run_both(evs, **cfg_kw):
    """Feed evs to both engines; {engine: ([status names], book)}.  The full
    invariant scan afterwards proves a rejection left the book consistent."""
    base = dict(ref_price=100, band=50)
    base.update(cfg_kw)
    out = {}
    for cls in (Book, ReferenceBook):
        book = cls(BookConfig(**base))
        names = [STATUS_NAMES[book.process(to_record(i + 1, ev))] for i, ev in enumerate(evs)]
        Checker().full_check(book)
        out[cls.__name__] = (names, book)
    return out


def rec(seq, symbol, kind, side, flags, trader, oid, price, qty, aux=0, ts=None):
    return E.pack(seq, TS + seq if ts is None else ts, symbol, kind, side, flags, trader,
                  oid, price, qty, aux)


def hand_stream(tmp, name, records, n_symbols=1, n_events=None):
    """Write raw records plus a meta file of the shape events.generate writes."""
    path = os.path.join(tmp, name)
    with open(path, 'wb') as fh:
        fh.write(b''.join(records))
    params = E.GenParams(n_events=len(records), n_symbols=n_symbols)
    meta = {'params': params.to_dict(), 'record_size': E.RECORD_SIZE,
            'n_events': len(records) if n_events is None else n_events,
            'sha256': E.file_sha256(path), 'last_oid': 0, 'epoch_ns': E.EPOCH_NS,
            'counts': {'new': 0, 'cancel': 0, 'modify': 0, 'bad': 0}, 'regimes': [],
            'books': {str(i): E.book_config_for(params, i) for i in range(n_symbols)}}
    with open(E.meta_path(path), 'w') as fh:
        json.dump(meta, fh)
    return path


class EngineBoundaryTest(unittest.TestCase):
    """Book.process / ReferenceBook.process: every malformed input is a REJ_* status."""

    def both(self, evs, expected, **cfg_kw):
        res = run_both(evs, **cfg_kw)
        for name, (statuses, _book) in res.items():
            self.assertEqual(statuses, expected, name)
        return res

    def test_new_qty_zero_negative_huge_or_off_lot(self):            # contract: E01
        for qty in (0, -5, I64, 10 ** 9 + 1):
            self.both([N(1, B, 100, qty)], ['REJ_QTY'])
        self.both([N(1, B, 100, 15)], ['REJ_QTY'], lot_size=10, min_qty=10)
        res = self.both([N(1, B, 100, 7)], ['REJ_QTY'], max_qty=6)
        for _name, (_st, book) in res.items():
            self.assertEqual((book.submitted, book.rejected, book.resting), (7, 7, 0))

    def test_new_price_zero_negative_or_off_tick(self):              # contract: E02
        for price in (0, -100, -(2 ** 63)):
            self.both([N(1, B, price, 5)], ['REJ_PRICE'])
        self.both([N(1, B, 102, 5)], ['REJ_PRICE'], tick_size=5)

    def test_new_price_outside_band(self):                           # contract: E03
        for price in (151, I64):
            self.both([N(1, B, price, 5)], ['REJ_BAND'])
        self.both([N(1, S, 49, 5)], ['REJ_BAND'])
        self.both([N(1, B, 100, 5), N(2, B, 101, 5), N(3, S, 99, 5)],
                  ['RESTED', 'REJ_BAND', 'REJ_BAND'], band=0)

    def test_new_duplicate_oid(self):                                # contract: E04
        self.both([N(1, B, 100, 5), N(1, S, 101, 5)], ['RESTED', 'REJ_DUP'])
        self.both([N(1, B, 100, 5), N(1, B, 100, 0)], ['RESTED', 'REJ_DUP'])

    def test_unknown_kind_leaves_counters_untouched(self):           # contract: E05
        for kind in (0, 9, 255):
            res = self.both([RAW(kind, B, 0, 1, 1, 100, 5)], ['REJ_KIND'])
            for _name, (_st, book) in res.items():
                self.assertEqual((book.submitted, book.rejected, book.n_events), (0, 0, 1))

    def test_unknown_side(self):                                     # contract: E06
        for side in (0, 3, 255):
            self.both([RAW(1, side, 0, 1, 1, 100, 5)], ['REJ_SIDE'])

    def test_unknown_flag_bits_and_forbidden_combinations(self):     # contract: E07
        for flags in (32, 64, 128, 255, MK | PO, MK | ICE, IOC | FOK, IOC | PO, FOK | PO):
            self.both([N(1, B, 100, 5, flags)], ['REJ_FLAGS'])

    def test_cancel_or_modify_unknown_oid(self):                     # contract: E08
        res = self.both([C(7), M(7, 5)], ['REJ_UNKNOWN', 'REJ_UNKNOWN'])
        for _name, (_st, book) in res.items():
            self.assertEqual((book.submitted, book.cancelled, book.n_events), (0, 0, 2))
        self.both([N(1, S, 101, 3), N(2, B, 101, 3), C(1), M(1, 2)],
                  ['RESTED', 'FILLED', 'REJ_UNKNOWN', 'REJ_UNKNOWN'])
        self.both([N(1, B, 100, 5), C(1), C(1), M(1, 4)],
                  ['RESTED', 'ACK_CANCEL', 'REJ_UNKNOWN', 'REJ_UNKNOWN'])

    def test_modify_bad_qty_leaves_book_and_counters(self):          # contract: E09
        for qty in (0, -1, I64, 10 ** 9 + 1):
            res = self.both([N(1, B, 100, 5), M(1, qty)], ['RESTED', 'REJ_QTY'])
            for _name, (_st, book) in res.items():
                self.assertEqual(book.levels(), ([(100, 5)], []))
                self.assertEqual((book.submitted, book.cancelled, book.resting), (5, 0, 5))
        self.both([N(1, B, 100, 20), M(1, 15)], ['RESTED', 'REJ_QTY'], lot_size=10, min_qty=10)

    def test_fok_that_cannot_fill_leaves_book(self):                 # contract: E10
        self.both([N(1, B, 100, 5, FOK)], ['REJ_FOK'])
        self.both([N(1, B, 0, 5, MK | FOK)], ['REJ_FOK'])
        for evs in ([N(1, S, 101, 3), N(2, B, 101, 5, FOK)],       # not enough
                    [N(1, S, 101, 3), N(2, B, 100, 3, FOK)]):      # limit below the ask
            res = self.both(evs, ['RESTED', 'REJ_FOK'])
            for _name, (_st, book) in res.items():
                self.assertEqual(book.levels(), ([], [(101, 3)]))
                self.assertEqual(book.trades, [])

    def test_post_only_that_would_cross(self):                       # contract: E11
        for price in (101, 105):
            self.both([N(1, S, 101, 3), N(2, B, price, 5, PO)], ['RESTED', 'REJ_POST_ONLY'])
        self.both([N(1, B, 100, 3), N(2, S, 100, 5, PO)], ['RESTED', 'REJ_POST_ONLY'])
        self.both([N(1, S, 101, 3), N(2, B, 100, 5, PO)], ['RESTED', 'RESTED'])

    def test_iceberg_display_zero_negative_or_huge(self):            # contract: E12
        for aux in (0, -3, 6, I64):
            self.both([N(1, B, 100, 5, ICE, 1, aux)], ['REJ_DISPLAY'])
        self.both([N(1, B, 100, 20, ICE, 1, 15)], ['REJ_DISPLAY'], lot_size=10, min_qty=10)

    def test_market_without_liquidity_is_cancelled_not_an_error(self):   # contract: E13
        self.both([N(1, B, 0, 5, MK)], ['CANCELLED'])
        self.both([N(1, S, 0, 5, MK)], ['CANCELLED'])
        self.both([N(1, S, 101, 3), N(2, B, I64, 8, MK)], ['RESTED', 'PARTIAL_CANCELLED'])
        self.both([N(1, S, 101, 3), N(2, B, 0, 10 ** 6, MK)], ['RESTED', 'PARTIAL_CANCELLED'],
                  max_qty=10 ** 6)

    def test_extreme_ids_traders_and_timestamp_regression(self):     # contract: E14
        self.both([N(0, B, 100, 5), C(0)], ['RESTED', 'ACK_CANCEL'])
        self.both([N(U64, B, 100, 5), M(U64, 3), M(U64, 9)], ['RESTED', 'ACK_MODIFY', 'ACK_MODIFY'])
        self.both([N(1, S, 101, 3, 0, 65535), N(2, B, 101, 5, 0, 65535)], ['RESTED', 'CANCELLED'])
        res = self.both([N(1, S, 101, 3, 0, 65535), N(2, B, 101, 5, 0, 65535)],
                        ['RESTED', 'RESTED'], stp='cancel_old')      # old one cancelled, new rests
        for _name, (_st, book) in res.items():
            self.assertEqual((book.stp_cancels, book.levels()), (1, ([(101, 5)], [])))
        book = Book(BookConfig(ref_price=100, band=50))
        late = list(to_record(1, N(1, B, 100, 5)))
        late[E.F_TS] = 2 * 10 ** 18          # later than EPOCH_NS + anything
        self.assertEqual(book.process(tuple(late)), 0)
        self.assertEqual(book.process(to_record(2, N(2, B, 99, 5))), 0)
        self.assertEqual(book.ts_regressions, 1)


class ConfigAndFormatBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_book_config_invalid_combinations(self):                 # contract: E15
        for kw in ({'tick_size': 0}, {'tick_size': -1}, {'lot_size': 0}, {'min_qty': 0},
                   {'band': -1}, {'lot_size': 10, 'min_qty': 5}, {'max_qty': 0},
                   {'max_qty': 5, 'min_qty': 10}, {'ref_price': 0}, {'stp': 'nope'},
                   {'stp': None}):
            with self.assertRaises(ValueError, msg=kw):
                BookConfig(**kw)
        self.assertEqual(BookConfig(max_qty=I64, band=I64, ref_price=I64).max_qty, I64)

    def test_book_config_from_dict_missing_key(self):                # contract: E16
        with self.assertRaises(ValueError):
            BookConfig.from_dict({'tick_size': 1})
        with self.assertRaises(ValueError):
            BookConfig.from_dict({})
        full = BookConfig().to_dict()
        del full['symbol']
        self.assertEqual(BookConfig.from_dict(full, symbol=2).symbol, 2)

    def test_pack_field_out_of_range(self):                          # contract: E17
        for args in ((-1, 0, 0, 1, 1, 0, 0, 1, 1, 1), (1, 0, 0, 1, 1, 0, 0, 1, 2 ** 63, 1),
                     (1, 0, 70000, 1, 1, 0, 0, 1, 1, 1), (1, 0, 0, 300, 1, 0, 0, 1, 1, 1),
                     (1, 0, 0, 1, 1, 256, 0, 1, 1, 1), (1, 0, 0, 1, 1, 0, 70000, 1, 1, 1),
                     (1, 0, 0, 1, 1, 0, 0, 2 ** 64, 1, 1), (1, 0, 0, 1, 1, 0, 0, 1, 1, -(2 ** 63) - 1)):
            with self.assertRaises(ValueError, msg=args):
                E.pack(*args)
        self.assertEqual(len(E.pack(U64, U64, 65535, 255, 255, 255, 65535, U64, I64, -(2 ** 63), I64)),
                         E.RECORD_SIZE)

    def test_iter_chunks_negative_bounds(self):                      # contract: E18
        path = os.path.join(self.tmp, 'one.bin')
        with open(path, 'wb') as fh:
            fh.write(rec(1, 0, 1, B, 0, 1, 1, 100, 5))
        for start, stop in ((-1, None), (0, -1), (-5, -1)):
            with self.assertRaises(ValueError):
                list(E.iter_records(path, start=start, stop=stop))
        self.assertEqual(len(list(E.iter_records(path, start=0, stop=0))), 0)
        self.assertEqual(len(list(E.iter_records(path, start=5, stop=2))), 0)

    def test_empty_and_truncated_stream_files(self):                 # contract: E19
        empty = os.path.join(self.tmp, 'empty.bin')
        open(empty, 'wb').close()
        self.assertEqual(list(E.iter_records(empty)), [])
        self.assertEqual(E.count_records(empty), 0)
        trunc = os.path.join(self.tmp, 'trunc.bin')
        with open(trunc, 'wb') as fh:
            fh.write(rec(1, 0, 1, B, 0, 1, 1, 100, 5) + b'\x01' * 10)
        self.assertEqual([r[E.F_SEQ] for r in E.iter_records(trunc)], [1])
        self.assertEqual(E.count_records(trunc), 1)

    def test_missing_stream_file(self):                              # contract: E20
        nope = os.path.join(self.tmp, 'nope.bin')
        with self.assertRaises(FileNotFoundError):
            list(E.iter_records(nope))
        with self.assertRaises(FileNotFoundError):
            E.load_meta(nope)

    def test_genparams_unknown_name_is_keyerror(self):               # contract: E21
        with self.assertRaises(KeyError):
            E.GenParams(no_such_knob=1)

    def test_genparams_out_of_range(self):                           # contract: E22
        out = os.path.join(self.tmp, 'never.bin')
        for kw in ({'n_events': -1}, {'n_symbols': 0}, {'n_symbols': 70000}, {'rate': 0.0},
                   {'depth_scale': 0.0}, {'lot_size': 0}, {'tick_size': 0}, {'min_qty': 0},
                   {'lot_size': 10, 'min_qty': 5}, {'max_qty': 0}, {'max_qty': 2 ** 62},
                   {'band': -1}, {'band': 2 ** 61}, {'base_price': 0}, {'base_price': 2 ** 62},
                   {'n_traders': 0}, {'n_traders': 70000}, {'target_resting': 0},
                   {'n_events': 10.5}, {'n_symbols': '2'}):
            with self.assertRaises(ValueError, msg=kw):
                E.generate(E.GenParams(**kw), out)
        self.assertFalse(os.path.exists(out))
        self.assertFalse(os.path.exists(E.meta_path(out)))


class StreamBoundaryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.empty = os.path.join(cls.tmp, 'empty.bin')
        E.generate(E.GenParams(n_events=0, n_symbols=2, seed=1), cls.empty)
        cls.one = os.path.join(cls.tmp, 'one.bin')
        E.generate(E.GenParams(n_events=1, n_symbols=2, seed=1), cls.one)
        cls.two_sym = os.path.join(cls.tmp, 'two.bin')
        E.generate(E.GenParams(n_events=2000, n_symbols=2, seed=3), cls.two_sym)

    def out(self, name):
        return os.path.join(self.tmp, name)

    def test_empty_stream_replay_and_compare(self):                  # contract: E23
        self.assertEqual(os.path.getsize(self.empty), 0)
        self.assertEqual(E.load_meta(self.empty)['n_events'], 0)
        st = R.replay(self.empty, self.out('r_empty'), check_every=1)
        self.assertEqual((st['events'], st['trades'], st['snapshots'], st['status_counts']),
                         (0, 0, 0, {}))
        self.assertEqual(sorted(os.listdir(self.out('r_empty'))),
                         ['snapshots.jsonl', 'stats.json', 'status.bin', 'trades.bin'])
        self.assertEqual(os.path.getsize(os.path.join(self.out('r_empty'), 'status.bin')), 0)
        self.assertEqual(R.replay_sharded(self.empty, self.out('rs_empty'), processes=2)['events'], 0)
        s = CMP.compare_stream(self.empty, full_every=1, digest_every=1)
        self.assertEqual((s['events'], s['trades'], s['first_divergence']), (0, 0, None))
        s = CMP.compare_sharded(self.empty, processes=2)
        self.assertEqual((s['events'], s['first_divergence']), (0, None))

    def test_single_event_stream(self):                              # contract: E24
        st = R.replay(self.one, self.out('r_one'), check_every=1)
        self.assertEqual((st['events'], sum(st['status_counts'].values())), (1, 1))
        self.assertEqual(os.path.getsize(os.path.join(self.out('r_one'), 'status.bin')), 1)
        self.assertEqual(R.replay_sharded(self.one, self.out('rs_one'), processes=2)['events'], 1)
        self.assertEqual(CMP.compare_stream(self.one)['events'], 1)
        self.assertEqual(CMP.compare_sharded(self.one, processes=2)['first_divergence'], None)

    def test_all_events_rejected_stream(self):                       # contract: E25
        bad = [rec(i + 1, 0, 9, B, 0, 1, i + 1, 100000, 5) for i in range(40)]
        bad += [rec(i + 41, 0, 1, 3, 0, 1, i + 41, 100000, 5) for i in range(40)]
        path = hand_stream(self.tmp, 'allrej.bin', bad)
        for name, check in (('ra_checked', 10), ('ra_plain', 0)):
            st = R.replay(path, self.out(name), check_every=check)
            self.assertEqual(st['status_counts'], {'REJ_KIND': 40, 'REJ_SIDE': 40})
            self.assertEqual((st['trades'], st['volume']), (0, 0))
            self.assertEqual(len(os.listdir(self.out(name))), 4)
        s = CMP.compare_stream(path, full_every=1, digest_every=1)
        self.assertEqual((s['events'], s['trades'], s['first_divergence']), (80, 0, None))

    def test_stream_and_meta_count_mismatch(self):                   # contract: E26
        three = [rec(i + 1, 0, 1, B, 0, 1, i + 1, 100000, 5) for i in range(3)]
        longer = hand_stream(self.tmp, 'longer.bin', three, n_events=2)
        shorter = hand_stream(self.tmp, 'shorter.bin', three, n_events=4)
        ragged = hand_stream(self.tmp, 'ragged.bin', three, n_events=3)
        with open(ragged, 'ab') as fh:
            fh.write(b'\x00' * 7)
        for path in (longer, shorter, ragged):
            for fn, name in ((R.replay, 'mm_single'), (R.replay_sharded, 'mm_sharded')):
                with self.assertRaises(ValueError, msg=path):
                    fn(path, self.out(name))
                self.assertFalse(os.path.exists(self.out(name)))      # nothing written

    def test_replay_record_with_unknown_symbol(self):                # contract: E27
        recs = [rec(1, 0, 1, B, 0, 1, 1, 100000, 5), rec(2, 5, 1, B, 0, 1, 2, 100000, 5)]
        path = hand_stream(self.tmp, 'unk.bin', recs, n_symbols=1)
        with self.assertRaises(ValueError) as cm:
            R.replay(path, self.out('unk_plain'))
        self.assertIn('symbol=5', str(cm.exception))
        with self.assertRaises(ValueError):
            R.replay(path, self.out('unk_checked'), check_every=1)
        with self.assertRaises(ValueError):
            R.replay_sharded(path, self.out('unk_sharded'), processes=1)
        self.assertEqual(os.listdir(self.out('unk_sharded')), [])     # shard files removed

    def test_replay_symbol_subset_skips_other_symbols(self):         # contract: E28
        st = R.replay(self.two_sym, self.out('subset'), symbols=[0], check_every=0)
        recs = list(E.iter_records(self.two_sym))
        self.assertEqual(st['events'], sum(1 for r in recs if r[E.F_SYMBOL] == 0))
        with open(os.path.join(self.out('subset'), 'status.bin'), 'rb') as fh:
            status = fh.read()
        self.assertEqual(len(status), len(recs))
        self.assertTrue(all(status[r[E.F_SEQ] - 1] == 0 for r in recs if r[E.F_SYMBOL] == 1))

    def test_replay_seq_out_of_range(self):                          # contract: E29
        recs = [rec(1, 0, 1, B, 0, 1, 1, 100000, 5), rec(99, 0, 1, B, 0, 1, 2, 100000, 5)]
        path = hand_stream(self.tmp, 'badseq.bin', recs, n_symbols=2)
        with self.assertRaises(ValueError) as cm:
            R.replay(path, self.out('seq_checked'), check_every=1)
        self.assertIn('seq=99', str(cm.exception))
        with self.assertRaises(ValueError):
            R.replay(path, self.out('seq_subset'), symbols=[0])     # subset => filtered mode
        with self.assertRaises(ValueError):
            R.replay_sharded(path, self.out('seq_sharded'), processes=2)
        st = R.replay(path, self.out('seq_plain'))        # pure mode: positional, documented
        self.assertEqual(st['events'], 2)

    def test_compare_filters_unknown_symbol(self):                   # contract: E30
        recs = [rec(1, 0, 1, B, 0, 1, 1, 100000, 5), rec(2, 5, 1, B, 0, 1, 2, 100000, 5)]
        path = hand_stream(self.tmp, 'unk_cmp.bin', recs, n_symbols=1)
        self.assertEqual(CMP.compare_stream(path)['events'], 1)
        self.assertEqual(CMP.compare_sharded(path, processes=1)['events'], 1)

    def test_missing_stream_or_meta(self):                           # contract: E31
        bare = os.path.join(self.tmp, 'bare.bin')
        open(bare, 'wb').close()
        for path in (bare, os.path.join(self.tmp, 'nope.bin')):
            with self.assertRaises(FileNotFoundError):
                R.replay(path, self.out('missing'))
            with self.assertRaises(FileNotFoundError):
                CMP.compare_stream(path)


class AnalysisBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _probes(self, stream, name):
        meta = E.load_meta(stream)
        symbols = list(range(meta['params']['n_symbols']))
        coll = ProbeCollector(symbols, meta, sample_every=10, seed=1)
        with open(os.devnull, 'wb') as tfh:
            core = R.Core(R.configs_from_meta(meta, symbols), bytearray(meta['n_events']), tfh)
            core.on_event = coll.on_event
            core.run(stream, symbols)
        coll.finalize()
        path = os.path.join(self.tmp, name)
        return path, coll.write_csv(path)

    def test_queue_model_with_zero_probes_or_empty_test_split(self):     # contract: E32
        empty = os.path.join(self.tmp, 'p0.csv')
        with open(empty, 'w') as fh:
            fh.write(','.join(FIELDS) + '\n')
        res, model = Q.run(empty, os.path.join(self.tmp, 'qm0'), horizons=(1, 2))
        self.assertEqual((res['probes'], res['train'], res['test']), (0, 0, 0))
        for h in ('1', '2'):
            ev = res['per_horizon'][h]
            self.assertEqual((ev['n'], ev['brier'], ev['brier_constant']), (0, None, None))
            with open(ev['svg']) as fh:
                self.assertIn('Brier n/a', fh.read())
        self.assertEqual(model.predict(0, 1, 0.0, 0.0, 0.0, 0, 1), 0.0)
        stream = os.path.join(self.tmp, 'ev.bin')
        E.generate(E.GenParams(n_events=3000, n_symbols=1, seed=5), stream)
        probes, n = self._probes(stream, 'p.csv')
        self.assertGreater(n, 0)
        res, _ = Q.run(probes, os.path.join(self.tmp, 'qm1'), horizons=(1,), train_frac=1.0)
        self.assertEqual((res['train'], res['test']), (n, 0))
        self.assertIsNone(res['per_horizon']['1']['brier'])

    def test_queue_collect_on_zero_events(self):                     # contract: E33
        stream = os.path.join(self.tmp, 'empty.bin')
        E.generate(E.GenParams(n_events=0, n_symbols=2, seed=1), stream)
        path, n = self._probes(stream, 'p_empty.csv')
        self.assertEqual(n, 0)
        with open(path) as fh:
            self.assertEqual(fh.read().strip(), ','.join(FIELDS))

    def test_checker_reports_invariant_error_type(self):             # contract: E34
        self.assertTrue(issubclass(InvariantError, AssertionError))
        book = Book(BookConfig(ref_price=100))
        book.process(to_record(1, N(1, B, 100, 5)))
        Checker().full_check(book)
        book.resting += 1
        with self.assertRaises(InvariantError):
            Checker().full_check(book)

    def test_cli_usage_errors_exit_2_and_quick_set(self):            # contract: E35
        for argv in ([], ['no-such-command'], ['test', '--nope'], ['test', '--scale', 'huge']):
            with self.assertRaises(SystemExit, msg=argv) as cm:
                cli.main(argv)
            self.assertEqual(cm.exception.code, 2, argv)
        self.assertEqual(cli.main(['test', '--quick', '--scale', 'full']), 2)
        self.assertEqual(set(cli.QUICK_TESTS), {'test_book', 'test_events', 'test_cli',
                                                'test_boundaries', 'test_contract'})
        for t in cli.QUICK_TESTS:
            self.assertTrue(os.path.exists(os.path.join(os.path.dirname(__file__), t + '.py')), t)


if __name__ == '__main__':
    unittest.main()
