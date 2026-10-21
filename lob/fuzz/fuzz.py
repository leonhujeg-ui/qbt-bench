"""Fuzzing: random and malformed events against the Book with the invariant
checker on every event and the reference engine in lockstep.

Malformed classes: negative/zero/huge price, zero/negative/huge/off-lot
quantity, tick-misaligned price, out-of-band price, cancel/modify of unknown
or already-dead ids, modify to negative, duplicate ids, bad side/kind/flags,
timestamp regressions, extreme int64 values.  Any exception = crash, any
InvariantError = violation, any Book/reference mismatch = divergence.
"""
import json
import random
import time
import traceback

from lob import events as E
from lob.book import Book
from lob.common import BookConfig, STATUS_NAMES
from lob.invariants import Checker, InvariantError
from lob.reference import ReferenceBook

INT64_MAX = 2 ** 63 - 1
INT64_MIN = -(2 ** 63)
UINT64_MAX = 2 ** 64 - 1


class FuzzGen:
    """Seeded generator of valid-looking and malformed events for one book."""

    def __init__(self, cfg, seed, bad_rate=0.3):
        self.cfg = cfg
        self.rng = random.Random(seed)
        self.bad_rate = bad_rate
        self.live = []          # ids we believe are live (may be stale)
        self.dead = []          # ids known to have been cancelled/filled
        self.next_oid = 1
        self.ts = E.EPOCH_NS
        self.ref = cfg.ref_price
        self.seq = 0

    def _ts(self):
        r = self.rng.random()
        if r < 0.02:
            return self.ts - self.rng.randint(1, 10 ** 10)      # regression
        self.ts += self.rng.randint(1, 10 ** 6)
        return self.ts

    def _price(self, side):
        cfg, rng = self.cfg, self.rng
        base = self.ref + (rng.randint(-20, 5) if side == E.SIDE_BUY else rng.randint(-5, 20))
        return max(cfg.tick_size, base // cfg.tick_size * cfg.tick_size)

    def _qty(self):
        return self.rng.randint(1, 30) * self.cfg.lot_size

    def _oid(self):
        oid = self.next_oid
        self.next_oid += 1
        return oid

    def valid(self):
        rng, cfg = self.rng, self.cfg
        self.seq += 1
        r = rng.random()
        if r < 0.50 or not self.live:
            side = rng.choice((E.SIDE_BUY, E.SIDE_SELL))
            flags = rng.choice((0, 0, 0, 0, E.FLAG_IOC, E.FLAG_FOK, E.FLAG_POST_ONLY,
                                E.FLAG_ICEBERG, E.FLAG_MARKET, E.FLAG_MARKET | E.FLAG_FOK,
                                E.FLAG_MARKET | E.FLAG_IOC, E.FLAG_ICEBERG | E.FLAG_IOC))
            qty = self._qty()
            aux = max(cfg.lot_size, qty // rng.randint(1, 4) // cfg.lot_size * cfg.lot_size) \
                if flags & E.FLAG_ICEBERG else 0
            oid = self._oid()
            price = 0 if flags & E.FLAG_MARKET else self._price(side)
            if not flags & (E.FLAG_MARKET | E.FLAG_IOC | E.FLAG_FOK):
                self.live.append(oid)
            return E.pack(self.seq, self._ts(), cfg.symbol, E.KIND_NEW, side, flags,
                          rng.randint(1, 6), oid, price, qty, aux)
        idx = rng.randrange(len(self.live))
        oid = self.live[idx]
        if r < 0.85:
            self.live[idx] = self.live[-1]
            self.live.pop()
            self.dead.append(oid)
            return E.pack(self.seq, self._ts(), cfg.symbol, E.KIND_CANCEL, 1, 0, 0, oid, 0, 0)
        return E.pack(self.seq, self._ts(), cfg.symbol, E.KIND_MODIFY, 1, 0, 0, oid, 0,
                      self._qty())

    def malformed(self):
        rng, cfg = self.rng, self.cfg
        self.seq += 1
        side = rng.choice((E.SIDE_BUY, E.SIDE_SELL))
        price, qty, trader = self._price(side), self._qty(), rng.randint(1, 6)
        kind, flags, oid, aux, ts = E.KIND_NEW, 0, self._oid(), 0, self._ts()
        c = rng.randrange(16)
        if c == 0:
            price = rng.choice((0, -1, -price, INT64_MIN, INT64_MAX, price + 1))
        elif c == 1:
            qty = rng.choice((0, -1, -qty, INT64_MIN, INT64_MAX, cfg.max_qty + cfg.lot_size,
                              qty + 1, cfg.min_qty - 1))
        elif c == 2:
            price = self.ref + rng.choice((-1, 1)) * (cfg.band + rng.randint(1, 10 ** 6))
        elif c == 3:
            kind = E.KIND_CANCEL
            oid = rng.choice((0, UINT64_MAX, 10 ** 15, oid)) if not self.dead or rng.random() < 0.5 \
                else rng.choice(self.dead)
        elif c == 4:
            kind = E.KIND_MODIFY
            oid = rng.choice(self.live) if self.live and rng.random() < 0.7 else rng.choice((0, oid))
            qty = rng.choice((0, -1, -qty, INT64_MIN, INT64_MAX, qty + 1))
        elif c == 5:
            side = rng.choice((0, 3, 255))
        elif c == 6:
            kind = rng.choice((0, 4, 9, 255))
        elif c == 7:
            flags = rng.choice((E.FLAG_MARKET | E.FLAG_POST_ONLY, E.FLAG_IOC | E.FLAG_FOK,
                                E.FLAG_MARKET | E.FLAG_ICEBERG, E.FLAG_POST_ONLY | E.FLAG_FOK,
                                32, 64, 255, E.FLAG_POST_ONLY | E.FLAG_IOC))
        elif c == 8:
            flags = E.FLAG_ICEBERG
            aux = rng.choice((0, -1, qty + 1, INT64_MAX, aux + 1 if cfg.lot_size > 1 else 0))
        elif c == 9:
            oid = rng.choice(self.live) if self.live else oid        # duplicate id
        elif c == 10:
            ts = rng.choice((0, UINT64_MAX, self.ts - 10 ** 12))
        elif c == 11:
            flags = E.FLAG_MARKET
            price = rng.choice((INT64_MIN, INT64_MAX, -5))             # ignored
        elif c == 12:
            kind = E.KIND_CANCEL
            oid = rng.choice(self.live) if self.live else oid
            if oid in self.live:
                self.live.remove(oid)
                self.dead.append(oid)
            price, qty, side = INT64_MIN, INT64_MAX, 0                 # garbage fields
        elif c == 13:
            qty = cfg.max_qty                                          # huge but legal
        elif c == 14:
            flags = E.FLAG_FOK
            qty = cfg.max_qty
        else:
            trader = rng.choice((0, 65535))
        return E.pack(self.seq, ts, cfg.symbol, kind, side, flags, trader, oid, price,
                      qty, aux)

    def next_event(self):
        if self.rng.random() < self.bad_rate:
            return self.malformed()
        return self.valid()


def run(n=200000, seed=2024, bad_rate=0.3, full_every=100, cfg=None, lockstep=True):
    cfg = cfg or BookConfig(tick_size=5, lot_size=10, min_qty=10, max_qty=100000,
                            band=400, ref_price=10000, stp='cancel_old', symbol=3)
    gen = FuzzGen(cfg, seed, bad_rate)
    book, ref = Book(cfg), (ReferenceBook(cfg) if lockstep else None)
    chk = Checker(full_every=full_every)
    chk.attach(book)
    counts, crashes, violations, divergences = {}, [], [], []
    t0 = time.perf_counter()
    for i in range(n):
        rec = E.RECORD.unpack(gen.next_event())
        n0 = len(book.trades)
        try:
            st = book.process(rec)
            chk.after_event(book, n0, rec)
        except InvariantError as exc:
            violations.append({'seq': rec[0], 'error': str(exc)})
            continue
        except Exception:                      # any crash is a finding
            crashes.append({'seq': rec[0], 'trace': traceback.format_exc()[-800:]})
            continue
        counts[STATUS_NAMES.get(st, str(st))] = counts.get(STATUS_NAMES.get(st, str(st)), 0) + 1
        if ref is not None:
            n1 = len(ref.trades)
            sr = ref.process(rec)
            if sr != st or ref.trades[n1:] != book.trades[n0:]:
                divergences.append({'seq': rec[0], 'book': st, 'ref': sr})
            if len(book.trades) > 50000:
                del book.trades[:], ref.trades[:]
        if book.trades and ref is None and len(book.trades) > 50000:
            del book.trades[:]
        gen.ref = book.ref
    chk.full_check(book)
    if ref is not None and book.levels() != ref.levels():
        divergences.append({'seq': gen.seq, 'book': 'levels', 'ref': 'levels differ'})
    return {'events': n, 'seed': seed, 'bad_rate': bad_rate, 'full_every': full_every,
            'seconds': round(time.perf_counter() - t0, 2), 'status_counts': counts,
            'crashes': crashes, 'invariant_violations': violations,
            'divergences': divergences[:20], 'n_divergences': len(divergences),
            'live_orders_end': len(book.orders), 'full_checks': chk.full_checks,
            'trades_checked': chk.trades_checked, 'config': cfg.to_dict()}


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--n', type=int, default=200000)
    ap.add_argument('--seed', type=int, default=2024)
    ap.add_argument('--bad-rate', type=float, default=0.3)
    ap.add_argument('--full-every', type=int, default=100)
    ap.add_argument('--json', default=None)
    args = ap.parse_args(argv)
    res = run(args.n, args.seed, args.bad_rate, args.full_every)
    text = json.dumps(res, indent=1, sort_keys=True)
    if args.json:
        with open(args.json, 'w') as fh:
            fh.write(text + '\n')
    print(text)
    ok = not res['crashes'] and not res['invariant_violations'] and not res['n_divergences']
    return 0 if ok else 1


if __name__ == '__main__':
    import sys
    sys.exit(main())
