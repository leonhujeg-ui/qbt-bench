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
            self.assertEqual(json.load(f)["n_streams"], 2)

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


if __name__ == "__main__":
    unittest.main()
