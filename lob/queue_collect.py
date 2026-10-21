"""Ground truth for the queue model: shadow ("virtual") orders.

At every `sample_every`-th event of a symbol a virtual order of ours is
placed on a random side, at a random one of the top-K price levels, at a
random queue position (behind j of the live orders, j = 0..n: j = n is the
tail, i.e. "place an order now").  It never changes the market (zero-impact
assumption) and is never cancelled, so there is no cancel censoring; only
the end of the stream censors.  It is tracked exactly:

  * a trade at our price against an order AHEAD of us consumes `ahead`;
  * a trade at our price against an order BEHIND us (arrived later, or an
    iceberg refreshed to the tail, or increased) means the aggressor had
    reached our position: we get that quantity first;
  * a trade on our side at a WORSE price means the aggressor swept past our
    level: we are filled by that quantity;
  * an opposite-side NEW order whose limit crosses our price and that keeps
    an unfilled remainder (rests / IOC-market remainder) would have hit us;
  * cancels / reductions of orders ahead reduce `ahead`; an increase moves
    the order behind us; a self-trade cancellation of an order ahead
    (disappears without a cancel record) is detected after the event.
Cancel / modify records carry the order's side (the generator writes it).
Event timestamps are read through a per-symbol monotone clock, so an
out-of-order (regressed) timestamp counts as "now".
"""
import bisect
import collections
import csv
import random

from lob import events as E
from lob.book import O_OID, O_VIS, O_PRIO, Q_HEAD
from lob.common import (SIDE_BUY, SIDE_SELL, ST_RESTED, ST_PARTIAL_RESTED,
                        ST_ACK_CANCEL, ST_ACK_MODIFY, ST_CANCELLED,
                        ST_PARTIAL_CANCELLED, FLAG_MARKET,
                        T_AGGR_SIDE, T_BUY_OID, T_SELL_OID, T_PRICE, T_QTY)

FIELDS = ['sym', 't0', 'oid', 'side', 'd', 'dist', 'ahead', 'n_ahead', 'level_total',
          'own', 'spread', 'opp_best_qty', 'n_levels', 'rate_arr', 'rate_cxl',
          'rate_trd', 'rate_trd_opp', 'rate_events', 'regime', 'first_fill_ns',
          'full_fill_ns', 'censor_ns']
DEFAULT_HORIZONS = (1, 2, 5, 10)


class _Probe:
    __slots__ = ('row', 'side', 'price', 'ahead', 'ahead_orders', 'own_rem', 'sym')

    def __init__(self, row, ahead_orders):
        self.row = row
        self.side = row['side']
        self.price = row['dist_price']
        self.ahead = row['ahead']
        self.ahead_orders = ahead_orders       # oid -> visible qty ahead of us
        self.own_rem = row['own']
        self.sym = row['sym']


class _SymState:
    __slots__ = ('rng', 'count', 'recent', 'probes', 'watch', 'cancelled_before',
                 'regimes', 'ref', 'clock')

    def __init__(self, seed, regimes, ref):
        self.rng = random.Random(seed)
        self.count = 0
        self.clock = 0                         # monotone symbol clock (ts regressions)
        self.recent = collections.deque()      # (ts, side, kind, lots)
        self.probes = []                       # active shadow orders
        self.watch = {}                        # oid -> [probes with that oid ahead]
        self.cancelled_before = 0
        self.regimes = regimes                 # sorted (start, end, name)
        self.ref = ref


class ProbeCollector:
    def __init__(self, symbols, meta, sample_every=50, seed=99, window_ns=2 * E.NS,
                 top_levels=5, own_qty=10, max_horizon_s=10):
        regimes = {s: [] for s in symbols}
        for sym, start, end, name in meta.get('regimes', []):
            if sym in regimes:
                regimes[sym].append((start, end, name))
        books = meta.get('books', {})
        self.state = {s: _SymState(seed * 1000 + s, sorted(regimes[s]),
                                   books.get(str(s), {}).get('ref_price', 0))
                      for s in symbols}
        self.sample_every = sample_every
        self.window_ns = window_ns
        self.top_levels = top_levels
        self.own_qty = own_qty
        self.max_horizon_ns = max_horizon_s * E.NS
        self.band = {s: books.get(str(s), {}).get('band', 10 ** 9) for s in symbols}
        self.rows = []
        self.end_ts = 0

    # ---------------------------------------------------------------- hook
    def on_event(self, book, rec, st, trades):
        s = self.state[rec[2]]
        kind, side = rec[3], rec[4]
        if rec[1] > s.clock:
            s.clock = rec[1]
        ts = s.clock                 # a regressed timestamp is treated as "now"
        if ts > self.end_ts:
            self.end_ts = ts
        if kind == E.KIND_NEW and (st == ST_RESTED or st == ST_PARTIAL_RESTED):
            s.recent.append((ts, side, 0, rec[10]))
        elif kind != E.KIND_NEW and (st == ST_ACK_CANCEL or st == ST_ACK_MODIFY):
            lots = book.cancelled - s.cancelled_before
            if lots > 0 and side in (SIDE_BUY, SIDE_SELL):
                s.recent.append((ts, side, 1, lots))
            if s.probes and rec[8] in s.watch:
                self._ahead_changed(s, book, rec[8], ts)
        s.cancelled_before = book.cancelled
        if trades:
            for t in trades:
                s.recent.append((ts, SIDE_SELL if t[T_AGGR_SIDE] == SIDE_BUY else SIDE_BUY,
                                 2, t[T_QTY]))
            if s.probes:
                self._trades(s, book, trades, ts)
        if s.probes and kind == E.KIND_NEW:
            self._aggressor_leftover(s, book, rec, st, trades, ts)
        s.ref = book.ref
        s.count += 1
        if s.count % self.sample_every == 0:
            self._expire(s, ts)
            self._sample(s, book, rec[2], ts)

    # ------------------------------------------------------- probe updates
    def _fill(self, s, p, qty, ts):
        if p.own_rem <= 0 or qty <= 0:
            return
        row = p.row
        if row['first_fill_ns'] is None:
            row['first_fill_ns'] = ts - row['t0']
        p.own_rem -= qty
        if p.own_rem <= 0:
            row['full_fill_ns'] = ts - row['t0']
            self._retire(s, p)

    def _retire(self, s, p):
        s.probes.remove(p)
        for oid in p.ahead_orders:
            lst = s.watch.get(oid)
            if lst is not None:
                lst.remove(p)
                if not lst:
                    del s.watch[oid]

    def _consume_ahead(self, s, p, oid, qty):
        """qty of the order `oid` (ahead of p) was consumed / cancelled."""
        left = p.ahead_orders[oid] - qty
        if left <= 0:
            del p.ahead_orders[oid]
            lst = s.watch[oid]
            lst.remove(p)
            if not lst:
                del s.watch[oid]
            qty += left              # never below the tracked quantity
        else:
            p.ahead_orders[oid] = left
        p.ahead -= qty

    def _trades(self, s, book, trades, ts):
        for t in trades:
            buy_aggr = t[T_AGGR_SIDE] == SIDE_BUY
            rest_side = SIDE_SELL if buy_aggr else SIDE_BUY
            roid = t[T_SELL_OID] if buy_aggr else t[T_BUY_OID]
            price, qty = t[T_PRICE], t[T_QTY]
            for p in list(s.probes):
                if p.side != rest_side:
                    continue
                if price == p.price:
                    if roid in p.ahead_orders:
                        self._consume_ahead(s, p, roid, qty)
                    else:                       # resting order was behind us
                        self._fill(s, p, qty, ts)
                elif (price > p.price) if rest_side == SIDE_SELL else (price < p.price):
                    self._fill(s, p, qty, ts)   # aggressor swept past our level

    def _aggressor_leftover(self, s, book, rec, st, trades, ts):
        """Opposite-side order with an unfilled remainder crossing our price.
        If the real matching stopped early (self-trade prevention, cancel_new:
        a crossing level with live orders is still there) only probes ahead
        of the blocking head order would have been hit."""
        if st not in (ST_RESTED, ST_PARTIAL_RESTED, ST_CANCELLED, ST_PARTIAL_CANCELLED):
            return
        side, flags, price, qty = rec[4], rec[5], rec[9], rec[10]
        if flags & FLAG_MARKET:
            limit = s.ref + self.band[rec[2]] if side == SIDE_BUY else s.ref - self.band[rec[2]]
        else:
            limit = price
        leftover = qty - sum(t[T_QTY] for t in trades)
        if leftover <= 0:
            return
        blocker = self._blocker(book, side, limit)
        for p in list(s.probes):
            if p.side == side:
                continue
            if (p.price > limit) if side == SIDE_BUY else (p.price < limit):
                continue
            if blocker is not None:
                b_price, b_oid = blocker
                behind_blocker = (p.price > b_price) if side == SIDE_BUY else (p.price < b_price)
                if behind_blocker or (p.price == b_price and b_oid in p.ahead_orders):
                    continue
            self._fill(s, p, leftover, ts)

    @staticmethod
    def _blocker(book, side, limit):
        """Head order of the best opposite level still crossing `limit`
        after the event (None if liquidity was genuinely exhausted)."""
        if side == SIDE_BUY:
            keys, qq, sgn, lim_key = book.ask_keys, book.ask_queue, -1, -limit
        else:
            keys, qq, sgn, lim_key = book.bid_keys, book.bid_queue, 1, limit
        if not keys or keys[-1] < lim_key:
            return None
        queue = qq[keys[-1]]
        for o in queue[queue[Q_HEAD]:]:
            if o is not None:
                return sgn * keys[-1], o[O_OID]
        return None

    def _ahead_changed(self, s, book, oid, ts):
        """Cancel / modify of an order that is ahead of some probe."""
        o = book.orders.get(oid)
        for p in list(s.watch.get(oid, ())):
            tracked = p.ahead_orders[oid]
            if o is None:                                   # cancelled
                self._consume_ahead(s, p, oid, tracked)
            elif o[O_PRIO] > p.row['_marker']:              # increased -> behind us
                self._consume_ahead(s, p, oid, tracked)
            elif o[O_VIS] < tracked:                        # reduced in place
                self._consume_ahead(s, p, oid, tracked - o[O_VIS])

    def _expire(self, s, ts):
        """Retire probes older than the longest horizon (outcome fully known)."""
        for p in list(s.probes):
            if ts - p.row['t0'] > self.max_horizon_ns:
                self._retire(s, p)

    # -------------------------------------------------------------- sample
    def _sample(self, s, book, sym, ts):
        side = SIDE_BUY if s.rng.random() < 0.5 else SIDE_SELL
        keys = book.bid_keys if side == SIDE_BUY else book.ask_keys
        if not keys:
            return None
        d = s.rng.randrange(min(self.top_levels, len(keys)))
        queue = (book.bid_queue if side == SIDE_BUY else book.ask_queue)[keys[-1 - d]]
        n_live = sum(1 for o in queue[queue[Q_HEAD]:] if o is not None)
        return self.place(book, sym, side, d, s.rng.randrange(n_live + 1), ts)

    def place(self, book, sym, side, d, j, ts):
        """Place a shadow order on `side`, at the d-th best level, behind the
        first j live orders of that level (j = number ahead; j = n: tail)."""
        s = self.state[sym]
        if side == SIDE_BUY:
            keys, qd, qq, sgn = book.bid_keys, book.bid_qty, book.bid_queue, 1
        else:
            keys, qd, qq, sgn = book.ask_keys, book.ask_qty, book.ask_queue, -1
        key = keys[-1 - d]
        queue = qq[key]
        live = [o for o in queue[queue[Q_HEAD]:] if o is not None]
        ahead_orders = {o[O_OID]: o[O_VIS] for o in live[:j]}
        bb, ba = book.best_bid(), book.best_ask()
        opp_keys, opp_qd = (book.ask_keys, book.ask_qty) if side == SIDE_BUY \
            else (book.bid_keys, book.bid_qty)
        row = {'sym': sym, 't0': ts, 'oid': live[j - 1][O_OID] if j else 0, 'side': side,
               'd': d, 'dist': abs(sgn * keys[-1] - sgn * key), 'dist_price': sgn * key,
               'ahead': sum(ahead_orders.values()), 'n_ahead': j, 'level_total': qd[key],
               'own': self.own_qty,
               'spread': (ba - bb) if (bb is not None and ba is not None) else -1,
               'opp_best_qty': opp_qd[opp_keys[-1]] if opp_keys else 0,
               'n_levels': len(keys), 'regime': self._regime(s, ts),
               'first_fill_ns': None, 'full_fill_ns': None, 'censor_ns': None,
               '_marker': book.prio}
        row.update(self._rates(s, ts, side))
        p = _Probe(row, ahead_orders)
        for oid in ahead_orders:
            s.watch.setdefault(oid, []).append(p)
        s.probes.append(p)
        self.rows.append(row)
        return p

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
        """Stream ended: probes still open are censored at the end timestamp."""
        for s in self.state.values():
            for p in list(s.probes):
                p.row['censor_ns'] = self.end_ts - p.row['t0']
                self._retire(s, p)
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
    """1 = filled within horizon, 0 = not filled within horizon,
    -1 = censored (stream ended before the horizon without a fill)."""
    fill = row['full_fill_ns'] if full else row['first_fill_ns']
    if fill is not None and fill <= horizon_ns:
        return 1
    cens = row['censor_ns']
    if cens is not None and cens < horizon_ns:
        return -1
    return 0
