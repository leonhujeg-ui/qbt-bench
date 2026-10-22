"""G3 判据（订单流）：同 seed 同结果（含跨哈希种子）、事件覆盖（下单 / 撤单 / 到期 / 部分成交 / 拒单 / 跨日结算 /
资金费 / 强平 / 穿仓封顶 / 反手 / 部分平仓 / 加仓）、全部随机流上两份账本逐根一致且零违背、性能（按比例折算 3 分钟）、
脚本化场景自动强平、qbt 真实用例冒烟。"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

from qdcheck import ledger as L
from qdcheck import stream as S
from qdcheck.tests._util import REPO, UNDER_COVERAGE, captured, scale


class TestStreams(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        n_streams, n_bars = scale(40, 12), scale(3000, 1500)
        t0 = time.perf_counter()
        cls.streams = [S.generate(seed, n_bars) for seed in range(n_streams)]
        cls.checks = [S.check_stream(st) for st in cls.streams]
        cls.seconds = time.perf_counter() - t0
        cls.n_streams, cls.n_bars = n_streams, n_bars

    def test_zero_violations_and_bar_by_bar_agreement(self):
        for st, res in zip(self.streams, self.checks):
            self.assertEqual(res["n_violations"], 0, (st["seed"], res["violations"]))
            self.assertEqual(res["n_alt_issues"], 0, (st["seed"], res["alt_issues"]))
            self.assertTrue(res["row_compare"]["ok"], (st["seed"], res["row_compare"]))
            self.assertEqual(res["row_compare"]["checked"], self.n_bars * len(L.ROW_FIELDS))
            self.assertEqual(st["sim_violations"], [])

    def test_event_coverage(self):
        stats, counters, modes = {}, {}, set()
        for st, res in zip(self.streams, self.checks):
            for k, v in st["stats"].items():
                stats[k] = stats.get(k, 0) + v
            for k, v in res["counters"].items():
                counters[k] = counters.get(k, 0) + v
            modes.add(st["config"]["margin_mode"])
        for key in ("submitted", "market", "limit", "trades", "partial_fills", "cancelled_user", "expired",
                    "rejected_margin", "funding_records", "settlements", "liquidations"):
            self.assertGreater(stats[key], 0, key)
        for key in ("open", "add", "reduce", "close", "reverse", "long", "short", "liquidation", "bankrupt_capped"):
            self.assertGreater(counters[key], 0, key)
        self.assertEqual(modes, {"isolated", "cross", "none"})
        self.assertEqual(stats["liquidations"], counters["liquidation"])
        lev = {st["config"]["leverage"] for st in self.streams if st["config"]["margin_mode"] == "isolated"}
        self.assertTrue(lev & {10, 20} and lev & {1, 2})

    def test_records_are_well_formed(self):
        for st in self.streams[:5]:
            last = -1
            for rec in st["records"]:
                L.normalize_record(rec)
                self.assertGreaterEqual(rec["bar"], last)
                last = rec["bar"]
                if rec["kind"] in ("trade", "liquidation"):
                    self.assertIsInstance(rec["qty"], int)
            self.assertEqual(len(st["closes"]), self.n_bars)

    def test_performance_scaled_to_200x3000_within_3_minutes(self):
        if UNDER_COVERAGE:
            self.skipTest("timing is meaningless under sys.monitoring")
        per_bar = self.seconds / (self.n_streams * self.n_bars)
        self.assertLess(per_bar * 200 * 3000, 180.0)


class TestDeterminism(unittest.TestCase):
    def test_same_seed_same_stream(self):
        a, b = S.generate(7, 600), S.generate(7, 600)
        self.assertEqual(a, b)
        self.assertNotEqual(S.generate(8, 600)["records"], a["records"])
        self.assertEqual(S.profile(7), S.profile(7))

    def test_identical_across_hash_seeds(self):
        code = ("import json; from qdcheck import stream as S; st = S.generate(5, 800); "
                "print(json.dumps({'r': st['records'], 's': st['stats']}, sort_keys=True))")
        outs = []
        for hs in ("0", "4242"):
            env = dict(os.environ, PYTHONHASHSEED=hs, PYTHONPATH=REPO)
            outs.append(subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True,
                                       text=True, check=True).stdout)
        self.assertEqual(outs[0], outs[1])
        self.assertGreater(len(json.loads(outs[0])["r"]), 10)


class TestScenariosAndCli(unittest.TestCase):
    def test_scripted_inserts_liquidations(self):
        cfg = {"initial_cash": 10_000.0, "margin_mode": "isolated", "leverage": 20, "mmr": 0.005}
        recs = S.scripted(cfg, [100.0, 96.0, 92.0], {0: [{"side": "buy", "qty": 1000, "price": 100.0}]})
        self.assertEqual([r["kind"] for r in recs], ["trade", "liquidation"])
        self.assertEqual((recs[1]["bar"], recs[1]["qty"], recs[1]["price"], recs[1]["fee"]), (2, 1000, 92.0, 0.0))
        none = dict(cfg, margin_mode="none", leverage=1)
        self.assertEqual(len(S.scripted(none, [100.0, 1.0], {0: [{"side": "buy", "qty": 1000, "price": 100.0}]})), 1)
        names = [s[0] for s in S.ledger_scenarios()]
        self.assertEqual(len(names), 7)
        self.assertEqual(len(set(names)), 7)

    def test_run_many_and_cli(self):
        rep = S.run_many(3, 500, seed0=20)
        self.assertEqual((rep["n_streams"], rep["total_bars"], rep["identity_violations"], rep["row_mismatches"]),
                         (3, 1500, 0, 0))
        self.assertEqual(rep["values_compared"], 1500 * len(L.ROW_FIELDS))
        self.assertEqual(len(rep["per_stream"]), 3)
        out = os.path.join(tempfile.mkdtemp(), "g3.json")
        with captured() as (stdout, _err):
            self.assertEqual(S.main(["--streams", "2", "--bars", "300", "--seed", "3", "--out", out]), 0)
        self.assertIn('"identity_violations": 0', stdout.getvalue())
        with open(out) as f:
            text = f.read()
        obj = json.loads(text)
        self.assertEqual(obj["n_streams"], 2)
        self.assertEqual(text, json.dumps(obj, indent=1, sort_keys=True))

    def test_check_stream_reports_tampering(self):
        st = S.generate(3, 800)
        k = next(i for i, r in enumerate(st["records"]) if r["kind"] == "trade")
        bad = dict(st, records=[dict(r) for r in st["records"]])
        bad["records"][k]["price"] *= 1.0001
        res = S.check_stream(bad)
        self.assertTrue(res["row_compare"]["ok"])           # 两份账本对同一成交流仍一致：篡改的是输入，不是账本
        liq = next((s for s in range(60) if S.profile(s)[0]["margin_mode"] == "isolated"
                    and S.generate(s, 1500)["stats"]["liquidations"]), None)
        self.assertIsNotNone(liq)
        st = S.generate(liq, 1500)
        dropped = dict(st, records=[r for r in st["records"] if r["kind"] != "liquidation"])
        res = S.check_stream(dropped)
        self.assertGreater(res["n_violations"], 0)
        self.assertGreater(res["n_alt_issues"], 0)


class TestQbtRealCase(unittest.TestCase):
    def test_qbt_fills_reconcile_with_reference_ledger(self):
        from qdcheck.tools import qbt_ledger_check as Q
        rows = Q.run(n=scale(4000, 1500), seeds=(1,))
        self.assertEqual(len(rows), 18)
        self.assertTrue(all(r["ok"] for r in rows), [r for r in rows if not r["ok"]][:2])
        self.assertTrue(any(r["config"] == "levered_funding" and r["records"] > r["fills"] for r in rows))


NONE = {"initial_cash": 100_000.0, "margin_mode": "none", "leverage": 1, "mmr": 0.005}
BARS = {"open": [100.0, 100.0, 100.0], "high": [101.0, 102.0, 101.0], "low": [99.0, 98.0, 99.0],
        "close": [100.0, 100.5, 100.0], "volume": [1000.0, 50.0, 0.0]}


def sim_with(cfg=None, bars=None, **kw):
    s = S._Sim(0, 3, cfg or NONE, dict(S.SIM_DEFAULTS, **kw), {})
    s.bars = {k: list(v) for k, v in (bars or BARS).items()}
    return s


def scripted(s, values):
    it = iter(values)
    s.rnd = lambda: next(it)
    return it


def trade(s, t, side, qty, price):
    s.led.apply({"bar": t, "kind": "trade", "side": side, "qty": qty, "price": price, "fee": 0.0, "funding": 0.0})


class TestSimulatorPieces(unittest.TestCase):
    def test_fill_prices(self):
        s = sim_with()
        mk = lambda side, otype, limit=None: S._Order(1, side, 5, otype, limit, 0, 9)
        self.assertEqual(s._fill_price(mk(1, "market"), 100.0, 102.0, 98.0), 100.0 * (1 + 0.0005))
        self.assertEqual(s._fill_price(mk(-1, "market"), 100.0, 102.0, 98.0), 100.0 * (1 - 0.0005))
        self.assertEqual(s._fill_price(mk(1, "limit", 98.5), 100.0, 102.0, 98.0), 98.5)
        self.assertEqual(s._fill_price(mk(1, "limit", 98.0), 100.0, 102.0, 98.0), 98.0)
        self.assertEqual(s._fill_price(mk(1, "limit", 101.0), 100.0, 102.0, 98.0), 100.0)
        self.assertIsNone(s._fill_price(mk(1, "limit", 97.9), 100.0, 102.0, 98.0))
        self.assertEqual(s._fill_price(mk(-1, "limit", 101.5), 100.0, 102.0, 98.0), 101.5)
        self.assertEqual(s._fill_price(mk(-1, "limit", 102.0), 100.0, 102.0, 98.0), 102.0)
        self.assertEqual(s._fill_price(mk(-1, "limit", 99.0), 100.0, 102.0, 98.0), 100.0)
        self.assertIsNone(s._fill_price(mk(-1, "limit", 102.1), 100.0, 102.0, 98.0))

    def test_matching_participation_partial_fills_expiry(self):
        s = sim_with()
        trade(s, 0, "buy", 1, 100.0)
        s.led.mark(0, 100.0)
        s.orders = [S._Order(1, 1, 4, "market", None, 0, 9), S._Order(2, 1, 2, "market", None, 0, 9),
                    S._Order(3, -1, 7, "limit", 101.5, 0, 0), S._Order(4, 1, 3, "limit", 98.5, 0, 1)]
        s._match(1)                                         # 本根上限 int(50 * 0.1) = 5，先到先得
        recs = [(r["order_id"], r["qty"], r["price"]) for r in s.records]
        self.assertEqual(recs, [(1, 4, 100.0 * 1.0005), (2, 1, 100.0 * 1.0005)])
        self.assertAlmostEqual(s.records[0]["fee"], 0.0005 * 4 * 100.0 * 1.0005)
        self.assertEqual((s.stats["trades"], s.stats["partial_fills"], s.stats["expired"]), (2, 1, 1))
        self.assertEqual([o.oid for o in s.orders], [2, 4])
        self.assertEqual(s.orders[0].remaining, 1)
        s.led.mark(1, 100.5)
        s._match(2)                                         # 停牌（量 0）：不成交
        self.assertEqual(len(s.records), 2)
        s2 = sim_with()
        s2.orders = [S._Order(1, 1, 6, "market", None, 0, 9)]
        s2.led.mark(0, 100.0)
        s2._match(1)
        self.assertEqual((s2.orders[0].remaining, s2.stats["partial_fills"]), (1, 1))
        self.assertEqual(s2.records[0]["order_id"], 1)

    def test_affordability(self):
        iso = sim_with({"initial_cash": 10_000.0, "margin_mode": "isolated", "leverage": 10, "mmr": 0.005})
        self.assertEqual(iso._affordable(1, 2000, 100.0, 0.0), 1000)
        self.assertEqual(iso._affordable(1, 2000, 100.0, 0.001), int(10_000 / (10 + 0.1)))
        trade(iso, 0, "buy", 500, 100.0)
        self.assertEqual(iso._affordable(-1, 300, 100.0, 0.0), 300)
        self.assertEqual(iso._affordable(-1, 800, 100.0, 0.0), 800)
        self.assertEqual(iso._affordable(-1, 1600, 100.0, 0.0), 1000)
        self.assertEqual(iso._affordable(1, 900, 100.0, 0.0), 500)
        cross = sim_with({"initial_cash": 10_000.0, "margin_mode": "cross", "leverage": 2, "mmr": 0.05})
        self.assertEqual(cross._affordable(1, 500, 100.0, 0.0), 200)
        trade(cross, 0, "buy", 100, 100.0)
        self.assertEqual(cross._affordable(1, 500, 100.0, 0.0), 100)
        self.assertEqual(cross._affordable(-1, 300, 100.0, 0.0), 300)
        self.assertEqual(cross._affordable(-1, 500, 100.0, 0.0), 300)
        none = sim_with()
        self.assertEqual(none._affordable(1, 10_000, 100.0, 0.0), 3000)
        poor = sim_with({"initial_cash": 100.0, "margin_mode": "isolated", "leverage": 1, "mmr": 0.005})
        poor.orders = [S._Order(1, 1, 10, "market", None, 0, 9)]
        poor.led.mark(0, 100.0)
        poor._match(1)
        self.assertEqual((poor.stats["rejected_margin"], poor.orders, poor.records), (1, [], []))

    def test_day_end_and_liquidation(self):
        s = sim_with(settlement=True, funding=True)
        s.led.mark(0, 100.0)
        s._day_end(1, 105.0)
        self.assertEqual(s.records, [])                     # 无仓：不结算、不收资金费
        trade(s, 1, "buy", 10, 100.0)
        s.funding_rate = 0.001
        scripted(s, [0.75])
        s._day_end(1, 105.0)
        self.assertEqual([r["kind"] for r in s.records], ["settlement", "funding"])
        self.assertEqual(s.records[0]["price"], 105.0)
        self.assertAlmostEqual(s.records[1]["funding"], 0.001 * 10 * 105.0)
        self.assertAlmostEqual(s.funding_rate, 0.5 * 0.0005)
        self.assertEqual((s.stats["settlements"], s.stats["funding_records"]), (1, 1))
        iso = sim_with({"initial_cash": 10_000.0, "margin_mode": "isolated", "leverage": 20, "mmr": 0.005})
        trade(iso, 0, "buy", 1000, 100.0)
        iso.orders = [S._Order(1, 1, 5, "market", None, 0, 9)]
        iso._liquidation_check(0, 99.0)
        self.assertEqual(iso.records, [])
        iso._liquidation_check(0, 92.0)
        rec = iso.records[-1]
        self.assertEqual((rec["kind"], rec["side"], rec["qty"], rec["price"], rec["fee"]),
                         ("liquidation", "sell", 1000, 92.0, 0.0))
        self.assertEqual((iso.orders, iso.stats["cancelled_by_liquidation"], iso.stats["liquidations"]), ([], 1, 1))
        mild = sim_with({"initial_cash": 10_000.0, "margin_mode": "isolated", "leverage": 20, "mmr": 0.05})
        trade(mild, 0, "buy", 1000, 100.0)
        mild._liquidation_check(0, 96.0)                    # 仓位权益 1000 <= 维持保证金 4800：强平费 480 未触顶
        self.assertAlmostEqual(mild.records[-1]["fee"], 480.0)
        capped = sim_with({"initial_cash": 10_000.0, "margin_mode": "isolated", "leverage": 20, "mmr": 0.05})
        trade(capped, 0, "buy", 1000, 100.0)
        capped._liquidation_check(0, 95.2)                  # 仓位权益约 200 < 强平费 476：封顶在 200
        self.assertAlmostEqual(capped.records[-1]["fee"], 200.0, delta=1e-6)
        flat = sim_with(dict(NONE, margin_mode="cross", leverage=2))
        flat._liquidation_check(0, 1.0)
        self.assertEqual(flat.records, [])

    def test_new_orders_scripted(self):
        s = sim_with()
        scripted(s, [0.0, 0.5, 0.3, 0.2, 0.9, 0.5, 0.5])
        s._new_orders(0, 100.0)
        o = s.orders[0]
        self.assertEqual((o.oid, o.side, o.remaining, o.otype, o.created, o.expire), (1, 1, 640, "limit", 0, 21))
        self.assertAlmostEqual(o.limit, 100.0 * (1 - 0.01 * 0.5))
        self.assertEqual((s.next_id, s.stats["submitted"], s.stats["limit"]), (2, 1, 1))
        scripted(s, [0.0, 0.99])                            # 第一笔挂单被撤；本根不再下单
        s._new_orders(0, 100.0)
        self.assertEqual((s.orders, s.stats["cancelled_user"]), ([], 1))
        trade(s, 0, "buy", 10, 100.0)
        cases = [([0.0, 0.5, 0.5, 0.4, 0.2, 0.0], -1, 4, "reduce"), ([0.0, 0.5, 0.7, 0.2, 0.0], -1, 10, "close"),
                 ([0.0, 0.5, 0.9, 0.2, 0.0], -1, 10 + 640, "reverse"), ([0.0, 0.5, 0.1, 0.2, 0.0], 1, 640, "add")]
        for draws, side, qty, _name in cases:
            s.orders = []
            scripted(s, draws)
            s._new_orders(1, 100.0)
            o = s.orders[0]
            self.assertEqual((o.side, o.remaining, o.otype), (side, qty, "market"), _name)
            self.assertEqual(o.expire, 2)
        scripted(s, [0.99])
        s.orders = []
        s._new_orders(2, 100.0)                             # 停牌那根不下单
        self.assertEqual(s.orders, [])
        sell_limit = sim_with()
        scripted(sell_limit, [0.0, 0.0, 0.1, 0.9, 0.9, 1.0, 0.0])
        sell_limit._new_orders(0, 100.0)
        o = sell_limit.orders[0]
        self.assertEqual((o.side, o.remaining), (-1, 40))
        self.assertAlmostEqual(o.limit, 101.0)

    def test_profile_distribution(self):
        profs = [S.profile(s) for s in range(400)]
        lev = lambda mode: {c["leverage"] for c, _s, _d in profs if c["margin_mode"] == mode}
        self.assertEqual((lev("isolated"), lev("cross"), lev("none")), ({1, 2, 3, 5, 10, 20}, {1, 2, 3, 5}, {1}))
        self.assertEqual({c["mmr"] for c, _s, _d in profs}, {0.005, 0.01, 0.025})
        self.assertEqual({c["initial_cash"] for c, _s, _d in profs}, {10_000.0, 100_000.0})
        share = lambda key: sum(s[key] for _c, s, _d in profs) / len(profs)
        self.assertLess(abs(share("settlement") - 0.4), 0.08)
        self.assertLess(abs(share("funding") - 0.8), 0.08)
        modes = [c["margin_mode"] for c, _s, _d in profs]
        self.assertLess(abs(modes.count("isolated") / 400 - 0.6), 0.08)
        self.assertEqual({s["bars_per_day"] for _c, s, _d in profs}, {48, 96, 288})
        self.assertEqual({d["jump_prob"] for _c, _s, d in profs}, {0.002, 0.01, 0.02})
        self.assertEqual({d["gap_prob"] for _c, _s, d in profs}, {0.002, 0.01})

    def test_cross_reversal_from_one_lot_short(self):
        cross = sim_with({"initial_cash": 10_000.0, "margin_mode": "cross", "leverage": 2, "mmr": 0.05})
        trade(cross, 0, "sell", 1, 100.0)
        self.assertEqual(cross._affordable(1, 500, 100.0, 0.0), 201)          # 平 1 手 + 新开 200（基数按 0）
        self.assertEqual(cross._affordable(-1, 500, 100.0, 0.0), 199)

    def test_close_one_lot_and_compare_rows_scales(self):
        s = sim_with()
        trade(s, 0, "buy", 1, 100.0)
        scripted(s, [0.0, 0.5, 0.7, 0.2, 0.0])
        s._new_orders(0, 100.0)
        self.assertEqual((s.orders[0].side, s.orders[0].remaining), (-1, 1))
        rows = L.run([], [1.0], initial_cash=1.0)["rows"]
        bumped = [dict(rows[0], cash=rows[0]["cash"] + 1.2e-9)]
        self.assertEqual(S.compare_rows(rows, bumped, 1.0)["field"], "cash")           # 尺度下限是 1.0
        q = L.run([{"bar": 0, "kind": "trade", "side": "buy", "qty": 0.3, "price": 10.0}], [10.0])["rows"]
        q2 = [dict(q[0], qty=0.1 + 0.2)]
        self.assertTrue(S.compare_rows(q, q2, 1e5)["ok"])                              # 浮点数量按相对容差
        st = S.generate(2, 300)
        res = S.check_stream(st)
        self.assertEqual(res["final_equity"], L.run(st["records"], st["closes"], **st["config"])["rows"][-1]["equity"])
        rep = S.run_many(3, 200, seed0=7)
        self.assertEqual(sum(rep["modes"].values()), 3)

    def test_aggregation_exit_code_and_defaults(self):
        from unittest import mock
        rep = S.run_many(2, 300, seed0=5)
        singles = [S.check_stream(S.generate(s, 300)) for s in (5, 6)]
        for key in rep["counters"]:
            self.assertEqual(rep["counters"][key], sum(r["counters"][key] for r in singles), key)
        self.assertEqual(set(rep["stats"]), set(singles[0]["stats"]))
        for key in rep["stats"]:
            self.assertEqual(rep["stats"][key], sum(r["stats"][key] for r in singles), key)
        import inspect
        self.assertEqual(inspect.signature(S.compare_rows).parameters["rel_tol"].default, 1e-9)
        self.assertEqual(min(r["order_id"] for r in S.generate(0, 600)["records"] if "order_id" in r), 1)
        bad = dict(singles[0], n_violations=1)
        with mock.patch.object(S, "check_stream", return_value=bad), captured():
            self.assertEqual(S.main(["--streams", "1", "--bars", "50"]), 1)
        args = S.build_parser().parse_args([])
        self.assertEqual((args.streams, args.bars, args.seed, args.out), (200, 3000, 0, None))


if __name__ == "__main__":
    unittest.main()
