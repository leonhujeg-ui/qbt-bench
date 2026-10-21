"""Command line entry point: python -m lob.cli <command> [options]."""
import argparse
import json
import os
import sys
import time

from lob import events as E

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'results')


def data_path(name):
    os.makedirs(DATA_DIR, exist_ok=True)
    return os.path.join(DATA_DIR, name)


def _progress(done, total):
    sys.stderr.write('\r  %d / %d' % (done, total))
    sys.stderr.flush()


def cmd_gen(args):
    kw = {'n_events': args.n, 'n_symbols': args.symbols, 'seed': args.seed}
    params = E.GenParams(**kw)
    out = args.out or data_path('events_%d_s%d.bin' % (args.n, args.seed))
    t0 = time.perf_counter()
    meta = E.generate(params, out, progress=_progress)
    dt = time.perf_counter() - t0
    sys.stderr.write('\n')
    print('wrote %s: %d events in %.1fs, sha256=%s' % (out, meta['n_events'], dt,
                                                       meta['sha256']))
    print('counts:', meta['counts'], 'regimes:', len(meta['regimes']))


def _dump(obj, out=None):
    text = json.dumps(obj, indent=1, sort_keys=True)
    if out:
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        with open(out, 'w') as fh:
            fh.write(text + '\n')
        print('wrote', out)
    print(text)


def cmd_replay(args):
    from lob import replay as R
    if args.processes > 1:
        stats = R.replay_sharded(args.stream, args.out, processes=args.processes,
                                 check_every=args.check, limit=args.limit)
    else:
        stats = R.replay(args.stream, args.out, check_every=args.check, limit=args.limit)
    _dump({k: v for k, v in stats.items() if k != 'books'}, args.json)


def cmd_compare(args):
    from lob import compare as C
    if args.processes > 1:
        s = C.compare_sharded(args.stream, processes=args.processes,
                              full_every=args.check, digest_every=args.digest,
                              limit=args.limit)
    else:
        s = C.compare_stream(args.stream, full_every=args.check,
                             digest_every=args.digest, limit=args.limit)
    _dump(s, args.json)
    return 0 if s['first_divergence'] is None else 1


def cmd_stress(args):
    from lob import stress
    res = stress.run(args.stream, args.out, check_every=args.check,
                     engine_events=args.engine_events, label=args.label)
    _dump(res, args.json)


def cmd_queue_collect(args):
    from lob import replay as R
    from lob.queue_collect import ProbeCollector
    meta = E.load_meta(args.stream)
    symbols = list(range(meta['params']['n_symbols']))
    coll = ProbeCollector(symbols, meta, sample_every=args.sample_every, seed=args.seed)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    status = bytearray(meta['n_events'])
    t0 = time.perf_counter()
    with open(os.devnull, 'wb') as tfh:
        core = R.Core(R.configs_from_meta(meta, symbols), status, tfh)
        core.on_event = coll.on_event
        core.run(args.stream, symbols, args.limit)
    coll.finalize()
    n = coll.write_csv(args.out)
    print('wrote %s: %d probes from %d events in %.1fs' % (args.out, n, core.events,
                                                          time.perf_counter() - t0))


def cmd_queue_model(args):
    from lob import queue_model as Q
    res, _model = Q.run(args.probes, args.out, horizons=tuple(args.horizons),
                        train_frac=args.train_frac)
    brief = {h: {k: v[k] for k in ('n', 'base_rate', 'brier', 'brier_constant',
                                   'brier_parametric', 'brier_train', 'by_regime')}
             for h, v in res['per_horizon'].items()}
    _dump({'probes': res['probes'], 'train': res['train'], 'test': res['test'],
           'per_horizon': brief}, args.json)


def cmd_fuzz(args):
    from lob.fuzz import fuzz
    return fuzz.main(['--n', str(args.n), '--seed', str(args.seed), '--bad-rate',
                      str(args.bad_rate), '--full-every', str(args.full_every)]
                     + (['--json', args.json] if args.json else []))


def cmd_mutate(args):
    from lob.mutants import run_mutations
    return run_mutations.main(['--target', args.target, '--max', str(args.max),
                               '--seed', str(args.seed), '--workers', str(args.workers)]
                              + (['--json', args.json] if args.json else []))


def cmd_coverage(args):
    from lob.tools import coverage
    return coverage.main((['--json', args.json] if args.json else []) + ['--'] + args.rest)


def cmd_bench(args):
    from lob.tools import bench_structures
    _dump(bench_structures.main(n_ops=args.ops), args.json)


def cmd_test(args):
    import subprocess
    env = dict(os.environ, LOB_TEST_SCALE=args.scale)
    cmd = [sys.executable, '-m', 'unittest', 'discover', '-s',
           os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tests'), '-t',
           os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
    return subprocess.call(cmd + (['-v'] if args.verbose else []), env=env)


def cmd_demo(args):
    """Small end-to-end run (about a minute): generate, compare, replay,
    stress, queue model, fuzz.  Numbers printed are from this run only."""
    from lob import compare as C, replay as R, stress, queue_model as Q
    from lob.fuzz import fuzz
    from lob.queue_collect import ProbeCollector
    out = args.out or data_path('demo')
    os.makedirs(out, exist_ok=True)
    stream = os.path.join(out, 'events.bin')
    meta = E.generate(E.GenParams(n_events=args.n, n_symbols=4, seed=args.seed), stream)
    print('[1/6] generated %d events, sha256=%s' % (meta['n_events'], meta['sha256'][:16]))
    s = C.compare_stream(stream, full_every=200, digest_every=200, limit=min(args.n, 50000))
    print('[2/6] book vs reference on %d events: trades=%d first_divergence=%s'
          % (s['events'], s['trades'], s['first_divergence']))
    st = R.replay(stream, os.path.join(out, 'replay'), check_every=1000)
    print('[3/6] replay: %d events/s (checker on), %d trades, %d snapshots, rss %.0f MB'
          % (st['events_per_second'], st['trades'], st['snapshots'], st['ru_maxrss_mb']))
    res = stress.run(stream, os.path.join(out, 'stress'), check_every=1000,
                     engine_events=min(args.n, 200000), label='demo', repeats=1)
    print('[4/6] engine-only %d ev/s, engine+checker %d ev/s, top10 after every event %.0f ns,'
          ' p50 %d ns p99 %d ns' % (res['engine_only']['events_per_second'],
                                    res['engine_checked']['events_per_second'],
                                    res['snapshot']['after_every_event_ns'],
                                    res['latency_ns']['p50'], res['latency_ns']['p99']))
    symbols = list(range(4))
    coll = ProbeCollector(symbols, meta, sample_every=10, seed=1)
    with open(os.devnull, 'wb') as tfh:
        core = R.Core(R.configs_from_meta(meta, symbols), bytearray(meta['n_events']), tfh)
        core.on_event = coll.on_event
        core.run(stream, symbols)
    coll.finalize()
    probes = os.path.join(out, 'probes.csv')
    coll.write_csv(probes)
    qres, _ = Q.run(probes, os.path.join(out, 'queue_model'), horizons=(1, 2, 5))
    for h, v in qres['per_horizon'].items():
        print('[5/6] queue model h=%ss: n=%d base=%.3f Brier=%.4f (constant %.4f, parametric %.4f)'
              % (h, v['n'], v['base_rate'], v['brier'], v['brier_constant'],
                 v['brier_parametric']))
    f = fuzz.run(n=min(args.n, 20000), seed=args.seed, full_every=100)
    print('[6/6] fuzz %d events: crashes=%d invariant_violations=%d divergences=%d'
          % (f['events'], len(f['crashes']), len(f['invariant_violations']),
             f['n_divergences']))
    print('outputs under', out)


def build_parser():
    ap = argparse.ArgumentParser(prog='python -m lob.cli')
    sub = ap.add_subparsers(dest='cmd', required=True)
    g = sub.add_parser('gen', help='generate a synthetic event stream')
    g.add_argument('--n', type=int, default=1_000_000)
    g.add_argument('--symbols', type=int, default=8)
    g.add_argument('--seed', type=int, default=12345)
    g.add_argument('--out', default=None)
    g.set_defaults(func=cmd_gen)
    r = sub.add_parser('replay', help='replay a stream (single or sharded)')
    r.add_argument('--stream', required=True)
    r.add_argument('--out', required=True)
    r.add_argument('--processes', type=int, default=1)
    r.add_argument('--check', type=int, default=1000, help='full invariant scan period (0=off)')
    r.add_argument('--limit', type=int, default=None)
    r.add_argument('--json', default=None, help='also write stats here')
    r.set_defaults(func=cmd_replay)
    c = sub.add_parser('compare', help='lockstep Book vs reference')
    c.add_argument('--stream', required=True)
    c.add_argument('--processes', type=int, default=1)
    c.add_argument('--check', type=int, default=1000)
    c.add_argument('--digest', type=int, default=1000)
    c.add_argument('--limit', type=int, default=None)
    c.add_argument('--json', default=None)
    c.set_defaults(func=cmd_compare)
    st = sub.add_parser('stress', help='throughput / latency / memory measurement')
    st.add_argument('--stream', required=True)
    st.add_argument('--out', required=True)
    st.add_argument('--check', type=int, default=10000)
    st.add_argument('--engine-events', type=int, default=1_000_000)
    st.add_argument('--label', default='run')
    st.add_argument('--json', default=None)
    st.set_defaults(func=cmd_stress)
    _add_analysis_commands(sub)
    return ap


def _add_analysis_commands(sub):
    qc = sub.add_parser('queue-collect', help='collect queue-model ground truth')
    qc.add_argument('--stream', required=True)
    qc.add_argument('--out', required=True)
    qc.add_argument('--sample-every', type=int, default=50)
    qc.add_argument('--seed', type=int, default=99)
    qc.add_argument('--limit', type=int, default=None)
    qc.set_defaults(func=cmd_queue_collect)
    qm = sub.add_parser('queue-model', help='calibrate + evaluate the queue model')
    qm.add_argument('--probes', required=True)
    qm.add_argument('--out', required=True)
    qm.add_argument('--horizons', type=int, nargs='+', default=[1, 2, 5, 10])
    qm.add_argument('--train-frac', type=float, default=0.7)
    qm.add_argument('--json', default=None)
    qm.set_defaults(func=cmd_queue_model)
    fz = sub.add_parser('fuzz', help='random + malformed events with invariants')
    fz.add_argument('--n', type=int, default=200000)
    fz.add_argument('--seed', type=int, default=2024)
    fz.add_argument('--bad-rate', type=float, default=0.3)
    fz.add_argument('--full-every', type=int, default=100)
    fz.add_argument('--json', default=None)
    fz.set_defaults(func=cmd_fuzz)
    mu = sub.add_parser('mutate', help='mutation testing')
    mu.add_argument('--target', default='book')
    mu.add_argument('--max', type=int, default=120)
    mu.add_argument('--seed', type=int, default=1)
    mu.add_argument('--workers', type=int, default=4)
    mu.add_argument('--json', default=None)
    mu.set_defaults(func=cmd_mutate)
    cv = sub.add_parser('coverage', help='statement coverage of the test suite')
    cv.add_argument('--json', default=None)
    cv.add_argument('rest', nargs='*')
    cv.set_defaults(func=cmd_coverage)
    be = sub.add_parser('bench', help='price-level index structure benchmark')
    be.add_argument('--ops', type=int, default=300000)
    be.add_argument('--json', default=None)
    be.set_defaults(func=cmd_bench)
    te = sub.add_parser('test', help='run the unittest suite')
    te.add_argument('--scale', default='standard', choices=['quick', 'standard', 'full'])
    te.add_argument('-v', '--verbose', action='store_true')
    te.set_defaults(func=cmd_test)
    de = sub.add_parser('demo', help='one-command end-to-end demonstration')
    de.add_argument('--n', type=int, default=200000)
    de.add_argument('--seed', type=int, default=7)
    de.add_argument('--out', default=None)
    de.set_defaults(func=cmd_demo)


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
