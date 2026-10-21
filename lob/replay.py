"""Replayer: feeds an event stream to one Book per symbol and writes
  trades.bin      TRADE records in (seq, idx) order
  status.bin      one status byte per event, indexed by seq-1
  snapshots.jsonl top-N book snapshot per symbol per simulated second,
                  sorted by (second, symbol)
  stats.json      counters and timings
Single-process and symbol-sharded multi-process modes produce byte-identical
trades/status/snapshots (symbols are independent books).
"""
import heapq
import json
import multiprocessing
import os
import resource
import struct
import time

from lob import events as E
from lob.book import Book
from lob.common import BookConfig, STATUS_NAMES
from lob.invariants import Checker

TRADE = struct.Struct('<QIHBxQQqq')   # seq, idx, symbol, aggr_side, buy, sell, price, qty
TRADE_SIZE = TRADE.size               # 48


def iter_trades(path):
    with open(path, 'rb') as fh:
        data = fh.read()
    return TRADE.iter_unpack(data)


def configs_from_meta(meta, symbols):
    return {s: BookConfig.from_dict(meta['books'][str(s)], symbol=s) for s in symbols}


def _snapshot_line(sec, sym, book, depth):
    bp, bq, ap, aq = book.top(depth)
    return json.dumps({'t': sec, 'sym': sym,
                       'bids': [[p, q] for p, q in zip(bp, bq)],
                       'asks': [[p, q] for p, q in zip(ap, aq)]},
                      separators=(',', ':'))


class Core:
    """The hot loop, shared by the single-process and sharded modes."""

    def __init__(self, cfgs, status, trades_fh, check_every=0, snapshot_ns=E.NS,
                 depth=10, timing=None, filter_symbols=True):
        self.books = {s: Book(c) for s, c in cfgs.items()}
        self.filter_symbols = filter_symbols   # False: every record's symbol has a book
        self.shared = []
        for b in self.books.values():
            b.trades = self.shared
        self.status = status
        self.trades_fh = trades_fh
        self.checkers = ({s: Checker(full_every=check_every) for s in cfgs}
                         if check_every else None)
        if self.checkers:
            for s, c in self.checkers.items():
                c.attach(self.books[s])
        self.snapshot_ns = snapshot_ns
        self.depth = depth
        self.timing = timing              # array('Q') of per-event ns, or None
        self.on_event = None              # optional hook(book, rec, status, trades)
        self.snapshots = []
        self.n_trades = self.volume = 0
        self.events = 0
        self.max_resting = 0

    def run(self, path, symbols, limit=None):
        books, shared, status = self.books, self.shared, self.status
        checkers, snap_ns = self.checkers, self.snapshot_ns
        boundary = {s: E.EPOCH_NS + snap_ns for s in books}
        second = {s: 0 for s in books}
        timing = self.timing
        perf = time.perf_counter_ns
        get_book = books.get if self.filter_symbols else books.__getitem__
        hook = self.on_event
        unpack = E.RECORD.iter_unpack
        for buf in E.iter_chunks(path, 0, limit):
            for rec in unpack(buf):
                sym = rec[2]
                book = get_book(sym)
                if book is None:
                    continue
                if rec[1] >= boundary[sym]:
                    self._cross_boundary(sym, book, rec[1], boundary, second)
                if checkers is None and timing is None and hook is None:
                    status[rec[0] - 1] = book.process(rec)
                    continue
                n0 = len(shared)
                if timing is not None:
                    t0 = perf()
                    st = book.process(rec)
                    timing.append(perf() - t0)
                else:
                    st = book.process(rec)
                status[rec[0] - 1] = st
                if checkers is not None:
                    checkers[sym].after_event(book, n0, rec, st)
                if hook is not None:
                    hook(book, rec, st, shared[n0:])
            self._flush()
            self.max_resting = max(self.max_resting,
                                   sum(len(b.orders) for b in books.values()))
        self.events = sum(b.n_events for b in books.values())
        if checkers is not None:
            for s, c in checkers.items():
                c.full_check(books[s])
        return self

    def _cross_boundary(self, sym, book, ts, boundary, second):
        while ts >= boundary[sym]:
            second[sym] += 1
            self.snapshots.append((second[sym], sym,
                                   _snapshot_line(second[sym], sym, book, self.depth)))
            boundary[sym] += self.snapshot_ns

    def _flush(self):
        shared = self.shared
        if not shared:
            return
        pack = TRADE.pack
        self.trades_fh.write(b''.join(pack(t[0], t[1], t[2], t[3], t[4], t[5], t[6], t[7])
                                      for t in shared))
        self.n_trades += len(shared)
        self.volume += sum(t[7] for t in shared)
        shared.clear()

    def book_stats(self):
        out = {}
        for s, b in sorted(self.books.items()):
            out[str(s)] = {'submitted': b.submitted, 'filled': b.filled,
                           'cancelled': b.cancelled, 'rejected': b.rejected,
                           'resting': b.resting, 'stp_cancels': b.stp_cancels,
                           'ts_regressions': b.ts_regressions,
                           'live_orders': len(b.orders), 'events': b.n_events,
                           'ref': b.ref, 'levels': len(b.bid_keys) + len(b.ask_keys)}
        return out


def _status_counts(status):
    counts = {}
    for code, name in STATUS_NAMES.items():
        c = status.count(code) if hasattr(status, 'count') else 0
        if c:
            counts[name] = c
    return counts


def _write_outputs(out_dir, status, snapshots, stats):
    with open(os.path.join(out_dir, 'status.bin'), 'wb') as fh:
        fh.write(bytes(status))
    with open(os.path.join(out_dir, 'snapshots.jsonl'), 'w') as fh:
        for _sec, _sym, line in sorted(snapshots, key=lambda x: (x[0], x[1])):
            fh.write(line)
            fh.write('\n')
    with open(os.path.join(out_dir, 'stats.json'), 'w') as fh:
        json.dump(stats, fh, indent=1, sort_keys=True)


def replay(path, out_dir, symbols=None, check_every=0, snapshot_ns=E.NS, depth=10,
           limit=None, timing=None):
    """Single-process replay.  Returns the stats dict."""
    meta = E.load_meta(path)
    n = meta['n_events'] if limit is None else min(limit, meta['n_events'])
    symbols = list(symbols) if symbols is not None else list(range(meta['params']['n_symbols']))
    os.makedirs(out_dir, exist_ok=True)
    status = bytearray(n)
    t0 = time.perf_counter()
    all_symbols = len(symbols) == meta['params']['n_symbols']
    with open(os.path.join(out_dir, 'trades.bin'), 'wb') as tfh:
        core = Core(configs_from_meta(meta, symbols), status, tfh, check_every,
                    snapshot_ns, depth, timing, not all_symbols).run(path, symbols, limit)
    dt = time.perf_counter() - t0
    stats = {'mode': 'single', 'events': core.events, 'trades': core.n_trades,
             'volume': core.volume, 'seconds': round(dt, 3),
             'events_per_second': round(core.events / dt) if dt else None,
             'check_every': check_every, 'snapshots': len(core.snapshots),
             'max_resting_orders': core.max_resting, 'status_counts': _status_counts(status),
             'books': core.book_stats(),
             'ru_maxrss_mb': round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)}
    _write_outputs(out_dir, status, core.snapshots, stats)
    return stats


_SHARED = {}


def _shard_worker(args):
    path, symbols, out_dir, idx, check_every, snapshot_ns, depth, limit = args
    meta = E.load_meta(path)
    tpath = os.path.join(out_dir, 'shard_%d.trades' % idx)
    with open(tpath, 'wb') as tfh:
        core = Core(configs_from_meta(meta, symbols), _SHARED['status'], tfh,
                    check_every, snapshot_ns, depth).run(path, symbols, limit)
    return {'symbols': symbols, 'events': core.events, 'trades': core.n_trades,
            'volume': core.volume, 'snapshots': core.snapshots,
            'books': core.book_stats(), 'max_resting': core.max_resting,
            'trades_path': tpath}


def _merge_trade_files(paths, out_path):
    def records(p):
        with open(p, 'rb') as fh:
            data = fh.read()
        for i in range(0, len(data), TRADE_SIZE):
            chunk = data[i:i + TRADE_SIZE]
            seq, idx = struct.unpack_from('<QI', chunk)
            yield (seq, idx, chunk)
    with open(out_path, 'wb') as out:
        buf = []
        for _seq, _idx, chunk in heapq.merge(*[records(p) for p in paths]):
            buf.append(chunk)
            if len(buf) >= 65536:
                out.write(b''.join(buf))
                buf.clear()
        out.write(b''.join(buf))
    for p in paths:
        os.remove(p)


def replay_sharded(path, out_dir, processes=4, check_every=0, snapshot_ns=E.NS,
                   depth=10, limit=None):
    """Symbol-sharded multi-process replay; outputs identical to replay()."""
    meta = E.load_meta(path)
    n = meta['n_events'] if limit is None else min(limit, meta['n_events'])
    n_sym = meta['params']['n_symbols']
    shards = [s for s in (list(range(i, n_sym, processes)) for i in range(processes)) if s]
    os.makedirs(out_dir, exist_ok=True)
    t0 = time.perf_counter()
    _SHARED['status'] = multiprocessing.RawArray('B', n)
    try:
        ctx = multiprocessing.get_context('fork')
        with ctx.Pool(len(shards)) as pool:
            parts = pool.map(_shard_worker, [(path, s, out_dir, i, check_every,
                                              snapshot_ns, depth, limit)
                                             for i, s in enumerate(shards)])
        status = bytearray(_SHARED['status'])
    finally:
        _SHARED.clear()
    _merge_trade_files([p['trades_path'] for p in parts],
                       os.path.join(out_dir, 'trades.bin'))
    dt = time.perf_counter() - t0
    snapshots = [s for p in parts for s in p['snapshots']]
    books = {}
    for p in parts:
        books.update(p['books'])
    stats = {'mode': 'sharded', 'processes': len(shards), 'shards': shards,
             'events': sum(p['events'] for p in parts),
             'trades': sum(p['trades'] for p in parts),
             'volume': sum(p['volume'] for p in parts), 'seconds': round(dt, 3),
             'events_per_second': round(n / dt) if dt else None,
             'check_every': check_every, 'snapshots': len(snapshots),
             'max_resting_orders': sum(p['max_resting'] for p in parts),
             'status_counts': _status_counts(status),
             'books': dict(sorted(books.items(), key=lambda kv: int(kv[0])))}
    _write_outputs(out_dir, status, snapshots, stats)
    return stats
