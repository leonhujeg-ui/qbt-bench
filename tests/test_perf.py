"""性能判据（带阈值，慢了就红）。

- 覆盖率监控（QBT_COVERAGE=1）下计时无意义，跳过并说明；
- 突变/模糊的快速模式（QBT_FAST_TESTS=1）并行跑多个测试进程，计时不可靠，跳过（突变体若把复杂度搞坏会被 timeout 抓住）。
阈值来自 PLAN.md 的目标按比例折算并留 2 倍余量：1M 根 <= 3s => 200k 根 <= 0.6s，阈值 1.2s。
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
        cache = IndicatorCache()
        t = best_of(lambda: run_backtest(d, REGISTRY["ma_cross"](), BrokerConfig(max_participation=1.0), cache=cache), 2)
        self.assertLess(t, 1.2, f"200k-bar ma_cross backtest took {t:.2f}s (> 1.2s)")

    def test_indicators_scale_linearly(self):
        small, big = generate(n=50_000, seed=2), generate(n=200_000, seed=2)
        for name, args in (("sma", lambda d: (d.close, 20)), ("rolling_max", lambda d: (d.high, 50)),
                           ("atr", lambda d: (d.high, d.low, d.close, 14)), ("rsi", lambda d: (d.close, 14)),
                           ("linreg_slope", lambda d: (d.close, 30))):
            ts = best_of(lambda: I.REGISTRY[name](*args(small)))
            tb = best_of(lambda: I.REGISTRY[name](*args(big)))
            self.assertLess(tb / max(ts, 1e-4), 8.0, f"{name}: 4x data took {tb / ts:.1f}x time (expect ~4x)")
            self.assertLess(tb, 1.0, f"{name} on 200k bars took {tb:.2f}s")

    def test_grid_100_combos_parallel_under_threshold(self):
        combos = grid({"fast": [3, 5, 8, 10, 12], "slow": [20, 30, 40, 50], "ma": ["sma", "ema", "wma", "sma"],
                       "size": [0.5], "short": [True]})
        combos = combos[:100]
        task = make_task("ma_cross", GenSpec(n=5000, seed=3).to_dict(), broker=BrokerConfig(max_participation=1.0))
        t0 = time.perf_counter()
        res = run_search(task, combos, workers=4)
        t = time.perf_counter() - t0
        self.assertEqual(len(res), len(combos) - 20)                                    # 重复的 'sma' 被去重
        self.assertLess(t, 15.0, f"80 combos x 5k bars with 4 workers took {t:.1f}s")


if __name__ == "__main__":
    unittest.main()
