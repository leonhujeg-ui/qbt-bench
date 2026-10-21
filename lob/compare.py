"""Lockstep differential test: fast Book vs ReferenceBook on an event stream.

Every event is fed to both engines; status codes and trades are compared
immediately, full level snapshots and counters every `digest_every` events
per symbol.  The invariant checker runs on both engines.  Symbols are
independent, so a stream can be split by symbol across processes and the
per-symbol results merged (first divergence = minimum seq over shards).
"""
import multiprocessing
import time

from lob import events as E
from lob.book import Book
from lob.common import BookConfig
from lob.invariants import Checker
from lob.reference import ReferenceBook


def _configs(meta, symbols):
    return {s: BookConfig.from_dict(meta['books'][str(s)], symbol=s) for s in symbols}


class _Pair:
    __slots__ = ('book', 'ref', 'chk_b', 'chk_r', 'n')

    def __init__(self, cfg, full_every):
        self.book = Book(cfg)
        self.ref = ReferenceBook(cfg)
        self.chk_b = Checker(full_every=full_every)
        self.chk_r = Checker(full_every=full_every)
        self.chk_b.attach(self.book)
        self.chk_r.attach(self.ref)
        self.n = 0


def _divergence(seq, sym, what, a, b):
    return {'seq': seq, 'symbol': sym, 'what': what, 'book': repr(a)[:400],
            'reference': repr(b)[:400]}


def compare_stream(path, symbols=None, full_every=1000, digest_every=1000,
                   limit=None, meta=None):
    """Return a summary dict; summary['first_divergence'] is None on success."""
    meta = meta or E.load_meta(path)
    symbols = list(symbols) if symbols is not None else list(range(meta['params']['n_symbols']))
    pairs = {s: _Pair(cfg, full_every) for s, cfg in _configs(meta, symbols).items()}
    t0 = time.perf_counter()
    n = trades = volume = 0
    div = None
    for rec in E.iter_records(path, symbols=symbols, stop=limit):
        pr = pairs[rec[2]]
        nb, nr = len(pr.book.trades), len(pr.ref.trades)
        sb = pr.book.process(rec)
        sr = pr.ref.process(rec)
        pr.chk_b.after_event(pr.book, nb, rec)
        pr.chk_r.after_event(pr.ref, nr, rec)
        n += 1
        pr.n += 1
        if sb != sr:
            div = _divergence(rec[0], rec[2], 'status', sb, sr)
            break
        tb, tr = pr.book.trades[nb:], pr.ref.trades[nr:]
        if tb != tr:
            div = _divergence(rec[0], rec[2], 'trades', tb, tr)
            break
        trades += len(tb)
        volume += sum(t[7] for t in tb)
        if pr.n % digest_every == 0:
            div = _compare_state(pr, rec)
            if div:
                break
        if len(pr.book.trades) > 100000:
            del pr.book.trades[:], pr.ref.trades[:]
    if div is None:
        for s, pr in pairs.items():
            div = _compare_state(pr, (pr.book.seq, 0, s)) or div
            pr.chk_b.full_check(pr.book)
            pr.chk_r.full_check(pr.ref)
            if div:
                break
    return {'events': n, 'trades': trades, 'volume': volume, 'symbols': symbols,
            'first_divergence': div, 'seconds': round(time.perf_counter() - t0, 3),
            'resting_orders': sum(len(p.book.orders) for p in pairs.values()),
            'full_checks': sum(p.chk_b.full_checks for p in pairs.values()),
            'trades_checked': sum(p.chk_b.trades_checked for p in pairs.values())}


def _compare_state(pr, rec):
    b, r = pr.book, pr.ref
    if b.levels() != r.levels():
        return _divergence(rec[0], rec[2], 'levels', b.levels()[:5], r.levels()[:5])
    cb = (b.submitted, b.filled, b.cancelled, b.rejected, b.resting, b.stp_cancels, b.ref)
    cr = (r.submitted, r.filled, r.cancelled, r.rejected, r.resting, r.stp_cancels, r.ref)
    if cb != cr:
        return _divergence(rec[0], rec[2], 'counters', cb, cr)
    if sorted(b.orders) != sorted(r.by_id):
        return _divergence(rec[0], rec[2], 'order ids', len(b.orders), len(r.by_id))
    return None


def _worker(args):
    path, symbols, full_every, digest_every, limit = args
    return compare_stream(path, symbols, full_every, digest_every, limit)


def compare_sharded(path, processes=4, full_every=1000, digest_every=1000, limit=None):
    """Split symbols across processes; merge the per-shard summaries."""
    meta = E.load_meta(path)
    n_sym = meta['params']['n_symbols']
    shards = [list(range(i, n_sym, processes)) for i in range(processes)]
    shards = [s for s in shards if s]
    t0 = time.perf_counter()
    with multiprocessing.get_context('fork').Pool(len(shards)) as pool:
        parts = pool.map(_worker, [(path, s, full_every, digest_every, limit) for s in shards])
    divs = [p['first_divergence'] for p in parts if p['first_divergence']]
    out = {k: sum(p[k] for p in parts) for k in ('events', 'trades', 'volume',
                                                   'resting_orders', 'full_checks',
                                                   'trades_checked')}
    out['first_divergence'] = min(divs, key=lambda d: d['seq']) if divs else None
    out['shards'] = shards
    out['seconds'] = round(time.perf_counter() - t0, 3)
    out['shard_seconds'] = [p['seconds'] for p in parts]
    return out
