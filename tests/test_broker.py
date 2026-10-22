"""阶段 2 判据：撮合规则、手续费/滑点、成交量上限、保证金/爆仓、资金费率，以及逐 bar 会计恒等式。"""
import math
import random
import unittest

from qbt.broker import (Broker, BrokerConfig, Order, Fill, round_trips, MARKET, LIMIT, STOP, TAKE_PROFIT,
                        ICEBERG, PENDING, PARTIAL, FILLED, CANCELLED, REJECTED)
from qbt.data import OHLCV, generate, generate_universe
from qbt.errors import ConfigError, OrderError
from tests._helpers import QBTTestCase, ACCOUNTING_TOL, scale


def bars(rows, symbol="S"):
    """rows: [(open, high, low, close, volume), ...]"""
    cols = list(zip(*rows))
    return OHLCV(symbol, list(range(len(rows))), *[list(c) for c in cols], bar_seconds=1).validate()


def broker(rows, **cfg):
    d = bars(rows)
    return Broker({d.symbol: d}, BrokerConfig(**cfg)), d


FLAT = [(100.0, 101.0, 99.0, 100.0, 1000.0)] * 10


class TestConfigAndValidation(QBTTestCase):
    def test_config_check(self):
        BrokerConfig().check()
        for kw in ({"cash": 0.0}, {"cash": math.nan}, {"maker_fee": -0.1}, {"taker_fee": 1.0}, {"slippage": math.inf},
                   {"impact": -1.0}, {"max_participation": 0.0}, {"max_participation": 1.5}, {"max_leverage": 0.0},
                   {"maintenance_margin": 1.0}, {"funding_rate": 1.0}, {"funding_interval": -1},
                   {"funding_interval": 2.5}, {"liquidation_fee": 1.0}):
            with self.assertRaises(ConfigError, msg=str(kw)):
                BrokerConfig(**kw).check()
        self.assertEqual(BrokerConfig(cash=5.0).to_dict()["cash"], 5.0)
        with self.assertRaises(ConfigError):
            BrokerConfig(maintenance_margin=-0.5).check()
        import dataclasses
        with self.assertRaises(dataclasses.FrozenInstanceError):
            BrokerConfig().cash = 1.0                                      # 配置不可变（进程间传递/缓存键的前提）
        for kw in ({"cash": True}, {"maker_fee": False}, {"max_leverage": True}, {"allow_short": 1}):
            with self.assertRaises(ConfigError, msg=str(kw)):
                BrokerConfig(**kw).check()                                 # bool 不是实数
        BrokerConfig(maintenance_margin=0.0).check()               # 0 合法（关闭爆仓）
        BrokerConfig(cash=0.5).check()                             # 现金在 (0,1] 合法（cash<=1 会误拒）
        self.assertEqual(BrokerConfig().to_dict(), {
            "cash": 100_000.0, "maker_fee": 0.0002, "taker_fee": 0.0005, "slippage": 0.0001, "impact": 0.0,
            "max_participation": 0.1, "max_leverage": 1.0, "maintenance_margin": 0.05, "liquidation_fee": 0.01,
            "funding_rate": 0.0, "funding_interval": 0, "allow_short": True})   # 默认值钉住

    def test_order_validation(self):
        b, d = broker(FLAT)
        for kw in (dict(side=0, qty=1), dict(side=1, qty=0), dict(side=1, qty=-1), dict(side=1, qty=math.nan),
                   dict(side=1, qty=math.inf), dict(side=1, qty=True), dict(side=1, qty="1"),
                   dict(side=1, qty=1, otype="bogus"), dict(side=1, qty=1, otype=LIMIT),
                   dict(side=1, qty=1, otype=LIMIT, price=0.0), dict(side=1, qty=1, otype=STOP, price=math.nan),
                   dict(side=1, qty=1, otype=ICEBERG, price=100.0), dict(side=1, qty=1, otype=ICEBERG, price=100.0, display=0),
                   dict(side=1, qty=1, otype=TAKE_PROFIT, price=-5)):
            with self.assertRaises(OrderError, msg=str(kw)):
                b.submit("S", **kw)
        with self.assertRaises(OrderError):
            b.submit("NOPE", 1, 1)
        with self.assertRaises(OrderError) as ctx:
            b.submit(5, 1, 1)
        self.assertIn("symbol must be str", str(ctx.exception))
        self.assertEqual(b.orders, [])
        o = b.submit("S", 1, 1)
        self.assertEqual((o.id, o.status, o.remaining), (1, PENDING, 1.0))
        self.assertEqual(o.to_dict()["symbol"], "S")
        self.assertEqual([x.id for x in (b.submit("S", 1, 1), b.submit("S", -1, 1))], [2, 3])   # 连续编号
        self.assertEqual(b.bar, 0)
        self.assertEqual(b.assets() - b.liabilities(), b.cash)
        b.cancel_all()
        b.cash = -0.5                                                       # -1 < 现金 < 0 的边界：负现金全部计入负债
        b.on_bar(0)
        self.assertEqual((b.assets(), b.liabilities()), (0.0, 0.5))
        self.assertEqual(b.assets() - b.liabilities(), b.equity)


class TestMatching(QBTTestCase):
    def test_market_fill_at_next_open_with_slippage_and_taker_fee(self):
        rows = [(100, 101, 99, 100, 1000), (102, 104, 101, 103, 1000)]
        b, d = broker(rows, slippage=0.001, taker_fee=0.001, max_participation=1.0)
        b.on_bar(0)
        o = b.submit("S", 1, 10, MARKET)
        self.assertEqual(b.on_bar(0 + 1), b.equity)
        f = b.fills[0]
        self.assertAlmostEqual(f.price, 102 * 1.001, delta=1e-12)
        self.assertEqual(f.liquidity, "taker")
        self.assertAlmostEqual(f.fee, 10 * f.price * 0.001, delta=1e-12)
        self.assertEqual(o.status, FILLED)
        self.assertAlmostEqual(b.cash, 100_000 - 10 * f.price - f.fee, delta=1e-9)
        self.assertAlmostEqual(b.equity, b.cash + 10 * 103, delta=1e-9)
        s = b.submit("S", -1, 10, MARKET)
        self.assertEqual(b.on_bar(1), b.equity)       # 同根 bar 再推进：挂单按该 bar 撮合（引擎不会这么做，仅验证规则）
        self.assertAlmostEqual(b.fills[1].price, 102 * (1 - 0.001), delta=1e-12)
        self.assertEqual(b.position("S"), 0.0)

    def test_limit_rules(self):
        rows = [(100, 101, 99, 100, 1000), (100, 101, 99.5, 100, 1000), (100, 101, 98, 100, 1000), (97, 101, 96, 100, 1000)]
        b, d = broker(rows, maker_fee=0.0003, max_participation=1.0)
        b.on_bar(0)
        o = b.submit("S", 1, 5, LIMIT, price=99.0)
        b.on_bar(1)
        self.assertEqual(o.status, PENDING)             # low 99.5 > 99：未触及
        b.on_bar(2)
        self.assertEqual(o.status, FILLED)
        self.assertEqual(b.fills[0].price, 99.0)        # 开盘 100 > 限价 => 按限价成交
        self.assertEqual(b.fills[0].liquidity, "maker")
        self.assertAlmostEqual(b.fills[0].fee, 5 * 99.0 * 0.0003, delta=1e-12)
        o2 = b.submit("S", 1, 5, LIMIT, price=99.0)
        b.on_bar(3)
        self.assertEqual(b.fills[1].price, 97.0)        # 跳空低开：按更优的开盘价成交
        s = b.submit("S", -1, 10, LIMIT, price=100.5)
        b.on_bar(3)
        self.assertEqual(b.fills[2].price, 100.5)       # high 101 >= 100.5，开盘 97 < 限价 => 按限价
        self.assertEqual(b.position("S"), 0.0)

    def test_stop_rules(self):
        rows = [(100, 101, 99, 100, 1000), (100, 100.5, 99, 100, 1000), (100, 103, 99, 102, 1000), (105, 106, 104, 105, 1000),
                (100, 101, 95, 96, 1000)]
        b, d = broker(rows, slippage=0.001, max_participation=1.0)
        b.on_bar(0)
        o = b.submit("S", 1, 5, STOP, price=101.0)
        b.on_bar(1)
        self.assertEqual(o.status, PENDING)
        b.on_bar(2)
        self.assertAlmostEqual(b.fills[0].price, 101.0 * 1.001, delta=1e-12)     # 触发价 101 > 开盘 100
        self.assertEqual(b.fills[0].liquidity, "taker")
        o2 = b.submit("S", 1, 5, STOP, price=101.0)
        b.on_bar(3)
        self.assertAlmostEqual(b.fills[1].price, 105.0 * 1.001, delta=1e-12)     # 跳空高开：按开盘价（更差）
        s = b.submit("S", -1, 10, STOP, price=97.0)
        b.on_bar(4)
        self.assertAlmostEqual(b.fills[2].price, 97.0 * (1 - 0.001), delta=1e-12)
        self.assertEqual(b.position("S"), 0.0)

    def test_take_profit_is_maker_limit(self):
        rows = [(100, 101, 99, 100, 1000), (100, 101, 99, 100, 1000), (100, 106, 99, 105, 1000)]
        b, d = broker(rows, maker_fee=0.0001, taker_fee=0.001, max_participation=1.0)
        b.on_bar(0)
        b.submit("S", 1, 3, MARKET)
        b.on_bar(1)
        tp = b.submit("S", -1, 3, TAKE_PROFIT, price=104.0, reduce_only=True)
        b.on_bar(2)
        self.assertEqual(tp.status, FILLED)
        self.assertEqual(b.fills[1].price, 104.0)
        self.assertEqual(b.fills[1].liquidity, "maker")
        self.assertEqual(b.position("S"), 0.0)

    def test_iceberg_slices_and_participation_cap(self):
        rows = [(100, 101, 99, 100, 1000)] * 8
        b, d = broker(rows, max_participation=0.1)
        b.on_bar(0)
        o = b.submit("S", 1, 250, ICEBERG, price=100.0, display=60)
        b.on_bar(1)
        self.assertEqual(o.status, PARTIAL)
        self.assertEqual(b.fills[-1].qty, 60.0)             # display 60 < cap 100
        for i in range(2, 6):
            b.on_bar(i)
        self.assertEqual(o.status, FILLED)
        self.assertEqual([f.qty for f in b.fills], [60.0, 60.0, 60.0, 60.0, 10.0])
        m = b.submit("S", -1, 250, MARKET)
        b.on_bar(6)
        self.assertEqual(b.fills[-1].qty, 100.0)            # 市价单也受 10% 参与上限
        self.assertEqual(m.status, PARTIAL)
        self.assertEqual(m.remaining, 150.0)

    def test_zero_volume_bar_no_fill(self):
        rows = [(100, 101, 99, 100, 1000), (100, 100, 100, 100, 0.0), (100, 101, 99, 100, 1000)]
        b, d = broker(rows, max_participation=1.0)
        b.on_bar(0)
        o = b.submit("S", 1, 1, MARKET)
        b.on_bar(1)
        self.assertEqual((o.status, len(b.fills)), (PENDING, 0))
        b.on_bar(2)
        self.assertEqual(o.status, FILLED)

    def test_impact_slippage_monotone(self):
        rows = [(100, 101, 99, 100, 1000)] * 2
        prices = []
        for q in (10, 50, 100):
            b, d = broker(rows, slippage=0.0, impact=0.01, max_participation=1.0)
            b.on_bar(0)
            b.submit("S", 1, q, MARKET)
            b.on_bar(1)
            prices.append(b.fills[0].price)
        self.assertEqual(prices, sorted(prices))
        self.assertAlmostEqual(prices[2], 100 * (1 + 0.01 * 100 / 1000), delta=1e-12)

    def test_cancel_expire_and_order_sequence(self):
        rows = [(100, 101, 99, 100, 1000)] * 5
        b, d = broker(rows, max_participation=1.0)
        b.on_bar(0)
        a = b.submit("S", 1, 1, LIMIT, price=50.0)
        c = b.submit("S", 1, 1, MARKET)
        e = b.submit("S", 1, 1, LIMIT, price=50.0, expires=1)
        self.assertTrue(b.cancel(a))
        self.assertEqual(a.status, CANCELLED)
        b.on_bar(1)
        self.assertEqual(c.status, FILLED)
        self.assertFalse(b.cancel(c))
        e2 = b.submit("S", 1, 1, LIMIT, price=50.0, expires=1)
        m2 = b.submit("S", 1, 1, MARKET)
        b.on_bar(2)
        self.assertEqual((e.status, e.reason), (CANCELLED, "expired"))
        self.assertEqual((e2.status, m2.status), (CANCELLED, FILLED))    # 过期单之后的挂单仍被处理
        x = b.submit("S", 1, 1, MARKET)
        y = b.submit("S", -1, 1, MARKET)
        b.on_bar(3)
        self.assertEqual([f.order_id for f in b.fills[2:]], [x.id, y.id])       # 提交顺序撮合（前两笔是 c 与 m2）
        z = b.submit("S", 1, 1, LIMIT, price=1.0)
        self.assertEqual(b.cancel_all("OTHER"), 0)
        self.assertEqual(b.cancel_all(), 1)
        self.assertEqual(b.pending, [])


class TestExactTouch(QBTTestCase):
    """触发价恰好等于 bar 极值时必须成交（<= / >= 的边界）。"""

    def test_limit_exact_touch(self):
        rows = [(100, 101, 99, 100, 1000), (100, 101, 99, 100, 1000)]
        b, d = broker(rows, max_participation=1.0)
        b.on_bar(0)
        buy = b.submit("S", 1, 1, LIMIT, price=99.0)
        sell = b.submit("S", -1, 1, LIMIT, price=101.0)
        b.on_bar(1)
        self.assertEqual((buy.status, sell.status), (FILLED, FILLED))
        self.assertEqual([f.price for f in b.fills], [99.0, 101.0])

    def test_stop_exact_touch(self):
        rows = [(100, 101, 99, 100, 1000), (100, 101, 99, 100, 1000)]
        b, d = broker(rows, max_participation=1.0, slippage=0.0)
        b.on_bar(0)
        buy = b.submit("S", 1, 1, STOP, price=101.0)
        sell = b.submit("S", -1, 1, STOP, price=99.0)
        b.on_bar(1)
        self.assertEqual((buy.status, sell.status), (FILLED, FILLED))
        self.assertEqual([f.price for f in b.fills], [101.0, 99.0])
        b2, _ = broker(rows, max_participation=1.0, slippage=0.0)
        b2.on_bar(0)
        b2.submit("S", 1, 1, STOP, price=100.0)                   # 触发价 == 开盘价：按开盘价成交
        b2.submit("S", 1, 1, LIMIT, price=100.0)
        b2.on_bar(1)
        self.assertEqual([f.price for f in b2.fills], [100.0, 100.0])

    def test_margin_exact_boundary_allowed(self):
        rows = [(100, 101, 99, 100, 1e9)] * 2
        b, d = broker(rows, max_leverage=1.0, slippage=0, taker_fee=0, max_participation=1.0)
        b.on_bar(0)
        o = b.submit("S", 1, 1000, MARKET)                                   # 名义恰好 == 权益 * 杠杆
        b.on_bar(1)
        self.assertEqual(o.status, FILLED)

    def test_margin_counts_own_symbol_once(self):
        rows = [(100, 101, 99, 100, 1e9)] * 3
        b, d = broker(rows, max_leverage=1.0, slippage=0, taker_fee=0, max_participation=1.0)
        b.on_bar(0)
        b.submit("S", 1, 500, MARKET)
        b.on_bar(1)
        o = b.submit("S", 1, 400, MARKET)                        # 总敞口 90k <= 100k：允许
        b.on_bar(2)
        self.assertEqual(o.status, FILLED)
        self.assertEqual(b.position("S"), 900.0)


class TestReduceOnlyAndMargin(QBTTestCase):
    def test_reduce_only_never_flips(self):
        """只减仓单：数量封顶在持仓，仓位归零后余量撤销；无仓/同向直接拒绝。"""
        rows = [(100, 101, 99, 100, 1000)] * 4
        b, d = broker(rows, max_participation=1.0)
        b.on_bar(0)
        b.submit("S", 1, 0.3, MARKET)
        b.on_bar(1)
        o = b.submit("S", -1, 0.5, STOP, price=99.5, reduce_only=True)     # 想卖 0.5，只持 0.3
        b.on_bar(2)
        self.assertEqual(b.position("S"), 0.0)
        self.assertEqual((o.status, o.reason, o.remaining), (CANCELLED, "reduce_only", 0.2))
        self.assertEqual(b.fills[-1].qty, 0.3)
        none = b.submit("S", -1, 1, MARKET, reduce_only=True)                # 无仓
        same = b.submit("S", 1, 1, MARKET, reduce_only=True)                 # 无仓（同向语义亦不成立）
        b.on_bar(3)
        self.assertEqual((none.status, none.reason), (REJECTED, "reduce_only"))
        self.assertEqual((same.status, same.reason), (REJECTED, "reduce_only"))
        self.assertEqual(b.position("S"), 0.0)
        b2, _ = broker(rows, max_participation=1.0)
        b2.on_bar(0)
        b2.submit("S", 1, 1, MARKET)
        b2.on_bar(1)
        add = b2.submit("S", 1, 1, MARKET, reduce_only=True)                  # 持多再买：同向 => 拒绝
        b2.on_bar(2)
        self.assertEqual((add.status, add.reason, b2.position("S")), (REJECTED, "reduce_only", 1.0))

    def test_partial_fill_then_margin_failure_is_cancelled_not_rejected(self):
        rows = [(100, 101, 99, 100, 300), (100, 101, 99, 100, 300), (100, 101, 99, 100, 300), (100, 301, 99, 300, 300),
                (300, 301, 299, 300, 300)]
        b, d = broker(rows, max_leverage=1.0, slippage=0, taker_fee=0, max_participation=1.0)
        b.on_bar(0)
        o = b.submit("S", 1, 600, MARKET)                                     # 60k 名义，首根只能成交 300 手
        b.on_bar(1)
        self.assertEqual((o.status, o.remaining), (PARTIAL, 300.0))
        b.on_bar(2)
        self.assertEqual(o.status, FILLED)
        o2 = b.submit("S", 1, 600, MARKET)                                    # 再买 600：第一根 300 手后名义 90k 可行
        b.on_bar(3)                                                           # 收盘涨到 300：权益 = 100k + 900*200 = 280k
        self.assertEqual(o2.status, PARTIAL)
        b.on_bar(4)                                                           # 开盘 300：再买 300 手 => 名义 1200*300 = 360k > 280k
        self.assertEqual((o2.status, o2.reason), (CANCELLED, "margin"))
        self.assertEqual(o2.remaining, 300.0)                                 # 保留已成交部分与余量记录
        self.assertEqual(b.position("S"), 900.0)

    def test_margin_marks_other_symbols_at_previous_close(self):
        """第 i 根开盘撮合时，其他标的只能按上一根收盘盯市（不能用本根收盘）。"""
        rows_a = [(100, 101, 99, 100, 1e9), (100, 101, 99, 100, 1e9), (100, 101, 50, 50, 1e9)]
        rows_b = [(100, 101, 99, 100, 1e9)] * 3
        a, bb = bars(rows_a, "A"), bars(rows_b, "B")
        b = Broker({"A": a, "B": bb}, BrokerConfig(max_leverage=1.0, slippage=0, taker_fee=0, max_participation=1.0))
        b.on_bar(0)
        b.submit("A", 1, 500, MARKET)                                         # 50k 名义
        b.on_bar(1)
        o = b.submit("B", 1, 600, MARKET)                                     # 60k：按上一根收盘 A=50k => 110k > 100k 拒绝
        b.on_bar(2)                                                           # 本根 A 收盘跌到 50（若偷看则 30k+60k 可行）
        self.assertEqual((o.status, o.reason), (REJECTED, "margin"))


class TestAccounting(QBTTestCase):
    def test_position_average_and_realized(self):
        rows = [(100, 101, 99, 100, 1e9), (100, 101, 99, 100, 1e9), (110, 111, 109, 110, 1e9),
                (120, 121, 119, 120, 1e9), (90, 91, 89, 90, 1e9)]
        b, d = broker(rows, maker_fee=0.0, taker_fee=0.0, slippage=0.0, max_participation=1.0)
        b.on_bar(0)
        b.submit("S", 1, 10, MARKET)
        b.on_bar(1)
        b.submit("S", 1, 10, MARKET)
        b.on_bar(2)
        p = b.positions["S"]
        self.assertEqual((p.qty, p.avg_price), (20.0, 105.0))
        b.submit("S", -1, 5, MARKET)
        b.on_bar(3)
        self.assertAlmostEqual(p.realized, (120 - 105) * 5, delta=1e-9)
        self.assertEqual(p.qty, 15.0)
        b.submit("S", -1, 25, MARKET)                    # 反手：平 15 再开空 10
        b.on_bar(4)
        self.assertAlmostEqual(p.realized, 75 + (90 - 105) * 15, delta=1e-9)
        self.assertEqual((p.qty, p.avg_price), (-10.0, 90.0))
        self.assertAlmostEqual(b.equity, 100_000 + p.realized, delta=1e-9)

    def test_funding(self):
        rows = [(100, 101, 99, 100, 1e6)] * 7
        b, d = broker(rows, funding_rate=0.001, funding_interval=3, maker_fee=0, taker_fee=0, slippage=0,
                      max_participation=1.0)
        b.on_bar(0)
        b.submit("S", 1, 10, MARKET)
        b.on_bar(1)
        cash1 = b.cash
        b.on_bar(2)                                       # (2+1) % 3 == 0 => 收取
        self.assertAlmostEqual(cash1 - b.cash, 10 * 100 * 0.001, delta=1e-12)
        b.on_bar(3)
        b.on_bar(4)
        self.assertAlmostEqual(b.funding_paid, 1.0, delta=1e-12)
        b.on_bar(5)
        self.assertAlmostEqual(b.funding_paid, 2.0, delta=1e-12)
        b.submit("S", -1, 30, MARKET)                     # 变成空头 20：空头收取资金费
        b.on_bar(6)
        self.assertEqual(b.position("S"), -20.0)
        b2, _ = broker(rows, funding_rate=0.001, funding_interval=3, maker_fee=0, taker_fee=0, slippage=0,
                       max_participation=1.0)
        b2.on_bar(0)
        b2.submit("S", -1, 10, MARKET)
        for i in range(1, 3):
            b2.on_bar(i)
        self.assertAlmostEqual(b2.funding_paid, -1.0, delta=1e-12)
        self.assertAlmostEqual(b2.equity, 100_000 + 1.0, delta=1e-9)

    def test_margin_rejection_and_leverage(self):
        rows = [(100, 101, 99, 100, 1e9)] * 4
        b, d = broker(rows, max_leverage=1.0, slippage=0, taker_fee=0, max_participation=1.0)
        b.on_bar(0)
        o = b.submit("S", 1, 1500, MARKET)                # 15 万名义 > 10 万权益
        b.on_bar(1)
        self.assertEqual((o.status, o.reason), (REJECTED, "margin"))
        self.assertEqual(b.fills, [])
        b2, _ = broker(rows, max_leverage=2.0, slippage=0, taker_fee=0, max_participation=1.0)
        b2.on_bar(0)
        o2 = b2.submit("S", 1, 1500, MARKET)
        b2.on_bar(1)
        self.assertEqual(o2.status, FILLED)
        self.assertLess(b2.cash, 0.0)                     # 借入现金
        self.assertAlmostEqual(b2.liabilities(), 50_000.0, delta=1e-6)
        self.assertAlmostEqual(b2.assets() - b2.liabilities(), b2.equity, delta=1e-6)
        r = b2.submit("S", 1, 1000, MARKET, reduce_only=True)
        b2.on_bar(2)
        self.assertEqual((r.status, r.reason), (REJECTED, "reduce_only"))   # reduce_only 不能加仓（同向直接拒绝）
        ok = b2.submit("S", -1, 500, MARKET, reduce_only=True)
        b2.on_bar(3)
        self.assertEqual(ok.status, FILLED)

    def test_short_toggle(self):
        rows = [(100, 101, 99, 100, 1e9)] * 3
        b, d = broker(rows, allow_short=False, max_participation=1.0)
        b.on_bar(0)
        o = b.submit("S", -1, 1, MARKET)
        self.assertEqual((o.status, o.reason), (REJECTED, "short not allowed"))
        b.submit("S", 1, 2, MARKET)
        b.on_bar(1)
        ok = b.submit("S", -1, 2, MARKET)
        self.assertEqual(ok.status, PENDING)
        # 成交时封顶：多头 2 手，两笔各卖 2 手 => 第二笔在成交时被拒并移出挂单，第三笔（买）仍正常撮合
        b3, _ = broker(rows, allow_short=False, max_participation=1.0)
        b3.on_bar(0)
        b3.submit("S", 1, 2, MARKET)
        b3.on_bar(1)
        s1 = b3.submit("S", -1, 2, MARKET)
        s2 = b3.submit("S", -1, 2, MARKET)
        buy = b3.submit("S", 1, 1, MARKET)
        b3.on_bar(2)
        self.assertEqual((s1.status, s2.status, buy.status), (FILLED, REJECTED, FILLED))
        self.assertEqual(b3.pending, [])
        self.assertEqual(b3.position("S"), 1.0)
        b2, _ = broker(rows, allow_short=True, max_participation=1.0, slippage=0, taker_fee=0)
        b2.on_bar(0)
        b2.submit("S", -1, 10, MARKET)
        b2.on_bar(1)
        self.assertEqual(b2.position("S"), -10.0)
        self.assertAlmostEqual(b2.equity, b2.cash + (-10) * 100, delta=1e-9)
        self.assertAlmostEqual(b2.liabilities(), 1000.0, delta=1e-9)

    def test_liquidation_and_bankruptcy(self):
        rows = [(100, 101, 99, 100, 1e9), (100, 101, 99, 100, 1e9), (100, 101, 60, 60, 1e9), (60, 61, 59, 60, 1e9)]
        b, d = broker(rows, max_leverage=3.0, maintenance_margin=0.2, liquidation_fee=0.01, slippage=0,
                      taker_fee=0, max_participation=1.0)
        b.on_bar(0)
        b.submit("S", 1, 2500, MARKET)                    # 25 万名义 / 10 万权益 = 2.5x
        pend = b.submit("S", -1, 1, LIMIT, price=500.0)
        b.on_bar(1)
        self.assertEqual(b.position("S"), 2500.0)
        b.on_bar(2)                                       # 收盘 60：权益 = 10万 - 2500*40 = 0 < 0.2*15万 => 爆仓
        self.assertEqual(b.liquidations, 1)
        self.assertEqual(b.position("S"), 0.0)
        self.assertEqual(b.fills[-1].tag, "liquidation")
        self.assertAlmostEqual(b.fills[-1].fee, 2500 * 60 * 0.01, delta=1e-9)
        self.assertAlmostEqual(b.equity, 100_000 - 2500 * 40 - 1500.0, delta=1e-9)
        self.assertEqual((pend.status, pend.reason), (CANCELLED, "liquidation"))
        self.assertTrue(b.bankrupt)
        self.assertEqual([e[0] for e in b.events], ["liquidation", "bankrupt"])
        o = b.submit("S", 1, 1, MARKET)
        self.assertEqual((o.status, o.reason), (REJECTED, "bankrupt"))
        b.on_bar(3)
        self.assertEqual(len(b.fills), 2)
        self.assertEqual(b.liquidations, 1)                       # 破产后没有持仓，不再重复“强平”
        self.assertAlmostEqual(b.fees_paid, sum(f.fee for f in b.fills), delta=1e-9)
        self.assertAlmostEqual(b.fees_paid, 1500.0, delta=1e-9)

    def test_short_liquidation(self):
        """空头 2.5x 杠杆，价格暴涨 => 强平必须把空头买回（side=+1），现金按买回价扣减。"""
        rows = [(100, 101, 99, 100, 1e9), (100, 101, 99, 100, 1e9), (100, 140, 99, 140, 1e9), (140, 141, 139, 140, 1e9)]
        b, d = broker(rows, max_leverage=3.0, maintenance_margin=0.2, liquidation_fee=0.0, slippage=0,
                      taker_fee=0, max_participation=1.0)
        b.on_bar(0)
        b.submit("S", -1, 2500, MARKET)
        b.on_bar(1)
        self.assertEqual(b.position("S"), -2500.0)
        b.on_bar(2)                                       # 权益 = 10万 - 2500*40 = 0 < 0.2 * 35万 => 强平
        self.assertEqual((b.liquidations, b.position("S")), (1, 0.0))
        self.assertEqual((b.fills[-1].side, b.fills[-1].qty, b.fills[-1].price), (1, 2500.0, 140.0))
        self.assertAlmostEqual(b.cash, 100_000 + 2500 * 100 - 2500 * 140, delta=1e-9)
        self.assertAlmostEqual(b.equity, b.cash, delta=1e-9)

    def test_tiny_long_liquidation_and_small_cap_fill(self):
        """数量 <= 1 的多头也能被强平（side 由 qty>0 决定，不是 qty>1）；参与上限 <= 1 手时仍按上限成交。"""
        rows = [(100, 101, 99, 100, 5.0), (100, 101, 99, 100, 5.0), (100, 101, 50, 50, 5.0), (50, 51, 49, 50, 5.0)]
        b, d = broker(rows, cash=30.0, max_leverage=3.0, maintenance_margin=0.25, slippage=0, taker_fee=0,
                      max_participation=0.1)
        b.on_bar(0)
        o = b.submit("S", 1, 0.5, MARKET)
        b.on_bar(1)
        self.assertEqual((o.status, b.position("S")), (FILLED, 0.5))      # cap = 0.1 * 5 = 0.5 手
        b.on_bar(2)                                                       # 权益 = 30 - 0.5*50 = 5 < 0.25 * 25 = 6.25 => 强平
        self.assertEqual((b.liquidations, b.position("S")), (1, 0.0))
        self.assertEqual(b.fills[-1].side, -1)
        # 强平后权益落在 (0, 1]：不算破产（破产判定是 <= 0，不是 <= 1）
        rows2 = [(100, 101, 99, 100, 5.0), (100, 101, 99, 100, 5.0), (100, 101, 41, 41, 5.0)]
        b2, _ = broker(rows2, cash=30.0, max_leverage=3.0, maintenance_margin=0.25, slippage=0, taker_fee=0,
                       liquidation_fee=0.0, max_participation=0.1)
        b2.on_bar(0)
        b2.submit("S", 1, 0.5, MARKET)
        b2.on_bar(1)
        b2.on_bar(2)                                                      # 权益 = 30 - 0.5*59 = 0.5
        self.assertEqual((b2.liquidations, b2.bankrupt), (1, False))
        self.assertAlmostEqual(b2.equity, 0.5, delta=1e-9)

    def test_liquidation_without_bankruptcy_keeps_trading(self):
        rows = [(100, 101, 99, 100, 1e9), (100, 101, 99, 100, 1e9), (100, 101, 75, 75, 1e9), (75, 76, 74, 75, 1e9)]
        b, d = broker(rows, max_leverage=3.0, maintenance_margin=0.2, liquidation_fee=0.0, slippage=0,
                      taker_fee=0, max_participation=1.0)
        b.on_bar(0)
        b.submit("S", 1, 2500, MARKET)
        b.on_bar(1)
        b.on_bar(2)                                       # 权益 = 10万 - 2500*25 = 37500 < 0.2*187500 = 37500? 等号不触发
        self.assertEqual(b.liquidations, 0)
        b2, _ = broker(rows, max_leverage=3.0, maintenance_margin=0.21, liquidation_fee=0.0, slippage=0,
                       taker_fee=0, max_participation=1.0)
        b2.on_bar(0)
        b2.submit("S", 1, 2500, MARKET)
        b2.on_bar(1)
        b2.on_bar(2)
        self.assertEqual((b2.liquidations, b2.bankrupt), (1, False))
        o = b2.submit("S", 1, 10, MARKET)
        b2.on_bar(3)
        self.assertEqual(o.status, FILLED)

    def test_multi_symbol_margin_and_exposure(self):
        uni = generate_universe(["A", "B"], 30, 1, halt_prob=0.0)
        b = Broker(uni, BrokerConfig(max_leverage=1.0, max_participation=1.0, slippage=0, taker_fee=0))
        b.on_bar(0)
        pa, pb = uni["A"].open[1], uni["B"].open[1]
        qa = 60_000 / pa
        b.submit("A", 1, qa, MARKET)
        b.submit("B", 1, 60_000 / pb, MARKET)             # 两者合计 12 万 > 10 万 => 第二笔拒绝
        b.on_bar(1)
        self.assertEqual([o.status for o in b.orders], [FILLED, REJECTED])
        self.assertAlmostEqual(b.gross_exposure(), qa * uni["A"].close[1], delta=1e-6)
        b.submit("B", -1, 30_000 / pb, MARKET)            # 空头也计入总敞口：6万 + 3万 <= 10万 允许
        b.on_bar(2)
        self.assertEqual(b.orders[-1].status, FILLED)
        self.assertAlmostEqual(b.assets() - b.liabilities(), b.equity, delta=1e-6)
        self.assertEqual(b.position("B") < 0, True)


class TestRandomFlowIdentities(QBTTestCase):
    """随机订单流（显式 seed）下，逐 bar 验证三条会计恒等式、成交量上限、成交价在 bar 区间内。"""

    CONFIGS = [dict(), dict(max_leverage=3.0, maintenance_margin=0.1), dict(funding_rate=0.0005, funding_interval=7),
               dict(allow_short=False, max_participation=0.05), dict(impact=0.02, slippage=0.0005, max_leverage=2.0)]

    def _run(self, seed, cfg):
        uni = generate_universe(["X", "Y"], scale(600, 200), seed, halt_prob=0.01, gap_prob=0.05, jump_prob=0.01)
        b = Broker(uni, BrokerConfig(**cfg))
        r = random.Random(seed)
        n = len(uni["X"])
        for i in range(n):
            b.on_bar(i)
            self._check_identities(b, uni, i)
            for _ in range(r.randint(0, 3)):
                sym = r.choice(["X", "Y"])
                px = uni[sym].close[i]
                otype = r.choice([MARKET, LIMIT, STOP, TAKE_PROFIT, ICEBERG])
                qty = r.choice([1, 5, 20, 100, 400]) * (1 if r.random() < 0.8 else 10)
                price = px * r.uniform(0.95, 1.05)
                try:
                    b.submit(sym, r.choice([1, -1]), qty, otype, price=price, display=max(1, qty // 3),
                             expires=i + r.choice([1, 5, 50]) if r.random() < 0.3 else None,
                             reduce_only=r.random() < 0.2)
                except OrderError:
                    self.fail("valid random order rejected with OrderError")
        return b

    def _check_identities(self, b, uni, i):
        marked = b.cash + sum(p.qty * uni[s].close[i] for s, p in b.positions.items())
        self.assertAlmostEqual(b.equity, marked, delta=ACCOUNTING_TOL)
        self.assertAlmostEqual(b.assets() - b.liabilities(), b.equity, delta=ACCOUNTING_TOL)
        self.assertAlmostEqual(b.initial_cash + b.realized_pnl() + b.unrealized_pnl() - b.fees_paid - b.funding_paid,
                               b.equity, delta=ACCOUNTING_TOL * 10)   # 多笔累加顺序不同，放宽 10 倍
        self.assertAlmostEqual(sum(f.fee for f in b.fills), b.fees_paid, delta=ACCOUNTING_TOL)

    def _check_fills(self, b, uni):
        cfg = b.cfg
        for f in b.fills:
            d = uni[f.symbol]
            if f.tag == "liquidation":
                self.assertEqual(f.price, d.close[f.bar])
                continue
            self.assertLessEqual(f.qty, cfg.max_participation * d.volume[f.bar] + 1e-9)
            lo, hi = d.low[f.bar], d.high[f.bar]
            if f.liquidity == "maker":
                self.assertTrue(lo - 1e-9 <= f.price <= hi + 1e-9)
            else:
                raw_lo, raw_hi = lo * (1 - cfg.slippage - cfg.impact), hi * (1 + cfg.slippage + cfg.impact)
                self.assertTrue(raw_lo - 1e-9 <= f.price <= raw_hi + 1e-9)

    def test_identities_hold(self):
        for k, cfg in enumerate(self.CONFIGS):
            for seed in range(scale(4, 2)):
                b = self._run(100 + seed + 10 * k, cfg)
                self._check_fills(b, b.data)
                self.assertGreater(len(b.fills), 10, f"cfg={cfg}")
                # 从成交流独立重建现金
                cash = b.initial_cash - sum(f.side * f.qty * f.price + f.fee for f in b.fills) - b.funding_paid
                self.assertAlmostEqual(cash, b.cash, delta=ACCOUNTING_TOL * 10)
                for s, p in b.positions.items():
                    self.assertAlmostEqual(sum(f.side * f.qty for f in b.fills if f.symbol == s), p.qty, delta=1e-9)

    def test_determinism(self):
        a = self._run(7, self.CONFIGS[1])
        c = self._run(7, self.CONFIGS[1])
        self.assertEqual([f.to_dict() for f in a.fills], [f.to_dict() for f in c.fills])
        self.assertEqual(a.equity, c.equity)

    def test_short_disabled_never_short(self):
        b = self._run(3, self.CONFIGS[3])
        self.assertTrue(all(p.qty >= -1e-9 for p in b.positions.values()))
        fills = {}
        for f in b.fills:
            fills[f.symbol] = fills.get(f.symbol, 0.0) + f.side * f.qty
            self.assertGreaterEqual(fills[f.symbol], -1e-9)


class TestRoundTrips(QBTTestCase):
    def test_round_trips_from_fills(self):
        F = lambda bar, side, qty, price, fee=0.0: Fill(1, "S", bar, side, qty, price, fee, "taker", "")
        fills = [F(1, 1, 10, 100, 1.0), F(2, 1, 10, 110, 1.0), F(3, -1, 5, 120, 0.5), F(4, -1, 25, 90, 2.0), F(6, 1, 10, 80, 0.0)]
        t = round_trips(fills)
        self.assertEqual(len(t), 2)
        self.assertEqual((t[0]["entry_bar"], t[0]["exit_bar"], t[0]["side"], t[0]["qty"], t[0]["bars"]), (1, 4, 1, 20.0, 3))
        self.assertAlmostEqual(t[0]["entry_price"], 105.0, delta=1e-12)   # 加权均价的浮点舍入
        self.assertAlmostEqual(t[0]["pnl"], (120 - 105) * 5 + (90 - 105) * 15 - 4.5, delta=1e-9)
        self.assertEqual((t[1]["side"], t[1]["qty"], t[1]["entry_price"], t[1]["exit_price"]), (-1, 10.0, 90, 80))
        self.assertAlmostEqual(t[1]["pnl"], (90 - 80) * 10, delta=1e-9)
        frac = [F(1, -1, 0.5, 10.0), F(2, -1, 0.25, 12.0), F(3, 1, 0.75, 9.0)]      # 分数数量的空头往返
        ft = round_trips(frac)
        self.assertEqual(len(ft), 1)
        self.assertEqual((ft[0]["side"], ft[0]["qty"]), (-1, 0.75))
        self.assertAlmostEqual(ft[0]["pnl"], (0.5 * 10 + 0.25 * 12) - 0.75 * 9, delta=1e-12)
        self.assertEqual(round_trips([]), [])
        self.assertEqual(round_trips([F(1, 1, 1, 1)]), [])        # 未平仓不计
        self.assertEqual(len(round_trips([F(1, 1, 2, 1), F(2, -1, 1, 1), F(3, -1, 1, 1)])), 1)

    def test_round_trips_consistent_with_broker_realized(self):
        """随机市价流 + 最后强制平仓：往返 pnl 总和 == 账户已实现盈亏 - 手续费，且往返数 == 持仓归零次数。"""
        d = generate(n=scale(500, 150), seed=5, halt_prob=0.0)
        b = Broker({d.symbol: d}, BrokerConfig(max_participation=1.0, maker_fee=0.001, taker_fee=0.001, slippage=0))
        r = random.Random(1)
        zero_crossings = 0
        for i in range(len(d) - 1):
            b.on_bar(i)
            if i == len(d) - 2:
                if b.position(d.symbol) != 0.0:
                    b.submit(d.symbol, -1 if b.position(d.symbol) > 0 else 1, abs(b.position(d.symbol)), MARKET)
            elif r.random() < 0.3:
                b.submit(d.symbol, r.choice([1, -1]), r.choice([1, 2, 3]), MARKET)
        b.on_bar(len(d) - 1)
        self.assertEqual(b.position(d.symbol), 0.0)
        trades = round_trips(b.fills)
        pos = 0.0
        for f in b.fills:
            new = pos + f.side * f.qty
            if pos != 0.0 and (abs(new) < 1e-12 or (new > 0) != (pos > 0)):
                zero_crossings += 1
            pos = new
        self.assertEqual(len(trades), zero_crossings)
        self.assertGreater(len(trades), 5)
        p = b.positions[d.symbol]
        self.assertAlmostEqual(sum(t["pnl"] for t in trades), p.realized - b.fees_paid, delta=1e-6)
        self.assertTrue(all(t["bars"] >= 0 and t["qty"] > 0 for t in trades))

if __name__ == "__main__":
    unittest.main()
