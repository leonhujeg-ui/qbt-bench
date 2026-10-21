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
    _dump({k: res[k] for k in ('engine_only', 'engine_checked', 'snapshot', 'e2e',
                               'latency_ns', 'tracemalloc')}, args.json)


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
    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
