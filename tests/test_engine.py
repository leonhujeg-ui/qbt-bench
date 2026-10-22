"""阶段 3 判据：事件顺序（T 收盘下单、T+1 成交）、不偷看未来（篡改未来 K 线历史不变 + 严格守卫模式）、
多策略多标的、目标仓位、指标缓存、确定性。"""
import random
import unittest

from qbt.broker import BrokerConfig, MARKET, LIMIT
from qbt.data import generate, generate_universe, OHLCV
from qbt.engine import (Engine, Strategy, Context, IndicatorCache, GuardedColumn, LookaheadError, run_backtest)
from qbt.errors import ConfigError, DataError
from strategies import REGISTRY
from tests._helpers import QBTTestCase, ACCOUNTING_TOL, scale, random_params

CFG = BrokerConfig(max_participation=1.0)


class EveryKBars(Strategy):
    """每 k 根交替买卖 1 手的探针策略，记录下单 bar。"""
    name = "probe"
    DEFAULTS = {"k": 5}

    def on_start(self, ctx):
        super().on_start(ctx)
        self.submitted = []
        self.side = 1

    def on_bar(self, ctx, i):
        if i % self.params["k"] == 0:
            ctx.order(self.symbol, self.side, 1.0)
            self.submitted.append(i)
            self.side = -self.side


class Peeker(Strategy):
    name = "peeker"

    def on_bar(self, ctx, i):
        c = ctx.data[self.symbol].close
        if i + 1 < len(c):
            _ = c[i + 1]            # 偷看未来


def tamper(d, cut, seed=3):
    t = d.slice(0, len(d))
    r = random.Random(seed)
    for i in range(cut, len(d)):
        f = r.uniform(0.7, 1.3)
        t.open[i] *= f
        t.close[i] *= f
        t.high[i] = max(t.open[i], t.close[i]) * 1.02
        t.low[i] = min(t.open[i], t.close[i]) * 0.98
        t.volume[i] *= r.uniform(0.2, 2.0)
    t._validated = False
    return t.validate()


class TestEngineSemantics(QBTTestCase):
    def test_construction_errors(self):
        d = generate(n=20, seed=1)
        with self.assertRaises(ConfigError) as ctx:
            Engine({}, [EveryKBars()])
        self.assertIn("no data", str(ctx.exception))
        with self.assertRaises(ConfigError):
            Engine(d, [])
        with self.assertRaises(ConfigError):
            Engine({"A": d, "B": generate(n=10, seed=2)}, [EveryKBars()])
        bad = d.slice(0, 20)
        bad.close[3] = -1.0
        bad._validated = False
        with self.assertRaises(DataError):
            Engine(bad, [EveryKBars()])
        with self.assertRaises(ConfigError):
            EveryKBars(bogus=1)
        with self.assertRaises(ConfigError):
            Engine(d, [EveryKBars(symbol="NOPE")]).run()
        with self.assertRaises(NotImplementedError):
            Engine(d, [Strategy()]).run()

    def test_orders_fill_next_bar(self):
        d = generate(n=60, seed=2, halt_prob=0.0)
        s = EveryKBars(k=7)
        r = Engine(d, s, CFG).run()
        self.assertEqual([f.bar for f in r.fills], [i + 1 for i in s.submitted if i + 1 < 60])
        for f in r.fills:
            self.assertAlmostEqual(f.price, d.open[f.bar] * (1 + f.side * CFG.slippage), delta=1e-9)
        self.assertEqual(len(r.equity), 60)
        self.assertEqual(r.equity[0], CFG.cash)         # 第 0 根只下单未成交
        self.assertEqual(r.final_equity, r.equity[-1])
        self.assertEqual(r.n, 60)
        self.assertTrue(0.0 <= r.elapsed < 60.0)
        self.assertEqual(r.summary()["fills"], len(r.fills))
        self.assertEqual(r.params[0]["k"], 7)

    def test_equity_curve_matches_marking(self):
        d = generate(n=scale(2000, 400), seed=3)
        r = run_backtest(d, REGISTRY["ma_cross"](), CFG)
        pos = 0.0
        cash = CFG.cash
        fills = sorted(r.fills, key=lambda f: f.bar)
        k = 0
        for i in range(len(d)):
            while k < len(fills) and fills[k].bar == i:
                f = fills[k]
                cash -= f.side * f.qty * f.price + f.fee
                pos += f.side * f.qty
                k += 1
            self.assertAlmostEqual(r.equity[i], cash + pos * d.close[i], delta=ACCOUNTING_TOL)

    def test_future_tamper_leaves_history_unchanged(self):
        n, cut = scale(3000, 800), scale(2000, 500)
        d = generate(n=n, seed=4, halt_prob=0.003, gap_prob=0.02)
        t = tamper(d, cut)
        self.assertNotEqual(d.close[cut:], t.close[cut:])
        for name, cls in REGISTRY.items():
            a = run_backtest(d, cls(), CFG)
            b = run_backtest(t, cls(), CFG)
            fa = [f.to_dict() for f in a.fills if f.bar < cut]
            fb = [f.to_dict() for f in b.fills if f.bar < cut]
            self.assertEqual(fa, fb, name)
            self.assertEqual(a.equity[:cut], b.equity[:cut], name)     # 逐字节相同
            oa = [o.to_dict() for o in a.orders if o.created < cut - 1]
            ob = [o.to_dict() for o in b.orders if o.created < cut - 1]
            self.assertEqual([x["id"] for x in oa], [x["id"] for x in ob], name)

    def test_strict_mode_guards(self):
        d = generate(n=scale(1500, 400), seed=5, halt_prob=0.003)
        for name, cls in REGISTRY.items():
            loose = run_backtest(d, cls(), CFG)
            strict = run_backtest(d, cls(), CFG, strict=True)
            self.assertEqual(loose.equity, strict.equity, name)
            self.assertEqual([f.to_dict() for f in loose.fills], [f.to_dict() for f in strict.fills], name)
        with self.assertRaises(LookaheadError):
            run_backtest(d, Peeker(), CFG, strict=True)
        two = [REGISTRY["ma_cross"](fast=5, slow=20), REGISTRY["rsi_reversion"](n=7)]
        loose2 = Engine(d, two, CFG).run()
        strict2 = Engine(d, [REGISTRY["ma_cross"](fast=5, slow=20), REGISTRY["rsi_reversion"](n=7)], CFG, strict=True).run()
        self.assertEqual(loose2.equity, strict2.equity)                        # 多策略循环也逐根推进 ctx.index
        with self.assertRaises(LookaheadError):
            Engine(d, [REGISTRY["ma_cross"](), Peeker()], CFG, strict=True).run()
        run_backtest(d.slice(0, 50), Peeker(), CFG)      # 非严格模式不拦截（由篡改判据兜底）

    def test_guarded_column_semantics(self):
        class Clock:
            index = 4
        col = GuardedColumn([10, 11, 12, 13, 14, 15, 16], Clock())
        self.assertEqual(col[4], 14)
        self.assertEqual(col[0], 10)
        self.assertEqual(col[-7], 10)
        self.assertEqual(col[0:5], [10, 11, 12, 13, 14])
        self.assertEqual(list(col), [10, 11, 12, 13, 14])
        self.assertEqual(len(col), 7)
        with self.assertRaises(LookaheadError):
            col[5]
        with self.assertRaises(LookaheadError):
            col[-1]
        with self.assertRaises(LookaheadError):
            col[3:6]
        ctx = Context({"S": generate(n=5, seed=1, symbol="S")}, None, strict=True)
        self.assertEqual(len(ctx.data["S"]), 5)
        self.assertEqual(ctx.data["S"].symbol, "S")

    def test_multi_strategy_multi_symbol(self):
        uni = generate_universe(["A", "B"], scale(1500, 400), 6, halt_prob=0.0)
        cfg = BrokerConfig(max_participation=1.0, max_leverage=2.0)      # 两策略各 0.5 仓位，杠杆 2 保证不触发保证金拒单
        s1 = REGISTRY["ma_cross"](symbol="A", fast=5, slow=20)
        s2 = REGISTRY["rsi_reversion"](symbol="B", n=7)
        r = Engine(uni, [s1, s2], cfg).run()
        syms = {f.symbol for f in r.fills}
        self.assertEqual(syms, {"A", "B"})
        self.assertEqual([p["symbol"] for p in r.params], ["A", "B"])
        self.assertEqual(r.symbols, ["A", "B"])
        single_a = Engine(uni, [REGISTRY["ma_cross"](symbol="A", fast=5, slow=20)], cfg).run()
        self.assertTrue(all(o.status != "rejected" for o in r.orders))
        # 两个策略共享账户，仓位规模依赖权益，所以数量不同、成交可能被成交量上限拆成多笔；
        # 但 A 上的订单创建 bar 与方向序列必须一致
        oa = [(o.created, o.side) for o in r.orders if o.symbol == "A"]
        ob = [(o.created, o.side) for o in single_a.orders]
        self.assertEqual(oa, ob)
        self.assertGreater(len(oa), 20)

    def test_target_handles_partial_fills(self):
        rows = [(100.0, 101.0, 99.0, 100.0, 100.0)] * 30
        d = OHLCV("S", list(range(30)), *[list(c) for c in zip(*rows)], bar_seconds=1)

        class Target(Strategy):
            name = "target"
            DEFAULTS = {}

            def on_bar(self, ctx, i):
                if i < 25:
                    ctx.target("S", 25.0)        # 每根最多成交 10% * 100 = 10 手，需要多根
                else:
                    ctx.target("S", 0.0)
        r = run_backtest(d, Target(), BrokerConfig(max_participation=0.1))
        self.assertEqual([f.qty for f in r.fills][:3], [10.0, 10.0, 5.0])
        self.assertEqual(sum(f.side * f.qty for f in r.fills), 0.0)
        self.assertLessEqual(max(abs(sum(f.side * f.qty for f in r.fills[:k])) for k in range(len(r.fills) + 1)), 25.0)

    def test_indicator_cache(self):
        d = generate(n=300, seed=7)
        cache = IndicatorCache(max_elements=1000)
        ctx = Context({"S": d}, None, cache=cache)
        a = ctx.indicator("sma", "close", 10)
        b = ctx.indicator("sma", "close", 10)
        self.assertIs(a, b)
        self.assertEqual((cache.hits, cache.misses), (1, 1))
        ctx.indicator("sma", "close", 20)
        ctx.indicator("sma", "close", 30)
        self.assertEqual((len(cache._store), cache._elements), (3, 900))
        ctx.indicator("macd", "close", 5, 10, 3)               # 900 元素，超容量 => 淘汰最旧的三个 sma
        self.assertEqual((len(cache._store), cache._elements), (1, 900))
        c = ctx.indicator("sma", "close", 10)
        self.assertEqual(c, a)
        cache.clear()
        self.assertEqual((cache._elements, len(cache._store)), (0, 0))
        # 不同数据同参数不串台
        d2 = generate(n=300, seed=8)
        ctx2 = Context({"S": d2}, None, cache=cache)
        self.assertNotEqual(ctx2.indicator("sma", "close", 10), a)
        r1 = run_backtest(d, REGISTRY["ma_cross"](), CFG, cache=cache)
        r2 = run_backtest(d, REGISTRY["ma_cross"](), CFG, cache=cache)
        self.assertEqual(r1.equity, r2.equity)
        self.assertGreater(r2.cache_stats[0], r1.cache_stats[0])

    def test_determinism_and_run_twice(self):
        d = generate(n=scale(3000, 600), seed=9, halt_prob=0.002)
        for name, cls in REGISTRY.items():
            a = run_backtest(d, cls(), CFG)
            b = run_backtest(d, cls(), CFG)
            self.assertEqual(a.equity, b.equity, name)
            self.assertEqual([f.to_dict() for f in a.fills], [f.to_dict() for f in b.fills], name)
            self.assertEqual([t for t in a.trades], [t for t in b.trades], name)

    def test_liquidation_recorded_in_engine(self):
        rows = [(100, 101, 99, 100, 1e9)] * 2 + [(100, 101, 50, 50, 1e9)] + [(50, 51, 49, 50, 1e9)] * 3
        d = OHLCV("S", list(range(len(rows))), *[list(map(float, c)) for c in zip(*rows)], bar_seconds=1)

        class Lever(Strategy):
            name = "lever"
            DEFAULTS = {}

            def on_bar(self, ctx, i):
                if i == 0:
                    ctx.order("S", 1, 2500.0)
        r = run_backtest(d, Lever(), BrokerConfig(max_leverage=3.0, maintenance_margin=0.2, max_participation=1.0))
        self.assertEqual(r.liquidations, 1)
        self.assertTrue(r.bankrupt)
        self.assertEqual([e[0] for e in r.events], ["liquidation", "bankrupt"])
        self.assertEqual(len(r.equity), 6)
        self.assertLess(r.final_equity, 0.0)

    def test_strategy_helpers(self):
        d = generate(n=30, seed=1, symbol="S")
        ctx = Context({"S": d}, Engine(d, EveryKBars(), CFG).broker)
        s = EveryKBars()
        self.assertEqual(s.size_qty(ctx, 0.0, 0.5), 0.0)
        self.assertAlmostEqual(s.size_qty(ctx, 50.0, 0.5), CFG.cash * 0.5 / 50.0, delta=1e-9)   # 乘除顺序不同的舍入
        self.assertEqual(ctx.pending_signed("S"), 0.0)
        o = ctx.order("S", 1, 2.0, LIMIT, price=1.0)
        self.assertEqual(ctx.pending_signed("S"), 0.0)          # 限价单不计入
        m = ctx.order("S", -1, 3.0)
        self.assertEqual(ctx.pending_signed("S"), -3.0)
        self.assertTrue(ctx.cancel(m))
        self.assertEqual(ctx.cancel_all("S"), 1)
        self.assertIsNone(ctx.target("S", 0.0))
        self.assertEqual(ctx.position("S"), 0.0)
        self.assertEqual(ctx.cash, CFG.cash)
        self.assertEqual(ctx.equity, CFG.cash)
        self.assertIs(ctx.data, ctx.raw)                       # 非严格模式默认直接暴露裸数据
        self.assertIs(Engine(d, EveryKBars(), CFG).ctx.strict, False)
        self.assertEqual(Strategy.name, "base")
        self.assertGreater(s.size_qty(ctx, 0.5, 0.5), 0.0)      # 价格在 (0,1] 也能定仓
        ctx.broker.equity = 0.5
        self.assertAlmostEqual(s.size_qty(ctx, 50.0, 0.5), 0.005, delta=1e-15)   # 权益在 (0,1] 也能定仓；舍入
        uni = generate_universe(["P", "Q"], 20, 1)
        eng = Engine(uni, EveryKBars(), CFG)
        eng.run()
        self.assertEqual(eng.strategies[0].symbol, "P")         # 未指定标的 => 第一个
        from qbt import indicators as I
        ctx2 = Context(uni, None)
        self.assertEqual(ctx2.indicator("sma", "close", 3), I.sma(uni["P"].close, 3))      # 默认标的 = 第一个
        q = generate(n=20, seed=1, symbol="Q", bar_seconds=300)
        res = Engine({"P": uni["P"], "Q": q}, EveryKBars(), CFG).run()
        self.assertEqual((res.bar_seconds, res.ts), (uni["P"].bar_seconds, uni["P"].ts))   # 结果时间轴取第一个标的
        self.assertEqual(ctx.index, -1)
        ctx.broker.equity = 0.0
        self.assertEqual(s.size_qty(ctx, 50.0, 0.5), 0.0)      # 权益 <= 0 不开仓
        ctx.broker.equity = -0.5
        self.assertEqual(s.size_qty(ctx, 50.0, 0.5), 0.0)
        with self.assertRaises(ConfigError):
            ctx.indicator("not_an_indicator", "close", 5)
        with self.assertRaises(ConfigError):
            EveryKBars(k="5")                                     # 参数类型按默认值校验
        with self.assertRaises(ConfigError):
            EveryKBars(k=float("nan"))
        with self.assertRaises(ConfigError):
            EveryKBars(symbol=5)
        with self.assertRaises(ConfigError):
            REGISTRY["ma_cross"](short=1)
        with self.assertRaises(ConfigError):
            run_backtest(d, REGISTRY["ma_cross"](ma="nope"), CFG)
        self.assertEqual(IndicatorCache().max_elements, 8_000_000)
        from qbt.engine import GuardedData
        self.assertFalse(hasattr(GuardedData(d, ctx), "__dict__"))   # __slots__ 契约


if __name__ == "__main__":
    unittest.main()
