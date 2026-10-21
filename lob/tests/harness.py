"""Data-driven case harness shared by the reference and the fast engine."""
from lob import events as E
from lob.common import BookConfig, T_BUY_OID, T_SELL_OID, T_PRICE, T_QTY


def N(oid, side, price, qty, flags=0, trader=0, aux=0):
    """trader=0 means 'unique per order' (100 + oid), so STP never fires."""
    return ('new', oid, side, price, qty, flags, trader, aux)


def C(oid):
    return ('cancel', oid)


def M(oid, qty):
    return ('modify', oid, qty)


def RAW(kind, side, flags, trader, oid, price, qty, aux=0):
    return ('raw', kind, side, flags, trader, oid, price, qty, aux)


def to_record(seq, ev, symbol=0):
    ts = E.EPOCH_NS + seq * 1000
    if ev[0] == 'new':
        _, oid, side, price, qty, flags, trader, aux = ev
        if trader == 0:
            trader = 100 + oid % 60000
        return E.RECORD.unpack(E.pack(seq, ts, symbol, E.KIND_NEW, side, flags,
                                      trader, oid, price, qty, aux))
    if ev[0] == 'cancel':
        return E.RECORD.unpack(E.pack(seq, ts, symbol, E.KIND_CANCEL, 1, 0, 0,
                                      ev[1], 0, 0))
    if ev[0] == 'modify':
        return E.RECORD.unpack(E.pack(seq, ts, symbol, E.KIND_MODIFY, 1, 0, 0,
                                      ev[1], 0, ev[2]))
    _, kind, side, flags, trader, oid, price, qty, aux = ev
    return E.RECORD.unpack(E.pack(seq, ts, symbol, kind, side, flags, trader,
                                  oid, price, qty, aux))


def run_events(engine_cls, cfg, evs, checker=None):
    """Return (statuses, trades, bids, asks, book)."""
    book = engine_cls(cfg)
    if checker is not None:
        checker.attach(book)
    statuses, trades = [], []
    for i, ev in enumerate(evs):
        n0 = len(book.trades)
        statuses.append(book.process(to_record(i + 1, ev)))
        trades.extend((t[T_BUY_OID], t[T_SELL_OID], t[T_PRICE], t[T_QTY])
                      for t in book.trades[n0:])
        if checker is not None:
            checker.after_event(book, n0)
    bids, asks = book.levels()
    return statuses, trades, bids, asks, book


class Case:
    __slots__ = ('name', 'events', 'statuses', 'trades', 'bids', 'asks', 'cfg')

    def __init__(self, name, events, statuses, trades=(), bids=(), asks=(),
                 cfg=None):
        self.name = name
        self.events = events
        self.statuses = list(statuses)
        self.trades = list(trades)
        self.bids = list(bids)
        self.asks = list(asks)
        self.cfg = cfg or {}

    def config(self):
        base = dict(tick_size=1, lot_size=1, min_qty=1, max_qty=10 ** 6,
                    band=10 ** 6, ref_price=100)
        base.update(self.cfg)
        return BookConfig(**base)
