"""Stress / performance measurement.  Everything is measured, nothing estimated.

  throughput_e2e   single-process replay (decode + book + invariant checker with
                   cheap checks every event and a full scan every `check_every`
                   events + status/trades/snapshots output)
  throughput_engine  pre-decoded records, Book.process only (no checker, no I/O)
  throughput_engine_checked  pre-decoded, Book.process + checker cheap checks
  latency          per-event Book.process wall time, p50/p99/p999/max
  snapshot         Book.top(10) cost: amortised with a call after EVERY event
                   (worst case) and on an unchanged book (cached)
  memory           ru_maxrss of the replay process; tracemalloc peak on a
                   separate smaller run (tracemalloc itself is slow)
"""
import array
import json
import os
import platform
import subprocess
import sys
import time
import tracemalloc

from lob import events as E
from lob import replay as R
from lob.book import Book
from lob.invariants import Checker


def machine_info():
    cpu = 'unknown'
    try:
        with open('/proc/cpuinfo') as fh:
            for line in fh:
                if line.startswith('model name'):
                    cpu = line.split(':', 1)[1].strip()
                    break
    except OSError:
        pass
    return {'python': platform.python_version(), 'implementation': platform.python_implementation(),
            'platform': platform.platform(), 'cpu': cpu, 'cpus': os.cpu_count()}


def percentiles(values, ps=(50, 90, 99, 99.9)):
    s = sorted(values)
    out = {}
    for p in ps:
        idx = min(len(s) - 1, int(round(p / 100.0 * (len(s) - 1))))
        out['p%s' % str(p).replace('.', '_')] = s[idx]
    out['max'] = s[-1]
    out['mean'] = sum(s) / len(s)
    return out


def _books(meta, limit=None):
    symbols = range(meta['params']['n_symbols'])
    return {s: Book(c) for s, c in R.configs_from_meta(meta, symbols).items()}


def engine_throughput(path, meta, n, checked=False):
    """Book.process on pre-decoded records (first n events)."""
    recs = list(E.iter_records(path, stop=n))
    books = _books(meta)
    checkers = None
    if checked:
        checkers = {s: Checker(full_every=0) for s in books}
        for s, c in checkers.items():
            c.attach(books[s])
    t0 = time.perf_counter()
    if checkers is None:
        for rec in recs:
            books[rec[2]].process(rec)
    else:
        for rec in recs:
            b = books[rec[2]]
            n0 = len(b.trades)
            b.process(rec)
            checkers[rec[2]].after_event(b, n0, rec)
    dt = time.perf_counter() - t0
    return {'events': len(recs), 'seconds': round(dt, 4),
            'events_per_second': round(len(recs) / dt)}


def snapshot_cost(path, meta, n):
    """Return per-call cost (ns) of top(10) in two regimes."""
    recs = list(E.iter_records(path, stop=n))
    books = _books(meta)
    t0 = time.perf_counter()
    for rec in recs:
        books[rec[2]].process(rec)
    base = time.perf_counter() - t0
    books = _books(meta)
    t0 = time.perf_counter()
    for rec in recs:
        b = books[rec[2]]
        b.process(rec)
        b.top(10)
    with_snap = time.perf_counter() - t0
    b = max(books.values(), key=lambda x: len(x.orders))
    reps = 200000
    t0 = time.perf_counter()
    for _ in range(reps):
        b.top(10)
    cached = time.perf_counter() - t0
    t0 = time.perf_counter()
    for _ in range(reps):
        b.dirty = True
        b.top(10)
    rebuild = time.perf_counter() - t0
    return {'events': len(recs),
            'after_every_event_ns': round((with_snap - base) / len(recs) * 1e9, 1),
            'cached_ns': round(cached / reps * 1e9, 1),
            'forced_rebuild_ns': round(rebuild / reps * 1e9, 1)}


def tracemalloc_peak(path, meta, n):
    tracemalloc.start()
    out = os.path.join(os.path.dirname(path), '_tm_out')
    R.replay(path, out, check_every=0, limit=n)
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {'events': n, 'peak_mb': round(peak / 1024 / 1024, 1)}


def e2e_subprocess(path, out_dir, check_every):
    """Run the single-process replay in a fresh interpreter so that
    ru_maxrss measures that replay alone (not this harness)."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    subprocess.run([sys.executable, '-m', 'lob.cli', 'replay', '--stream', path, '--out',
                    out_dir, '--check', str(check_every)], cwd=repo, check=True,
                   stdout=subprocess.DEVNULL)
    with open(os.path.join(out_dir, 'stats.json')) as fh:
        return json.load(fh)


def best_of(fn, repeats):
    runs = [fn() for _ in range(repeats)]
    best = max(runs, key=lambda r: r['events_per_second'])
    return dict(best, runs=[r['events_per_second'] for r in runs])


def run(path, out_dir, check_every=10000, engine_events=1_000_000,
        latency_events=None, tracemalloc_events=1_000_000, label='run', repeats=3):
    meta = E.load_meta(path)
    os.makedirs(out_dir, exist_ok=True)
    res = {'label': label, 'stream': os.path.basename(path), 'stream_sha256': meta['sha256'],
           'n_events': meta['n_events'], 'machine': machine_info(),
           'check_every': check_every}
    # e2e first: a child inherits the parent's RSS high-water mark across exec
    stats = e2e_subprocess(path, os.path.join(out_dir, 'e2e'), check_every)
    res['e2e'] = {k: stats[k] for k in ('events', 'trades', 'volume', 'seconds',
                                        'events_per_second', 'snapshots',
                                        'max_resting_orders', 'ru_maxrss_mb')}
    res['engine_only'] = best_of(lambda: engine_throughput(path, meta, engine_events), repeats)
    res['engine_checked'] = best_of(lambda: engine_throughput(path, meta, engine_events, True),
                                    repeats)
    res['snapshot'] = snapshot_cost(path, meta, min(engine_events, 500_000))
    timing = array.array('Q')
    R.replay(path, os.path.join(out_dir, 'lat'), check_every=0, limit=latency_events,
             timing=timing)
    res['latency_ns'] = percentiles(timing)
    res['latency_ns']['events'] = len(timing)
    res['tracemalloc'] = tracemalloc_peak(path, meta, min(tracemalloc_events, meta['n_events']))
    with open(os.path.join(out_dir, 'stress_%s.json' % label), 'w') as fh:
        json.dump(res, fh, indent=1, sort_keys=True)
    return res
