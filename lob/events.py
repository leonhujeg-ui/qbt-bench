"""Event stream format and seeded synthetic event generator.

Record layout (little endian, 56 bytes):
    seq:u64  ts_ns:u64  symbol:u16  kind:u8  side:u8  flags:u8  pad:u8
    trader:u16  oid:u64  price:i64  qty:i64  aux:i64

kind  : 1=NEW 2=CANCEL 3=MODIFY
side  : 1=BUY 2=SELL
flags : bit0 MARKET, bit1 IOC, bit2 FOK, bit3 POST_ONLY, bit4 ICEBERG
aux   : iceberg display quantity (NEW+ICEBERG); unused otherwise
qty   : order quantity (NEW) / new remaining quantity (MODIFY)
Prices are integer ticks, quantities integer lots.  No floats anywhere.
"""
import hashlib
import heapq
import json
import os
import random
import struct

RECORD = struct.Struct('<QQHBBBBHQqqq')
RECORD_SIZE = RECORD.size  # 56
(F_SEQ, F_TS, F_SYMBOL, F_KIND, F_SIDE, F_FLAGS, F_PAD, F_TRADER, F_OID,
 F_PRICE, F_QTY, F_AUX) = range(12)

KIND_NEW, KIND_CANCEL, KIND_MODIFY = 1, 2, 3
SIDE_BUY, SIDE_SELL = 1, 2
FLAG_MARKET, FLAG_IOC, FLAG_FOK, FLAG_POST_ONLY, FLAG_ICEBERG = 1, 2, 4, 8, 16

EPOCH_NS = 1_700_000_000 * 1_000_000_000
NS = 1_000_000_000


def pack(seq, ts, symbol, kind, side, flags, trader, oid, price, qty, aux=0):
    return RECORD.pack(seq, ts, symbol, kind, side, flags, 0, trader, oid,
                       price, qty, aux)


CHUNK_RECORDS = 1 << 16


def iter_chunks(path, start=0, stop=None):
    """Yield raw byte chunks holding whole records (no mmap: small RSS)."""
    with open(path, 'rb') as fh:
        size = os.fstat(fh.fileno()).st_size
        lo = start * RECORD_SIZE
        hi = size if stop is None else min(size, stop * RECORD_SIZE)
        if lo >= hi:
            return
        fh.seek(lo)
        remaining = hi - lo
        while remaining > 0:
            buf = fh.read(min(remaining, CHUNK_RECORDS * RECORD_SIZE))
            if not buf:
                break
            remaining -= len(buf)
            yield buf[:len(buf) - len(buf) % RECORD_SIZE]


def iter_records(path, symbols=None, start=0, stop=None):
    """Yield decoded 12-tuples from a stream file (optionally symbol filtered)."""
    want = set(symbols) if symbols is not None else None
    unpack = RECORD.iter_unpack
    for buf in iter_chunks(path, start, stop):
        if want is None:
            yield from unpack(buf)
        else:
            for rec in unpack(buf):
                if rec[2] in want:
                    yield rec


def count_records(path):
    return os.path.getsize(path) // RECORD_SIZE


def file_sha256(path, chunk=1 << 22):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


class GenParams:
    """All generator knobs.  Every field is explicit so a run is reproducible."""

    def __init__(self, **kw):
        self.n_events = 1_000_000
        self.n_symbols = 8
        self.seed = 12345
        self.rate = 2000.0            # events / simulated second / symbol
        self.base_price = 100_000     # ticks
        self.spread = 4               # half-spread in ticks around latent mid
        self.depth_scale = 6.0        # mean offset (ticks) of passive orders
        self.cancel_rate = 0.42       # base share of cancel events
        self.modify_rate = 0.10
        self.aggr_ratio = 0.12        # share of NEW orders that cross
        self.big_ratio = 0.02         # share of NEW orders that are 20x
        self.qty_mean_lots = 8
        self.lot_size = 1
        self.tick_size = 1
        self.min_qty = 1
        self.max_qty = 1_000_000
        self.band = 5000              # price band around last trade (ticks)
        self.drift = 2.0              # latent mid drift std (ticks / sqrt s)
        self.withdraw_prob = 0.0004   # per event: enter liquidity withdrawal
        self.shock_prob = 0.0002      # per event: enter extreme move regime
        self.bad_rate = 0.002         # share of malformed events
        self.n_traders = 64
        self.target_resting = 1200    # resting orders / symbol steady state
        for k, v in kw.items():
            if not hasattr(self, k):
                raise KeyError('unknown generator parameter: %s' % k)
            setattr(self, k, v)

    def to_dict(self):
        return dict(sorted(self.__dict__.items()))


def book_config_for(params, symbol):
    """Per-symbol engine configuration (self-trade policy alternates)."""
    return {
        'tick_size': params.tick_size, 'lot_size': params.lot_size,
        'min_qty': params.min_qty, 'max_qty': params.max_qty,
        'band': params.band, 'ref_price': params.base_price,
        'stp': 'cancel_new' if symbol % 2 == 0 else 'cancel_old',
    }


class _Sym:
    __slots__ = ('mid', 'live', 'pos', 'regime', 'regime_until', 'shock_dir',
                 'last_ts', 'next_ts', 'ids')

    def __init__(self, mid, ts):
        self.mid = float(mid)
        self.live = []          # [oid, side, price, qty]
        self.pos = {}
        self.regime = 'normal'
        self.regime_until = 0
        self.shock_dir = 1
        self.last_ts = ts
        self.next_ts = ts
        self.ids = []           # recent oids (for duplicate-id malformed events)


class Generator:
    """Seeded synthetic multi-symbol event generator."""

    def __init__(self, params):
        self.p = params
        self.rng = random.Random(params.seed)
        self.seq = 0
        self.oid = 1
        self.timeline = []      # (symbol, start_ts, end_ts, regime)
        self.syms = [_Sym(params.base_price + 1000 * i, EPOCH_NS)
                     for i in range(params.n_symbols)]
        self.heap = [(EPOCH_NS, i) for i in range(params.n_symbols)]
        heapq.heapify(self.heap)
        self.counts = {'new': 0, 'cancel': 0, 'modify': 0, 'bad': 0}

    # ---- time & regime ---------------------------------------------------
    def _advance(self, s, i):
        ts = s.next_ts
        gap = self.rng.expovariate(self.p.rate)
        s.next_ts = ts + max(1, int(gap * NS))
        heapq.heappush(self.heap, (s.next_ts, i))
        dt = (ts - s.last_ts) / NS
        s.last_ts = ts
        self._regime(s, i, ts)
        step = self.rng.gauss(0.0, self.p.drift * (dt ** 0.5)) if dt > 0 else 0.0
        if s.regime == 'shock':
            step += s.shock_dir * self.p.drift * 2.0
        s.mid = min(max(s.mid + step, self.p.base_price * 0.5),
                    self.p.base_price * 2.0)
        return ts

    def _regime(self, s, i, ts):
        if s.regime != 'normal':
            if ts >= s.regime_until:
                s.regime = 'normal'
            return
        r = self.rng.random()
        if r < self.p.withdraw_prob:
            s.regime, dur = 'withdraw', self.rng.uniform(0.5, 3.0)
        elif r < self.p.withdraw_prob + self.p.shock_prob:
            s.regime, dur = 'shock', self.rng.uniform(0.2, 1.0)
            s.shock_dir = self.rng.choice((-1, 1))
        else:
            return
        s.regime_until = ts + int(dur * NS)
        self.timeline.append((i, ts, s.regime_until, s.regime))

    # ---- event mix ---------------------------------------------------------
    def _mix(self, s):
        p = self.p
        n = len(s.live)
        if s.regime == 'withdraw':
            cancel, modify = 0.80, 0.05
        elif s.regime == 'shock':
            cancel, modify = 0.30, 0.05
        else:
            cancel, modify = p.cancel_rate, p.modify_rate
        ratio = n / p.target_resting
        if ratio > 1.0:
            cancel = min(0.9, cancel * ratio)
        elif ratio < 0.5:
            cancel *= ratio * 2
        if n == 0:
            cancel = modify = 0.0
        return cancel, modify

    def next_event(self):
        """Return the next packed record (bytes)."""
        _, i = heapq.heappop(self.heap)
        s = self.syms[i]
        ts = self._advance(s, i)
        self.seq += 1
        rng = self.rng
        if rng.random() < self.p.bad_rate:
            self.counts['bad'] += 1
            return self._bad(s, i, ts)
        cancel, modify = self._mix(s)
        r = rng.random()
        if r < cancel:
            return self._cancel(s, i, ts)
        if r < cancel + modify:
            return self._modify(s, i, ts)
        return self._new(s, i, ts)

    # ---- order builders ----------------------------------------------------
    def _qty(self, s):
        p, rng = self.p, self.rng
        lots = max(1, int(rng.lognormvariate(0.0, 0.9) * p.qty_mean_lots))
        big = p.big_ratio * (3 if s.regime == 'shock' else 1)
        if rng.random() < big:
            lots *= 20
        return min(lots * p.lot_size, p.max_qty)

    def _trader(self):
        return min(self.p.n_traders - 1,
                   int(self.rng.expovariate(4.0 / self.p.n_traders)))

    def _new(self, s, i, ts):
        p, rng = self.p, self.rng
        self.counts['new'] += 1
        side = SIDE_BUY if rng.random() < 0.5 else SIDE_SELL
        if s.regime == 'shock' and rng.random() < 0.5:
            side = SIDE_BUY if s.shock_dir > 0 else SIDE_SELL
        r = rng.random()
        if r < 0.80:
            flags = 0
        elif r < 0.86:
            flags = FLAG_IOC
        elif r < 0.89:
            flags = FLAG_FOK
        elif r < 0.93:
            flags = FLAG_POST_ONLY
        elif r < 0.97:
            flags = FLAG_ICEBERG
        else:
            flags = FLAG_MARKET | (FLAG_FOK if rng.random() < 0.2 else 0)
        qty = self._qty(s)
        aux = 0
        if flags & FLAG_ICEBERG:
            aux = max(p.lot_size, (qty // rng.choice((4, 5, 10)))
                      // p.lot_size * p.lot_size)
        price = 0 if flags & FLAG_MARKET else self._price(s, side, flags)
        oid = self.oid
        self.oid += 1
        s.ids.append(oid)
        if len(s.ids) > 64:
            del s.ids[:32]
        passive = not (flags & (FLAG_MARKET | FLAG_IOC | FLAG_FOK))
        if passive and self._is_passive(s, side, price):
            s.pos[oid] = len(s.live)
            s.live.append([oid, side, price, qty])
        return pack(self.seq, ts, i, KIND_NEW, side, flags, self._trader(),
                    oid, price, qty, aux)

    def _touch(self, s):
        mid = s.mid
        half = self.p.spread / 2.0
        return int(mid - half), int(mid + half) + 1

    def _is_passive(self, s, side, price):
        bid, ask = self._touch(s)
        return price < ask if side == SIDE_BUY else price > bid

    def _price(self, s, side, flags):
        p, rng = self.p, self.rng
        bid, ask = self._touch(s)
        aggr = p.aggr_ratio * (3 if s.regime == 'shock' else 1)
        if flags & FLAG_POST_ONLY:
            aggr *= 0.3
        scale = p.depth_scale * (2.0 if s.regime == 'withdraw' else 1.0)
        off = int(rng.expovariate(1.0 / scale))
        if rng.random() < aggr:
            depth = int(rng.expovariate(0.5))
            price = ask + depth if side == SIDE_BUY else bid - depth
        else:
            price = bid - off if side == SIDE_BUY else ask + off
        price = max(p.tick_size, price // p.tick_size * p.tick_size)
        return price

    def _pick_live(self, s):
        idx = self.rng.randrange(len(s.live))
        return idx, s.live[idx]

    def _drop_live(self, s, idx):
        last = s.live.pop()
        del s.pos[s.live[idx][0] if idx < len(s.live) else last[0]]
        if idx < len(s.live):
            s.live[idx] = last
            s.pos[last[0]] = idx

    def _cancel(self, s, i, ts):
        self.counts['cancel'] += 1
        idx, o = self._pick_live(s)
        self._drop_live(s, idx)
        return pack(self.seq, ts, i, KIND_CANCEL, o[1], 0, 0, o[0], o[2], 0)

    def _modify(self, s, i, ts):
        p, rng = self.p, self.rng
        self.counts['modify'] += 1
        idx, o = self._pick_live(s)
        if rng.random() < 0.7:
            new = max(p.lot_size, (o[3] - rng.randint(1, max(1, o[3] // 2)))
                      // p.lot_size * p.lot_size)
        else:
            new = min(p.max_qty, o[3] + self._qty(s))
        o[3] = new
        return pack(self.seq, ts, i, KIND_MODIFY, o[1], 0, 0, o[0], o[2], new)

    def _bad(self, s, i, ts):
        """Malformed events: the engine must reject them without crashing."""
        p, rng = self.p, self.rng
        bid, ask = self._touch(s)
        kind = rng.randrange(10)
        oid = self.oid
        self.oid += 1
        q = p.lot_size * p.qty_mean_lots
        side = SIDE_BUY if rng.random() < 0.5 else SIDE_SELL
        if kind == 0:    # negative price
            return pack(self.seq, ts, i, KIND_NEW, side, 0, 1, oid, -bid, q)
        if kind == 1:    # zero quantity
            return pack(self.seq, ts, i, KIND_NEW, side, 0, 1, oid, bid, 0)
        if kind == 2:    # absurd quantity
            return pack(self.seq, ts, i, KIND_NEW, side, 0, 1, oid, bid,
                        p.max_qty * 1000)
        if kind == 3:    # timestamp regression
            return pack(self.seq, ts - 5 * NS, i, KIND_NEW, side, 0, 1, oid,
                        bid - 1, q)
        if kind == 4:    # cancel of unknown order
            return pack(self.seq, ts, i, KIND_CANCEL, side, 0, 0,
                        10 ** 12 + oid, 0, 0)
        if kind == 5:    # modify to negative quantity
            o = s.live[rng.randrange(len(s.live))] if s.live else [oid, side, bid, q]
            return pack(self.seq, ts, i, KIND_MODIFY, o[1], 0, 0, o[0], o[2], -q)
        if kind == 6:    # price outside band
            return pack(self.seq, ts, i, KIND_NEW, side, 0, 1, oid,
                        bid + p.band * 3, q)
        if kind == 7:    # bad side / bad kind
            if rng.random() < 0.5:
                return pack(self.seq, ts, i, KIND_NEW, 3, 0, 1, oid, bid, q)
            return pack(self.seq, ts, i, 9, side, 0, 1, oid, bid, q)
        if kind == 8:    # conflicting flags
            fl = rng.choice((FLAG_MARKET | FLAG_POST_ONLY, FLAG_IOC | FLAG_FOK,
                             FLAG_MARKET | FLAG_ICEBERG, FLAG_ICEBERG))
            return pack(self.seq, ts, i, KIND_NEW, side, fl, 1, oid, bid, q, 0)
        dup = s.ids[rng.randrange(len(s.ids))] if s.ids else oid   # duplicate id
        return pack(self.seq, ts, i, KIND_NEW, side, 0, 1, dup, bid, q)


def generate(params, path, progress=None):
    """Write params.n_events records to `path`; return the meta dict."""
    gen = Generator(params)
    chunk = []
    n = params.n_events
    with open(path, 'wb') as fh:
        for k in range(n):
            chunk.append(gen.next_event())
            if len(chunk) >= 16384:
                fh.write(b''.join(chunk))
                chunk.clear()
                if progress and (k + 1) % (1 << 20) < 16384:
                    progress(k + 1, n)
        if chunk:
            fh.write(b''.join(chunk))
    meta = {
        'params': params.to_dict(), 'n_events': n, 'record_size': RECORD_SIZE,
        'sha256': file_sha256(path), 'counts': gen.counts,
        'last_oid': gen.oid - 1, 'epoch_ns': EPOCH_NS,
        'regimes': gen.timeline,
        'books': {str(i): book_config_for(params, i)
                  for i in range(params.n_symbols)},
    }
    with open(meta_path(path), 'w') as fh:
        json.dump(meta, fh, indent=1, sort_keys=True)
    return meta


def meta_path(path):
    return path + '.meta.json'


def load_meta(path):
    with open(meta_path(path)) as fh:
        return json.load(fh)
