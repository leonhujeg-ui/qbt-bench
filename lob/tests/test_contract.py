"""CONTRACT.md is machine-checked.

Every table in lob/CONTRACT.md sits under a `<!-- contract-table: NAME -->`
marker; this module parses each one and compares it with the code.  A row
missing from the document, a constant missing from the code, or a value that
disagrees turns the corresponding test red.  LOB_CONTRACT_PATH=<file> points
the parser at another copy of the document (used to demonstrate that a
deleted row is caught without editing the real file).
"""
import json
import os
import re
import struct
import tempfile
import unittest

from lob import book as BK
from lob import common as CM
from lob import compare as CMP
from lob import events as E
from lob import replay as R
from lob.book import Book
from lob.common import BookConfig, STATUS_NAMES, flags_valid
from lob.reference import ReferenceBook
from lob.tests.harness import N, RAW, to_record

LOB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTRACT = os.environ.get('LOB_CONTRACT_PATH') or os.path.join(LOB_DIR, 'CONTRACT.md')
MARK = re.compile(r'<!--\s*contract-table:\s*([a-z_]+)\s*-->')   # ASCII names only
TYPE_CHAR = {'u64': 'Q', 'u32': 'I', 'u16': 'H', 'u8': 'B', 'i64': 'q', 'pad': 'x'}
TABLES = ('record', 'record_fields', 'kind', 'side', 'flags', 'flag_rules', 'meta_keys',
          'status', 'new_order', 'new_outcomes', 'order_slots', 'queue_consts',
          'trade_slots', 'trade_record', 'config_fields', 'config_errors', 'replay_files',
          'replay_stats', 'book_stats', 'snapshot_keys', 'compare_result', 'divergence',
          'divergence_what', 'errors')
B, S = 1, 2
MK, IOC, FOK, PO, ICE = 1, 2, 4, 8, 16
ASK = N(1, S, 101, 3)          # one resting ask, 3 lots at 101 (trader 101)
# NEW validation precedence: for the documented order a < b, the last event
# violates both a and b and must come back as a (both engines).
PRECEDENCE = {
    ('REJ_KIND', 'REJ_SIDE'): [RAW(9, 3, 0, 1, 1, 100, 5)],
    ('REJ_SIDE', 'REJ_FLAGS'): [RAW(1, 3, IOC | FOK, 1, 1, 100, 5)],
    ('REJ_FLAGS', 'REJ_DUP'): [N(1, B, 100, 5), N(1, B, 100, 5, IOC | FOK)],
    ('REJ_DUP', 'REJ_QTY'): [N(1, B, 100, 5), N(1, B, 100, 0)],
    ('REJ_QTY', 'REJ_PRICE'): [N(1, B, 0, 0)],
    ('REJ_PRICE', 'REJ_BAND'): [N(1, B, -1000, 5)],
    ('REJ_BAND', 'REJ_DISPLAY'): [N(1, B, 1000, 5, ICE, 1, 0)],
    ('REJ_DISPLAY', 'REJ_POST_ONLY'): [ASK, N(2, B, 101, 5, ICE | PO, 1, 0)],
    ('REJ_POST_ONLY', 'REJ_FOK'): None,       # POST_ONLY+FOK is REJ_FLAGS: unobservable
}
BEFORE = {('REJ_DISPLAY', 'REJ_FOK'): [ASK, N(2, B, 101, 5, ICE | FOK, 1, 0)]}
OUTCOMES = {
    'RESTED': [N(1, B, 100, 5)], 'FILLED': [ASK, N(2, B, 101, 3)],
    'PARTIAL_RESTED': [ASK, N(2, B, 101, 5)], 'PARTIAL_CANCELLED': [ASK, N(2, B, 101, 5, IOC)],
    'CANCELLED': [N(1, B, 100, 5, IOC)],
}


def load_tables(path=CONTRACT):
    """name -> data rows (header and separator dropped), every cell stripped."""
    with open(path, encoding='utf-8') as fh:
        lines = fh.read().split('\n')
    tables, i = {}, 0
    while i < len(lines):
        m = MARK.search(lines[i])
        if not m:
            i += 1
            continue
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        rows = []
        while j < len(lines) and lines[j].strip().startswith('|'):
            cells = lines[j].strip().strip('|').split('|')
            rows.append([c.strip().strip('`').strip() for c in cells])
            j += 1
        if len(rows) < 3:
            raise ValueError('contract table %s has no data rows' % m.group(1))
        tables[m.group(1)] = rows[2:]
        i = j
    return tables


def names_with_prefix(module, prefix):
    return {n for n in dir(module) if n.startswith(prefix)}


def last_status(evs, **cfg_kw):
    """Status name of the last event on both engines (a set: one name if they agree)."""
    out = set()
    for cls in (Book, ReferenceBook):
        book = cls(BookConfig(**dict(dict(ref_price=100, band=50), **cfg_kw)))
        st = None
        for i, ev in enumerate(evs):
            st = book.process(to_record(i + 1, ev))
        out.add(STATUS_NAMES[st])
    return out


class TableParsingTest(unittest.TestCase):
    def test_every_table_present_exactly_once(self):
        self.assertEqual(sorted(load_tables()), sorted(TABLES))
        with open(CONTRACT, encoding='utf-8') as fh:
            src = fh.read()
        for name in TABLES:
            self.assertEqual(len(re.findall(r'contract-table:\s*%s\s*-->' % name, src)), 1, name)


class EventFormatContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.t = load_tables()

    def test_record_summary(self):
        kv = {r[0]: r[1] for r in self.t['record']}
        self.assertEqual(int(kv['record_size']), E.RECORD_SIZE)
        self.assertEqual(E.RECORD_SIZE, 56)
        self.assertEqual(kv['struct_format'], E.RECORD.format)
        self.assertEqual(kv['byte_order'], 'little')
        self.assertTrue(E.RECORD.format.startswith('<'))
        self.assertEqual(int(kv['n_fields']), len(E.RECORD.unpack(bytes(E.RECORD_SIZE))))
        self.assertEqual(kv['file_header'], 'none')
        self.assertEqual(E.meta_path('x'), 'x' + kv['meta_suffix'])
        self.assertEqual(kv['trade_struct_format'], R.TRADE.format)
        self.assertEqual(int(kv['trade_record_size']), R.TRADE_SIZE)

    def test_record_fields(self):
        rows = self.t['record_fields']
        fmt = E.RECORD.format
        self.assertEqual(len(rows), len(fmt) - 1)
        self.assertEqual([int(r[0]) for r in rows], list(range(len(rows))))
        self.assertEqual({r[1] for r in rows}, names_with_prefix(E, 'F_'))
        for r in rows:
            idx, const, off, size, typ = int(r[0]), r[1], int(r[3]), int(r[4]), r[5]
            self.assertEqual(getattr(E, const), idx, const)
            self.assertEqual(struct.calcsize('<' + fmt[1:idx + 1]), off, const)
            self.assertEqual(struct.calcsize('<' + fmt[idx + 1]), size, const)
            self.assertEqual(TYPE_CHAR[typ], fmt[idx + 1], const)
        self.assertEqual(sum(int(r[4]) for r in rows), E.RECORD_SIZE)
        rec = E.RECORD.unpack(E.pack(1, 2, 3, 1, 1, 0, 4, 5, 6, 7, 8))
        self.assertEqual(rec[E.F_PAD], 0)
        self.assertEqual(rec, (1, 2, 3, 1, 1, 0, 0, 4, 5, 6, 7, 8))

    def test_enumerations(self):
        for table, prefix in (('kind', 'KIND_'), ('side', 'SIDE_'), ('flags', 'FLAG_')):
            rows = self.t[table]
            self.assertEqual({r[0] for r in rows}, names_with_prefix(E, prefix), table)
            for r in rows:
                self.assertEqual(getattr(E, r[0]), int(r[1]), r[0])
        bits = sorted(int(r[1]) for r in self.t['flags'])
        self.assertEqual(bits, [1, 2, 4, 8, 16])
        self.assertEqual(CM.ALL_FLAGS, sum(bits))
        self.assertEqual(sorted(int(r[1]) for r in self.t['kind']), [1, 2, 3])
        self.assertEqual(sorted(int(r[1]) for r in self.t['side']), [1, 2])

    def test_flag_rules_complete_and_true(self):
        rows = self.t['flag_rules']
        values = [int(r[0]) for r in rows]
        bits = [1, 2, 4, 8, 16]
        pairs = {a | b for a in bits for b in bits if a < b}
        self.assertEqual(set(values), {0, 32} | set(bits) | pairs)    # 1 + 5 + 10 + 1 rows
        self.assertEqual(len(values), len(set(values)))
        names = {'MARKET': 1, 'IOC': 2, 'FOK': 4, 'POST_ONLY': 8, 'ICEBERG': 16}
        for r in rows:
            self.assertIn(r[2], ('是', '否'), r)
            self.assertEqual(flags_valid(int(r[0])), r[2] == '是', r)
            parts = r[1].split('+')
            if all(p in names for p in parts):
                self.assertEqual(sum(names[p] for p in parts), int(r[0]), r)
        self.assertFalse(flags_valid(255))


class StatusContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.t = load_tables()

    def test_status_table_matches_code(self):
        rows = self.t['status']
        codes = {int(r[0]): (r[1], r[2]) for r in rows}
        self.assertEqual(len(rows), len(codes))
        self.assertEqual(set(codes), set(STATUS_NAMES))
        for code, (const, name) in codes.items():
            self.assertEqual(STATUS_NAMES[code], name, code)
            self.assertEqual(getattr(CM, const), code, const)
        consts = names_with_prefix(CM, 'ST_') | names_with_prefix(CM, 'REJ_')
        self.assertEqual(consts, {r[1] for r in rows})
        self.assertTrue(all(name.startswith('REJ_') == (code >= 10)
                            for code, name in STATUS_NAMES.items()))

    def test_new_validation_order(self):
        rows = self.t['new_order']
        order = [r[2] for r in rows]
        self.assertEqual([int(r[0]) for r in rows], list(range(len(rows))))
        rejects = {n for n in STATUS_NAMES.values() if n.startswith('REJ_')}
        self.assertEqual(set(order), rejects - {'REJ_UNKNOWN'})
        for a, b in zip(order, order[1:]):
            self.assertIn((a, b), PRECEDENCE, 'no construction for documented order %s < %s' % (a, b))
            evs = PRECEDENCE[(a, b)]
            if evs is None:
                self.assertFalse(flags_valid(PO | FOK))
                continue
            self.assertEqual(last_status(evs), {a}, (a, b))
        for (a, b), evs in BEFORE.items():
            self.assertLess(order.index(a), order.index(b))
            self.assertEqual(last_status(evs), {a}, (a, b))

    def test_new_outcomes(self):
        names = [r[1] for r in self.t['new_outcomes']]
        self.assertEqual(sorted(names), sorted(OUTCOMES))
        self.assertEqual(set(names), {STATUS_NAMES[c] for c in range(5)})
        for name, evs in OUTCOMES.items():
            self.assertEqual(last_status(evs), {name}, name)


class StructureContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.t = load_tables()

    def test_order_slots(self):
        rows = self.t['order_slots']
        self.assertEqual([int(r[0]) for r in rows], list(range(len(rows))))
        self.assertEqual({r[1] for r in rows}, names_with_prefix(BK, 'O_'))
        for r in rows:
            self.assertEqual(getattr(BK, r[1]), int(r[0]), r[1])
        book = Book(BookConfig(ref_price=100))
        book.process(to_record(1, N(7, B, 100, 5, 0, 3)))
        o = book.live_orders()[0]
        self.assertEqual(len(o), len(rows))
        self.assertEqual((o[BK.O_OID], o[BK.O_SIDE], o[BK.O_PRICE], o[BK.O_REM], o[BK.O_VIS],
                          o[BK.O_DISP], o[BK.O_TRADER], o[BK.O_PRIO], o[BK.O_SLOT]),
                         (7, 1, 100, 5, 5, 0, 3, 1, BK.Q_FIRST))
        ref = ReferenceBook(BookConfig(ref_price=100))
        ref.process(to_record(1, N(7, B, 100, 5, 0, 3)))
        ro = ref.live_orders()[0]
        self.assertEqual((ro.oid, ro.side, ro.price, ro.rem, ro.vis, ro.display, ro.trader,
                          ro.prio), (7, 1, 100, 5, 5, 0, 3, 1))

    def test_queue_consts(self):
        rows = self.t['queue_consts']
        self.assertEqual({r[0] for r in rows},
                         names_with_prefix(BK, 'Q_') | names_with_prefix(BK, 'COMPACT_'))
        for r in rows:
            self.assertEqual(getattr(BK, r[0]), int(r[1]), r[0])

    def test_trade_slots(self):
        rows = self.t['trade_slots']
        self.assertEqual([int(r[0]) for r in rows], list(range(len(rows))))
        self.assertEqual({r[1] for r in rows}, names_with_prefix(CM, 'T_'))
        for r in rows:
            self.assertEqual(getattr(CM, r[1]), int(r[0]), r[1])
        book = Book(BookConfig(ref_price=100, symbol=4))
        book.process(to_record(1, N(1, S, 101, 3, 0, 8)))
        book.process(to_record(2, N(2, B, 101, 5, 0, 9)))
        t = book.trades[0]
        self.assertEqual(len(t), len(rows))
        self.assertEqual(t, (2, 0, 4, 1, 2, 1, 101, 3, 9, 8))

    def test_trade_record(self):
        rows = self.t['trade_record']
        fmt = '<' + ''.join(TYPE_CHAR[r[4]] for r in rows)
        self.assertEqual(fmt, R.TRADE.format)
        self.assertEqual(struct.calcsize(fmt), R.TRADE_SIZE)
        self.assertEqual([int(r[0]) for r in rows], list(range(len(rows))))
        for r in rows:
            idx = int(r[0])
            self.assertEqual(struct.calcsize('<' + fmt[1:idx + 1]), int(r[2]), r[1])
            self.assertEqual(struct.calcsize('<' + fmt[idx + 1]), int(r[3]), r[1])
        # the non-pad fields are trade tuple slots T_SEQ .. T_QTY, in that order
        fields = [r[1] for r in rows if r[4] != 'pad']
        self.assertEqual([getattr(CM, 'T_' + f.upper()) for f in fields], list(range(len(fields))))
        self.assertEqual(R.TRADE.unpack(R.TRADE.pack(2, 0, 4, 1, 2, 1, 101, 3)),
                         (2, 0, 4, 1, 2, 1, 101, 3))

    def test_config_fields_and_defaults(self):
        rows = self.t['config_fields']
        self.assertEqual([r[0] for r in rows], list(BookConfig.__slots__))
        cfg = BookConfig()
        for r in rows:
            self.assertEqual(getattr(cfg, r[0]), json.loads(r[1]), r[0])
        self.assertEqual(set(cfg.to_dict()), set(BookConfig.__slots__))
        self.assertEqual(BookConfig.from_dict(cfg.to_dict(), symbol=0).to_dict(), cfg.to_dict())

    def test_config_errors_each_row_raises(self):
        rows = self.t['config_errors']
        for r in rows:
            with self.assertRaises(ValueError, msg=r[0]):
                BookConfig(**json.loads(r[1]))
        # the three raise statements in BookConfig.__init__ cover eight atomic conditions
        self.assertEqual(len(rows), 8)


class StreamOutputsContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.t = load_tables()
        cls.tmp = tempfile.mkdtemp()
        cls.path = os.path.join(cls.tmp, 'ev.bin')
        cls.meta = E.generate(E.GenParams(n_events=8000, n_symbols=2, seed=9), cls.path)
        cls.single_dir = os.path.join(cls.tmp, 'single')
        cls.single = R.replay(cls.path, cls.single_dir, check_every=100)
        cls.sharded = R.replay_sharded(cls.path, os.path.join(cls.tmp, 'sharded'),
                                       processes=2, check_every=100)
        cls.cmp = CMP.compare_stream(cls.path, full_every=100, digest_every=100)
        cls.cmp_sharded = CMP.compare_sharded(cls.path, processes=2, full_every=100,
                                              digest_every=100)

    def test_stream_has_no_header(self):
        with open(self.path, 'rb') as fh:
            first = E.RECORD.unpack(fh.read(E.RECORD_SIZE))
        self.assertEqual(first[E.F_SEQ], 1)
        self.assertEqual(E.count_records(self.path), self.meta['n_events'])

    def test_meta_keys(self):
        rows = self.t['meta_keys']
        self.assertEqual({r[0] for r in rows}, set(self.meta))
        self.assertEqual(set(E.load_meta(self.path)), set(self.meta))
        self.assertEqual(set(self.meta['counts']), {'new', 'cancel', 'modify', 'bad'})
        cfg_keys = {r[0] for r in self.t['config_fields']} - {'symbol'}
        self.assertEqual(set(self.meta['books']['0']), cfg_keys)
        self.assertEqual(self.meta['record_size'], E.RECORD_SIZE)
        self.assertEqual(self.meta['sha256'], E.file_sha256(self.path))
        self.assertEqual(self.meta['epoch_ns'], E.EPOCH_NS)

    def test_replay_files(self):
        rows = self.t['replay_files']
        self.assertEqual({r[0] for r in rows}, set(os.listdir(self.single_dir)))
        with open(os.path.join(self.single_dir, 'stats.json')) as fh:
            self.assertEqual(set(json.load(fh)), set(self.single))
        self.assertEqual(os.path.getsize(os.path.join(self.single_dir, 'status.bin')),
                         self.meta['n_events'])
        self.assertEqual(os.path.getsize(os.path.join(self.single_dir, 'trades.bin')),
                         R.TRADE_SIZE * self.single['trades'])

    def test_replay_stats_keys(self):
        modes = {r[0]: r[1] for r in self.t['replay_stats']}
        self.assertTrue(set(modes.values()) <= {'both', 'single', 'sharded'})
        self.assertEqual({k for k, m in modes.items() if m != 'sharded'}, set(self.single))
        self.assertEqual({k for k, m in modes.items() if m != 'single'}, set(self.sharded))
        self.assertEqual((self.single['mode'], self.sharded['mode']), ('single', 'sharded'))

    def test_book_stats_keys(self):
        keys = {r[0] for r in self.t['book_stats']}
        for stats in (self.single, self.sharded):
            self.assertEqual(set(stats['books']), {'0', '1'})
            for sym, b in stats['books'].items():
                self.assertEqual(set(b), keys, sym)

    def test_snapshot_keys(self):
        self.assertGreater(self.single['snapshots'], 0)
        with open(os.path.join(self.single_dir, 'snapshots.jsonl')) as fh:
            lines = [json.loads(line) for line in fh]
        self.assertEqual(len(lines), self.single['snapshots'])
        for snap in lines:
            self.assertEqual(set(snap), {r[0] for r in self.t['snapshot_keys']})

    def test_compare_result_keys(self):
        modes = {r[0]: r[1] for r in self.t['compare_result']}
        self.assertTrue(set(modes.values()) <= {'both', 'stream', 'sharded'})
        self.assertEqual({k for k, m in modes.items() if m != 'sharded'}, set(self.cmp))
        self.assertEqual({k for k, m in modes.items() if m != 'stream'}, set(self.cmp_sharded))
        self.assertIsNone(self.cmp['first_divergence'])
        self.assertIsNone(self.cmp_sharded['first_divergence'])

    def test_divergence_keys_and_kinds(self):
        class Broken(CMP.Book):
            def process(self, rec):
                st = super().process(rec)
                return 99 if self.n_events == 5 else st
        orig = CMP.Book
        CMP.Book = Broken
        try:
            d = CMP.compare_stream(self.path, symbols=[0], limit=100)['first_divergence']
        finally:
            CMP.Book = orig
        self.assertEqual(set(d), {r[0] for r in self.t['divergence']})
        self.assertEqual(d['what'], 'status')
        with open(CMP.__file__) as fh:
            kinds = set(re.findall(r"_divergence\([^,]+,[^,]+,\s*'([^']+)'", fh.read()))
        self.assertEqual(kinds, {r[0] for r in self.t['divergence_what']})


class ErrorTableContractTest(unittest.TestCase):
    def test_every_error_row_has_a_boundary_criterion_and_vice_versa(self):
        rows = load_tables()['errors']
        ids = [r[0] for r in rows]
        self.assertEqual(ids, sorted(set(ids)))
        with open(os.path.join(LOB_DIR, 'tests', 'test_boundaries.py'), encoding='utf-8') as fh:
            tagged = re.findall(r'#\s*contract:\s*(E\d+)', fh.read())
        self.assertEqual(sorted(set(tagged)), ids)
        for r in rows:
            for rej in re.findall(r'REJ_[A-Z_]+', r[3]):
                self.assertIn(rej, STATUS_NAMES.values(), r[0])


if __name__ == '__main__':
    unittest.main()
