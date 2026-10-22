"""G3 判据（账本）：手算场景逐根数值、恒等式检查能抓到状态损坏、强平合法性 / 漏强平 / 全平 / 穿仓封顶、
强平价公式、浮点数量归零、记录与配置校验、check_engine / check_vector、独立写法 ledger_alt 的对应判定。"""
import json
import math
import os
import random
import unittest

from qdcheck import gen_vectors as G
from qdcheck import ledger as L
from qdcheck import ledger_alt as LA
from qdcheck import stream as S


def T(bar, side, qty, price, fee=0.0, funding=0.0, kind="trade"):
    return {"bar": bar, "kind": kind, "side": side, "qty": qty, "price": price, "fee": fee, "funding": funding}


BASIC = [T(0, "buy", 10, 100.0, 1.0), T(1, "buy", 5, 110.0, 0.5), T(2, "sell", 8, 120.0, 0.8),
         T(3, "sell", 12, 90.0, 1.2), {"bar": 4, "kind": "funding", "funding": 2.0},
         {"bar": 4, "kind": "settlement", "price": 95.0}, T(5, "buy", 5, 80.0, 0.4)]
BASIC_CLOSES = [101.0, 112.0, 118.0, 91.0, 94.0, 85.0, 85.0]


def _close(test, a, b, tol=1e-9):
    test.assertTrue(abs(a - b) <= tol * max(1.0, abs(a), abs(b)), f"{a} vs {b}")


class TestHandScenarios(unittest.TestCase):
    def test_basic_long_add_reduce_reverse_settle(self):
        res = L.run(BASIC, BASIC_CLOSES)
        self.assertEqual(res["violations"], [])
        rows = res["rows"]
        want = [(98999.0, 10, 100.0, 0.0, 100009.0), (98448.5, 15, 1550 / 15, 0.0, 100128.5),
                (99407.7, 7, 1550 / 15, 8 * (120 - 1550 / 15), 100233.7),
                (100486.5, -5, 90.0, 40.0, 100031.5), (100484.5, -5, 95.0, 15.0, 100014.5),
                (100084.1, 0, 0.0, 90.0, 100084.1), (100084.1, 0, 0.0, 90.0, 100084.1)]
        for row, (cash, qty, avg, realized, equity) in zip(rows, want):
            _close(self, row["cash"], cash)
            self.assertEqual(row["qty"], qty)
            _close(self, row["avg_price"], avg)
            _close(self, row["realized"], realized)
            _close(self, row["equity"], equity)
        _close(self, rows[4]["unrealized"], 5.0)
        _close(self, rows[6]["fees"], 3.9)
        _close(self, rows[6]["funding"], 2.0)
        self.assertEqual(res["counters"], {"open": 1, "add": 1, "reduce": 1, "close": 1, "reverse": 1, "long": 1,
                                           "short": 1, "funding": 1, "settlement": 1, "liquidation": 0,
                                           "bankrupt_capped": 0})
        alt = LA.run(BASIC, BASIC_CLOSES)
        self.assertEqual(alt["issues"], [])
        self.assertTrue(S.compare_rows(rows, alt["rows"], 100_000.0)["ok"])

    def test_isolated_liquidation_capped_and_normal(self):
        vec = next(v for v in G.ledger_vectors() if v["name"] == "isolated_x20_liquidations")
        e = vec["expected"]
        self.assertEqual(e["liquidations"], [0, 0, 0, 1, 1, 1, 2, 2])
        _close(self, e["insurance"][3], 3000.0)                    # 5000 保证金 - 8000 亏损 => 保险补 3000
        _close(self, e["cash"][3], 4950.0)                         # 10000 - 50 - 5000
        _close(self, e["realized"][3], -5000.0)
        _close(self, e["equity"][-1], 2601.75)                     # 4950 - 23.25 - 2325
        _close(self, e["margin_used"][4], 2325.0)
        self.assertEqual(vec["records"][-1]["fee"], 75.0)          # 强平费封顶在剩余仓位权益 75
        self.assertEqual(vec["counters"]["bankrupt_capped"], 1)
        self.assertTrue(all(c >= 0 for c, q in zip(e["cash"], e["qty"]) if q == 0))
        _close(self, e["liq_price"][0], (1000 * 100.0 - 5000.0) / (1000 * (1 - 0.005)))

    def test_float_quantities_snap_to_zero(self):
        recs = [T(0, "buy", 0.1, 10.0), T(0, "buy", 0.2, 10.0), T(1, "sell", 0.3, 11.0)]
        res = L.run(recs, [10.0, 11.0])
        self.assertEqual(res["rows"][1]["qty"], 0)
        self.assertEqual(res["rows"][1]["avg_price"], 0.0)
        self.assertEqual(res["violations"], [])
        alt = LA.run(recs, [10.0, 11.0])
        self.assertEqual(alt["rows"][1]["qty"], 0)
        self.assertEqual(L.snap_qty(1e-13, 1.0), 0)
        self.assertEqual(L.snap_qty(1e-11, 1.0), 1e-11)
        rev = L.run([T(0, "buy", 0.3, 10.0), T(1, "sell", 0.1 + 0.2 + 0.5, 11.0)], [10.0, 11.0])
        self.assertAlmostEqual(rev["rows"][1]["qty"], -0.5, delta=1e-12)
        self.assertEqual(rev["rows"][1]["avg_price"], 11.0)

    def test_liq_price_is_where_position_equity_meets_maintenance(self):
        rnd = random.Random(4)
        checked = 0
        for _ in range(300):
            mode = rnd.choice(("isolated", "cross"))
            led = L.Ledger(initial_cash=10_000.0, margin_mode=mode, leverage=rnd.choice((2, 5, 20)),
                           mmr=rnd.choice((0.005, 0.05)))
            side = rnd.choice(("buy", "sell"))
            led.apply(T(0, side, rnd.randint(1, 300), 100.0 * (0.5 + rnd.random()), 1.0))
            p = led.liq_price()
            if p is None:
                continue
            checked += 1
            _close(self, led.position_equity(p), led.maint_margin(p), 1e-9)
        self.assertGreater(checked, 100)
        self.assertIsNone(L.Ledger(margin_mode="isolated", leverage=1).liq_price())
        flat = L.Ledger(margin_mode="isolated", leverage=1)
        flat.apply(T(0, "buy", 10, 100.0))
        self.assertIsNone(flat.liq_price())                           # 1 倍逐仓多头：不可能强平
        self.assertIsNone(L.Ledger().liq_price())


class TestViolationsAreDetected(unittest.TestCase):
    def _corrupt(self, attr, delta, bar=1):
        led = L.Ledger()
        led.apply(T(0, "buy", 10, 100.0, 1.0))
        led.mark(0, 101.0)
        setattr(led, attr, getattr(led, attr) + delta)
        led.mark(bar, 102.0)
        return {v["check"] for v in led.violations}

    def test_identity_checks_fire_on_corrupted_state(self):
        self.assertEqual(self._corrupt("realized", 1.0), {"I1_equity_eq_initial_realized_unrealized_fees_funding"})
        self.assertEqual(self._corrupt("cash", 1.0), {"I2_equity_eq_cash_plus_mark"})
        self.assertEqual(self._corrupt("fees", 1e-3), {"I1_equity_eq_initial_realized_unrealized_fees_funding"})
        self.assertEqual(self._corrupt("realized", 1e-7), set())      # 1e-7 < 1e-9 * 1e5：容差内
        led = L.Ledger(margin_mode="isolated", leverage=2)
        led.apply(T(0, "buy", 10, 100.0))
        led.margin += 1.0
        led.mark(0, 100.0)
        self.assertEqual({v["check"] for v in led.violations}, {"I4_margin_consistency"})
        self.assertEqual((led.violations[0]["lhs"], led.violations[0]["rhs"]), (501.0, 500.0))
        led = L.Ledger()
        led.avg = 5.0
        led.mark(0, 100.0)
        self.assertEqual({v["check"] for v in led.violations}, {"I3_flat_state"})

    def test_liquidation_rules(self):
        base = {"initial_cash": 10_000.0, "margin_mode": "isolated", "leverage": 20, "mmr": 0.005}
        prem = L.run([T(0, "buy", 1000, 100.0), T(1, "sell", 1000, 99.0, kind="liquidation")], [100.0, 99.0], **base)
        self.assertEqual([v["check"] for v in prem["violations"]], ["premature_liquidation"])
        part = L.run([T(0, "buy", 1000, 100.0), T(1, "sell", 400, 92.0, kind="liquidation")], [100.0, 92.0], **base)
        self.assertIn("liquidation_not_full", [v["check"] for v in part["violations"]])
        missed = L.run([T(0, "buy", 1000, 100.0)], [100.0, 99.0, 94.0, 93.0], **base)
        self.assertEqual([(v["bar"], v["check"]) for v in missed["violations"]],
                         [(2, "I4_missed_liquidation"), (3, "I4_missed_liquidation")])
        cross = dict(base, margin_mode="cross", leverage=3, mmr=0.05)
        cm = L.run([T(0, "buy", 290, 100.0)], [100.0, 68.0], **cross)
        self.assertEqual([v["check"] for v in cm["violations"]], ["I4_missed_liquidation"])
        none = L.run([T(0, "buy", 290, 100.0), T(1, "sell", 290, 10.0, kind="liquidation")], [100.0, 10.0],
                     **dict(base, margin_mode="none", leverage=1))
        self.assertEqual(none["violations"], [])                     # 无保证金模式不判强平合法性
        self.assertEqual([w["check"] for w in none["warnings"]], ["bankrupt"])
        bk = L.run([T(0, "buy", 290, 100.0), T(1, "sell", 290, 50.0, 5.0, kind="liquidation")], [100.0, 50.0], **cross)
        self.assertEqual([w["check"] for w in bk["warnings"]], ["bankrupt"])
        self.assertEqual(bk["violations"], [])
        alt = LA.run([T(0, "buy", 1000, 100.0), T(1, "sell", 1000, 99.0, kind="liquidation")], [100.0, 99.0], **base)
        self.assertEqual(alt["issues"], [(1, "premature_liquidation")])
        alt = LA.run([T(0, "buy", 1000, 100.0), T(1, "sell", 400, 92.0, kind="liquidation")], [100.0, 92.0], **base)
        self.assertIn((1, "liquidation_not_full"), alt["issues"])
        alt = LA.run([T(0, "buy", 1000, 100.0)], [100.0, 99.0, 94.0], **base)
        self.assertEqual(alt["issues"], [(2, "missed_liquidation")])

    def test_cross_bankrupt_warning_uses_remaining_position(self):
        cfg = {"initial_cash": 10_000.0, "margin_mode": "cross", "leverage": 3, "mmr": 0.05}
        res = L.run([T(0, "sell", 100, 100.0), T(1, "buy", 10, 300.0, kind="liquidation")], [100.0, 300.0], **cfg)
        self.assertEqual([w["check"] for w in res["warnings"]], ["bankrupt"])
        self.assertAlmostEqual(res["warnings"][0]["equity"], 17_000.0 - 90 * 300.0)
        self.assertIn("liquidation_not_full", [v["check"] for v in res["violations"]])

    def test_wallet_erosion_caps_loss_at_wallet(self):
        """资金费把钱包侵蚀到低于逐仓保证金：有效保证金 = 钱包，强平后余额恰为 0 而不是负数。"""
        cfg = {"initial_cash": 1_000.0, "margin_mode": "isolated", "leverage": 1, "mmr": 0.005}
        recs = [T(0, "sell", 9, 100.0), {"bar": 1, "kind": "funding", "funding": 300.0}]
        closes = [100.0, 100.0, 180.0]
        led = L.Ledger(**cfg)
        for t, c in enumerate(closes):
            for r in (x for x in recs if x["bar"] == t):
                led.apply(r)
            if t == 2:
                pe = led.position_equity(c)
                self.assertAlmostEqual(led.effective_margin(), 700.0)
                self.assertLess(pe, led.maint_margin(c))
                led.apply(T(2, "buy", 9, c, kind="liquidation"))
            led.mark(t, c)
        self.assertEqual(led.violations, [])
        self.assertAlmostEqual(led.cash, 0.0, delta=1e-9)
        self.assertAlmostEqual(led.insurance, 9 * 80 - 700.0, delta=1e-9)
        alt = LA.run(recs + [T(2, "buy", 9, 180.0, kind="liquidation")], closes, **cfg)
        self.assertEqual(alt["issues"], [])
        self.assertAlmostEqual(alt["rows"][-1]["cash"], 0.0, delta=1e-9)


class TestEdgeCases(unittest.TestCase):
    """多次反手 / 同根开平 / 数量为 0 / 手续费为 0 / 强平恰在边界。"""

    def _both(self, recs, closes, **cfg):
        ref = L.run(recs, closes, **cfg)
        alt = LA.run(recs, closes, **cfg)
        self.assertEqual(ref["violations"], [])
        self.assertEqual(alt["issues"], [])
        self.assertTrue(S.compare_rows(ref["rows"], alt["rows"], cfg.get("initial_cash", 1e5))["ok"])
        return ref

    def test_multiple_reversals(self):
        recs = [T(0, "buy", 10, 100.0, 1.0), T(1, "sell", 25, 101.0, 1.0), T(2, "buy", 40, 99.0, 1.0),
                T(3, "sell", 55, 102.0, 1.0), T(4, "buy", 70, 98.0, 1.0), T(5, "sell", 30, 100.0, 1.0)]
        ref = self._both(recs, [100.0, 101.0, 99.0, 102.0, 98.0, 100.0])
        self.assertEqual([r["qty"] for r in ref["rows"]], [10, -15, 25, -30, 40, 10])
        self.assertEqual([r["avg_price"] for r in ref["rows"]], [100.0, 101.0, 99.0, 102.0, 98.0, 98.0])
        self.assertEqual(ref["counters"]["reverse"], 4)
        realized = 10 * 1 + 15 * (101 - 99) + 25 * (102 - 99) + 30 * (102 - 98) + 30 * (100 - 98)
        self.assertAlmostEqual(ref["rows"][-1]["realized"], realized)
        self.assertAlmostEqual(ref["rows"][-1]["equity"], 1e5 + realized - 6.0 + 10 * (100 - 98))

    def test_open_and_close_in_the_same_bar(self):
        recs = [T(0, "buy", 10, 100.0, 0.5), T(0, "sell", 10, 101.0, 0.5), T(1, "sell", 5, 50.0), T(1, "buy", 5, 49.0)]
        ref = self._both(recs, [97.0, 52.0])
        rows = ref["rows"]
        self.assertEqual((rows[0]["qty"], rows[0]["avg_price"], rows[0]["unrealized"]), (0, 0.0, 0.0))
        self.assertAlmostEqual(rows[0]["cash"], 1e5 + 10.0 - 1.0)
        self.assertAlmostEqual(rows[0]["equity"], rows[0]["cash"])
        self.assertAlmostEqual(rows[1]["realized"], 10.0 + 5.0)
        self.assertEqual(ref["counters"], {**ref["counters"], "open": 2, "close": 2, "long": 1, "short": 1})

    def test_zero_quantity_trade_is_rejected(self):
        for rec in (T(0, "buy", 0, 100.0), T(0, "sell", 0.0, 100.0), T(0, "buy", 0, 100.0, kind="liquidation")):
            with self.assertRaises(L.LedgerError, msg=rec):
                L.normalize_record(rec)
            with self.assertRaises(L.LedgerError):
                L.run([rec], [100.0])
        # 数量为 0 的“成交”不是成交：引擎若产出这样的记录，参考账本拒绝而不是默默跳过

    def test_zero_fees_everywhere(self):
        recs = [T(0, "buy", 10, 100.0, 0.0), T(1, "sell", 10, 110.0, 0.0), T(1, "sell", 3, 110.0, 0.0),
                {"bar": 2, "kind": "funding", "funding": 0.0}, T(3, "buy", 3, 100.0, 0.0)]
        ref = self._both(recs, [100.0, 110.0, 105.0, 100.0])
        self.assertEqual([r["fees"] for r in ref["rows"]], [0.0, 0.0, 0.0, 0.0])
        self.assertAlmostEqual(ref["rows"][-1]["equity"], 1e5 + 100.0 + 30.0)
        self.assertAlmostEqual(ref["rows"][-1]["cash"], ref["rows"][-1]["equity"])

    def test_liquidation_exactly_at_the_boundary(self):
        """仓位权益 == 维持保证金：强平与不强平都在容差带内被接受。"""
        cfg = {"initial_cash": 10_000.0, "margin_mode": "isolated", "leverage": 10, "mmr": 0.1}
        # 多头 100 @ 100，保证金 1000；p 使 1000 + 100(p-100) == 0.1*100*p  =>  p = 9000/90 = 100  (恰好开仓价)
        led = L.Ledger(**cfg)
        led.apply(T(0, "buy", 100, 100.0))
        self.assertAlmostEqual(led.position_equity(100.0), led.maint_margin(100.0))
        self.assertAlmostEqual(led.liq_price(), 100.0)
        self.assertEqual(L.run([T(0, "buy", 100, 100.0)], [100.0], **cfg)["violations"], [])                  # 不强平：不算漏
        with_liq = L.run([T(0, "buy", 100, 100.0), T(0, "sell", 100, 100.0, kind="liquidation")], [100.0], **cfg)
        self.assertEqual(with_liq["violations"], [])                                                           # 强平：不算提前
        self.assertEqual(with_liq["rows"][0]["cash"], 10_000.0)
        just_above = L.run([T(0, "buy", 100, 100.0), T(0, "sell", 100, 100.0 + 1e-3, kind="liquidation")], [100.0], **cfg)
        self.assertEqual([v["check"] for v in just_above["violations"]], ["premature_liquidation"])
        just_below = L.run([T(0, "buy", 100, 100.0)], [100.0 - 1e-3], **cfg)
        self.assertEqual([v["check"] for v in just_below["violations"]], ["I4_missed_liquidation"])
        alt_ok = LA.run([T(0, "buy", 100, 100.0), T(0, "sell", 100, 100.0, kind="liquidation")], [100.0], **cfg)
        self.assertEqual(alt_ok["issues"], [])
        self.assertEqual(LA.run([T(0, "buy", 100, 100.0)], [100.0 - 1e-3], **cfg)["issues"], [(0, "missed_liquidation")])

    def test_second_implementation_does_not_import_the_first(self):
        import ast
        for alt in ("ledger_alt", "indicators_alt"):
            with open(os.path.join(os.path.dirname(L.__file__), alt + ".py"), encoding="utf-8") as f:
                tree = ast.parse(f.read())
            mods = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    mods |= {a.name for a in node.names}
                elif isinstance(node, ast.ImportFrom):
                    mods.add(node.module)
            self.assertNotIn("qdcheck.ledger", mods)
            self.assertFalse({m for m in mods if "ledger" in m and "alt" not in m}, (alt, mods))
        # indicators_alt 只复用 indicators_ref.resolve（口径字符串与参数解析，不含计算）与 LINES / apply_warmup
        import qdcheck.indicators_alt as IA
        import inspect
        src = inspect.getsource(IA)
        self.assertIn("from qdcheck.indicators_ref import LINES, apply_warmup, resolve", src)
        self.assertNotIn("indicators_ref.ma_series", src)
        self.assertNotIn("indicators_ref.compute", src)

    def test_check_returns_violation_list(self):
        self.assertEqual(L.check(BASIC, BASIC_CLOSES), [])
        viol = L.check([T(0, "buy", 1000, 100.0)], [100.0, 94.0], initial_cash=10_000.0, margin_mode="isolated",
                       leverage=20, mmr=0.005)
        self.assertEqual(len(viol), 1)
        self.assertEqual(set(viol[0]), {"bar", "check", "lhs", "rhs", "detail"})
        self.assertEqual((viol[0]["bar"], viol[0]["check"]), (1, "I4_missed_liquidation"))

    def test_sign_conventions(self):
        rows = L.run([T(0, "buy", 2, 10.0, 0.5), T(1, "sell", 5, 12.0, -0.25), {"bar": 2, "kind": "funding", "funding": 1.5},
                      {"bar": 3, "kind": "funding", "funding": -0.5}], [10.0, 12.0, 12.0, 12.0])["rows"]
        self.assertAlmostEqual(rows[0]["cash"], 1e5 - 20.0 - 0.5)          # 买入：现金减少 qty*price + fee
        self.assertEqual(rows[1]["qty"], -3)                                # 空头持仓为负
        self.assertAlmostEqual(rows[1]["cash"], rows[0]["cash"] + 60.0 + 0.25)   # 卖出加现金；负手续费 = 返佣
        self.assertAlmostEqual(rows[1]["fees"], 0.25)
        self.assertAlmostEqual(rows[2]["cash"], rows[1]["cash"] - 1.5)     # funding 正数 = 账户支付
        self.assertAlmostEqual(rows[3]["cash"], rows[2]["cash"] + 0.5)     # 负数 = 账户收取
        self.assertAlmostEqual(rows[3]["funding"], 1.0)


class TestValidation(unittest.TestCase):
    def test_bad_records(self):
        bad = ["x", {"bar": -1}, {"bar": 1.0}, {"bar": True}, {"bar": 0, "kind": "swap"}, {"bar": 0, "side": "long"},
               {"bar": 0, "side": True, "qty": 1, "price": 1.0}, T(0, "buy", 0, 1.0), T(0, "none", 1, 1.0),
               T(0, "buy", 1, 0.0), T(0, "buy", 1, math.nan), T(0, "buy", 1, 1.0, fee=math.inf),
               T(0, "buy", "1", 1.0), {"bar": 0, "kind": "funding", "side": "buy"},
               {"bar": 0, "kind": "settlement", "price": None}, {"bar": 0, "kind": "funding", "qty": 1},
               T(0, "buy", 1, 1.0, funding=True)]
        for rec in bad:
            with self.assertRaises(L.LedgerError, msg=rec):
                L.normalize_record(rec)
        self.assertEqual(L.normalize_record({"bar": 3, "kind": "funding", "funding": -2})[6], "funding")
        self.assertEqual(L.normalize_record(T(0, -1, 2, 5.0))[1], -1)

    def test_bar_must_be_a_non_negative_int_even_for_valid_trades(self):
        for bar in (-1, True, 1.0, "1", None):
            with self.assertRaises(L.LedgerError, msg=bar):
                L.normalize_record(T(bar, "buy", 1, 1.0))
        self.assertEqual(L.normalize_record(T(0, "buy", 1, 1.0))[:4], (0, 1, 1, 1.0))

    def test_bad_config_and_order(self):
        for kw in ({"initial_cash": 0}, {"initial_cash": math.inf}, {"margin_mode": "portfolio"},
                   {"margin_mode": "isolated", "leverage": 25}, {"margin_mode": "isolated", "leverage": 0.5},
                   {"leverage": -1}, {"mmr": 1.0}, {"mmr": -0.1}, {"rel_tol": "x"}):
            with self.assertRaises(L.LedgerError, msg=kw):
                L.Ledger(**kw)
        with self.assertRaises(L.LedgerError):
            L.run([T(1, "buy", 1, 1.0), T(0, "buy", 1, 1.0)], [1.0, 1.0])
        with self.assertRaises(L.LedgerError):
            L.run([T(5, "buy", 1, 1.0)], [1.0, 1.0])
        led = L.Ledger()
        with self.assertRaises(L.LedgerError):
            led.apply(T(1, "buy", 1, 1.0))
        with self.assertRaises(L.LedgerError):
            led.mark(1, 1.0)
        with self.assertRaises(L.LedgerError):
            led.mark(0, 0.0)
        self.assertEqual(L.run([], [])["rows"], [])


class TestConsumerApis(unittest.TestCase):
    def test_check_engine(self):
        eq = [r["equity"] for r in L.run(BASIC, BASIC_CLOSES)["rows"]]
        ok = L.check_engine(BASIC, BASIC_CLOSES, eq)
        self.assertTrue(ok["ok"])
        self.assertEqual((ok["bars"], ok["first_mismatch"]), (7, None))
        bad = list(eq)
        bad[3] += 1e-3
        rep = L.check_engine(BASIC, BASIC_CLOSES, bad)
        self.assertFalse(rep["ok"])
        self.assertEqual((rep["first_mismatch"]["bar"], rep["first_mismatch"]["actual"]), (3, bad[3]))
        self.assertTrue(L.check_engine(BASIC, BASIC_CLOSES, [e + 1e-5 for e in eq])["ok"])
        self.assertEqual(L.check_engine(BASIC, BASIC_CLOSES, eq[:-1])["first_mismatch"]["field"], "length")
        prem = L.check_engine([T(0, "buy", 1000, 100.0), T(1, "sell", 1000, 99.0, kind="liquidation")],
                              [100.0, 99.0], [10_000.0, 9_000.0], initial_cash=10_000.0, margin_mode="isolated",
                              leverage=20)
        self.assertFalse(prem["ok"])
        self.assertIsNone(prem["first_mismatch"])

    def test_check_vector_on_committed_files(self):
        names = [f for f in sorted(os.listdir(G.VECTOR_DIR)) if f.startswith("ledger_")]
        self.assertGreaterEqual(len(names), 7)
        for name in names:
            with open(os.path.join(G.VECTOR_DIR, name), encoding="utf-8") as f:
                vec = json.load(f)
            ref = L.run(vec["records"], vec["closes"], **vec["config"])
            self.assertEqual(ref["violations"], [])
            self.assertTrue(L.check_vector(vec, L.columns(ref["rows"]))["ok"], name)
            alt = LA.run(vec["records"], vec["closes"], **vec["config"])
            self.assertTrue(L.check_vector(vec, L.columns(alt["rows"]))["ok"], name)
            eq = list(vec["expected"]["equity"])
            eq[len(eq) // 2] += 1e-6 * vec["config"]["initial_cash"]      # 远大于 1e-9 * 初始资金
            res = L.check_vector(vec, {"equity": eq})
            self.assertEqual((res["ok"], res["field"], res["bar"]), (False, "equity", len(eq) // 2))
        vec = {"config": {"initial_cash": 100.0}, "expected": {"qty": [1, 2], "liq_price": [None, 5.0],
                                                                   "fills": [0, 1], "cash": [1.0, 2.0]}}
        self.assertFalse(L.check_vector(vec, {"qty": [1, 2.5]})["ok"])
        self.assertTrue(L.check_vector(vec, {"qty": [1, 2.0]})["ok"])
        self.assertFalse(L.check_vector(vec, {"liq_price": [5.0, 5.0]})["ok"])
        self.assertFalse(L.check_vector(vec, {"fills": [0, True]})["ok"])
        self.assertFalse(L.check_vector(vec, {"cash": [1.0, "2"]})["ok"])
        self.assertEqual(L.check_vector(vec, {"bogus": [1]})["detail"], "unknown field")
        self.assertIn("length", L.check_vector(vec, {"qty": [1]})["detail"])


class TestAltPieces(unittest.TestCase):
    def test_exact_sum(self):
        s = LA.ExactSum()
        for x in (1e100, 1.0, -1e100, 1e-20):
            s.add(x)
        self.assertEqual(s.value(), 1.0 + 1e-20)
        self.assertEqual(LA.ExactSum(0.1).value(), 0.1)
        acc = LA.ExactSum()
        for _ in range(10):
            acc.add(0.1)
        self.assertEqual(acc.value(), 1.0)

    def test_alt_accepts_integer_sides_and_snaps_like_ledger(self):
        recs = [dict(r, side={"buy": 1, "sell": -1}.get(r.get("side"), 0)) for r in BASIC]
        self.assertTrue(S.compare_rows(LA.run(recs, BASIC_CLOSES)["rows"], LA.run(BASIC, BASIC_CLOSES)["rows"], 1e5)["ok"])
        tiny = [T(0, "buy", 0.5, 10.0), T(1, "sell", 0.5 - 1.2e-12, 10.0)]    # 残量 1.2e-12 > 1e-12：不归零
        ref, alt = L.run(tiny, [10.0, 10.0]), LA.run(tiny, [10.0, 10.0])
        self.assertNotEqual(ref["rows"][1]["qty"], 0)
        self.assertNotEqual(alt["rows"][1]["qty"], 0)
        self.assertTrue(S.compare_rows(ref["rows"], alt["rows"], 1e5)["ok"])
        zeroed = [dict(r) for r in alt["rows"]]
        zeroed[1]["qty"] = 0.0
        self.assertEqual(S.compare_rows(ref["rows"], zeroed, 1e5)["field"], "qty")   # 数量不用金额尺度
        snap = [T(0, "buy", 0.5, 10.0), T(1, "sell", 0.5 - 4e-13, 10.0)]
        self.assertEqual(LA.run(snap, [10.0, 10.0])["rows"][1]["qty"], 0)
        self.assertEqual(L.run(snap, [10.0, 10.0])["rows"][1]["qty"], 0)

    def test_compare_rows(self):
        rows = L.run(BASIC, BASIC_CLOSES)["rows"]
        alt = LA.run(BASIC, BASIC_CLOSES)["rows"]
        self.assertTrue(S.compare_rows(rows, alt, 1e5)["ok"])
        for field, value in (("fills", 99), ("qty", 8), ("cash", rows[2]["cash"] + 1e-3), ("liq_price", 1.0),
                             ("avg_price", rows[2]["avg_price"] * (1 + 1e-8))):
            bad = [dict(r) for r in alt]
            bad[2][field] = value
            res = S.compare_rows(rows, bad, 1e5)
            self.assertEqual((res["ok"], res["bar"], res["field"]), (False, 2, field), field)
        self.assertEqual(S.compare_rows(rows, alt[:-1], 1e5)["field"], "length")


if __name__ == "__main__":
    unittest.main()
