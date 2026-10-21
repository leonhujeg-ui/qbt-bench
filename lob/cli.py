"""Command line entry point: python -m lob.cli <command> [options]."""
import argparse
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


def build_parser():
    ap = argparse.ArgumentParser(prog='python -m lob.cli')
    sub = ap.add_subparsers(dest='cmd', required=True)
    g = sub.add_parser('gen', help='generate a synthetic event stream')
    g.add_argument('--n', type=int, default=1_000_000)
    g.add_argument('--symbols', type=int, default=8)
    g.add_argument('--seed', type=int, default=12345)
    g.add_argument('--out', default=None)
    g.set_defaults(func=cmd_gen)
    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
