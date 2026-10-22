"""阶段 3 判据：6 个策略的参数空间、信号与成交的一致性、随机参数健壮性。"""
import json
import random
import unittest

from qbt import indicators as I
from qbt.broker import BrokerConfig
from qbt.data import generate
from qbt.engine import run_backtest
from strategies import REGISTRY
from tests._helpers import QBTTestCase, ACCOUNTING_TOL, scale, random_params

CFG = BrokerConfig(max_participation=1.0)


def entries(result):
    """返回 (信号 bar, 成交) 列表：持仓从 0 变为非 0 的成交；信号 bar = 对应订单的创建 bar
    （停牌时市价单会延后成交，所以不能用 fill.bar - 1）。"""
    orders = {o.id: o for o in result.orders}
    pos = 0.0
    out = []
    for f in result.fills:
        new = pos + f.side * f.qty
        if pos == 0.0 and new != 0.0:
            out.append((orders[f.order_id].created, f))
        pos = new
    return out


class TestParamSpaces(QBTTestCase):
    def test_six_strategies_with_four_plus_dims(self):
        self.assertEqual(len(REGISTRY), 6)
        for name, cls in REGISTRY.items():
            self.assertEqual(cls.name, name)
            self.assertGreaterEqual(len(cls.PARAM_SPACE), 4, name)
            for k, vals in cls.PARAM_SPACE.items():
                self.assertGreaterEqual(len(vals), 2, f"{name}.{k}")
                self.assertIn(k, cls.DEFAULTS, f"{name}.{k}")
                self.assertEqual(len(set(map(repr, vals))), len(vals), f"{name}.{k} duplicates")
            json.dumps(cls.PARAM_SPACE)
            json.dumps(cls.DEFAULTS)

    def test_defaults_trade_on_synthetic_data(self):
        d = generate(n=scale(5000, 1500), seed=11, halt_prob=0.003, gap_prob=0.02)
        for name, cls in REGISTRY.items():
            r = run_backtest(d, cls(), CFG)
            self.assertGreater(len(r.fills), 0, name)
            self.assertAlmostEqual(r.final_equity, r.equity[-1], delta=ACCOUNTING_TOL)

    def test_random_params_robust(self):
        d = generate(n=scale(2000, 600), seed=12, halt_prob=0.01, gap_prob=0.05, jump_prob=0.01)
        for name, cls in REGISTRY.items():
            for seed in range(scale(6, 3)):
                params = random_params(cls.PARAM_SPACE, seed)
                r = run_backtest(d, cls(**params), CFG)
                b_pos = sum(f.side * f.qty for f in r.fills)
                self.assertAlmostEqual(r.final_equity, r.equity[-1], delta=ACCOUNTING_TOL, msg=f"{name} {params}")
                self.assertTrue(all(o.status != "rejected" or o.reason in ("margin", "short not allowed", "bankrupt", "reduce_only")
                                    for o in r.orders), name)
                self.assertTrue(abs(b_pos) < 1e6)


class TestSignalConsistency(QBTTestCase):
    def setUp(self):
        self.d = generate(n=scale(4000, 1200), seed=13, halt_prob=0.002, gap_prob=0.02)

    def test_ma_cross(self):
        d = self.d
        r = run_backtest(d, REGISTRY["ma_cross"](fast=5, slow=20, ma="ema"), CFG)
        fast, slow = I.ema(d.close, 5), I.ema(d.close, 20)
        orders = {o.id: o for o in r.orders}
        for f in r.fills:
            c = orders[f.order_id].created
            self.assertEqual(f.side > 0, fast[c] > slow[c], f"bar {f.bar} created {c}")
        self.assertGreater(len(r.fills), 10)
        none = run_backtest(d, REGISTRY["ma_cross"](fast=20, slow=20), CFG)
        self.assertEqual(none.fills, [])
        long_only = run_backtest(d, REGISTRY["ma_cross"](fast=5, slow=20, short=False), CFG)
        pos = 0.0
        for f in long_only.fills:
            pos += f.side * f.qty
            self.assertGreaterEqual(pos, -1e-9)

    def test_bollinger_entries_beyond_band(self):
        d = self.d
        n, k = 20, 2.0
        r = run_backtest(d, REGISTRY["bollinger_reversion"](n=n, k=k, exit_z=0.5, max_hold=50), CFG)
        z = I.zscore(d.close, n)
        self.assertGreater(len(entries(r)), 0)
        for i, f in entries(r):
            self.assertGreater(abs(z[i]), k)
            self.assertEqual(f.side, -1 if z[i] > 0 else 1)
        for t in r.trades:
            self.assertLessEqual(t["bars"], 50 + 1)

    def test_donchian_entries_break_channel(self):
        d = self.d
        r = run_backtest(d, REGISTRY["donchian_breakout"](entry_n=20, exit_n=10), CFG)
        up, lo, _ = I.donchian(d.high, d.low, 20)
        self.assertGreater(len(entries(r)), 0)
        for i, f in entries(r):
            if f.side > 0:
                self.assertGreater(d.close[i], up[i - 1])
            else:
                self.assertLess(d.close[i], lo[i - 1])
        atr = I.atr(d.high, d.low, d.close, 14)
        for i, f in entries(r):
            self.assertLessEqual(f.qty * d.close[i], 1.0 * r.initial_cash * 3)   # 不超过 max_size 的数量级

    def test_rsi_entries(self):
        d = self.d
        r = run_backtest(d, REGISTRY["rsi_reversion"](n=14, oversold=30.0, overbought=70.0), CFG)
        rsi = I.rsi(d.close, 14)
        self.assertGreater(len(entries(r)), 0)
        for i, f in entries(r):
            self.assertTrue((f.side > 0 and rsi[i] < 30.0) or (f.side < 0 and rsi[i] > 70.0), f"bar {i} rsi={rsi[i]}")

    def test_grid_uses_limit_orders_at_levels(self):
        d = self.d
        s = REGISTRY["grid"](levels=3, spacing=0.01, recenter_n=50, max_inventory=5)
        r = run_backtest(d, s, CFG)
        self.assertGreater(len(r.fills), 0)
        self.assertTrue(all(f.liquidity == "maker" for f in r.fills))
        orders = {o.id: o for o in r.orders}
        for f in r.fills:
            o = orders[f.order_id]
            self.assertEqual(o.type, "limit")
            if f.side > 0:
                self.assertLessEqual(f.price, o.price + 1e-9)        # 买单成交价不劣于限价
            else:
                self.assertGreaterEqual(f.price, o.price - 1e-9)
        pos = 0.0
        for f in r.fills:
            pos += f.side * f.qty
        self.assertLessEqual(abs(pos) / max(s.level_qty, 1e-9), 5 + 3 + 1e-6)

    def test_trend_stop_has_protective_orders(self):
        d = self.d
        r = run_backtest(d, REGISTRY["trend_stop"](ma_n=20, atr_n=14, stop_mult=2.0, tp_mult=4.0), CFG)
        self.assertGreater(len(entries(r)), 0)
        by_bar = {}
        for o in r.orders:
            by_bar.setdefault(o.created, []).append(o)
        for i, f in entries(r):
            kinds = sorted(o.type for o in by_bar[i])
            self.assertEqual(kinds, ["market", "stop", "take_profit"], f"bar {i}")
            stop = next(o for o in by_bar[i] if o.type == "stop")
            tp = next(o for o in by_bar[i] if o.type == "take_profit")
            self.assertLess(stop.price, d.close[i])
            self.assertGreater(tp.price, d.close[i])
            self.assertTrue(stop.reduce_only and tp.reduce_only)
        pos = 0.0
        for f in r.fills:
            pos += f.side * f.qty
            self.assertGreaterEqual(pos, -1e-9)                          # 只做多
        # 与网格策略同账户同标的共跑：网格常年挂限价单，趋势策略仍必须能入场（不能看账户级 pending）
        from qbt.engine import Engine
        both = Engine(d, [REGISTRY["grid"](), REGISTRY["trend_stop"](ma_n=20, atr_n=14)],
                      BrokerConfig(max_participation=1.0, max_leverage=2.0)).run()
        self.assertGreater(sum(1 for o in both.orders if o.type == "stop"), 0)
        # 默认参与上限 10% 下入场常被拆成多笔：reduce_only 止损/止盈绝不能把仓位打成空头
        partial = run_backtest(d, REGISTRY["trend_stop"](ma_n=20), BrokerConfig())
        pos = 0.0
        for f in partial.fills:
            pos += f.side * f.qty
            self.assertGreaterEqual(pos, -1e-9)
        self.assertGreater(len(partial.fills), 0)
        # 极端波动（止损价 <= 0）不抛异常、不入场
        wild = generate(n=300, seed=99, sigma=80.0, halt_prob=0.0)
        run_backtest(wild, REGISTRY["trend_stop"](ma_n=20, atr_n=5, stop_mult=3.0), CFG)
        trailing = run_backtest(d, REGISTRY["trend_stop"](ma_n=20, trailing=True), CFG)
        fixed = run_backtest(d, REGISTRY["trend_stop"](ma_n=20, trailing=False), CFG)
        self.assertGreater(sum(1 for o in trailing.orders if o.type == "stop"),
                           sum(1 for o in fixed.orders if o.type == "stop"))


if __name__ == "__main__":
    unittest.main()
