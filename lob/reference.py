"""Brute-force reference matching engine: a flat list of resting orders,
every lookup is a linear scan.  Written for obviousness, not speed.
Semantics: see SPEC.md.  The fast engine (book.py) must agree trade-for-trade.
"""
from lob.common import (
    KIND_NEW, KIND_CANCEL, KIND_MODIFY, SIDE_BUY, SIDE_SELL, FLAG_MARKET,
    FLAG_IOC, FLAG_FOK, FLAG_POST_ONLY, FLAG_ICEBERG, ST_RESTED, ST_FILLED,
    ST_PARTIAL_RESTED, ST_PARTIAL_CANCELLED, ST_CANCELLED, ST_ACK_CANCEL,
    ST_ACK_MODIFY, REJ_KIND, REJ_SIDE, REJ_FLAGS, REJ_DUP, REJ_QTY, REJ_PRICE,
    REJ_BAND, REJ_DISPLAY, REJ_POST_ONLY, REJ_FOK, REJ_UNKNOWN,
    STP_CANCEL_NEW, flags_valid)


class RefOrder:
    __slots__ = ('oid', 'side', 'price', 'rem', 'vis', 'display', 'trader',
                 'prio')

    def __init__(self, oid, side, price, rem, display, trader):
        self.oid = oid
        self.side = side
        self.price = price
        self.rem = rem
        self.display = display
        self.vis = min(display, rem) if display else rem
        self.trader = trader
        self.prio = 0


def _better(a, b, side):
    """Is resting order a ahead of b for an incoming order of `side`?"""
    if a.price != b.price:
        return a.price < b.price if side == SIDE_BUY else a.price > b.price
    return a.prio < b.prio


class ReferenceBook:
    def __init__(self, cfg):
        self.cfg = cfg
        self.rest = []
        self.by_id = {}
        self.prio = 0
        self.ref = cfg.ref_price
        self.trades = []
        self.seq = 0
        self.submitted = self.filled = self.cancelled = self.rejected = 0
        self.resting = 0
        self.stp_cancels = 0
        self.n_events = 0

    # ---- public API -----------------------------------------------------
    def process(self, rec):
        seq, _ts, _sym, kind, side, flags, _pad, trader, oid, price, qty, aux = rec
        self.seq = seq
        self.n_events += 1
        n0 = len(self.trades)
        if kind == KIND_NEW:
            st = self._new(side, flags, trader, oid, price, qty, aux)
        elif kind == KIND_CANCEL:
            st = self._cancel(oid)
        elif kind == KIND_MODIFY:
            st = self._modify(oid, qty)
        else:
            st = REJ_KIND
        if len(self.trades) > n0:
            self.ref = self.trades[-1][6]
        return st

    def best(self, side):
        """Best resting order on `side` (None if empty)."""
        best = None
        for o in self.rest:
            if o.side != side:
                continue
            if best is None:
                best = o
            elif o.price > best.price if side == SIDE_BUY else o.price < best.price:
                best = o
            elif o.price == best.price and o.prio < best.prio:
                best = o
        return best

    def best_bid(self):
        o = self.best(SIDE_BUY)
        return None if o is None else o.price

    def best_ask(self):
        o = self.best(SIDE_SELL)
        return None if o is None else o.price

    def levels(self, depth=None):
        bids, asks = {}, {}
        for o in self.rest:
            d = bids if o.side == SIDE_BUY else asks
            d[o.price] = d.get(o.price, 0) + o.vis
        b = sorted(bids.items(), reverse=True)
        a = sorted(asks.items())
        if depth is not None:
            b, a = b[:depth], a[:depth]
        return b, a

    def live_orders(self):
        return list(self.rest)

    def resting_check(self):
        return sum(o.rem for o in self.rest)

    # ---- NEW -------------------------------------------------------------
    def _validate(self, side, flags, oid, price, qty, aux):
        cfg = self.cfg
        if side not in (SIDE_BUY, SIDE_SELL):
            return REJ_SIDE
        if not flags_valid(flags):
            return REJ_FLAGS
        if oid in self.by_id:
            return REJ_DUP
        if qty < cfg.min_qty or qty > cfg.max_qty or qty % cfg.lot_size:
            return REJ_QTY
        if not flags & FLAG_MARKET:
            if price <= 0 or price % cfg.tick_size:
                return REJ_PRICE
            if abs(price - self.ref) > cfg.band:
                return REJ_BAND
        if flags & FLAG_ICEBERG:
            if aux < cfg.lot_size or aux > qty or aux % cfg.lot_size:
                return REJ_DISPLAY
        return None

    def _limit(self, side, flags, price):
        if flags & FLAG_MARKET:
            return self.ref + self.cfg.band if side == SIDE_BUY else self.ref - self.cfg.band
        return price

    def _new(self, side, flags, trader, oid, price, qty, aux):
        self.submitted += qty
        rej = self._validate(side, flags, oid, price, qty, aux)
        if rej is not None:
            self.rejected += qty
            return rej
        limit = self._limit(side, flags, price)
        if flags & FLAG_POST_ONLY:
            opp = self.best(SIDE_SELL if side == SIDE_BUY else SIDE_BUY)
            if opp is not None and self._crosses(side, limit, opp.price):
                self.rejected += qty
                return REJ_POST_ONLY
        if flags & FLAG_FOK and self._fillable(side, trader, limit, qty) < qty:
            self.rejected += qty
            return REJ_FOK
        rem, stopped = self._match(side, trader, oid, limit, qty)
        filled = qty - rem
        if rem == 0:
            return ST_FILLED
        if stopped or flags & (FLAG_IOC | FLAG_MARKET | FLAG_FOK):
            self.cancelled += rem
            return ST_PARTIAL_CANCELLED if filled else ST_CANCELLED
        o = RefOrder(oid, side, price, rem, aux if flags & FLAG_ICEBERG else 0, trader)
        self.prio += 1
        o.prio = self.prio
        self.rest.append(o)
        self.by_id[oid] = o
        self.resting += rem
        return ST_PARTIAL_RESTED if filled else ST_RESTED

    @staticmethod
    def _crosses(side, limit, price):
        return price <= limit if side == SIDE_BUY else price >= limit

    def _match(self, side, trader, oid, limit, rem):
        """Match incoming order against the book; return (remaining, stp_stop)."""
        opp_side = SIDE_SELL if side == SIDE_BUY else SIDE_BUY
        k = 0
        while rem > 0:
            o = self.best(opp_side)
            if o is None or not self._crosses(side, limit, o.price):
                break
            if o.trader == trader:
                if self.cfg.stp == STP_CANCEL_NEW:
                    return rem, True
                self._remove(o)
                self.cancelled += o.rem
                self.stp_cancels += 1
                continue
            fill = min(rem, o.vis)
            buy_oid, sell_oid = (oid, o.oid) if side == SIDE_BUY else (o.oid, oid)
            bt, stt = (trader, o.trader) if side == SIDE_BUY else (o.trader, trader)
            self.trades.append((self.seq, k, self.cfg.symbol, side, buy_oid,
                                sell_oid, o.price, fill, bt, stt))
            k += 1
            rem -= fill
            o.rem -= fill
            o.vis -= fill
            self.filled += 2 * fill    # both legs consume quantity
            self.resting -= fill
            if o.rem == 0:
                self._remove(o)
            elif o.vis == 0:
                o.vis = min(o.display, o.rem)
                self.prio += 1
                o.prio = self.prio
        return rem, False

    def _fillable(self, side, trader, limit, want):
        """Pure simulation of _match: quantity obtainable, book untouched."""
        sim = [RefOrder(o.oid, o.side, o.price, o.rem, o.display, o.trader)
               for o in self.rest if o.side != side]
        for s, o in zip(sim, [o for o in self.rest if o.side != side]):
            s.prio, s.vis = o.prio, o.vis
        prio = self.prio
        avail = 0
        while avail < want and sim:
            o = sim[0]
            for c in sim:
                if _better(c, o, side):
                    o = c
            if not self._crosses(side, limit, o.price):
                break
            if o.trader == trader:
                if self.cfg.stp == STP_CANCEL_NEW:
                    break
                sim.remove(o)
                continue
            fill = min(want - avail, o.vis)
            avail += fill
            o.rem -= fill
            o.vis -= fill
            if o.rem == 0:
                sim.remove(o)
            elif o.vis == 0:
                o.vis = min(o.display, o.rem)
                prio += 1
                o.prio = prio
        return avail

    def _remove(self, o):
        self.rest.remove(o)
        del self.by_id[o.oid]
        self.resting -= o.rem

    # ---- CANCEL / MODIFY ---------------------------------------------------
    def _cancel(self, oid):
        o = self.by_id.get(oid)
        if o is None:
            return REJ_UNKNOWN
        self.cancelled += o.rem
        self._remove(o)
        return ST_ACK_CANCEL

    def _modify(self, oid, new):
        o = self.by_id.get(oid)
        if o is None:
            return REJ_UNKNOWN
        cfg = self.cfg
        if new < cfg.min_qty or new > cfg.max_qty or new % cfg.lot_size:
            return REJ_QTY
        if new < o.rem:
            self.cancelled += o.rem - new
            self.resting -= o.rem - new
            o.rem = new
            o.vis = min(o.vis, new)
        elif new > o.rem:
            self.submitted += new - o.rem
            self.resting += new - o.rem
            o.rem = new
            o.vis = min(o.display, new) if o.display else new
            self.prio += 1
            o.prio = self.prio
        return ST_ACK_MODIFY
