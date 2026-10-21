"""Price-time priority limit order book (the fast engine).

Data structures (chosen after the benchmark in tools/bench_structures.py):
  * per side a sorted Python list of price keys with the BEST key at the END
    (bids: price ascending; asks: negated price ascending) -> best lookup and
    best removal are O(1), insertion is bisect + memmove (C speed);
  * per price level: a visible-quantity dict and a FIFO list
    [head_index, dead_count, order, order, ...] with lazy deletion (cancelled
    slots become None) and amortised compaction;
  * orders are plain lists (no objects): see O_* field indices below;
  * trades are appended to self.trades as tuples (layout: common.T_*).
Semantics: SPEC.md.  Must agree trade-for-trade with reference.py.
"""
from bisect import bisect_left, insort

from lob.common import (
    KIND_NEW, KIND_CANCEL, KIND_MODIFY, SIDE_BUY, SIDE_SELL, FLAG_MARKET,
    FLAG_IOC, FLAG_FOK, FLAG_POST_ONLY, FLAG_ICEBERG, ST_RESTED, ST_FILLED,
    ST_PARTIAL_RESTED, ST_PARTIAL_CANCELLED, ST_CANCELLED, ST_ACK_CANCEL,
    ST_ACK_MODIFY, REJ_KIND, REJ_SIDE, REJ_FLAGS, REJ_DUP, REJ_QTY, REJ_PRICE,
    REJ_BAND, REJ_DISPLAY, REJ_POST_ONLY, REJ_FOK, REJ_UNKNOWN,
    STP_CANCEL_NEW, flags_valid)

# order list layout
O_OID, O_SIDE, O_PRICE, O_REM, O_VIS, O_DISP, O_TRADER, O_PRIO, O_SLOT = range(9)
# queue list layout: [head_index, dead_count, entries...]
#   dead_count = number of None slots anywhere in entries (compaction trigger)
Q_HEAD, Q_DEAD, Q_FIRST = 0, 1, 2
COMPACT_MIN_DEAD = 32
COMPACT_MIN_HEAD = 64


class Book:
    __slots__ = ('cfg', 'symbol', 'tick', 'lot', 'min_qty', 'max_qty', 'band',
                 'stp_new', 'ref', 'orders', 'bid_keys', 'ask_keys', 'bid_qty',
                 'ask_qty', 'bid_queue', 'ask_queue', 'trades', 'prio', 'seq',
                 'submitted', 'filled', 'cancelled', 'rejected', 'resting',
                 'stp_cancels', 'n_events', 'last_ts', 'ts_regressions',
                 'dirty', '_snap', '_depth')

    def __init__(self, cfg):
        self.cfg = cfg
        self.symbol = cfg.symbol
        self.tick = cfg.tick_size
        self.lot = cfg.lot_size
        self.min_qty = cfg.min_qty
        self.max_qty = cfg.max_qty
        self.band = cfg.band
        self.stp_new = cfg.stp == STP_CANCEL_NEW
        self.ref = cfg.ref_price
        self.orders = {}
        self.bid_keys, self.ask_keys = [], []
        self.bid_qty, self.ask_qty = {}, {}
        self.bid_queue, self.ask_queue = {}, {}
        self.trades = []
        self.prio = 0
        self.seq = 0
        self.submitted = self.filled = self.cancelled = self.rejected = 0
        self.resting = 0
        self.stp_cancels = 0
        self.n_events = 0
        self.last_ts = 0
        self.ts_regressions = 0
        self.dirty = True
        self._snap = None
        self._depth = 0

    # ------------------------------------------------------------------ API
    def process(self, rec):
        """Process one decoded event record; return a status code."""
        kind = rec[3]
        self.seq = rec[0]
        self.n_events += 1
        ts = rec[1]
        if ts < self.last_ts:
            self.ts_regressions += 1
        else:
            self.last_ts = ts
        if kind == KIND_NEW:
            return self._new(rec[4], rec[5], rec[7], rec[8], rec[9], rec[10], rec[11])
        if kind == KIND_CANCEL:
            return self._cancel(rec[8])
        if kind == KIND_MODIFY:
            return self._modify(rec[8], rec[10])
        return REJ_KIND

    def best_bid(self):
        return self.bid_keys[-1] if self.bid_keys else None

    def best_ask(self):
        return -self.ask_keys[-1] if self.ask_keys else None

    def top(self, depth=10):
        """Top-`depth` snapshot: (bid_prices, bid_qtys, ask_prices, ask_qtys),
        best first.  Cached until the next mutation (amortised cost)."""
        if not self.dirty and self._depth == depth:
            return self._snap
        bp = self.bid_keys[-1:-depth - 1:-1]
        ap = [-k for k in self.ask_keys[-1:-depth - 1:-1]]
        snap = (bp, list(map(self.bid_qty.__getitem__, bp)),
                ap, list(map(self.ask_qty.__getitem__, ap)))
        self._snap, self.dirty, self._depth = snap, False, depth
        return snap

    def levels(self, depth=None):
        n = depth if depth is not None else max(len(self.bid_keys), len(self.ask_keys))
        bp, bq, ap, aq = self.top(n) if n else ([], [], [], [])
        return list(zip(bp, bq)), list(zip(ap, aq))

    def live_orders(self):
        return list(self.orders.values())

    def resting_check(self):
        return sum(o[O_REM] for o in self.orders.values())

    def digest(self):
        """Structural fingerprint used by the differential test."""
        h = hash((self.ref, self.submitted, self.filled, self.cancelled,
                  self.rejected, self.resting, self.stp_cancels))
        for keys, qd in ((self.bid_keys, self.bid_qty), (self.ask_keys, self.ask_qty)):
            for k in keys:
                p = abs(k)
                h = hash((h, p, qd[p]))
        return h

    # ------------------------------------------------------------------ NEW
    def _validate(self, side, flags, oid, price, qty, aux):
        if side != SIDE_BUY and side != SIDE_SELL:
            return REJ_SIDE
        if flags and not flags_valid(flags):
            return REJ_FLAGS
        if oid in self.orders:
            return REJ_DUP
        if qty < self.min_qty or qty > self.max_qty or qty % self.lot:
            return REJ_QTY
        if not flags & FLAG_MARKET:
            if price <= 0 or price % self.tick:
                return REJ_PRICE
            if price > self.ref + self.band or price < self.ref - self.band:
                return REJ_BAND
        if flags & FLAG_ICEBERG and (aux < self.lot or aux > qty or aux % self.lot):
            return REJ_DISPLAY
        return 0

    def _new(self, side, flags, trader, oid, price, qty, aux):
        self.submitted += qty
        rej = self._validate(side, flags, oid, price, qty, aux)
        if rej:
            self.rejected += qty
            return rej
        if flags & FLAG_MARKET:
            limit = self.ref + self.band if side == SIDE_BUY else self.ref - self.band
        else:
            limit = price
        if side == SIDE_BUY:
            crosses = bool(self.ask_keys) and -self.ask_keys[-1] <= limit
        else:
            crosses = bool(self.bid_keys) and self.bid_keys[-1] >= limit
        if crosses:
            if flags & FLAG_POST_ONLY:
                self.rejected += qty
                return REJ_POST_ONLY
            if flags & FLAG_FOK and self._fillable(side, trader, limit, qty) < qty:
                self.rejected += qty
                return REJ_FOK
            rem, stopped = self._match(side, trader, oid, limit, qty)
        elif flags & FLAG_FOK:
            self.rejected += qty
            return REJ_FOK
        else:
            rem, stopped = qty, False
        if rem == 0:
            return ST_FILLED
        if stopped or flags & (FLAG_IOC | FLAG_MARKET):
            self.cancelled += rem
            return ST_PARTIAL_CANCELLED if rem < qty else ST_CANCELLED
        self._rest(side, price, rem, aux if flags & FLAG_ICEBERG else 0, trader, oid)
        return ST_PARTIAL_RESTED if rem < qty else ST_RESTED

    def _rest(self, side, price, rem, disp, trader, oid):
        vis = disp if disp and disp < rem else rem
        self.prio += 1
        o = [oid, side, price, rem, vis, disp, trader, self.prio, 0]
        if side == SIDE_BUY:
            qd, qq, keys, key = self.bid_qty, self.bid_queue, self.bid_keys, price
        else:
            qd, qq, keys, key = self.ask_qty, self.ask_queue, self.ask_keys, -price
        q = qd.get(price)
        if q is None:
            insort(keys, key)
            qd[price] = vis
            o[O_SLOT] = Q_FIRST
            qq[price] = [Q_FIRST, 0, o]
        else:
            qd[price] = q + vis
            queue = qq[price]
            o[O_SLOT] = len(queue)
            queue.append(o)
        self.orders[oid] = o
        self.resting += rem
        self.dirty = True

    def _match(self, side, trader, oid, limit, rem):
        """Match against the opposite side; return (remaining, stp_stopped)."""
        if side == SIDE_BUY:
            keys, qd, qq, sgn, lim_key = self.ask_keys, self.ask_qty, self.ask_queue, -1, -limit
        else:
            keys, qd, qq, sgn, lim_key = self.bid_keys, self.bid_qty, self.bid_queue, 1, limit
        orders, trades, seq, sym = self.orders, self.trades, self.seq, self.symbol
        stp_new, stopped = self.stp_new, False
        filled = cancelled = removed = k = 0
        while rem and keys and keys[-1] >= lim_key:
            price = sgn * keys[-1]
            queue = qq[price]
            head, n, lq = queue[Q_HEAD], len(queue), qd[price]
            while rem and head < n:
                o = queue[head]
                if o is None:
                    head += 1
                    continue
                if o[O_TRADER] == trader:
                    if stp_new:
                        stopped = True
                        break
                    head += 1
                    del orders[o[O_OID]]
                    lq -= o[O_VIS]
                    cancelled += o[O_REM]
                    removed += o[O_REM]
                    self.stp_cancels += 1
                    continue
                vis = o[O_VIS]
                fill = rem if rem < vis else vis
                if side == SIDE_BUY:
                    trades.append((seq, k, sym, side, oid, o[O_OID], price, fill,
                                   trader, o[O_TRADER]))
                else:
                    trades.append((seq, k, sym, side, o[O_OID], oid, price, fill,
                                   o[O_TRADER], trader))
                k += 1
                rem -= fill
                filled += fill
                lq -= fill
                o[O_REM] -= fill
                if o[O_REM] == 0:
                    del orders[o[O_OID]]
                    head += 1
                elif fill == vis:                       # iceberg slice exhausted
                    o[O_VIS] = o[O_DISP] if o[O_DISP] < o[O_REM] else o[O_REM]
                    lq += o[O_VIS]
                    self.prio += 1
                    o[O_PRIO] = self.prio
                    o[O_SLOT] = n
                    queue.append(o)
                    n += 1
                    head += 1
                else:
                    o[O_VIS] -= fill
            if lq == 0:
                keys.pop()
                del qd[price]
                del qq[price]
            else:
                qd[price] = lq
                queue[Q_HEAD] = head
                if head >= COMPACT_MIN_HEAD and head * 2 >= len(queue):
                    _compact(queue)
            if stopped:
                break
        self.filled += 2 * filled
        self.resting -= filled + removed
        self.cancelled += cancelled
        self.ref = self.ref if not filled else trades[-1][6]
        self.dirty = True
        return rem, stopped

    def _fillable(self, side, trader, limit, want):
        """Pure simulation of _match; the book is not modified."""
        if side == SIDE_BUY:
            keys, qq, sgn, lim_key = self.ask_keys, self.ask_queue, -1, -limit
        else:
            keys, qq, sgn, lim_key = self.bid_keys, self.bid_queue, 1, limit
        avail = 0
        for idx in range(len(keys) - 1, -1, -1):
            if keys[idx] < lim_key or avail >= want:
                break
            queue = qq[sgn * keys[idx]]
            sim = [(o[O_REM], o[O_VIS], o[O_DISP], o[O_TRADER])
                   for o in queue[queue[Q_HEAD]:] if o is not None]
            j = 0
            while j < len(sim) and avail < want:
                rem, vis, disp, tr = sim[j]
                j += 1
                if tr == trader:
                    if self.stp_new:
                        return avail
                    continue
                fill = want - avail if want - avail < vis else vis
                avail += fill
                if fill == vis and rem > vis:
                    sim.append((rem - vis, disp if disp < rem - vis else rem - vis, disp, tr))
        return avail

    # ------------------------------------------------------- CANCEL / MODIFY
    def _unlink(self, o):
        """Remove a live order from its level (lazy slot deletion)."""
        price, vis = o[O_PRICE], o[O_VIS]
        if o[O_SIDE] == SIDE_BUY:
            qd, qq, keys, key = self.bid_qty, self.bid_queue, self.bid_keys, price
        else:
            qd, qq, keys, key = self.ask_qty, self.ask_queue, self.ask_keys, -price
        q = qd[price] - vis
        if q == 0:
            del qd[price]
            del qq[price]
            del keys[bisect_left(keys, key)]
        else:
            qd[price] = q
            queue = qq[price]
            queue[o[O_SLOT]] = None
            dead = queue[Q_DEAD] + 1
            if dead >= COMPACT_MIN_DEAD and dead * 2 >= len(queue) - Q_FIRST:
                _compact(queue)
            else:
                queue[Q_DEAD] = dead
        self.dirty = True

    def _cancel(self, oid):
        o = self.orders.pop(oid, None)
        if o is None:
            return REJ_UNKNOWN
        self._unlink(o)
        self.cancelled += o[O_REM]
        self.resting -= o[O_REM]
        return ST_ACK_CANCEL

    def _modify(self, oid, new):
        o = self.orders.get(oid)
        if o is None:
            return REJ_UNKNOWN
        if new < self.min_qty or new > self.max_qty or new % self.lot:
            return REJ_QTY
        rem = o[O_REM]
        if new < rem:
            self.cancelled += rem - new
            self.resting -= rem - new
            o[O_REM] = new
            if o[O_VIS] > new:
                qd = self.bid_qty if o[O_SIDE] == SIDE_BUY else self.ask_qty
                qd[o[O_PRICE]] -= o[O_VIS] - new
                o[O_VIS] = new
                self.dirty = True
        elif new > rem:
            self.submitted += new - rem
            self.resting -= rem
            del self.orders[oid]
            self._unlink(o)
            self._rest(o[O_SIDE], o[O_PRICE], new, o[O_DISP], o[O_TRADER], oid)
        return ST_ACK_MODIFY


def _compact(queue):
    """Drop consumed/cancelled slots and renumber the survivors' slots."""
    live = [o for o in queue[queue[Q_HEAD]:] if o is not None]
    del queue[Q_FIRST:]
    queue[Q_HEAD] = Q_FIRST
    queue[Q_DEAD] = 0
    for i, o in enumerate(live, Q_FIRST):
        o[O_SLOT] = i
    queue.extend(live)
