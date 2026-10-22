"""命令行入口：python -m qbt.cli <demo|backtest|optimize|bench|fuzz|test> ...

demo：合成数据上跑 6 个策略的默认参数，打印对比表，写出文本报告 + SVG 权益曲线到 out/demo/。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

from qbt.broker import BrokerConfig                     # noqa: E402
from qbt.data import GenSpec, generate                  # noqa: E402
from qbt.engine import run_backtest                     # noqa: E402
from qbt import metrics as M                            # noqa: E402
from qbt import report as R                             # noqa: E402


DEMO_SPEC_KW = {"halt_prob": 0.002, "gap_prob": 0.01, "jump_prob": 0.002}   # demo 合成数据的注入参数
DEMO_CFG_KW = {"max_participation": 0.5}                                      # demo / backtest 的撮合配置


def _registry():
    from strategies import REGISTRY
    return REGISTRY


def cmd_demo(args):
    reg = _registry()
    d = generate(GenSpec(n=args.n, seed=args.seed, **DEMO_SPEC_KW))
    cfg = BrokerConfig(**DEMO_CFG_KW)
    rows, best = [], None
    out_dir = args.out
    print(f"合成数据 n={args.n} seed={args.seed} 指纹={d.fingerprint()[:12]}  (合成数据，不构成任何投资结论)")
    print(f"{'strategy':22s} {'final':>12s} {'ret%':>8s} {'sharpe':>8s} {'mdd%':>7s} {'trades':>7s} {'fills':>6s} {'sec':>6s}")
    for name in sorted(reg):
        r = run_backtest(d, reg[name](), cfg)
        m = M.compute(r.equity, r.ts, r.bar_seconds, r.trades)
        paths = R.write_report(r, out_dir, name, title=f"{name} (synthetic seed={args.seed})")
        rows.append((name, m))
        print(f"{name:22s} {r.final_equity:12,.0f} {100 * m['total_return']:8.2f} {m['sharpe']:8.2f} "
              f"{100 * m['max_drawdown']:7.2f} {m['n_trades']:7d} {len(r.fills):6d} {r.elapsed:6.2f}")
        if best is None or m["sharpe"] > best[1]["sharpe"]:
            best = (name, m, paths)
    print(f"\n报告目录: {out_dir}/   最佳夏普: {best[0]}  -> {best[2]['txt']}, {best[2]['svg']}")
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump({name: {k: v for k, v in m.items() if k != "monthly"} for name, m in rows}, f, indent=1, sort_keys=True)
    return 0


def cmd_backtest(args):
    reg = _registry()
    params = json.loads(args.params) if args.params else {}
    d = generate(GenSpec(n=args.n, seed=args.seed))
    r = run_backtest(d, reg[args.strategy](**params), BrokerConfig(**DEMO_CFG_KW))
    m = M.compute(r.equity, r.ts, r.bar_seconds, r.trades)
    print(R.text_report(r, m, title=f"{args.strategy} {params}"))
    if args.out:
        paths = R.write_report(r, args.out, args.strategy)
        print("written:", paths)
    return 0


QUICK_MODULES = ("test_limits", "test_golden", "test_broker", "test_engine", "test_boundaries", "test_contract")


def cmd_test(args):
    """退出码：0 全部通过；1 有失败/错误；2 参数错误（argparse）。默认跑 tests/ 全量；--quick 只跑冒烟子集。"""
    import unittest
    loader = unittest.defaultTestLoader
    if args.quick:
        os.environ["QBT_FAST_TESTS"] = "1"                    # 冒烟：缩短随机判据的数据规模，性能阈值判据跳过
        names = [f"tests.{m}" for m in QUICK_MODULES if os.path.exists(os.path.join(REPO, "tests", m + ".py"))]
        suite = loader.loadTestsFromNames(names)
    else:
        suite = loader.discover(os.path.join(REPO, "tests"), pattern=args.pattern)
    result = unittest.TextTestRunner(verbosity=1, stream=sys.stderr).run(suite)
    return 0 if result.wasSuccessful() else 1


def build_parser():
    ap = argparse.ArgumentParser(prog="python -m qbt.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    demo = sub.add_parser("demo", help="一键演示：6 策略 + 报告 + SVG")
    demo.add_argument("--n", type=int, default=20_000)
    demo.add_argument("--seed", type=int, default=1)
    demo.add_argument("--out", default=os.path.join("out", "demo"))
    demo.set_defaults(fn=cmd_demo)
    bt = sub.add_parser("backtest", help="单策略回测")
    bt.add_argument("--strategy", default="ma_cross")
    bt.add_argument("--params", default="", help='JSON，如 \'{"fast": 5, "slow": 30}\'')
    bt.add_argument("--n", type=int, default=20_000)
    bt.add_argument("--seed", type=int, default=1)
    bt.add_argument("--out", default=None)
    bt.set_defaults(fn=cmd_backtest)
    for name, module, help_ in (("optimize", "qbt.optimizer", "网格/随机/walk-forward 优化"),
                                ("bench", "bench.bench", "性能基准"), ("fuzz", "fuzz.fuzz", "模糊测试")):
        p = sub.add_parser(name, help=help_, add_help=False)
        p.set_defaults(fn=_delegate(module))
    t = sub.add_parser("test", help="运行单元测试（默认全量；--quick 冒烟子集）")
    t.add_argument("--pattern", default="test_*.py")
    t.add_argument("--quick", action="store_true", help="冒烟：只跑 " + ", ".join(QUICK_MODULES))
    t.set_defaults(fn=cmd_test)
    return ap


def _delegate(module_name):
    def run(args):
        import importlib
        mod = importlib.import_module(module_name)
        return mod.main(args.rest)
    return run


def main(argv=None):
    ap = build_parser()
    args, rest = ap.parse_known_args(argv)
    if rest and args.cmd not in ("optimize", "bench", "fuzz"):     # 只有委托子命令才透传未知参数
        ap.error(f"unrecognized arguments: {' '.join(rest)}")       # argparse => 退出码 2
    args.rest = rest
    t0 = time.perf_counter()
    code = args.fn(args)
    if args.cmd == "demo":
        print(f"总耗时 {time.perf_counter() - t0:.2f}s")
    return code


if __name__ == "__main__":
    sys.exit(main())
