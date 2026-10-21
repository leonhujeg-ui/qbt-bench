"""Queue model: truth collection, calibration, evaluation, plotting."""
import json
import math
import os
import random
import tempfile
import unittest

from lob import events as E
from lob import queue_model as Q
from lob.book import Book
from lob.common import BookConfig
from lob.queue_collect import ProbeCollector, outcome, read_csv, FIELDS
from lob.tests.harness import N, C, M, RAW, to_record
from lob.events import KIND_MODIFY, KIND_CANCEL

NS = E.NS


def synthetic_rows(n, seed, horizons=(1, 5)):
    """Rows whose fill probability is a known function of (d, u)."""
    rng = random.Random(seed)
    rows = []
    t0 = E.EPOCH_NS
    for i in range(n):
        d = rng.choice((0, 0, 0, 1, 1, 2, 3, 4))
        rate = rng.choice((0.0, 2.0, 10.0, 40.0))
        ahead = rng.randint(0, 80)
        u = ahead / (rate * 5 + 1.0)
        p = 0.9 * math.exp(-0.6 * u) * (0.75 ** d) + 0.02
        filled = rng.random() < p
        row = {'sym': 0, 't0': t0 + i * 10 ** 7, 'oid': i, 'side': 1 + (i % 2), 'd': d,
               'dist': d, 'ahead': ahead, 'n_ahead': ahead // 8, 'level_total': ahead + 5,
               'own': 10, 'spread': 2, 'opp_best_qty': 10, 'n_levels': 8,
               'rate_arr': rate, 'rate_cxl': rate / 2, 'rate_trd': rate,
               'rate_trd_opp': rate, 'rate_events': 100.0,
               'regime': rng.choice(('normal', 'normal', 'shock')),
               'first_fill_ns': int(rng.uniform(0.05, 4.9) * NS) if filled else None,
               'full_fill_ns': None, 'censor_ns': None}
        if not filled and rng.random() < 0.1:
            row['censor_ns'] = int(rng.uniform(0.1, 3.0) * NS)
        rows.append(row)
    return rows


class OutcomeTest(unittest.TestCase):
    def test_outcome_semantics(self):
        r = {'first_fill_ns': 2 * NS, 'full_fill_ns': 4 * NS, 'censor_ns': None}
        self.assertEqual(outcome(r, 1 * NS), 0)
        self.assertEqual(outcome(r, 2 * NS), 1)
        self.assertEqual(outcome(r, 3 * NS, full=True), 0)
        self.assertEqual(outcome(r, 4 * NS, full=True), 1)
        r = {'first_fill_ns': None, 'full_fill_ns': None, 'censor_ns': 3 * NS}
        self.assertEqual(outcome(r, 2 * NS), 0)          # alive at horizon
        self.assertEqual(outcome(r, 3 * NS), 0)
        self.assertEqual(outcome(r, 4 * NS), -1)         # censored before horizon
        r = {'first_fill_ns': 1 * NS, 'full_fill_ns': None, 'censor_ns': 2 * NS}
        self.assertEqual(outcome(r, 5 * NS), 1)          # fill wins over censor
        self.assertEqual(outcome(r, 5 * NS, full=True), -1)

    def test_bins(self):
        self.assertEqual(Q.u_bin(0.0), 0)
        self.assertEqual(Q.u_bin(0.25), 1)
        self.assertEqual(Q.u_bin(15.9), 6)
        self.assertEqual(Q.u_bin(16.0), 7)
        self.assertEqual(Q.cell_key(0, 0, 0.0, 5), 0)
        self.assertEqual(Q.cell_key(1, 0, 0.0, 5), 8)
        self.assertEqual(Q.cell_key(9, 0, 0.0, 5), 24)          # d clipped to 3
        self.assertEqual(Q.cell_key(0, 10, 2.0, 5), Q.u_bin(10 / 11.0))
        self.assertEqual(Q.cell_key(0, 100, 0.0, 1), 7)


class ModelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rows = synthetic_rows(6000, 3)
        cls.train, cls.test = Q.split_by_time(rows, 0.7)
        cls.model = Q.QueueModel().fit(cls.train, (1, 5))
        cls.param = Q.ParametricModel().fit(cls.train, (1, 5))

    def test_split_is_by_time(self):
        self.assertLess(max(r['t0'] for r in self.train), min(r['t0'] for r in self.test))
        self.assertEqual(len(self.train) + len(self.test), 6000)

    def test_binned_beats_constant_and_is_calibrated(self):
        for h in (1, 5):
            ev = Q.evaluate(self.model, self.test, h)
            self.assertLess(ev['brier'], ev['brier_constant'])
            self.assertLess(ev['brier'], 0.2)
            self.assertEqual(sum(b['n'] for b in ev['reliability']), ev['n'])
            for b in ev['reliability']:
                self.assertTrue(b['lo'] <= b['mean_pred'] <= b['hi'] or b['hi'] == 1.0)
            self.assertEqual(set(ev['by_regime']), {'normal', 'shock'})
            self.assertEqual(sum(v['n'] for v in ev['by_d'].values()), ev['n'])

    def test_predictions_monotone_in_queue_position(self):
        p = [self.model.predict(a, 100, 1.0, 1.0, 10.0, 0, 5) for a in (0, 10, 40, 200)]
        self.assertEqual(p, sorted(p, reverse=True))
        self.assertTrue(all(0.0 <= x <= 1.0 for x in p))
        self.assertGreater(self.model.predict(0, 1, 0, 0, 10.0, 0, 5),
                           self.model.predict(0, 1, 0, 0, 10.0, 3, 5))

    def test_unseen_cell_falls_back_to_global_rate(self):
        m = Q.QueueModel().fit(self.train[:50], (5,))
        m.tables[5] = {}
        self.assertEqual(m.predict(5, 5, 0, 0, 0, 0, 5), m.global_rate[5])
        self.assertIsNone(m.time_to_fill(5, 0, 0, 5))
        self.assertEqual(m._horizon(4), 5)                     # nearest horizon

    def test_time_to_fill_is_cell_median(self):
        h = 5
        key = Q.cell_key(0, 0, 10.0, h)
        fills = sorted(r['first_fill_ns'] for r in self.train
                       if outcome(r, h * NS) == 1
                       and Q.cell_key(r['d'], r['ahead'], r['rate_trd'], h) == key)
        self.assertEqual(self.model.time_to_fill(0, 10.0, 0, h), fills[len(fills) // 2] / NS)

    def test_roundtrip_and_laplace_smoothing(self):
        d = json.loads(json.dumps(self.model.to_dict()))
        m2 = Q.QueueModel.from_dict(d)
        for r in self.test[:200]:
            self.assertEqual(m2.predict_row(r, 5), self.model.predict_row(r, 5))
        n, k, _ = self.model.tables[5][Q.cell_key(0, 0, 10.0, 5)]
        g = self.model.global_rate[5]
        self.assertAlmostEqual(self.model.predict(0, 0, 0, 0, 10.0, 0, 5),
                               (k + Q.ALPHA * g) / (n + Q.ALPHA))

    def test_parametric_model(self):
        ev = Q.evaluate(self.param, self.test, 5)
        self.assertLess(ev['brier'], ev['brier_constant'])
        for h in (1, 5):
            for r in self.test[:100]:
                self.assertTrue(0.0 < self.param.predict_row(r, h) < 1.0)
        a, b = self.param.params[(5, 0)]
        self.assertTrue(-6.0 <= a <= 6.0 and b >= 0.0)
        self.assertLess(self.param.predict_row({'d': 0, 'rate_trd': 1.0, 'ahead': 500}, 5),
                        self.param.predict_row({'d': 0, 'rate_trd': 1.0, 'ahead': 0}, 5))

    def test_brier_and_reliability_helpers(self):
        self.assertAlmostEqual(Q.brier([(1.0, 1), (0.0, 1), (0.5, 0)]), (0 + 1 + 0.25) / 3)
        self.assertIsNone(Q.brier([]))
        rel = Q.reliability([(0.05, 0), (0.07, 1), (1.0, 1), (0.95, 1)], bins=10)
        self.assertEqual([b['n'] for b in rel], [2, 2])
        self.assertAlmostEqual(rel[0]['mean_obs'], 0.5)

    def test_svg_written(self):
        tmp = tempfile.mkdtemp()
        rel = Q.reliability([(0.1, 0), (0.2, 1), (0.8, 1), (0.9, 1)])
        path = Q.reliability_svg(rel, 'test', os.path.join(tmp, 'r.svg'))
        with open(path) as fh:
            svg = fh.read()
        self.assertTrue(svg.startswith('<svg'))
        self.assertEqual(svg.count('<circle'), len(rel))
        self.assertIn('observed frequency', svg)
        self.assertIn('polyline', svg)


class CollectorTest(unittest.TestCase):
    """Shadow-order tracking checked against hand-derived outcomes.
    Event k has timestamp EPOCH + 1000*k ns."""

    def _setup(self, evs, stp='cancel_new', **kw):
        book = Book(BookConfig(ref_price=100, band=50, stp=stp))
        coll = ProbeCollector([0], {'regimes': [[0, E.EPOCH_NS, E.EPOCH_NS + 10 ** 6, 'shock']],
                                    'books': {'0': {'ref_price': 100, 'band': 50}}},
                              sample_every=10 ** 9, seed=1, **kw)
        self._feed(book, coll, evs)
        return book, coll

    def _feed(self, book, coll, evs, start_seq=1):
        for i, ev in enumerate(evs):
            rec = to_record(start_seq + i, ev)
            n0 = len(book.trades)
            st = book.process(rec)
            coll.on_event(book, rec, st, book.trades[n0:])

    def test_tail_probe_filled_by_aggressor_leftover(self):
        book, coll = self._setup([N(1, 2, 101, 5), N(2, 2, 101, 7)])
        p = coll.place(book, 0, 2, 0, 2, E.EPOCH_NS + 2000)
        r = p.row
        self.assertEqual((r['ahead'], r['n_ahead'], r['oid'], r['d'], r['regime']), (12, 2, 2, 0, 'shock'))
        self._feed(book, coll, [N(3, 1, 101, 5)], 3)          # eats order 1
        self.assertEqual((p.ahead, p.own_rem), (7, 10))
        self._feed(book, coll, [N(4, 1, 101, 10)], 4)         # eats order 2, 3 left -> rests
        self.assertEqual((p.ahead, p.own_rem), (0, 7))
        self.assertEqual(r['first_fill_ns'], 2000)             # seq 4 - t0 (seq 2)
        self._feed(book, coll, [N(5, 1, 102, 20)], 5)         # no asks: would hit us
        self.assertEqual(r['full_fill_ns'], 3000)
        self.assertEqual(coll.state[0].probes, [])
        self.assertEqual(coll.state[0].watch, {})
        self.assertEqual(outcome(r, 2500), 1)
        self.assertEqual(outcome(r, 2500, full=True), 0)

    def test_cancel_reduce_increase_ahead(self):
        book, coll = self._setup([N(1, 2, 101, 5), N(2, 2, 101, 7), N(3, 2, 101, 4)])
        p = coll.place(book, 0, 2, 0, 2, E.EPOCH_NS + 3000)
        self.assertEqual(p.ahead, 12)
        self._feed(book, coll, [RAW(KIND_CANCEL, 2, 0, 0, 1, 0, 0)], 4)
        self.assertEqual((p.ahead, sorted(p.ahead_orders)), (7, [2]))
        self._feed(book, coll, [RAW(KIND_MODIFY, 2, 0, 0, 2, 0, 3)], 5)    # reduce 7 -> 3
        self.assertEqual((p.ahead, p.ahead_orders), (3, {2: 3}))
        self._feed(book, coll, [RAW(KIND_MODIFY, 2, 0, 0, 2, 0, 9)], 6)    # increase: behind us
        self.assertEqual((p.ahead, p.ahead_orders, coll.state[0].watch), (0, {}, {}))
        self._feed(book, coll, [N(7, 1, 101, 1)], 7)          # trade with order 3 (behind us)
        self.assertEqual(p.row['first_fill_ns'], 4000)
        self.assertEqual(p.own_rem, 9)

    def test_head_probe_and_sweep_past_level(self):
        book, coll = self._setup([N(1, 2, 101, 5), N(2, 2, 103, 4)])
        head = coll.place(book, 0, 2, 0, 0, E.EPOCH_NS + 2000)       # at the head of 101
        tail = coll.place(book, 0, 2, 0, 1, E.EPOCH_NS + 2000)       # behind order 1
        self._feed(book, coll, [N(3, 1, 103, 9)], 3)   # 5@101 then 4@103
        self.assertEqual(head.row['first_fill_ns'], 1000)        # trade vs order 1 (behind head)
        self.assertEqual(head.own_rem, 1)                        # 5 + 4 taken
        self.assertEqual(tail.row['first_fill_ns'], 1000)        # swept past via 103 trade
        self.assertEqual((tail.ahead, tail.own_rem), (0, 6))

    def test_market_leftover_and_expiry(self):
        book, coll = self._setup([N(1, 2, 101, 5)], max_horizon_s=1)
        p = coll.place(book, 0, 2, 0, 1, E.EPOCH_NS + 1000)
        q = coll.place(book, 0, 2, 0, 1, E.EPOCH_NS + 1000)
        self._feed(book, coll, [N(2, 1, 0, 30, 1)], 2)         # market: 5@101, 25 cancelled
        self.assertEqual((p.row['first_fill_ns'], p.row['full_fill_ns']), (1000, 1000))
        self.assertEqual(q.row['full_fill_ns'], 1000)
        coll.sample_every = 1
        rec = to_record(3, C(999))
        rec = tuple([3, E.EPOCH_NS + 3 * NS] + list(rec[2:]))      # 3 s later
        coll.on_event(book, rec, book.process(rec), [])
        self.assertEqual(coll.state[0].probes, [])                 # expired, outcome known
        self.assertEqual(outcome(p.row, NS), 1)

    def test_stp_stop_only_fills_probes_ahead_of_blocker(self):
        book, coll = self._setup([N(1, 2, 101, 5, 0, 3), N(2, 2, 101, 6, 0, 4)])
        ahead = coll.place(book, 0, 2, 0, 0, E.EPOCH_NS + 2000)      # ahead of order 1
        behind = coll.place(book, 0, 2, 0, 1, E.EPOCH_NS + 2000)     # behind order 1
        self._feed(book, coll, [N(3, 1, 101, 4, 0, 3)], 3)   # trader 3: STP stop at order 1
        self.assertEqual(ahead.row['first_fill_ns'], 1000)
        self.assertIsNone(behind.row['first_fill_ns'])
        book2, coll2 = self._setup([N(1, 2, 101, 5, 0, 3)], stp='cancel_old')
        p = coll2.place(book2, 0, 2, 0, 1, E.EPOCH_NS + 1000)
        self._feed(book2, coll2, [N(2, 1, 101, 2, 0, 3)], 2)  # order 1 vanishes (cancel_old)
        self.assertEqual(p.row['first_fill_ns'], 1000)
        self.assertEqual(p.own_rem, 8)

    def test_rates_and_regime(self):
        book, coll = self._setup([N(1, 2, 101, 3), N(2, 2, 101, 4), N(3, 2, 101, 5),
                                  N(4, 1, 101, 2), N(5, 1, 99, 6),
                                  RAW(KIND_CANCEL, 1, 0, 0, 5, 0, 0),
                                  RAW(KIND_MODIFY, 2, 0, 0, 2, 0, 1)], window_ns=NS)
        s = coll.state[0]
        rates = coll._rates(s, E.EPOCH_NS + 8000, 2)
        self.assertEqual((rates['rate_arr'], rates['rate_trd'], rates['rate_cxl'],
                          rates['rate_events']), (12.0, 2.0, 3.0, 7.0))
        bid = coll._rates(s, E.EPOCH_NS + 8000, 1)
        self.assertEqual((bid['rate_cxl'], bid['rate_arr'], bid['rate_trd_opp']), (6.0, 6.0, 2.0))
        self.assertEqual(coll._rates(s, E.EPOCH_NS + NS + 8000, 2)['rate_events'], 0.0)
        self.assertEqual(coll._regime(s, E.EPOCH_NS + 10 ** 6), 'normal')
        self.assertEqual(coll._regime(s, E.EPOCH_NS + 10), 'shock')
        for k in range(20):
            s.rng = random.Random(k)
            coll._sample(s, book, 0, E.EPOCH_NS + 9000)
        self.assertTrue(all(r['ahead'] + 0 <= r['level_total'] for r in coll.rows))
        self.assertTrue(any(r['n_ahead'] == 0 for r in coll.rows))

    def test_csv_roundtrip_and_fields(self):
        rows = synthetic_rows(30, 1)
        coll = ProbeCollector([0], {'regimes': []})
        coll.rows = rows
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, 'p.csv')
        self.assertEqual(coll.write_csv(path), 30)
        back = read_csv(path)
        self.assertEqual(len(back), 30)
        for a, b in zip(rows, back):
            for f in FIELDS:
                self.assertEqual(a[f], b[f], f)


class EndToEndTest(unittest.TestCase):
    def test_collect_fit_evaluate_on_small_stream(self):
        from lob import replay as R
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, 'ev.bin')
        meta = E.generate(E.GenParams(n_events=60000, n_symbols=2, seed=9, rate=5000.0),
                          path)
        coll = ProbeCollector([0, 1], meta, sample_every=5, seed=1)
        status = bytearray(meta['n_events'])
        with open(os.path.join(tmp, 't.bin'), 'wb') as tfh:
            core = R.Core(R.configs_from_meta(meta, [0, 1]), status, tfh)
            core.on_event = coll.on_event
            core.run(path, [0, 1])
        coll.finalize()
        csv_path = os.path.join(tmp, 'probes.csv')
        n = coll.write_csv(csv_path)
        self.assertGreater(n, 1000)
        self.assertEqual(coll.state[0].probes, [])
        fills = 0
        for r in coll.rows:
            self.assertLessEqual(r['ahead'], r['level_total'])
            self.assertLess(r['d'], 5)
            if r['full_fill_ns'] is not None:
                self.assertLessEqual(r['first_fill_ns'], r['full_fill_ns'])
            fills += r['first_fill_ns'] is not None
        self.assertTrue(0 < fills < n)
        res, model = Q.run(csv_path, os.path.join(tmp, 'qm'), horizons=(1, 2))
        self.assertEqual(res['probes'], n)
        for h in ('1', '2'):
            ev = res['per_horizon'][h]
            self.assertGreater(ev['n'], 100)
            self.assertIsNotNone(ev['brier'])
            self.assertTrue(os.path.exists(ev['svg']))
        self.assertTrue(os.path.exists(os.path.join(tmp, 'qm', 'queue_model.json')))
