"""Queue model: truth collection, calibration, evaluation, plotting."""
import json
import math
import os
import random
import tempfile
import unittest
import xml.etree.ElementTree as ET

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
        self.assertEqual(Q.u_bin(0.1), 1)
        self.assertEqual(Q.u_bin(15.9), 11)
        self.assertEqual(Q.u_bin(16.0), 12)
        self.assertEqual(Q.imbalance_bin(10.0, 10.0), 1)
        self.assertEqual(Q.imbalance_bin(10.0, 0.0), 2)
        self.assertEqual(Q.imbalance_bin(0.0, 10.0), 0)
        self.assertEqual(Q.imbalance_bin(6.0, 3.0), 1)      # x == +0.3 exactly: middle bin
        self.assertEqual(Q.imbalance_bin(3.0, 6.0), 1)      # x == -0.3 exactly: middle bin
        self.assertEqual(Q.imbalance_bin(6.1, 3.0), 2)
        self.assertEqual(Q.imbalance_bin(3.0, 6.1), 0)
        self.assertEqual((Q.spread_bin(1), Q.spread_bin(2), Q.spread_bin(5), Q.spread_bin(6)),
                         (0, 0, 1, 2))
        self.assertEqual(Q.cell_key(0, 0, 0.0, 5), (0 * 3 + 1) * 3 + 0)
        self.assertEqual(Q.cell_key(1, 0, 0.0, 5), (Q.N_U * 3 + 1) * 3)
        self.assertEqual(Q.cell_key(9, 0, 0.0, 5), Q.cell_key(8, 0, 0.0, 5))   # clipped
        self.assertEqual(Q.cell_key(0, 10, 2.0, 5), (Q.u_bin(10 / 11.0) * 3 + 1) * 3)
        self.assertEqual(Q.cell_key(0, 100, 0.0, 1, 0.0, 9), (12 * 3 + 1) * 3 + 2)
        keys = set(Q.cell_key(d, a, 1.0, 5, o, sp) for d in range(10) for a in (0, 5, 50, 500)
                   for o in (0.0, 1.0, 9.0) for sp in (1, 4, 9))
        self.assertEqual(len(keys), len(set(keys)))


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

    def test_cell_counts_cover_every_uncensored_row(self):
        for h in (1, 5):
            n_rows = sum(1 for r in self.train if outcome(r, h * NS) >= 0)
            self.assertEqual(sum(c[0] for c in self.model.tables[h].values()), n_rows)
            self.assertEqual(sum(c[1] for c in self.model.tables[h].values()),
                             sum(1 for r in self.train if outcome(r, h * NS) == 1))

    def test_default_spread_is_two_ticks(self):
        a = self.model.predict(3, 10, 1.0, 1.0, 4.0, 0, 5, 0, 4.0)
        self.assertEqual(a, self.model.predict(3, 10, 1.0, 1.0, 4.0, 0, 5, 0, 4.0, 2))
        self.assertEqual(self.model.time_to_fill(3, 4.0, 0, 5, 0, 4.0),
                         self.model.time_to_fill(3, 4.0, 0, 5, 0, 4.0, 2))

    def test_parametric_fit_is_the_grid_argmax(self):
        h = 5
        for d in range(Q.D_MAX + 1):
            data = [(r['rate_trd'], r['ahead'], outcome(r, h * NS)) for r in self.train
                    if min(r['d'], Q.D_MAX) == d]
            data = [t for t in data if t[2] >= 0]
            a, b = self.param.params[(h, d)]
            best = self.param._loglik(a, b, data, h)
            coarse = max(self.param._loglik(a10 / 10.0, bb, data, h)
                         for a10 in range(-60, 61, 5)
                         for bb in (0.0, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0))
            self.assertGreaterEqual(best, coarse, d)
            a0 = max(((a10 / 10.0, bb) for a10 in range(-60, 61, 5)
                      for bb in (0.0, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0)),
                     key=lambda ab: self.param._loglik(ab[0], ab[1], data, h))
            fine = [(a100 / 100.0, a0[1]) for a100 in range(int(a0[0] * 100) - 50,
                                                           int(a0[0] * 100) + 51, 5)]
            fine_best = max(fine, key=lambda ab: self.param._loglik(ab[0], ab[1], data, h))
            self.assertAlmostEqual(best, self.param._loglik(fine_best[0], fine_best[1], data, h),
                                   places=9, msg=str(d))
            self.assertEqual(b, a0[1])
        self.assertEqual(set(self.param.params), {(hh, d) for hh in (1, 5)
                                                  for d in range(Q.D_MAX + 1)})

    def test_parametric_refinement_window(self):
        """Optimum between coarse grid points (true scale 5*e^1.25) must be
        found by the fine search around the coarse argmax."""
        rng = random.Random(12)
        rows = []
        for i in range(3000):
            ahead = rng.randint(10, 30)
            p = 1.0 - math.exp(-5.0 * math.exp(1.25) / (ahead + 1))
            filled = rng.random() < p
            rows.append({'d': 0, 'ahead': ahead, 'rate_trd': 1.0,
                         'first_fill_ns': 2 * NS if filled else None, 'full_fill_ns': None,
                         'censor_ns': None})
        pm = Q.ParametricModel().fit(rows, (5,))
        a, b = pm.params[(5, 0)]
        data = [(r['rate_trd'], r['ahead'], outcome(r, 5 * NS)) for r in rows]
        grid = [(a10 / 10.0, bb) for a10 in range(-60, 61, 5)
                for bb in (0.0, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0)]
        a0, b0 = max(grid, key=lambda ab: pm._loglik(ab[0], ab[1], data, 5))
        fine = [a100 / 100.0 for a100 in range(int(a0 * 100) - 50, int(a0 * 100) + 51, 5)]
        best_a = max(fine, key=lambda aa: pm._loglik(aa, b0, data, 5))
        self.assertEqual((a, b), (best_a, b0))
        self.assertNotEqual(a, a0)                                  # refinement did move
        self.assertAlmostEqual(a, math.log(5.0 * math.exp(1.25) / (5.0 + b0)), delta=0.06)

    def test_base_rate_rounding(self):
        rows = synthetic_rows(7, 4)
        for i, r in enumerate(rows):
            r['censor_ns'] = None
            r['first_fill_ns'] = NS if i < 3 else None
        m = Q.QueueModel().fit(rows, (5,))
        self.assertEqual(Q.evaluate(m, rows, 5)['base_rate'], 0.4286)

    def test_evaluate_rounding_and_bins(self):
        rows = synthetic_rows(70, 9)
        m = Q.QueueModel().fit(rows, (5,))
        ev = Q.evaluate(m, rows, 5)
        pairs = [(m.predict_row(r, 5), outcome(r, 5 * NS)) for r in rows
                 if outcome(r, 5 * NS) >= 0]
        self.assertEqual(ev['base_rate'], round(sum(y for _, y in pairs) / len(pairs), 4))
        self.assertEqual(ev['brier'], round(Q.brier(pairs), 5))
        by_regime = {}
        for r in rows:                                                        # by_regime base_rate 也是 4 位
            if outcome(r, 5 * NS) >= 0:
                by_regime.setdefault(r['regime'], []).append(outcome(r, 5 * NS))
        self.assertEqual(sorted(ev['by_regime']), sorted(by_regime))
        for k, ys in by_regime.items():
            self.assertEqual(ev['by_regime'][k]['base_rate'], round(sum(ys) / len(ys), 4), k)
            self.assertEqual(ev['by_regime'][k]['n'], len(ys))
        self.assertLessEqual(len(ev['reliability']), 10)
        for b in ev['reliability']:
            self.assertAlmostEqual(b['hi'] - b['lo'], 0.1)
            self.assertEqual(b['lo'], b['bin'] / 10)

    def test_predictions_monotone_in_queue_position(self):
        p = [self.model.predict(a, 100, 1.0, 1.0, 10.0, 0, 5, 0, 10.0, 2) for a in (0, 10, 40, 200)]
        self.assertEqual(p, sorted(p, reverse=True))
        self.assertTrue(all(0.0 <= x <= 1.0 for x in p))
        self.assertGreater(self.model.predict(0, 1, 0, 0, 10.0, 0, 5, 0, 10.0),
                           self.model.predict(0, 1, 0, 0, 10.0, 3, 5, 3, 10.0))

    def test_nearest_horizon_table(self):
        """查询的 horizon 不在表里时取最近的表（|h − horizon| 最小），不是最小的 h。"""
        m = self.model                                                       # horizons (1, 5)
        self.assertEqual(m._horizon(5), 5)
        self.assertEqual(m._horizon(4), 5)
        self.assertEqual(m._horizon(2), 1)
        self.assertEqual(m._horizon(100), 5)

    def test_unseen_cell_falls_back_to_global_rate(self):
        m = Q.QueueModel().fit(self.train[:50], (5,))
        m.tables[5] = {}
        self.assertEqual(m.predict(5, 5, 0, 0, 0, 0, 5), m.global_rate[5])
        self.assertIsNone(m.time_to_fill(5, 0, 0, 5))
        self.assertIsNone(m.time_to_fill(5, 0, 0, 5, 0, 0.0, 2))
        self.assertEqual(m._horizon(4), 5)                     # nearest horizon

    def test_time_to_fill_is_cell_median(self):
        h = 5
        key = Q.cell_key(0, 0, 10.0, h, 10.0, 2)
        fills = sorted(r['first_fill_ns'] for r in self.train
                       if outcome(r, h * NS) == 1 and Q.row_key(r, h) == key)
        self.assertEqual(self.model.time_to_fill(0, 10.0, 0, h, 0, 10.0, 2),
                         fills[len(fills) // 2] / NS)

    def test_roundtrip_and_laplace_smoothing(self):
        d = json.loads(json.dumps(self.model.to_dict()))
        m2 = Q.QueueModel.from_dict(d)
        for r in self.test[:200]:
            self.assertEqual(m2.predict_row(r, 5), self.model.predict_row(r, 5))
        n, k, _ = self.model.tables[5][Q.cell_key(0, 0, 10.0, 5, 10.0, 2)]
        g = self.model.global_rate[5]
        self.assertAlmostEqual(self.model.predict(0, 0, 0, 0, 10.0, 0, 5, 0, 10.0, 2),
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

    def test_svg_geometry(self):
        tmp = tempfile.mkdtemp()
        rel = [{'bin': 0, 'lo': 0.0, 'hi': 0.1, 'n': 4, 'mean_pred': 0.05, 'mean_obs': 0.25},
               {'bin': 5, 'lo': 0.5, 'hi': 0.6, 'n': 16, 'mean_pred': 0.5, 'mean_obs': 0.5},
               {'bin': 9, 'lo': 0.9, 'hi': 1.0, 'n': 1, 'mean_pred': 0.9, 'mean_obs': 1.0}]
        path = Q.reliability_svg(rel, 'geometry check', os.path.join(tmp, 'r.svg'))
        root = ET.parse(path).getroot()
        ns = '{http://www.w3.org/2000/svg}'
        W, H, M = 480, 480, 56
        scale = W - 2 * M

        def X(p):
            return M + p * scale

        def Y(p):
            return H - M - p * scale
        self.assertEqual(root.get('viewBox'), '0 0 480 480')
        lines = root.findall(ns + 'line')
        self.assertEqual(len(lines), 3)
        self.assertEqual([lines[0].get(k) for k in ('x1', 'y1', 'x2', 'y2')],
                         ['%d' % X(0), '%d' % Y(0), '%d' % X(1), '%d' % Y(1)])
        self.assertEqual(lines[0].get('stroke-dasharray'), '4 4')
        self.assertEqual([lines[1].get(k) for k in ('x1', 'y1', 'x2', 'y2')],
                         ['56', '424', '424', '424'])
        self.assertEqual([lines[2].get(k) for k in ('x1', 'y1', 'x2', 'y2')],
                         ['56', '424', '56', '56'])
        texts = root.findall(ns + 'text')
        self.assertEqual(texts[0].text, 'geometry check')
        x_ticks = [t for t in texts if t.get('text-anchor') == 'middle' and t.get('y') == '442']
        y_ticks = [t for t in texts if t.get('text-anchor') == 'end']
        self.assertEqual([t.get('x') for t in x_ticks], ['%.0f' % X(v / 10) for v in range(0, 11, 2)])
        self.assertEqual([t.text for t in x_ticks], ['0.0', '0.2', '0.4', '0.6', '0.8', '1.0'])
        self.assertEqual([(t.get('x'), t.get('y')) for t in y_ticks],
                         [('50', '%.0f' % (Y(v / 10) + 4)) for v in range(0, 11, 2)])
        xlabel = [t for t in texts if t.text == 'predicted probability'][0]
        self.assertEqual((xlabel.get('x'), xlabel.get('y')), ('240', '466'))
        ylabel = [t for t in texts if t.text == 'observed frequency'][0]
        self.assertEqual(ylabel.get('transform'), 'translate(16,240) rotate(-90)')
        circles = root.findall(ns + 'circle')
        self.assertEqual(len(circles), 3)
        for c, b in zip(circles, rel):
            self.assertEqual((c.get('cx'), c.get('cy'), c.get('r')),
                             ('%.1f' % X(b['mean_pred']), '%.1f' % Y(b['mean_obs']),
                              '%.1f' % (3 + 9 * (b['n'] / 16) ** 0.5)))
            self.assertEqual(c.find(ns + 'title').text,
                             'n=%d pred=%.3f obs=%.3f' % (b['n'], b['mean_pred'], b['mean_obs']))
        poly = root.find(ns + 'polyline')
        self.assertEqual(poly.get('points'), ' '.join('%.1f,%.1f' % (X(b['mean_pred']), Y(b['mean_obs']))
                                                      for b in rel))
        single = Q.reliability_svg(rel[:1], 't', os.path.join(tmp, 's.svg'))
        self.assertIsNone(ET.parse(single).getroot().find(ns + 'polyline'))


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
            self.assertIsInstance(ev['brier_parametric'], float)
            self.assertIsInstance(ev['brier_train'], float)
            self.assertTrue(os.path.exists(ev['svg']))
            with open(ev['svg']) as sfh:
                self.assertIn('Brier %.4f' % ev['brier'], sfh.read())            # 标题写的是真实 Brier，不是 n/a
            self.assertEqual(set(ev['parametric_params']), {str(d) for d in range(Q.D_MAX + 1)})
        with open(os.path.join(tmp, 'qm', 'queue_model.json')) as fh:
            text = fh.read()
        self.assertEqual(text, json.dumps(model.to_dict(), sort_keys=True))     # canonical bytes
        with open(os.path.join(tmp, 'qm', 'queue_model_eval.json')) as fh:
            self.assertEqual(fh.read(), json.dumps(res, indent=1, sort_keys=True))
        res2, _ = Q.run(csv_path, os.path.join(tmp, 'qm'), horizons=(1,))     # existing dir ok
        self.assertEqual(res2['train'], res['train'])
