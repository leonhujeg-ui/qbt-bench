"""Stage 0: event format + generator reproducibility."""
import os
import tempfile
import unittest

from lob import events as E


class EventFormatTest(unittest.TestCase):
    def test_record_size_and_roundtrip(self):
        self.assertEqual(E.RECORD_SIZE, 56)
        b = E.pack(7, 123, 3, E.KIND_NEW, E.SIDE_BUY, E.FLAG_ICEBERG, 9, 42,
                   100, -5, 7)
        self.assertEqual(len(b), 56)
        rec = E.RECORD.unpack(b)
        self.assertEqual(rec[E.F_SEQ], 7)
        self.assertEqual(rec[E.F_TS], 123)
        self.assertEqual(rec[E.F_SYMBOL], 3)
        self.assertEqual(rec[E.F_KIND], E.KIND_NEW)
        self.assertEqual(rec[E.F_SIDE], E.SIDE_BUY)
        self.assertEqual(rec[E.F_FLAGS], E.FLAG_ICEBERG)
        self.assertEqual(rec[E.F_TRADER], 9)
        self.assertEqual(rec[E.F_OID], 42)
        self.assertEqual(rec[E.F_PRICE], 100)
        self.assertEqual(rec[E.F_QTY], -5)
        self.assertEqual(rec[E.F_AUX], 7)

    def test_int64_extremes_pack(self):
        b = E.pack(1, 1, 0, 1, 1, 0, 0, 2 ** 64 - 1, -(2 ** 63), 2 ** 63 - 1, 0)
        rec = E.RECORD.unpack(b)
        self.assertEqual(rec[E.F_PRICE], -(2 ** 63))
        self.assertEqual(rec[E.F_QTY], 2 ** 63 - 1)

    def test_unknown_param_rejected(self):
        with self.assertRaises(KeyError):
            E.GenParams(no_such_knob=1)


class GeneratorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _gen(self, name, **kw):
        path = os.path.join(self.tmp, name)
        meta = E.generate(E.GenParams(**kw), path)
        return path, meta

    def test_same_seed_byte_identical(self):
        p1, m1 = self._gen('a.bin', n_events=20000, n_symbols=3, seed=5)
        p2, m2 = self._gen('b.bin', n_events=20000, n_symbols=3, seed=5)
        self.assertEqual(m1['sha256'], m2['sha256'])
        with open(p1, 'rb') as f1, open(p2, 'rb') as f2:
            self.assertEqual(f1.read(), f2.read())
        self.assertEqual(E.count_records(p1), 20000)
        self.assertEqual(m1['regimes'], m2['regimes'])

    def test_different_seed_differs(self):
        _, m1 = self._gen('a.bin', n_events=5000, n_symbols=2, seed=1)
        _, m2 = self._gen('b.bin', n_events=5000, n_symbols=2, seed=2)
        self.assertNotEqual(m1['sha256'], m2['sha256'])

    def test_sequence_and_symbol_filter(self):
        path, meta = self._gen('c.bin', n_events=9000, n_symbols=4, seed=3)
        recs = list(E.iter_records(path))
        self.assertEqual([r[E.F_SEQ] for r in recs], list(range(1, 9001)))
        self.assertEqual(set(r[E.F_SYMBOL] for r in recs), {0, 1, 2, 3})
        only = list(E.iter_records(path, symbols=[2]))
        self.assertTrue(all(r[E.F_SYMBOL] == 2 for r in only))
        self.assertEqual(len(only), sum(1 for r in recs if r[E.F_SYMBOL] == 2))
        part = list(E.iter_records(path, start=10, stop=20))
        self.assertEqual([r[E.F_SEQ] for r in part], list(range(11, 21)))
        self.assertEqual(sum(meta['counts'].values()), 9000)
        self.assertEqual(E.load_meta(path)['sha256'], meta['sha256'])

    def test_mix_covers_every_kind_and_flag(self):
        path, _ = self._gen('d.bin', n_events=30000, n_symbols=2, seed=11)
        kinds, flags = set(), set()
        for r in E.iter_records(path):
            kinds.add(r[E.F_KIND])
            if r[E.F_KIND] == E.KIND_NEW:
                flags.add(r[E.F_FLAGS])
        self.assertEqual(kinds, {1, 2, 3, 9})
        for f in (0, E.FLAG_IOC, E.FLAG_FOK, E.FLAG_POST_ONLY, E.FLAG_ICEBERG,
                  E.FLAG_MARKET, E.FLAG_MARKET | E.FLAG_FOK):
            self.assertIn(f, flags)

    def test_regimes_appear_with_high_probability_knobs(self):
        _, meta = self._gen('e.bin', n_events=20000, n_symbols=1, seed=2,
                            withdraw_prob=0.01, shock_prob=0.01)
        names = set(r[3] for r in meta['regimes'])
        self.assertEqual(names, {'withdraw', 'shock'})
        for sym, start, end, _ in meta['regimes']:
            self.assertLess(start, end)
            self.assertEqual(sym, 0)

    def test_empty_file_iterates_nothing(self):
        path = os.path.join(self.tmp, 'empty.bin')
        open(path, 'wb').close()
        self.assertEqual(list(E.iter_records(path)), [])
