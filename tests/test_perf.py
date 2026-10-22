"""性能判据（带阈值，慢了就红）。

- 覆盖率监控（QBT_COVERAGE=1）下计时无意义，跳过并说明；
- 突变/模糊的快速模式（QBT_FAST_TESTS=1）并行跑多个测试进程，计时不可靠，跳过（突变体若把复杂度搞坏会被 timeout 抓住）。
阈值直接取 PLAN.md 目标的按比例折算值：1M 根 <= 3s => 200k 根 <= 0.6s（冷缓存，每次新建指标缓存，含指标计算）；
指标 200k 根 <= 0.25s（实测 0.02-0.06s）；80 组 x 5k 根 4 进程 <= 8s。取 2-3 次中的最小值以抑制机器噪声。
"""
import os
import time
import unittest

from qbt.broker import BrokerConfig
from qbt.data import generate
from qbt.engine import run_backtest, IndicatorCache
from qbt import indicators as I
from qbt.optimizer import grid, make_task, run_search
from qbt.data import GenSpec
from strategies import REGISTRY
from tests._helpers import FAST, UNDER_COVERAGE

SKIP = UNDER_COVERAGE or FAST
REASON = "perf thresholds are only meaningful on an idle CPU without tracing (QBT_COVERAGE / QBT_FAST_TESTS set)"


def best_of(fn, repeats=3):
    best = float("inf")
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best


@unittest.skipIf(SKIP, REASON)
class TestPerformance(unittest.TestCase):
    def test_engine_200k_bars_under_threshold(self):
        d = generate(n=200_000, seed=1)
        t = best_of(lambda: run_backtest(d, REGISTRY["ma_cross"](), BrokerConfig(max_participation=1.0),
                                         cache=IndicatorCache()), 3)        # 每次新缓存 => 冷路径（含 SMA 计算）
        self.assertLess(t, 0.6, f"200k-bar ma_cross cold backtest took {t:.3f}s (> 0.6s = 3s/1M 按比例)")

    def test_indicators_scale_linearly(self):
        small, big = generate(n=50_000, seed=2), generate(n=200_000, seed=2)
        for name, args in (("sma", lambda d: (d.close, 20)), ("rolling_max", lambda d: (d.high, 50)),
                           ("atr", lambda d: (d.high, d.low, d.close, 14)), ("rsi", lambda d: (d.close, 14)),
                           ("linreg_slope", lambda d: (d.close, 30))):
            ts = best_of(lambda: I.REGISTRY[name](*args(small)))
            tb = best_of(lambda: I.REGISTRY[name](*args(big)))
            self.assertLess(tb / max(ts, 1e-4), 8.0, f"{name}: 4x data took {tb / ts:.1f}x time (expect ~4x)")
            self.assertLess(tb, 0.25, f"{name} on 200k bars took {tb:.3f}s (> 0.25s)")

    def test_grid_100_combos_parallel_under_threshold(self):
        combos = grid({"fast": [3, 5, 8, 10, 12], "slow": [20, 30, 40, 50], "ma": ["sma", "ema", "wma", "sma"],
                       "size": [0.5], "short": [True]})
        combos = combos[:100]
        task = make_task("ma_cross", GenSpec(n=5000, seed=3).to_dict(), broker=BrokerConfig(max_participation=1.0))
        t0 = time.perf_counter()
        res = run_search(task, combos, workers=4)
        t = time.perf_counter() - t0
        self.assertEqual(len(res), len(combos) - 20)                                    # 重复的 'sma' 被去重
        self.assertLess(t, 8.0, f"80 combos x 5k bars with 4 workers took {t:.1f}s (> 8s)")


if __name__ == "__main__":
    unittest.main()
