"""Runtime invariant checker (switchable).

Cheap O(1) checks after every event, a full structural scan every
`full_every` events (and on demand via full_check()):
  1. best bid < best ask;
  2. conservation: submitted == filled + resting + cancelled + rejected;
  3. every trade of the event: price >= pre-event best ask (buy aggressor;
     symmetric for sells), strictly beyond the pre-event own-side best,
     within the incoming limit / band, non-decreasing along the sweep,
     qty >= 1, aggressor id matches the event;
  4. no self-trade: buyer trader != seller trader;
  5. full scan: level qty == sum of visible qty of its live orders, queues
     strictly increasing in priority, slots consistent, sorted keys,
     resting counter == sum of remaining, no crossed book.
"""
from lob.common import (SIDE_BUY, FLAG_MARKET, T_AGGR_SIDE, T_BUY_OID,
                        T_SELL_OID, T_PRICE, T_QTY, T_BUY_TRADER, T_SELL_TRADER)
from lob.events import KIND_NEW, F_KIND, F_FLAGS, F_OID, F_PRICE


class InvariantError(AssertionError):
    pass


def _fail(msg, *args):
    raise InvariantError(msg % args if args else msg)


class Checker:
    def __init__(self, full_every=1000, enabled=True):
        self.full_every = full_every
        self.enabled = enabled
        self.events = 0
        self.full_checks = 0
        self.trades_checked = 0
        self._bb = self._ba = self._ref = None
        self._fast = True

    def attach(self, book):
        self._bb, self._ba, self._ref = book.best_bid(), book.best_ask(), book.ref
        self._fast = hasattr(book, 'bid_keys')

    def after_event(self, book, n_before, rec=None, status=None):
        """`status` (the event's status code) lets the caller skip the trade
        scan for events that cannot have traded."""
        if not self.enabled:
            return
        if self._fast:                      # Book: read the key arrays directly
            bk, ak = book.bid_keys, book.ask_keys
            bb = bk[-1] if bk else None
            ba = -ak[-1] if ak else None
        else:                               # ReferenceBook: O(n) scans
            bb, ba = book.best_bid(), book.best_ask()
        if bb is not None and ba is not None and bb >= ba:
            _fail('crossed book: bid %d >= ask %d at seq %d', bb, ba, book.seq)
        if book.submitted != book.filled + book.resting + book.cancelled + book.rejected:
            _fail('conservation broken at seq %d: %d != %d+%d+%d+%d', book.seq,
                  book.submitted, book.filled, book.resting, book.cancelled,
                  book.rejected)
        if status is None or status < 4:
            trades = book.trades
            if len(trades) > n_before:
                self._check_trades(book, trades[n_before:], rec)
        self.events += 1
        if self.full_every and self.events % self.full_every == 0:
            self.full_check(book)
        self._bb, self._ba, self._ref = bb, ba, book.ref

    def _check_trades(self, book, trades, rec):
        pre_bb, pre_ba, pre_ref = self._bb, self._ba, self._ref
        side = trades[0][T_AGGR_SIDE]
        buy = side == SIDE_BUY
        if rec is not None:
            if rec[F_KIND] != KIND_NEW:
                _fail('trade produced by non-NEW event at seq %d', book.seq)
            own = rec[F_OID]
            if rec[F_FLAGS] & FLAG_MARKET:
                limit = pre_ref + book.cfg.band if buy else pre_ref - book.cfg.band
            else:
                limit = rec[F_PRICE]
        else:
            own, limit = None, None
        if (pre_ba if buy else pre_bb) is None:
            _fail('trade without opposite liquidity at seq %d', book.seq)
        last = None
        for t in trades:
            p, q = t[T_PRICE], t[T_QTY]
            if q < 1:
                _fail('non-positive trade qty at seq %d', book.seq)
            if t[T_BUY_TRADER] == t[T_SELL_TRADER]:
                _fail('self-trade at seq %d', book.seq)
            if t[T_AGGR_SIDE] != side:
                _fail('mixed aggressor sides at seq %d', book.seq)
            if own is not None and (t[T_BUY_OID] if buy else t[T_SELL_OID]) != own:
                _fail('aggressor id mismatch at seq %d', book.seq)
            if buy:
                if p < pre_ba or (pre_bb is not None and p <= pre_bb):
                    _fail('buy trade price %d outside touch (%s/%s) at seq %d', p,
                          pre_bb, pre_ba, book.seq)
                if limit is not None and p > limit:
                    _fail('buy trade above limit at seq %d', book.seq)
                if last is not None and p < last:
                    _fail('buy sweep prices not non-decreasing at seq %d', book.seq)
            else:
                if p > pre_bb or (pre_ba is not None and p >= pre_ba):
                    _fail('sell trade price %d outside touch (%s/%s) at seq %d', p,
                          pre_bb, pre_ba, book.seq)
                if limit is not None and p < limit:
                    _fail('sell trade below limit at seq %d', book.seq)
                if last is not None and p > last:
                    _fail('sell sweep prices not non-increasing at seq %d', book.seq)
            last = p
            self.trades_checked += 1

    # ------------------------------------------------------------ full scan
    def full_check(self, book):
        self.full_checks += 1
        if hasattr(book, 'bid_keys'):
            _full_check_book(book)
        else:
            _full_check_reference(book)


def _full_check_book(book):
    from lob.book import (O_OID, O_SIDE, O_PRICE, O_REM, O_VIS, O_DISP, O_PRIO,
                          O_SLOT, Q_HEAD, Q_DEAD, Q_FIRST)
    seen = 0
    for side, keys, qd, qq, sgn in ((SIDE_BUY, book.bid_keys, book.bid_qty, book.bid_queue, 1),
                                    (2, book.ask_keys, book.ask_qty, book.ask_queue, -1)):
        if any(keys[i] >= keys[i + 1] for i in range(len(keys) - 1)):
            _fail('price keys not strictly sorted at seq %d', book.seq)
        if len(keys) != len(qd) or len(qd) != len(qq):
            _fail('level containers disagree at seq %d', book.seq)
        for k in keys:
            price = sgn * k
            queue = qq[k]
            head = queue[Q_HEAD]
            if head < Q_FIRST or head > len(queue):
                _fail('bad queue head at seq %d', book.seq)
            total, prio = 0, -1
            dead = sum(1 for o in queue[Q_FIRST:] if o is None)
            for i in range(head, len(queue)):
                o = queue[i]
                if o is None:
                    continue
                if o[O_SLOT] != i or o[O_SIDE] != side or o[O_PRICE] != price:
                    _fail('order %d slot/side/price inconsistent at seq %d', o[O_OID], book.seq)
                if o[O_PRIO] <= prio:
                    _fail('time priority broken at price %d seq %d', price, book.seq)
                prio = o[O_PRIO]
                if o[O_REM] < 1 or o[O_VIS] < 1 or o[O_VIS] > o[O_REM] \
                        or (o[O_DISP] and o[O_VIS] > o[O_DISP]):
                    _fail('bad quantities on order %d at seq %d', o[O_OID], book.seq)
                if book.orders.get(o[O_OID]) is not o:
                    _fail('order %d not registered at seq %d', o[O_OID], book.seq)
                total += o[O_VIS]
                seen += 1
            if dead != queue[Q_DEAD]:
                _fail('dead count mismatch at price %d seq %d', price, book.seq)
            if total != qd[k] or total < 1:
                _fail('level qty %d != sum of visible %d at price %d seq %d',
                      qd[k], total, price, book.seq)
    if seen != len(book.orders):
        _fail('orders dict has %d entries but queues hold %d at seq %d',
              len(book.orders), seen, book.seq)
    if book.resting != book.resting_check():
        _fail('resting counter %d != sum of remaining %d at seq %d', book.resting,
              book.resting_check(), book.seq)
    bb, ba = book.best_bid(), book.best_ask()
    if bb is not None and ba is not None and bb >= ba:
        _fail('crossed book in full check at seq %d', book.seq)


def _full_check_reference(book):
    prios = set()
    for o in book.rest:
        if o.rem < 1 or o.vis < 1 or o.vis > o.rem or (o.display and o.vis > o.display):
            _fail('bad quantities on reference order %d', o.oid)
        if o.prio in prios:
            _fail('duplicate priority in reference book')
        prios.add(o.prio)
        if book.by_id.get(o.oid) is not o:
            _fail('reference index inconsistent for %d', o.oid)
    if len(book.by_id) != len(book.rest):
        _fail('reference index size mismatch')
    if book.resting != book.resting_check():
        _fail('reference resting counter mismatch')
    bb, ba = book.best_bid(), book.best_ask()
    if bb is not None and ba is not None and bb >= ba:
        _fail('crossed reference book')
