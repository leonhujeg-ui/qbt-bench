"""阶段 4 判据：<= 20 根的小样本按定义手算对拍（夏普/索提诺/回撤/年化/VaR/月度表/交易统计）+ 随机数据性质。

容差：手算值用 1e-12 绝对容差（同一算式不同结合顺序的舍入），随机性质用 1e-9 相对容差。
"""
import math
import random
import unittest
from datetime import datetime, timezone

from qbt import metrics as M
from qbt.data import SECONDS_PER_YEAR, generate
from qbt.engine import run_backtest
from qbt.broker import BrokerConfig
from strategies import REGISTRY
from tests._helpers import QBTTestCase, scale

DAY = 86400


def ts_days(*dates):
    return [int(datetime(*d, tzinfo=timezone.utc).timestamp()) for d in dates]


class TestHandComputed(QBTTestCase):
    """equity = [100, 120, 90, 135]，bar = 1 年（ppy = 1）：
    returns = [0.2, -0.25, 0.5]；mean = 0.15；偏差 [0.05, -0.4, 0.35]；平方和 0.285；样本方差 0.1425。"""

    EQ = [100.0, 120.0, 90.0, 135.0]

    def test_returns_and_total(self):
        self.assertCloseList(M.returns_from_equity(self.EQ), [0.2, -0.25, 0.5], abs_tol=1e-12)
        self.assertAlmostEqual(M.total_return(self.EQ), 0.35, delta=1e-12)
        self.assertEqual(M.returns_from_equity([100.0]), [])
        self.assertEqual(M.returns_from_equity([100.0, 0.0, 50.0]), [-1.0, 0.0])    # 权益为 0 后的收益定义为 0
        self.assertEqual(M.total_return([]), 0.0)
        self.assertEqual(M.total_return([0.0, 5.0]), 0.0)

    def test_sharpe_sortino_hand(self):
        ppy = 1.0
        sd = math.sqrt(0.285 / 2)                    # 0.37749...
        self.assertAlmostEqual(M.sharpe([0.2, -0.25, 0.5], ppy), 0.15 / sd, delta=1e-12)
        dd = math.sqrt(0.25 ** 2 / 3)                # 只有 -0.25 为负
        self.assertAlmostEqual(M.sortino([0.2, -0.25, 0.5], ppy), 0.15 / dd, delta=1e-12)
        # 年化：ppy = 252 => 乘 sqrt(252)
        self.assertAlmostEqual(M.sharpe([0.2, -0.25, 0.5], 252.0), 0.15 / sd * math.sqrt(252.0), delta=1e-12)
        # 无风险利率：每期扣 rf/ppy
        self.assertAlmostEqual(M.sharpe([0.2, -0.25, 0.5], 1.0, rf=0.05), 0.10 / sd, delta=1e-12)
        self.assertEqual(M.sharpe([0.1, 0.1, 0.1], 1.0), 0.0)         # 零波动（均值舍入残差 ~1e-17 视为 0）
        self.assertEqual(M.sharpe([0.0, 0.0], 1.0), 0.0)
        self.assertEqual(M.sortino([0.1, 0.2], 1.0), 0.0)              # 无下行
        self.assertEqual(M.sharpe([0.1], 1.0), 0.0)
        self.assertEqual(M.sortino([], 1.0), 0.0)

    def test_drawdown_hand(self):
        dd = M.max_drawdown(self.EQ)
        self.assertAlmostEqual(dd["mdd"], 0.25, delta=1e-12)          # 120 -> 90
        self.assertEqual((dd["peak_idx"], dd["trough_idx"], dd["duration_bars"], dd["recovered"]), (1, 2, 2, True))
        nr = M.max_drawdown([100.0, 80.0, 90.0])
        self.assertAlmostEqual(nr["mdd"], 0.2, delta=1e-12)                 # 1 - 80/100 的舍入
        self.assertEqual((nr["duration_bars"], nr["recovered"]), (2, False))
        self.assertEqual(M.max_drawdown([1.0, 2.0, 3.0])["mdd"], 0.0)
        self.assertEqual(M.max_drawdown([1.0, 2.0, 3.0])["duration_bars"], 0)
        self.assertEqual(M.max_drawdown([])["mdd"], 0.0)
        self.assertEqual(M.longest_drawdown_bars([100.0, 90.0, 95.0, 100.0, 80.0, 85.0]), 3)   # 峰 100@0 -> 恢复@3；之后 80,85 水下 2
        self.assertEqual(M.longest_drawdown_bars([1.0, 2.0, 3.0]), 0)
        neg = M.max_drawdown([100.0, -50.0, 10.0])
        self.assertEqual(neg["mdd"], 1.5)

    def test_cagr_calmar_hand(self):
        c = M.cagr(self.EQ, SECONDS_PER_YEAR)        # 3 年，1.35^(1/3) - 1
        self.assertAlmostEqual(c, 1.35 ** (1 / 3) - 1, delta=1e-12)
        self.assertAlmostEqual(M.calmar(c, 0.25), c / 0.25, delta=1e-12)
        self.assertIsNone(M.calmar(c, 0.0))
        self.assertEqual(M.cagr([100.0], DAY), 0.0)
        self.assertEqual(M.cagr([100.0, 0.0], DAY), -1.0)
        self.assertEqual(M.cagr([0.0, 10.0], DAY), 0.0)
        self.assertAlmostEqual(M.cagr([100.0, 110.0], SECONDS_PER_YEAR / 2), 1.1 ** 2 - 1, delta=1e-12)   # 半年 10% => 年化 21%

    def test_var_cvar_hand(self):
        r = [0.01, -0.02, 0.03, -0.05, 0.0, 0.02, -0.01, 0.04, -0.03, 0.05]     # n=10, alpha=0.1 => k=0
        self.assertEqual(M.var_cvar(r, 0.1), (0.05, 0.05))
        var, cvar = M.var_cvar(r, 0.25)                                          # k = ceil(2.5)-1 = 2 => sorted[2] = -0.02
        self.assertAlmostEqual(var, 0.02, delta=1e-12)
        self.assertAlmostEqual(cvar, (0.05 + 0.03 + 0.02) / 3, delta=1e-12)
        self.assertEqual(M.var_cvar([], 0.05), (0.0, 0.0))
        self.assertEqual(M.var_cvar([0.1], 0.05), (-0.1, -0.1))                   # 单样本：VaR 为负表示“最坏也是盈利”
        self.assertEqual(M.var_cvar([-0.1, 0.2], 1.0), (-0.2, -0.05))             # alpha=1 => 取全部

    def test_period_returns_hand(self):
        eq = [100.0, 120.0, 90.0, 135.0, 140.0]
        ts = ts_days((2020, 1, 30), (2020, 1, 31), (2020, 2, 1), (2020, 2, 2), (2020, 2, 2))
        monthly = M.monthly_returns(eq, ts)
        self.assertEqual([(m["year"], m["month"]) for m in monthly], [(2020, 1), (2020, 2)])
        self.assertAlmostEqual(monthly[0]["ret"], 0.2, delta=1e-12)              # 1 月末 120 / 起点 100
        self.assertAlmostEqual(monthly[1]["ret"], 140 / 120 - 1, delta=1e-12)    # 2 月末 140 / 1 月末 120（同日两根取最后）
        daily = M.daily_returns(eq, ts)
        self.assertCloseList(daily, [0.0, 0.2, -0.25, 140 / 90 - 1], abs_tol=1e-12)   # 2 月 2 日两根取最后 140，上一日末 90
        self.assertEqual(M.monthly_returns([], []), [])
        prod = 1.0
        for m in monthly:
            prod *= 1 + m["ret"]
        self.assertAlmostEqual(prod - 1, M.total_return(eq), delta=1e-12)        # 月度复合 == 总收益
        self.assertEqual(M.period_returns([0.0, 5.0], [0, DAY], M._day_key)[1][1], 0.0)

    def test_trade_stats_hand(self):
        T = lambda pnl, bars=3: {"pnl": pnl, "bars": bars}
        s = M.trade_stats([T(10.0), T(-5.0), T(20.0, 5), T(-10.0), T(0.0)])
        self.assertEqual(s["n_trades"], 5)
        self.assertAlmostEqual(s["win_rate"], 0.4)
        self.assertAlmostEqual(s["profit_factor"], 2.0)
        self.assertAlmostEqual(s["avg_win"], 15.0)
        self.assertAlmostEqual(s["avg_loss"], -7.5)
        self.assertAlmostEqual(s["expectancy"], 3.0)
        self.assertAlmostEqual(s["avg_bars"], 3.4)
        self.assertEqual((s["gross_profit"], s["gross_loss"]), (30.0, 15.0))
        e = M.trade_stats([])
        self.assertEqual((e["n_trades"], e["win_rate"], e["profit_factor"], e["expectancy"]), (0, 0.0, None, 0.0))
        self.assertIsNone(M.trade_stats([T(1.0)])["profit_factor"])

    def test_compute_assembles(self):
        ts = ts_days((2020, 1, 30), (2020, 1, 31), (2020, 2, 1), (2020, 2, 2))
        m = M.compute(self.EQ, ts, DAY, [{"pnl": 5.0, "bars": 1}])
        self.assertAlmostEqual(m["total_return"], 0.35, delta=1e-12)
        self.assertAlmostEqual(m["max_drawdown"], 0.25, delta=1e-12)
        self.assertEqual(m["max_drawdown_bars"], 2)
        self.assertAlmostEqual(m["max_drawdown_days"], 2.0, delta=1e-12)
        self.assertTrue(m["drawdown_recovered"])
        self.assertEqual(m["n_trades"], 1)
        self.assertEqual(len(m["monthly"]), 2)
        self.assertAlmostEqual(m["sharpe"], 0.15 / math.sqrt(0.285 / 2) * math.sqrt(365.25), delta=1e-9)
        self.assertAlmostEqual(m["volatility_annual"], math.sqrt(0.285 / 2) * math.sqrt(365.25), delta=1e-9)
        self.assertEqual(m["var_bar"], 0.25)
        self.assertEqual(m["bars"], 4)
        self.assertEqual((m["start_equity"], m["end_equity"]), (100.0, 135.0))
        self.assertIsNotNone(m["calmar"])
        self.assertEqual(m["alpha"], 0.05)
        empty = M.compute([], [], DAY)
        self.assertEqual((empty["bars"], empty["total_return"], empty["max_drawdown"]), (0, 0.0, 0.0))


class TestSurvivorKillers(QBTTestCase):
    """针对突变测试漏网项补的判据（边界与常数）。"""

    def test_returns_with_sub_unit_equity(self):
        self.assertCloseList(M.returns_from_equity([0.5, 1.0]), [1.0], abs_tol=1e-12)     # 0 < 权益 < 1 也要算收益

    def test_std_edge_and_two_returns(self):
        self.assertEqual(M._std([1.0], 1), 0.0)
        self.assertAlmostEqual(M.sharpe([0.1, 0.3], 1.0), 0.2 / math.sqrt(0.02), delta=1e-12)   # 恰好 2 个收益也要算
        self.assertAlmostEqual(M.sortino([0.1, -0.3], 1.0), -0.1 / math.sqrt(0.09 / 2), delta=1e-12)

    def test_sortino_with_rf_and_ppy(self):
        """ppy=4, rf=0.05 => 每期扣 0.0125：ex=[0.1875,-0.2625,0.4875]，下行=sqrt(0.2625^2/3)，均值 0.1375。"""
        ex = [0.2 - 0.0125, -0.25 - 0.0125, 0.5 - 0.0125]
        dd = math.sqrt(ex[1] ** 2 / 3)
        self.assertAlmostEqual(M.sortino([0.2, -0.25, 0.5], 4.0, rf=0.05), sum(ex) / 3 / dd * 2.0, delta=1e-12)
        sd = math.sqrt(sum((x - sum(ex) / 3) ** 2 for x in ex) / 2)
        self.assertAlmostEqual(M.sharpe([0.2, -0.25, 0.5], 4.0, rf=0.05), sum(ex) / 3 / sd * 2.0, delta=1e-12)

    def test_drawdown_recovery_index_and_flat(self):
        dd = M.max_drawdown([100.0, 80.0, 100.0, 100.0, 100.0])
        self.assertEqual((dd["duration_bars"], dd["recovered"]), (2, True))          # 首次恢复即止
        self.assertEqual(M.longest_drawdown_bars([100.0, 100.0, 100.0]), 0)           # 持平不算水下
        self.assertEqual(M.longest_drawdown_bars([100.0, 90.0, 100.0, 100.0, 95.0]), 2)
        self.assertEqual(M.max_drawdown([100.0, 80.0, 90.0])["recovered"], False)

    def test_trade_stats_zero_loss_and_zero_profit(self):
        s = M.trade_stats([{"pnl": -1.0, "bars": 1}])
        self.assertEqual((s["profit_factor"], s["gross_profit"], s["win_rate"]), (0.0, 0.0, 0.0))


class TestProperties(QBTTestCase):
    def test_against_bruteforce_on_random_equity(self):
        r = random.Random(1)
        for _ in range(scale(20, 5)):
            eq = [100.0]
            for _ in range(r.randint(2, 40)):
                eq.append(max(1e-6, eq[-1] * (1 + r.uniform(-0.1, 0.1))))
            dd = M.max_drawdown(eq)
            brute = max(1 - eq[j] / eq[i] for i in range(len(eq)) for j in range(i, len(eq)))
            self.assertAlmostEqual(dd["mdd"], brute, delta=1e-12)
            self.assertTrue(0 <= dd["mdd"] <= 1)
            rets = M.returns_from_equity(eq)
            var, cvar = M.var_cvar(rets)
            self.assertGreaterEqual(cvar, var - 1e-12)
            s = M.sharpe(rets, 252.0)
            self.assertEqual(s > 0, sum(rets) > 0)
            self.assertGreaterEqual(M.longest_drawdown_bars(eq), dd["trough_idx"] - dd["peak_idx"])

    def test_backtest_metrics_consistency(self):
        d = generate(n=scale(3000, 800), seed=21, bar_seconds=3600)
        res = run_backtest(d, REGISTRY["donchian_breakout"](), BrokerConfig(max_participation=1.0))
        m = M.compute(res.equity, res.ts, res.bar_seconds, res.trades)
        self.assertAlmostEqual(m["end_equity"], res.final_equity)
        self.assertAlmostEqual(1 + m["total_return"], res.final_equity / res.initial_cash, delta=1e-12)
        prod = 1.0
        for mm in m["monthly"]:
            prod *= 1 + mm["ret"]
        self.assertAlmostEqual(prod - 1, m["total_return"], delta=1e-9)
        self.assertEqual(m["n_trades"], len(res.trades))
        self.assertAlmostEqual(m["max_drawdown_days"], m["max_drawdown_bars"] / 24.0, delta=1e-12)
        import json
        json.dumps(m)                                           # 全部可序列化（无 inf/nan）


if __name__ == "__main__":
    unittest.main()
