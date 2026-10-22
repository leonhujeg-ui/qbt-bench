"""Shared constants and configuration for both matching engines."""
from lob.events import (KIND_NEW, KIND_CANCEL, KIND_MODIFY, SIDE_BUY, SIDE_SELL,
                        FLAG_MARKET, FLAG_IOC, FLAG_FOK, FLAG_POST_ONLY,
                        FLAG_ICEBERG)

ALL_FLAGS = FLAG_MARKET | FLAG_IOC | FLAG_FOK | FLAG_POST_ONLY | FLAG_ICEBERG

# per-event status codes
ST_RESTED = 0            # new order rests in full
ST_FILLED = 1            # new order fully filled
ST_PARTIAL_RESTED = 2    # partially filled, remainder rests
ST_PARTIAL_CANCELLED = 3  # partially filled, remainder cancelled
ST_CANCELLED = 4         # nothing filled, order cancelled (IOC/market/STP)
ST_ACK_CANCEL = 5
ST_ACK_MODIFY = 6
REJ_KIND = 10
REJ_SIDE = 11
REJ_FLAGS = 12
REJ_DUP = 13
REJ_QTY = 14
REJ_PRICE = 15
REJ_BAND = 16
REJ_DISPLAY = 17
REJ_POST_ONLY = 18
REJ_FOK = 19
REJ_UNKNOWN = 20
STATUS_NAMES = {
    0: 'RESTED', 1: 'FILLED', 2: 'PARTIAL_RESTED', 3: 'PARTIAL_CANCELLED',
    4: 'CANCELLED', 5: 'ACK_CANCEL', 6: 'ACK_MODIFY', 10: 'REJ_KIND',
    11: 'REJ_SIDE', 12: 'REJ_FLAGS', 13: 'REJ_DUP', 14: 'REJ_QTY',
    15: 'REJ_PRICE', 16: 'REJ_BAND', 17: 'REJ_DISPLAY', 18: 'REJ_POST_ONLY',
    19: 'REJ_FOK', 20: 'REJ_UNKNOWN',
}

STP_CANCEL_NEW = 'cancel_new'
STP_CANCEL_OLD = 'cancel_old'

# in-memory trade tuple layout
(T_SEQ, T_IDX, T_SYMBOL, T_AGGR_SIDE, T_BUY_OID, T_SELL_OID, T_PRICE, T_QTY,
 T_BUY_TRADER, T_SELL_TRADER) = range(10)


class BookConfig:
    """Engine configuration; integers only."""
    __slots__ = ('tick_size', 'lot_size', 'min_qty', 'max_qty', 'band',
                 'ref_price', 'stp', 'symbol')

    def __init__(self, tick_size=1, lot_size=1, min_qty=1, max_qty=10 ** 9,
                 band=10 ** 6, ref_price=1000, stp=STP_CANCEL_NEW, symbol=0):
        if tick_size < 1 or lot_size < 1 or min_qty < 1 or band < 0:
            raise ValueError('tick_size, lot_size, min_qty >= 1 and band >= 0')
        if min_qty % lot_size or max_qty < min_qty or ref_price < 1:
            raise ValueError('inconsistent quantity / price configuration')
        if stp not in (STP_CANCEL_NEW, STP_CANCEL_OLD):
            raise ValueError('unknown self-trade policy: %r' % (stp,))
        self.tick_size = tick_size
        self.lot_size = lot_size
        self.min_qty = min_qty
        self.max_qty = max_qty
        self.band = band
        self.ref_price = ref_price
        self.stp = stp
        self.symbol = symbol

    @classmethod
    def from_dict(cls, d, symbol=0):
        missing = [k for k in cls.__slots__ if k != 'symbol' and k not in d]
        if missing:
            raise ValueError('BookConfig.from_dict: missing keys %s' % missing)
        return cls(tick_size=d['tick_size'], lot_size=d['lot_size'],
                   min_qty=d['min_qty'], max_qty=d['max_qty'], band=d['band'],
                   ref_price=d['ref_price'], stp=d['stp'], symbol=symbol)

    def to_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}


def flags_valid(flags):
    """Shared flag-combination rule (see SPEC.md)."""
    if flags & ~ALL_FLAGS:
        return False
    if flags & FLAG_MARKET and flags & (FLAG_POST_ONLY | FLAG_ICEBERG):
        return False
    if flags & FLAG_IOC and flags & FLAG_FOK:
        return False
    if flags & FLAG_POST_ONLY and flags & (FLAG_IOC | FLAG_FOK):
        return False
    return True
