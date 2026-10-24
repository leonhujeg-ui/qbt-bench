"""参考客户端：直接单元测试内部行为 + 几个代表性场景（天真红 / 正确绿）。"""
import os
import unittest

from qdexsim import clients, harness, scenarios
from qdexsim.adapters import ExchangeError
from qdexsim.tests._util import inproc_sim, tmpdir, wire


def intent(i, side="BUY", otype="LIMIT", qty=0.01, price=59000.0, **kw):
    return dict({"intent": i, "symbol": "BTCUSDT", "side": side, "type": otype, "qty": qty, "price": price if otype == "LIMIT" else None,
                 "reduce_only": False}, **kw)


class Base(unittest.TestCase):
    def make(self, cls, dialect="binance", **sim_kw):
        self.sim = inproc_sim(**sim_kw)
        c = cls(dialect, "http://127.0.0.1:1", os.path.join(tmpdir(self), "c"), timeout_s=0.2)
        wire(c.ad, self.sim)
        return c


class TestNaive(Base):
    def test_blind_retry_duplicates_and_forgets(self):
        c = self.make(clients.NaiveClient, chaos_cfg={"rules": [{"kind": "unknown_result", "op": "place", "max": 1, "mode": "drop"}]})
        self.assertEqual(c.balance(), 10000.0)
        self.assertEqual(c.place(intent("i1")), "i1")
        self.assertEqual(len(self.sim.engine.all_orders()), 2)                       # 两张单
        self.assertEqual(c.query("i1")["status"], "NEW")
        self.assertIsNone(c.query("nope"))
        m = c.place(intent("i2", otype="MARKET"))
        self.assertEqual((c.query(m)["status"], c.positions()), ("FILLED", {"BTCUSDT": 0.01}))
        self.assertEqual(len(c.open_orders()), 1)
        c.cancel("i1")
        c.cancel("i1")                                                                # 第二次报错也当撤掉
        self.assertEqual(c.query("i1")["status"], "CANCELED")
        c.cancel("ghost")
        self.assertTrue(any(e["what"] == "cancel_error_ignored" for e in c.log))
        c.restart()
        self.assertIsNone(c.query("i1"))
        self.assertEqual(c.positions(), {})

    def test_rejections_and_kill(self):
        c = self.make(clients.NaiveClient)
        self.assertIsNone(c.place(intent("bad", qty=100)))                            # INSUFFICIENT → 不记
        c.place(intent("i1"))
        c.kill_switch()
        self.assertEqual(self.sim.engine.open_orders(), [])
        with self.assertRaises(RuntimeError):
            c.place(intent("i2"))
        g = self.make(clients.NaiveClient, chaos_cfg={"rules": [{"kind": "unknown_result", "op": "place", "mode": "drop"}]})
        self.assertIsNone(g.place(intent("i1")))
        self.assertTrue(any(e["what"] == "gave_up" for e in g.log))
        self.assertEqual(len(self.sim.engine.all_orders()), 3)                        # 三次盲重试三张单
        b = self.make(clients.NaiveClient, chaos_cfg={"rules": [{"kind": "server_error", "op": "account", "max": 1}]})
        self.assertIsNone(b.balance())                                                # 启动时读不到、再读一次还是读不到 → None
        self.assertEqual(b.balance(), 10000.0)                                        # 第三次读到了


class TestCorrect(Base):
    def test_journal_before_send_and_unknown_result(self):
        c = self.make(clients.CorrectClient, chaos_cfg={"rules": [{"kind": "unknown_result", "op": "place", "max": 1, "mode": "timeout", "hold_ms": 10}]})
        c.place(intent("i1"))
        self.assertEqual(len(self.sim.engine.all_orders()), 1)
        r = c.query("i1")
        self.assertEqual((r["status"], r["cid"], r["phase"]), ("NEW", "i1", "acked"))
        self.assertTrue(os.path.exists(c.journal_path))
        self.assertEqual(c.query(r["server_id"])["local_id"], "i1")
        self.assertEqual(c.query("i1")["local_id"], c.query("i1")["cid"])
        with self.assertRaises(KeyError):
            c.cancel("ghost")

    def test_unknown_then_not_found_resends_same_cid(self):
        c = self.make(clients.CorrectClient)
        calls = {"n": 0}
        real = c.ad.place

        def flaky(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise clients.UnknownResult("lost before reaching server")
            return real(*a, **kw)
        c.ad.place = flaky
        c.place(intent("i1"))
        self.assertEqual(len(self.sim.engine.all_orders()), 1)
        self.assertEqual(self.sim.engine.all_orders()[0]["cid"], "i1")

    def test_dup_cid_is_claimed(self):
        c = self.make(clients.CorrectClient)
        self.sim.engine.place("BTCUSDT", "BUY", "LIMIT", 0.01, 59000, cid="i1")       # 服务端已有（上次成功了没收到）
        c.place(intent("i1"))
        self.assertEqual(len(self.sim.engine.all_orders()), 1)
        self.assertEqual(c.query("i1")["status"], "NEW")

    def test_reject_paths(self):
        c = self.make(clients.CorrectClient)
        c.place(intent("big", qty=100))
        r = c.query("big")
        self.assertEqual((r["status"], r["reject_kind"]), ("REJECTED", "INSUFFICIENT"))
        self.sim.engine.set_symbol("BTCUSDT", tick=1.0)
        c.place(intent("i2", price=59000.5))
        self.assertEqual(c.query("i2")["status"], "NEW")
        self.assertEqual(self.sim.engine.get_order(cid="i2")["price"], 59000.0)         # 重新归整后成功
        self.assertTrue(any(e["what"] == "requantized" and e["changed"] for e in c.log))
        self.sim.engine.set_symbol("BTCUSDT", status="SUSPENDED")
        c.place(intent("i3"))
        self.assertEqual(c.query("i3")["status"], "REJECTED")
        self.assertFalse(c._requantize({"symbol": "NOPE", "price": 1.0, "qty": 1.0, "local_id": "x"}))

    def test_cancel_variants_and_settle(self):
        c = self.make(clients.CorrectClient, chaos_cfg={"rules": [{"kind": "cancel_race", "op": "cancel", "max": 1}]})
        c.place(intent("i1"))
        c.cancel("i1")                                                                 # 撤单竞态：其实成了
        self.assertEqual(c.query("i1")["status"], "FILLED")
        c.place(intent("i2", price=58000.0))
        c.cancel("i2")
        self.assertEqual(c.query("i2")["status"], "CANCELED")
        c.cancel("i2")                                                                 # 已终局：直接返回
        c.place(intent("i3", price=57000.0))
        self.sim.chaos.update({"rules": [{"kind": "unknown_result", "op": "cancel", "max": 1, "mode": "drop"}]})
        c.cancel("i3")
        self.assertEqual(self.sim.engine.get_order(cid="i3")["status"], "CANCELED")
        self.assertEqual(c.positions(), {"BTCUSDT": 0.01})
        self.assertEqual(c.balance(), self.sim.engine.wallet)

    def test_restart_reconciles_and_resends(self):
        c = self.make(clients.CorrectClient)
        c.place(intent("i1"))
        c.orders["i1"]["phase"] = "sent"                                               # 模拟：发出后崩溃，服务端有
        c.orders["pend"] = c._rec(intent("pend", price=58500.0), cid="pend")           # 模拟：写了日志没发出去
        c.orders["pend"]["phase"] = "pending"
        c.orders["gone"] = dict(c._rec(intent("gone", price=58400.0), cid="gone"), phase="acked")   # 服务端没有，且不是待发
        c._save()
        d = clients.CorrectClient("binance", "http://127.0.0.1:1", c.state_dir, timeout_s=0.2)
        wire(d.ad, self.sim)
        self.assertEqual(d.query("i1")["status"], "NEW")
        d.restart()
        self.assertEqual(d.query("pend")["status"], "NEW")                              # 补发了
        self.assertIsNone(d.query("gone"))                                              # NOT_SENT：不算知道
        self.assertEqual(d.orders["gone"]["status"], "NOT_SENT")
        self.assertEqual(len(self.sim.engine.all_orders()), 2)

    def test_backoff_resync_and_server_error(self):
        c = self.make(clients.CorrectClient, chaos_cfg={"clock_drift_ms": 60000, "rules": [
            {"kind": "rate_limit", "op": "query", "max": 1, "retry_after_ms": 30},
            {"kind": "server_error", "op": "account", "max": 1}, {"kind": "maintenance", "op": "place", "max": 1}]})
        c.place(intent("i1"))                                                           # 维护中 → 退避重试；时间窗错 → 对时
        self.assertEqual(c.query("i1")["status"], "NEW")
        self.assertGreater(c.ad.offset_ms, 50000)
        self.assertEqual(c.balance(), self.sim.engine.wallet)
        whats = {e["what"] for e in c.log}
        self.assertIn("backoff", whats)
        self.assertIn("resync", whats)
        c.MAX_RETRY = 1
        with self.assertRaises(RuntimeError):
            c._call(lambda: (_ for _ in ()).throw(clients.RateLimited(1)))
        with self.assertRaises(ExchangeError):
            c._call(lambda: (_ for _ in ()).throw(ExchangeError("INSUFFICIENT")))

    def test_stable_positions_and_adoption(self):
        c = self.make(clients.CorrectClient)
        self.sim.engine.place("BTCUSDT", "BUY", "MARKET", 0.05)                           # 客户端不知道的持仓（例如别处下的）
        self.assertEqual(c._stable_positions(reads=2, gap_s=0.0), {"BTCUSDT": 0.05})
        self.assertEqual(c.positions(), {"BTCUSDT": 0.05})                               # 稳定且不同 → 采纳服务端
        self.assertTrue(any(e["what"] == "position_adopted_from_server" for e in c.log))
        seq = iter([{"BTCUSDT": 0.05}, {"BTCUSDT": 0.07}])
        c.ad.positions = lambda: next(seq)
        self.assertIsNone(c._stable_positions(reads=2, gap_s=0.0))                       # 读不稳 → None
        c.ad.positions = lambda: (_ for _ in ()).throw(ExchangeError("SERVER_ERROR"))
        c.MAX_RETRY = 1
        self.assertIsNone(c._stable_positions(reads=1, gap_s=0.0))

    def test_kill_switch_loops_until_empty(self):
        c = self.make(clients.CorrectClient, chaos_cfg={"rules": [{"kind": "unknown_result", "op": "cancel", "max": 1, "mode": "drop"},
                                                                  {"kind": "server_error", "op": "open_orders", "max": 1}]})
        c.place(intent("i1")), c.place(intent("i2", price=58000.0))
        c.kill_switch()
        self.assertEqual(self.sim.engine.open_orders(), [])
        with self.assertRaises(RuntimeError):
            c.place(intent("i3"))
        self.assertTrue(c.killed)


class TestScenarios(unittest.TestCase):
    def run_pair(self, name):
        scn = scenarios.load(name)
        out = {}
        for cname in ("naive", "correct"):
            r = harness.run_scenario(scn, cname)
            self.assertNotIn("harness_error", r, r.get("harness_error"))
            out[cname] = r
            self.assertEqual(r["ok"], scn["expect"][cname] == "green", (name, cname, [v for v in r["verdicts"] if not v["ok"]], r["client_errors"]))
        return out

    def test_unknown_result_has_teeth(self):
        r = self.run_pair("binance/unknown_timeout")
        reds = {v["judge"] for v in r["naive"]["verdicts"] if not v["ok"]}
        self.assertIn("no_duplicate", reds)

    def test_client_restart_has_teeth(self):
        r = self.run_pair("bybit/client_restart")
        self.assertIn("no_lost", {v["judge"] for v in r["naive"]["verdicts"] if not v["ok"]})

    def test_rate_limit_and_kill_switch(self):
        self.run_pair("okx/rate_limit")
        self.run_pair("bitget/kill_switch")

    def test_cancel_race_and_stale(self):
        self.run_pair("bitunix/cancel_race")
        self.run_pair("binance/stale_views")

    def test_clock_drift_and_symbol_rules(self):
        self.run_pair("bitunix/clock_drift")
        self.run_pair("bybit/symbol_rules")

    def test_server_restart_and_502(self):
        self.run_pair("okx/server_restart")
        self.run_pair("bitget/unknown_502")


if __name__ == "__main__":
    unittest.main()
