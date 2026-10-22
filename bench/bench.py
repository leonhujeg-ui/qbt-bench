"""性能基准：输出 JSON，REPORT.md 直接引用。

固定基准（PLAN.md 阶段 6）：
- single_core_backtest: 1,000,000 根 K 线 x 1 个策略（ma_cross 默认参数），单进程，冷/热指标缓存各一次。
- grid_search: 2,000 组参数（ma_cross 的基准参数空间，全部 fast<slow）x 20,000 根，8 进程；并记录 1 进程耗时。
- 另外记录数据生成、校验、常用指标在 1M 根上的耗时。

用法::

    python bench/bench.py --out bench/results/after_xxx.json [--quick] [--skip-serial] [--repeat N]

记录口径（审核要求）：machine 里有平台 / Python / CPU 型号 / os.cpu_count() / 1 分钟负载 / 完整命令行；
每个计时都是 best-of-N（`--repeat`，默认 1，JSON 里 `samples` 给出全部样本，表里取最小值）；
"冷缓存" = 进程内指标缓存清空、数据指纹未算（含 sha256 指纹 + 指标计算；数据已在内存，不涉及磁盘），"热缓存" = 紧接着第二次；
网格的进程数同时记录进程池上限（`workers`）与运行中实际观察到的最大子进程数（`processes_observed_max`）。
"""
from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import platform
import subprocess
import sys
import threading
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


def _best_of(fn, repeats):
    """fn() -> (seconds, result)；跑 repeats 次，返回 (最小秒数, 全部样本, 最后一次结果)。"""
    samples, out = [], None
    for _ in range(max(1, repeats)):
        t, out = fn()
        samples.append(t)
    return min(samples), samples, out


class _ChildWatcher:
    """后台线程每 20ms 采样 multiprocessing.active_children()，记录运行期间的最大子进程数。"""

    def __init__(self):
        self.max_seen, self._stop = 0, threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)

    def _loop(self):
        while not self._stop.is_set():
            self.max_seen = max(self.max_seen, len(multiprocessing.active_children()))
            self._stop.wait(0.02)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join()


def machine_info():
    try:
        commit = subprocess.run(["git", "-C", REPO, "rev-parse", "--short", "HEAD"], capture_output=True,
                                text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        commit = ""
    load = os.getloadavg()[0] if hasattr(os, "getloadavg") else None
    return {"platform": platform.platform(), "machine": platform.machine(), "python": platform.python_version(),
            "python_impl": platform.python_implementation(), "cpus": os.cpu_count(),
            "processor": platform.processor() or _cpu_model(), "cpu_model": _cpu_model(), "git_commit": commit,
            "load_avg_1m_at_start": load, "command": "python3.12 " + " ".join([os.path.relpath(sys.argv[0], REPO)]
                                                                            + sys.argv[1:]),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S")}


def _cpu_model():
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    if sys.platform == "darwin":
        try:
            return subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True,
                                  text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return ""


def bench_data_and_indicators(n, repeats=1):
    out = {"best_of": max(1, repeats)}
    out["generate_seconds"], out["generate_samples"], d = _best_of(lambda: _timed(generate, GenSpec(n=n, seed=1)),
                                                                   repeats)

    def validate():
        d._validated = False
        return _timed(d.validate)

    out["validate_seconds"], out["validate_samples"], _ = _best_of(validate, repeats)
    for name, args in (("sma", (d.close, 20)), ("ema", (d.close, 20)), ("wma", (d.close, 20)),
                       ("rsi", (d.close, 14)), ("atr", (d.high, d.low, d.close, 14)),
                       ("bollinger", (d.close, 20)), ("donchian", (d.high, d.low, 20)),
                       ("adx", (d.high, d.low, d.close, 14)), ("supertrend", (d.high, d.low, d.close, 10))):
        out[f"indicator_{name}_seconds"], out[f"indicator_{name}_samples"], _ = _best_of(
            lambda: _timed(I.REGISTRY[name], *args), repeats)
    return d, out


def bench_single_core(d, strategy="ma_cross", repeats=1):
    out = {"bars": len(d), "strategy": strategy, "best_of": max(1, repeats), "processes": 1,
           "cache_semantics": "cold = INDICATOR_CACHE.clear() 且数据指纹（OHLCV._fingerprint，sha256）未算：含指纹 + 指标计算，"
                              "数据已在内存，不涉及磁盘；warm = 紧接着的第二次（指纹与指标全部命中）"}
    cfg = BrokerConfig(max_participation=1.0)

    def cold():
        INDICATOR_CACHE.clear()
        d._fingerprint = None            # 否则 best-of-N 的第 2 次起指纹已记忆，"冷"只剩指标，与 01–04 的单次冷口径不一致
        return _timed(run_backtest, d, REGISTRY[strategy](), cfg, cache=INDICATOR_CACHE)

    def warm():
        return _timed(run_backtest, d, REGISTRY[strategy](), cfg, cache=INDICATOR_CACHE)

    out["cold_seconds"], out["cold_samples"], r = _best_of(cold, repeats)
    out["warm_seconds"], out["warm_samples"], r2 = _best_of(warm, repeats)
    out["metrics_seconds"], out["metrics_samples"], m = _best_of(
        lambda: _timed(M.compute, r.equity, r.ts, r.bar_seconds, r.trades), repeats)
    out["fills"] = len(r.fills)
    out["trades"] = len(r.trades)
    out["final_equity"] = r.final_equity
    out["identical_rerun"] = r.equity == r2.equity
    out["bars_per_second_cold"] = len(d) / out["cold_seconds"]
    return out


def bench_grid(workers=8, serial=True, n_bars=N_GRID_BARS, n_combos=N_GRID_COMBOS, repeats=1):
    combos = grid(BENCH_SPACE)[:n_combos]
    task = make_task("ma_cross", GenSpec(n=n_bars, seed=3).to_dict(), broker=BrokerConfig(max_participation=1.0))
    out = {"combos": len(combos), "bars": n_bars, "workers": workers, "cpus": os.cpu_count(),
           "best_of": max(1, repeats)}
    with _ChildWatcher() as w:
        t_par, out["parallel_samples"], res_par = _best_of(
            lambda: _timed(run_search, task, combos, workers=workers), repeats)
    out["processes_observed_max"] = w.max_seen
    out["parallel_seconds"] = t_par
    out["per_combo_ms_parallel"] = 1000.0 * t_par / len(combos)
    if serial:
        t_ser, out["serial_samples"], res_ser = _best_of(
            lambda: _timed(run_search, task, combos, workers=1), repeats)
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
    ap.add_argument("--repeat", type=int, default=1, help="每个计时跑 N 次取最小值（best-of-N）")
    args = ap.parse_args(argv)
    n_single = 100_000 if args.quick else N_SINGLE
    n_combos = 100 if args.quick else N_GRID_COMBOS
    report = {"label": args.label, "machine": machine_info(), "quick": args.quick}
    d, report["data_indicators_1M"] = bench_data_and_indicators(n_single, repeats=args.repeat)
    report["single_core_backtest"] = bench_single_core(d, repeats=args.repeat)
    del d
    report["grid_search"] = bench_grid(args.workers, serial=not args.skip_serial, n_combos=n_combos,
                                       repeats=args.repeat)
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
