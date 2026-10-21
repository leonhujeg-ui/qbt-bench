"""性能基准：输出 JSON，REPORT.md 直接引用。

固定基准（PLAN.md 阶段 6）：
- single_core_backtest: 1,000,000 根 K 线 x 1 个策略（ma_cross 默认参数），单进程，冷/热指标缓存各一次。
- grid_search: 2,000 组参数（ma_cross 的基准参数空间，全部 fast<slow）x 20,000 根，8 进程；并记录 1 进程耗时。
- 另外记录数据生成、校验、常用指标在 1M 根上的耗时。

用法::

    python bench/bench.py --out bench/results/after_xxx.json [--quick] [--skip-serial]
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from qbt.broker import BrokerConfig                                   # noqa: E402
from qbt.data import GenSpec, generate                                # noqa: E402
from qbt.engine import run_backtest, INDICATOR_CACHE, IndicatorCache  # noqa: E402
from qbt import indicators as I                                       # noqa: E402
from qbt import metrics as M                                          # noqa: E402
from qbt.optimizer import grid, make_task, run_search                 # noqa: E402
from strategies import REGISTRY                                       # noqa: E402

BENCH_SPACE = {"fast": [3, 4, 5, 6, 8, 10, 12, 15], "slow": [20, 30, 40, 50, 60, 80, 100, 120, 150, 200],
               "ma": ["sma", "ema", "wma"], "size": [0.25, 0.5, 0.75, 1.0, 1.5], "short": [True, False]}
N_GRID_COMBOS, N_GRID_BARS, N_SINGLE = 2000, 20_000, 1_000_000


def _timed(fn, *args, **kw):
    t0 = time.perf_counter()
    out = fn(*args, **kw)
    return time.perf_counter() - t0, out


def machine_info():
    try:
        commit = subprocess.run(["git", "-C", REPO, "rev-parse", "--short", "HEAD"], capture_output=True,
                                text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        commit = ""
    return {"platform": platform.platform(), "python": platform.python_version(), "cpus": os.cpu_count(),
            "processor": platform.processor() or _cpu_model(), "git_commit": commit,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S")}


def _cpu_model():
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return ""


def bench_data_and_indicators(n):
    out = {}
    t, d = _timed(generate, GenSpec(n=n, seed=1))
    out["generate_seconds"] = t
    d._validated = False
    out["validate_seconds"], _ = _timed(d.validate)
    for name, args in (("sma", (d.close, 20)), ("ema", (d.close, 20)), ("wma", (d.close, 20)),
                       ("rsi", (d.close, 14)), ("atr", (d.high, d.low, d.close, 14)),
                       ("bollinger", (d.close, 20)), ("donchian", (d.high, d.low, 20)),
                       ("adx", (d.high, d.low, d.close, 14)), ("supertrend", (d.high, d.low, d.close, 10))):
        out[f"indicator_{name}_seconds"], _ = _timed(I.REGISTRY[name], *args)
    return d, out


def bench_single_core(d, strategy="ma_cross"):
    out = {"bars": len(d), "strategy": strategy}
    cfg = BrokerConfig(max_participation=1.0)
    INDICATOR_CACHE.clear()
    t_cold, r = _timed(run_backtest, d, REGISTRY[strategy](), cfg, cache=INDICATOR_CACHE)
    out["cold_seconds"] = t_cold
    t_warm, r2 = _timed(run_backtest, d, REGISTRY[strategy](), cfg, cache=INDICATOR_CACHE)
    out["warm_seconds"] = t_warm
    out["metrics_seconds"], m = _timed(M.compute, r.equity, r.ts, r.bar_seconds, r.trades)
    out["fills"] = len(r.fills)
    out["trades"] = len(r.trades)
    out["final_equity"] = r.final_equity
    out["identical_rerun"] = r.equity == r2.equity
    out["bars_per_second_cold"] = len(d) / t_cold
    return out


def bench_grid(workers=8, serial=True, n_bars=N_GRID_BARS, n_combos=N_GRID_COMBOS):
    combos = grid(BENCH_SPACE)[:n_combos]
    task = make_task("ma_cross", GenSpec(n=n_bars, seed=3).to_dict(), broker=BrokerConfig(max_participation=1.0))
    out = {"combos": len(combos), "bars": n_bars, "workers": workers}
    t_par, res_par = _timed(run_search, task, combos, workers=workers)
    out["parallel_seconds"] = t_par
    out["per_combo_ms_parallel"] = 1000.0 * t_par / len(combos)
    if serial:
        t_ser, res_ser = _timed(run_search, task, combos, workers=1)
        out["serial_seconds"] = t_ser
        out["speedup"] = t_ser / t_par if t_par > 0 else None
        out["bytewise_identical"] = json.dumps(res_par, sort_keys=True) == json.dumps(res_ser, sort_keys=True)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    ap.add_argument("--quick", action="store_true", help="缩小规模（只用于调试，不作为达标依据）")
    ap.add_argument("--skip-serial", action="store_true")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--label", default="")
    args = ap.parse_args(argv)
    n_single = 100_000 if args.quick else N_SINGLE
    n_combos = 100 if args.quick else N_GRID_COMBOS
    report = {"label": args.label, "machine": machine_info(), "quick": args.quick}
    d, report["data_indicators_1M"] = bench_data_and_indicators(n_single)
    report["single_core_backtest"] = bench_single_core(d)
    del d
    report["grid_search"] = bench_grid(args.workers, serial=not args.skip_serial, n_combos=n_combos)
    report["targets"] = {"single_core_backtest_1M_seconds": 3.0, "grid_2000x8proc_seconds": 90.0}
    report["pass"] = {"single_core": report["single_core_backtest"]["cold_seconds"] <= 3.0 and not args.quick,
                      "grid": report["grid_search"]["parallel_seconds"] <= 90.0 and not args.quick}
    text = json.dumps(report, indent=1, sort_keys=True)
    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
