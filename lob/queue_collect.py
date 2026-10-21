"""Ground truth for the queue model: probe real resting orders during a
replay and record whether/when they get filled.

Every `sample_every` events of a symbol one live order from the top-K levels
of a random side is sampled ("our order").  Features are taken at that
moment; the outcome is the first (and full) execution time of that very
order in the same replay.  A probe whose order is cancelled or modified
before any fill is censored (the synthetic generator cancels uniformly at
random, independently of queue state, so this censoring is non-informative).
"""
import bisect
import collections
import csv
import random

from lob import events as E
from lob.book import O_OID, O_REM, O_VIS, O_SLOT, Q_HEAD
from lob.common import (SIDE_BUY, SIDE_SELL, ST_RESTED, ST_PARTIAL_RESTED,
                        ST_ACK_CANCEL, ST_ACK_MODIFY, T_AGGR_SIDE, T_BUY_OID,
                        T_SELL_OID, T_QTY)

FIELDS = ['sym', 't0', 'oid', 'side', 'd', 'dist', 'ahead', 'level_total', 'own_vis',
          'own_rem', 'spread', 'opp_best_qty', 'n_levels', 'rate_arr', 'rate_cxl',
          'rate_trd', 'rate_trd_opp', 'rate_events', 'regime', 'first_fill_ns',
          'full_fill_ns', 'censor_ns']
DEFAULT_HORIZONS = (1, 2, 5, 10)


class _SymState:
    __slots__ = ('rng', 'count', 'recent', 'active', 'cancelled_before', 'regimes')

    def __init__(self, seed, regimes):
        self.rng = random.Random(seed)
        self.count = 0
        self.recent = collections.deque()      # (ts, side, kind, lots)
        self.active = {}                       # oid -> [row, ...]
        self.cancelled_before = 0
        self.regimes = regimes                 # sorted (start, end, name)


class ProbeCollector:
    def __init__(self, symbols, meta, sample_every=50, seed=99, window_ns=2 * E.NS,
                 top_levels=5):
        regimes = {s: [] for s in symbols}
        for sym, start, end, name in meta.get('regimes', []):
            if sym in regimes:
                regimes[sym].append((start, end, name))
        self.state = {s: _SymState(seed * 1000 + s, sorted(regimes[s])) for s in symbols}
        self.sample_every = sample_every
        self.window_ns = window_ns
        self.top_levels = top_levels
        self.rows = []
        self.end_ts = 0

    # ---------------------------------------------------------------- hook
    def on_event(self, book, rec, st, trades):
        s = self.state[rec[2]]
        ts, kind, side = rec[1], rec[3], rec[4]
        if ts > self.end_ts:
            self.end_ts = ts
        if kind == E.KIND_NEW and (st == ST_RESTED or st == ST_PARTIAL_RESTED):
            s.recent.append((ts, side, 0, rec[10]))
        elif kind != E.KIND_NEW and (st == ST_ACK_CANCEL or st == ST_ACK_MODIFY):
            lots = book.cancelled - s.cancelled_before
            if lots > 0 and side in (SIDE_BUY, SIDE_SELL):
                s.recent.append((ts, side, 1, lots))
            if rec[8] in s.active:
                self._censor(s, rec[8], ts)
        s.cancelled_before = book.cancelled
        for t in trades:
            resting_side = SIDE_SELL if t[T_AGGR_SIDE] == SIDE_BUY else SIDE_BUY
            s.recent.append((ts, resting_side, 2, t[T_QTY]))
            roid = t[T_SELL_OID] if t[T_AGGR_SIDE] == SIDE_BUY else t[T_BUY_OID]
            if roid in s.active:
                self._fill(s, roid, ts, t[T_QTY])
        s.count += 1
        if s.count % self.sample_every == 0:
            self._sample(s, book, rec[2], ts)

    def _censor(self, s, oid, ts):
        for row in s.active.pop(oid):
            row['censor_ns'] = ts - row['t0']

    def _fill(self, s, oid, ts, qty):
        rows = s.active[oid]
        done = []
        for row in rows:
            if row['first_fill_ns'] is None:
                row['first_fill_ns'] = ts - row['t0']
            row['_filled'] += qty
            if row['_filled'] >= row['own_rem'] and row['full_fill_ns'] is None:
                row['full_fill_ns'] = ts - row['t0']
                done.append(row)
        for row in done:
            rows.remove(row)
        if not rows:
            del s.active[oid]

    # -------------------------------------------------------------- sample
    def _sample(self, s, book, sym, ts):
        side = SIDE_BUY if s.rng.random() < 0.5 else SIDE_SELL
        if side == SIDE_BUY:
            keys, qd, qq, sgn = book.bid_keys, book.bid_qty, book.bid_queue, 1
        else:
            keys, qd, qq, sgn = book.ask_keys, book.ask_qty, book.ask_queue, -1
        if not keys:
            return
        d = s.rng.randrange(min(self.top_levels, len(keys)))
        key = keys[-1 - d]
        queue = qq[key]
        live = [o for o in queue[queue[Q_HEAD]:] if o is not None]
        if not live:
            return
        o = live[s.rng.randrange(len(live))]
        ahead = 0
        for x in live:
            if x is o:
                break
            ahead += x[O_VIS]
        rates = self._rates(s, ts, side)
        bb, ba = book.best_bid(), book.best_ask()
        opp_keys, opp_qd = (book.ask_keys, book.ask_qty) if side == SIDE_BUY \
            else (book.bid_keys, book.bid_qty)
        row = {'sym': sym, 't0': ts, 'oid': o[O_OID], 'side': side, 'd': d,
               'dist': abs(sgn * keys[-1] - sgn * key), 'ahead': ahead,
               'level_total': qd[key], 'own_vis': o[O_VIS], 'own_rem': o[O_REM],
               'spread': (ba - bb) if (bb is not None and ba is not None) else -1,
               'opp_best_qty': opp_qd[opp_keys[-1]] if opp_keys else 0,
               'n_levels': len(keys), 'regime': self._regime(s, ts),
               'first_fill_ns': None, 'full_fill_ns': None, 'censor_ns': None,
               '_filled': 0}
        row.update(rates)
        s.active.setdefault(o[O_OID], []).append(row)
        self.rows.append(row)

    def _rates(self, s, ts, side):
        lo = ts - self.window_ns
        recent = s.recent
        while recent and recent[0][0] < lo:
            recent.popleft()
        arr = cxl = trd = trd_opp = 0
        for _ts, sd, kind, lots in recent:
            if kind == 2:
                if sd == side:
                    trd += lots
                else:
                    trd_opp += lots
            elif sd == side:
                if kind == 0:
                    arr += lots
                else:
                    cxl += lots
        w = self.window_ns / E.NS
        return {'rate_arr': arr / w, 'rate_cxl': cxl / w, 'rate_trd': trd / w,
                'rate_trd_opp': trd_opp / w, 'rate_events': len(recent) / w}

    def _regime(self, s, ts):
        i = bisect.bisect_right(s.regimes, (ts, float('inf'), ''))
        if i and s.regimes[i - 1][0] <= ts < s.regimes[i - 1][1]:
            return s.regimes[i - 1][2]
        return 'normal'

    # ---------------------------------------------------------------- output
    def finalize(self):
        """Censor unresolved probes at the end of the stream."""
        for s in self.state.values():
            for oid in list(s.active):
                self._censor(s, oid, self.end_ts)
        return self.rows

    def write_csv(self, path):
        with open(path, 'w', newline='') as fh:
            w = csv.writer(fh)
            w.writerow(FIELDS)
            for r in self.rows:
                w.writerow(['' if r[f] is None else r[f] for f in FIELDS])
        return len(self.rows)


def read_csv(path):
    rows = []
    with open(path, newline='') as fh:
        for r in csv.DictReader(fh):
            row = {}
            for k, v in r.items():
                if v == '':
                    row[k] = None
                elif k == 'regime':
                    row[k] = v
                elif k.startswith('rate_'):
                    row[k] = float(v)
                else:
                    row[k] = int(v)
            rows.append(row)
    return rows


def outcome(row, horizon_ns, full=False):
    """1 = filled within horizon, 0 = not filled (and alive at horizon),
    -1 = censored (cancelled/modified or stream ended before horizon)."""
    fill = row['full_fill_ns'] if full else row['first_fill_ns']
    if fill is not None and fill <= horizon_ns:
        return 1
    cens = row['censor_ns']
    if cens is not None and cens < horizon_ns:
        return -1
    return 0
