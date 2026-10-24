import os
import unittest

from qdexsim import common, store as store_mod
from qdexsim.engine import Engine, EngineError
from qdexsim.tests._util import tmpdir

H8 = common.EIGHT_HOURS_MS


class Clock:
    def __init__(self, t=1_700_000_000_000):
        self.t = t

    def __call__(self):
        return self.t


def engine(**cfg):
    return Engine(clock=Clock(), config=cfg or None)


class TestValidation(unittest.TestCase):
    def kind(self, e, *a, **kw):
        with self.assertRaises(EngineError) as cm:
            e.place(*a, **kw)
        return cm.exception.kind

    def test_each_error_kind(self):
        e = engine()
        self.assertEqual(self.kind(e, "DOGEUSDT", "BUY", "LIMIT", 1, 1), "SYMBOL_UNKNOWN")
        self.assertEqual(self.kind(e, "BTCUSDT", "HOLD", "LIMIT", 0.01, 59000), "BAD_PARAM")
        self.assertEqual(self.kind(e, "BTCUSDT", "BUY", "STOP", 0.01, 59000), "BAD_PARAM")
        self.assertEqual(self.kind(e, "BTCUSDT", "BUY", "LIMIT", "x", 59000), "BAD_PARAM")
        self.assertEqual(self.kind(e, "BTCUSDT", "BUY", "LIMIT", 0.01, None), "BAD_PARAM")
        self.assertEqual(self.kind(e, "BTCUSDT", "BUY", "LIMIT", 0.0005, 59000), "BAD_QTY")       # < min_qty
        self.assertEqual(self.kind(e, "BTCUSDT", "BUY", "LIMIT", 0.0105, 59000), "BAD_QTY")       # 不是 step 的倍数
        self.assertEqual(self.kind(e, "BTCUSDT", "BUY", "LIMIT", -1, 59000), "BAD_QTY")
        self.assertEqual(self.kind(e, "BTCUSDT", "BUY", "LIMIT", 0.01, 59000.05), "BAD_PRICE")
        self.assertEqual(self.kind(e, "BTCUSDT", "BUY", "LIMIT", 0.01, 0), "BAD_PRICE")
        self.assertEqual(self.kind(e, "ETHUSDT", "BUY", "LIMIT", 0.01, 100), "MIN_NOTIONAL")      # 1 USDT < 5
        self.assertEqual(self.kind(e, "BTCUSDT", "BUY", "LIMIT", 10, 59000), "INSUFFICIENT")
        self.assertEqual(self.kind(e, "BTCUSDT", "SELL", "MARKET", 0.01, reduce_only=True), "REDUCE_ONLY")
        e.place("BTCUSDT", "BUY", "LIMIT", 0.01, 59000, cid="c")
        self.assertEqual(self.kind(e, "BTCUSDT", "SELL", "LIMIT", 0.02, 61000, cid="c"), "DUP_CID")
        self.assertEqual(len(e.all_orders()), 1)
        e.set_symbol("ETHUSDT", status="SUSPENDED")
        self.assertEqual(self.kind(e, "ETHUSDT", "BUY", "MARKET", 0.1), "SYMBOL_SUSPENDED")
        e.set_symbol("ETHUSDT", status="DELISTED")
        self.assertEqual(self.kind(e, "ETHUSDT", "BUY", "MARKET", 0.1), "SYMBOL_DELISTED")

    def test_min_notional_skipped_for_reduce_only_and_market_uses_mark(self):
        e = engine()
        e.place("ETHUSDT", "BUY", "MARKET", 0.1)
        e.set_symbol("ETHUSDT", min_notional=100.0)
        self.assertEqual(self.kind(e, "ETHUSDT", "SELL", "LIMIT", 0.01, 3100), "MIN_NOTIONAL")     # 31 < 100
        o = e.place("ETHUSDT", "SELL", "LIMIT", 0.01, 3100, reduce_only=True)                      # 只减仓放行
        self.assertEqual(o["status"], "NEW")
        self.assertEqual(self.kind(e, "ETHUSDT", "BUY", "MARKET", 0.01), "MIN_NOTIONAL")           # 市价按标记价 0.01 × 3000
        e.set_symbol("ETHUSDT", min_notional=5.0)
        self.assertEqual(e.place("ETHUSDT", "BUY", "MARKET", 0.01)["status"], "FILLED")

    def test_insufficient_does_not_consume_order_id(self):
        e = engine()
        nid = e.next_id
        with self.assertRaises(EngineError):
            e.place("BTCUSDT", "BUY", "LIMIT", 10, 59000)
        self.assertEqual(e.next_id, nid)
        self.assertEqual(e.store.counts()["orders"], 0)


class TestMatching(unittest.TestCase):
    def test_market_fills_fully_as_taker(self):
        e = engine()
        o = e.place("BTCUSDT", "BUY", "MARKET", 0.5)
        self.assertEqual((o["status"], o["filled"], o["avg_price"]), ("FILLED", 0.5, 60000.0))
        self.assertAlmostEqual(o["fee"], 0.5 * 60000 * 0.0005)
        p = e.position("BTCUSDT")
        self.assertEqual((p["qty"], p["entry"]), (0.5, 60000.0))
        self.assertAlmostEqual(e.wallet, 10000 - 15.0)
        self.assertFalse(e.user_trades()[0]["maker"])

    def test_limit_crossing_at_placement_is_taker_up_to_liquidity(self):
        e = engine()
        o = e.place("BTCUSDT", "BUY", "LIMIT", 0.2, 60100)       # 穿价：以最新价 60000 吃 0.05，剩 0.15 挂着
        self.assertEqual((o["status"], o["filled"], o["avg_price"]), ("PARTIALLY_FILLED", 0.05, 60000.0))
        self.assertFalse(e.user_trades()[0]["maker"])
        self.assertEqual(len(e.open_orders("BTCUSDT")), 1)
        s = e.place("BTCUSDT", "SELL", "LIMIT", 0.01, 59900)      # 卖价 ≤ 最新价也穿
        self.assertEqual(s["status"], "FILLED")

    def test_price_time_priority_and_partial_fills(self):
        e = engine()
        a = e.place("BTCUSDT", "BUY", "LIMIT", 0.04, 59000)
        e.clock.t += 1
        b = e.place("BTCUSDT", "BUY", "LIMIT", 0.04, 59500)       # 价更好，后到
        e.clock.t += 1
        c = e.place("BTCUSDT", "BUY", "LIMIT", 0.04, 59000)       # 同价，后到
        fills = e.on_price("BTCUSDT", 58900, liquidity=0.05)
        self.assertEqual([(f["order_id"], f["qty"], f["price"]) for f in fills], [(b["id"], 0.04, 59500.0), (a["id"], 0.01, 59000.0)])
        self.assertTrue(all(f["maker"] for f in fills))
        self.assertEqual(e.get_order(b["id"])["status"], "FILLED")
        self.assertEqual(e.get_order(a["id"])["status"], "PARTIALLY_FILLED")
        self.assertEqual(e.get_order(c["id"])["status"], "NEW")
        fills = e.on_price("BTCUSDT", 58900, liquidity=1.0)
        self.assertEqual([f["order_id"] for f in fills], [a["id"], c["id"]])
        self.assertEqual(e.on_price("BTCUSDT", 58900), [])
        self.assertEqual(e.on_price("BTCUSDT", 58900, liquidity=0), [])

    def test_sell_side_priority(self):
        e = engine()
        e.place("BTCUSDT", "BUY", "MARKET", 0.1)
        hi = e.place("BTCUSDT", "SELL", "LIMIT", 0.03, 61000)
        lo = e.place("BTCUSDT", "SELL", "LIMIT", 0.03, 60500)
        fills = e.on_price("BTCUSDT", 61500, liquidity=0.04)
        self.assertEqual([(f["order_id"], f["qty"]) for f in fills], [(lo["id"], 0.03), (hi["id"], 0.01)])

    def test_no_cross_no_fill(self):
        e = engine()
        e.place("BTCUSDT", "BUY", "LIMIT", 0.01, 59000)
        self.assertEqual(e.on_price("BTCUSDT", 59000.1), [])
        self.assertEqual(e.on_price("BTCUSDT", 59000.0)[0]["qty"], 0.01)        # 等于也算穿

    def test_reduce_only_capped_and_auto_cancel(self):
        e = engine()
        e.place("BTCUSDT", "BUY", "MARKET", 0.02)
        o = e.place("BTCUSDT", "SELL", "LIMIT", 0.05, 61000, reduce_only=True)
        self.assertEqual(o["qty"], 0.02)                                          # 下单时截到持仓
        e.place("BTCUSDT", "SELL", "MARKET", 0.02)                               # 持仓先被别的单平掉
        self.assertEqual(e.position("BTCUSDT")["qty"], 0.0)
        e.on_price("BTCUSDT", 61500)
        self.assertEqual(e.get_order(o["id"])["status"], "CANCELED")
        self.assertEqual(e.get_order(o["id"])["reason"], "REDUCE_ONLY_NOTHING_LEFT")
        e.place("BTCUSDT", "BUY", "MARKET", 0.02)
        r = e.place("BTCUSDT", "SELL", "LIMIT", 0.02, 61600, reduce_only=True)
        e.place("BTCUSDT", "SELL", "MARKET", 0.01)
        e.on_price("BTCUSDT", 61700)
        self.assertEqual(e.get_order(r["id"])["filled"], 0.01)                   # 成交量不超过当时的反向持仓

    def test_cancel_paths(self):
        e = engine()
        o = e.place("BTCUSDT", "BUY", "LIMIT", 0.01, 59000, cid="k")
        c = e.cancel(cid="k")
        self.assertEqual((c["status"], c["reason"]), ("CANCELED", "USER"))
        with self.assertRaises(EngineError) as cm:
            e.cancel(order_id=o["id"])
        self.assertEqual(cm.exception.kind, "ORDER_NOT_OPEN")
        with self.assertRaises(EngineError) as cm:
            e.cancel(order_id=999)
        self.assertEqual(cm.exception.kind, "ORDER_NOT_FOUND")
        with self.assertRaises(EngineError) as cm:
            e.get_order()
        self.assertEqual(cm.exception.kind, "BAD_PARAM")
        with self.assertRaises(EngineError):
            e.get_order(cid="nope")
        e.place("BTCUSDT", "BUY", "LIMIT", 0.01, 59000)
        e.place("ETHUSDT", "BUY", "LIMIT", 0.1, 2900)
        self.assertEqual(len(e.cancel_all("BTCUSDT")), 1)
        self.assertEqual(len(e.cancel_all()), 1)
        self.assertEqual(e.open_orders(), [])
        self.assertIsNone(e.fill_remaining(o["id"]))
        self.assertIsNone(e.fill_remaining(424242))

    def test_fill_remaining_for_cancel_race(self):
        e = engine()
        o = e.place("BTCUSDT", "BUY", "LIMIT", 0.03, 59000)
        e.on_price("BTCUSDT", 58990, liquidity=0.01)
        r = e.fill_remaining(o["id"])
        self.assertEqual((r["status"], r["filled"]), ("FILLED", 0.03))
        self.assertEqual(e.position("BTCUSDT")["qty"], 0.03)
        self.assertTrue(e.user_trades()[-1]["maker"])


class TestPositions(unittest.TestCase):
    def test_average_entry_realized_and_flip(self):
        e = engine(taker_fee=0.0, maker_fee=0.0)
        e.place("BTCUSDT", "BUY", "MARKET", 0.1)                 # 60000
        e.on_price("BTCUSDT", 62000)
        e.place("BTCUSDT", "BUY", "MARKET", 0.1)                 # 均价 61000
        p = e.position("BTCUSDT")
        self.assertEqual((p["qty"], p["entry"]), (0.2, 61000.0))
        self.assertAlmostEqual(p["upl"], 0.2 * (62000 - 61000))
        e.on_price("BTCUSDT", 63000)
        e.place("BTCUSDT", "SELL", "MARKET", 0.05)              # 平一部分：已实现 0.05 × 2000 = 100
        self.assertAlmostEqual(e.wallet, 10100.0)
        self.assertEqual(e.position("BTCUSDT")["qty"], 0.15)
        e.place("BTCUSDT", "SELL", "MARKET", 0.25)              # 反手：平 0.15 赚 300，再空 0.1 @63000
        p = e.position("BTCUSDT")
        self.assertEqual((p["qty"], p["entry"]), (-0.1, 63000.0))
        self.assertAlmostEqual(e.wallet, 10400.0)
        e.on_price("BTCUSDT", 62000)
        e.place("BTCUSDT", "BUY", "MARKET", 0.1)                # 空头平仓赚 100
        p = e.position("BTCUSDT")
        self.assertEqual((p["qty"], p["entry"]), (0.0, 0.0))
        self.assertAlmostEqual(e.wallet, 10500.0)
        self.assertAlmostEqual(p["realized"], 500.0)

    def test_fees_and_account(self):
        e = engine()
        e.place("BTCUSDT", "BUY", "LIMIT", 0.1, 59000)
        a = e.account()
        self.assertAlmostEqual(a["order_margin"], 0.1 * 59000 / 10)
        self.assertAlmostEqual(a["available"], 10000 - 590)
        e.on_price("BTCUSDT", 58000, liquidity=1.0)
        a = e.account()
        self.assertAlmostEqual(a["wallet"], 10000 - 0.1 * 59000 * 0.0002)
        self.assertAlmostEqual(a["upl"], 0.1 * (58000 - 59000))
        self.assertAlmostEqual(a["position_margin"], 0.1 * 58000 / 10)
        self.assertAlmostEqual(a["equity"], a["wallet"] + a["upl"])
        self.assertEqual(a["order_margin"], 0.0)

    def test_leverage_and_margin_mode(self):
        e = engine()
        self.assertEqual(e.set_leverage("BTCUSDT", "20"), 20)
        for bad in (0, 126, "x"):
            with self.assertRaises(EngineError) as cm:
                e.set_leverage("BTCUSDT", bad)
            self.assertEqual(cm.exception.kind, "BAD_LEVERAGE")
        with self.assertRaises(EngineError):
            e.set_leverage("NOPE", 5)
        self.assertEqual(e.set_margin_mode("BTCUSDT", "isolated"), "isolated")
        with self.assertRaises(EngineError) as cm:
            e.set_margin_mode("BTCUSDT", "portfolio")
        self.assertEqual(cm.exception.kind, "BAD_PARAM")
        e.place("BTCUSDT", "BUY", "LIMIT", 0.01, 59000)
        with self.assertRaises(EngineError) as cm:
            e.set_margin_mode("BTCUSDT", "cross")
        self.assertEqual(cm.exception.kind, "MARGIN_MODE_BUSY")
        self.assertEqual(e.set_margin_mode("BTCUSDT", "isolated"), "isolated")     # 不变可以
        e.cancel_all()
        e.place("BTCUSDT", "BUY", "MARKET", 0.01)
        with self.assertRaises(EngineError):
            e.set_margin_mode("BTCUSDT", "cross")

    def test_liq_price_formulas(self):
        e = engine()
        p = {"symbol": "BTCUSDT", "qty": 0.0, "entry": 0.0, "leverage": 10, "margin_mode": "isolated"}
        self.assertEqual(e.liq_price(p), 0.0)
        p.update(qty=1.0, entry=60000.0)
        self.assertAlmostEqual(e.liq_price(p), 60000 * (1 - 0.1 + 0.005))
        p.update(qty=-1.0)
        self.assertAlmostEqual(e.liq_price(p), 60000 * (1 + 0.1 - 0.005))
        p.update(margin_mode="cross", qty=1.0)
        e.wallet = 10000.0
        lp = e.liq_price(p)
        self.assertAlmostEqual(10000 + 1.0 * (lp - 60000), 0.005 * lp)
        p.update(qty=-1.0)
        lp = e.liq_price(p)
        self.assertAlmostEqual(10000 + 1.0 * (60000 - lp), 0.005 * lp)
        e.wallet = 1e6                                                   # 钱包盖过名义：强平价不会是负数
        p.update(qty=1.0, entry=60000.0)
        self.assertEqual(e.liq_price(p), 0.0)

    def test_liquidation_isolated_and_cross(self):
        e = engine()
        e.set_margin_mode("BTCUSDT", "isolated")
        e.place("BTCUSDT", "BUY", "MARKET", 0.1)
        e.place("BTCUSDT", "SELL", "LIMIT", 0.1, 70000)            # 挂单会被强平一起撤掉
        e.on_price("BTCUSDT", 55000)                                  # 跌 8.3% > 9.5%? 不到 → 还活着
        self.assertEqual(e.position("BTCUSDT")["qty"], 0.1)
        e.on_price("BTCUSDT", 54000)                                  # 跌 10% → 逐仓强平
        self.assertEqual(e.position("BTCUSDT")["qty"], 0.0)
        kinds = [ev["kind"] for ev in e.store.events()]
        self.assertIn("liquidation", kinds)
        self.assertEqual(e.open_orders(), [])
        liq = [o for o in e.all_orders() if o["origin"] == "system"]
        self.assertEqual((len(liq), liq[0]["reason"], liq[0]["status"]), (1, "LIQUIDATION", "FILLED"))
        f = engine()
        f.place("BTCUSDT", "BUY", "MARKET", 1.0)                      # 全仓：6 万名义，钱包 1 万
        f.place("ETHUSDT", "BUY", "MARKET", 0.1)
        f.on_price("BTCUSDT", 51000)                                  # 亏 9000 < 1 万 - 维持 → 活
        self.assertEqual(f.position("BTCUSDT")["qty"], 1.0)
        f.on_price("BTCUSDT", 50000)                                  # 亏 1 万 → 全仓全部强平
        self.assertEqual(f.position("BTCUSDT")["qty"], 0.0)
        self.assertEqual(f.position("ETHUSDT")["qty"], 0.0)
        self.assertGreaterEqual(f.wallet, 0.0)

    def test_funding_every_8h(self):
        e = engine(taker_fee=0.0)
        c = e.clock
        c.t = (c.t // H8) * H8 + 1000
        e.place("BTCUSDT", "BUY", "MARKET", 0.1)
        e.on_price("BTCUSDT", 60000)                                 # 第一次见到时钟：只记槽位
        self.assertEqual(e.wallet, 10000.0)
        c.t += H8 - 2000
        e.on_price("BTCUSDT", 60000)                                 # 还在同一个 8 小时槽
        self.assertEqual(e.wallet, 10000.0)
        c.t += 2000
        e.on_price("BTCUSDT", 60000)                                 # 跨过边界：多头付 0.1 × 60000 × 0.0001 = 0.6
        self.assertAlmostEqual(e.wallet, 10000.0 - 0.6)
        c.t += H8
        e.on_price("BTCUSDT", 60000)
        self.assertAlmostEqual(e.wallet, 10000.0 - 1.2)
        fund = [ev for ev in e.store.events() if ev["kind"] == "funding"]
        self.assertEqual(len(fund), 2)
        self.assertAlmostEqual(fund[0]["payment"], 0.6)


class TestPersistence(unittest.TestCase):
    def test_restart_restores_everything(self):
        d = tmpdir(self)
        st = store_mod.Store(os.path.join(d, "e.db"))
        e = Engine(st, clock=Clock())
        e.set_leverage("BTCUSDT", 7)
        e.place("BTCUSDT", "BUY", "LIMIT", 0.02, 59000, cid="keep")
        e.on_price("BTCUSDT", 58900, liquidity=0.01)
        e.set_symbol("ETHUSDT", tick=0.5)
        snap = e.snapshot()
        st.close()
        st2 = store_mod.Store(os.path.join(d, "e.db"))
        e2 = Engine(st2, clock=Clock())
        self.assertEqual(e2.get_order(cid="keep")["filled"], 0.01)
        self.assertEqual(e2.position("BTCUSDT")["qty"], 0.01)
        self.assertEqual(e2.position("BTCUSDT")["leverage"], 7)
        self.assertEqual(e2.wallet, e.wallet)
        self.assertEqual(e2.next_id, e.next_id)
        self.assertEqual(e2.mark["BTCUSDT"], 58900)
        self.assertEqual(e2.symbols["ETHUSDT"]["tick"], 0.5)
        self.assertEqual(len(e2.user_trades()), 1)
        with self.assertRaises(EngineError) as cm:                     # 幂等跨重启
            e2.place("BTCUSDT", "SELL", "LIMIT", 0.01, 61000, cid="keep")
        self.assertEqual(cm.exception.kind, "DUP_CID")
        snap2 = e2.snapshot()
        for k in ("orders", "fills", "positions", "mark", "symbols"):
            self.assertEqual(snap[k], snap2[k], k)
        n = e2.place("BTCUSDT", "SELL", "LIMIT", 0.01, 61000)
        self.assertGreater(n["id"], max(o["id"] for o in snap["orders"]))

    def test_db_unique_cid_is_last_line_of_defense(self):
        e = engine()
        e.place("BTCUSDT", "BUY", "LIMIT", 0.01, 59000, cid="x")
        e.by_cid.clear()                                                # 模拟内存查重坏了
        with self.assertRaises(store_mod.DuplicateClientId):
            e.place("BTCUSDT", "BUY", "LIMIT", 0.01, 59000, cid="x")
        self.assertEqual(e.store.counts()["orders"], 1)

    def test_set_symbol_cancels_open_orders_and_visible_delay(self):
        e = engine()
        o = e.place("BTCUSDT", "BUY", "LIMIT", 0.01, 59000)
        e.set_symbol("BTCUSDT", status="SUSPENDED")
        self.assertEqual(e.get_order(o["id"])["reason"], "SYMBOL_SUSPENDED")
        e.set_symbol("BTCUSDT", status="TRADING")
        e.place("BTCUSDT", "BUY", "LIMIT", 0.01, 59000)
        e.on_price("BTCUSDT", 58000, visible_delay_ms=500)
        now = e.clock()
        self.assertEqual(e.user_trades(visible_before=now), [])
        self.assertEqual(len(e.user_trades(visible_before=now + 500)), 1)
        self.assertEqual(len(e.user_trades("ETHUSDT")), 0)
        snap = e.snapshot()
        self.assertEqual(snap["counts"]["orders"], 2)
        self.assertIn("time", snap)


if __name__ == "__main__":
    unittest.main()
