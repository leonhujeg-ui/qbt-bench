import json
import os
import unittest

from qdexsim import chaos as chaos_mod, store as store_mod
from qdexsim.tests._util import tmpdir


def mk(rules, seed=1, **kw):
    return chaos_mod.Chaos(dict({"seed": seed, "rules": rules}, **kw), store=store_mod.Store())


class TestRules(unittest.TestCase):
    def test_matching_fields(self):
        c = mk([{"kind": "latency", "op": ["place", "cancel"], "dialect": "okx", "ms": 7, "from_req": 2, "max": 2}])
        self.assertEqual(c.before("place", "okx").delay_ms, 0)           # req 1 < from_req
        self.assertEqual(c.before("query", "okx").delay_ms, 0)           # op 不匹配
        self.assertEqual(c.before("place", "bybit").delay_ms, 0)         # dialect 不匹配
        self.assertEqual(c.before("cancel", "okx").delay_ms, 7)
        self.assertEqual(c.before("place", "okx").delay_ms, 7)
        self.assertEqual(c.before("place", "okx").delay_ms, 0)           # max 用完
        self.assertEqual(c.status()["counts"], [2])

    def test_probability_is_seeded(self):
        a = mk([{"kind": "server_error", "op": "*", "p": 0.5}], seed=9)
        b = mk([{"kind": "server_error", "op": "*", "p": 0.5}], seed=9)
        sa = [a.before("place", "x").reject is not None for _ in range(50)]
        sb = [b.before("place", "x").reject is not None for _ in range(50)]
        self.assertEqual(sa, sb)
        self.assertTrue(10 < sum(sa) < 40)
        z = mk([{"kind": "server_error", "op": "*", "p": 0.0}])
        self.assertTrue(all(z.before("place", "x").reject is None for _ in range(20)))
        c = mk([{"kind": "server_error", "op": "*", "p": 0.5}], seed=10)
        self.assertNotEqual([c.before("place", "x").reject is not None for _ in range(50)], sa)

    def test_each_kind_effect(self):
        c = mk([{"kind": "latency", "ms": 5, "jitter_ms": 3}, {"kind": "rate_limit", "op": "place", "retry_after_ms": 250},
                {"kind": "server_error", "op": "place", "status": 502},
                {"kind": "unknown_result", "op": "place", "mode": "drop", "hold_ms": 10},
                {"kind": "unknown_result", "op": "place", "mode": "502"},
                {"kind": "cancel_race", "op": "cancel"}, {"kind": "stale", "op": "positions", "ms": 300},
                {"kind": "stale", "op": "positions", "ms": 900}, {"kind": "zero_once", "op": "account", "max": 1},
                {"kind": "fill_delay", "ms": 400}, {"kind": "symbol", "at_req": 99, "symbol": "BTCUSDT", "changes": {}}])
        d = c.before("place", "binance")
        self.assertTrue(5 <= d.delay_ms <= 8)
        self.assertEqual(d.reject, {"kind": "RATE_LIMIT", "status": None, "retry_after_ms": 250})   # 第一条拒绝胜出
        self.assertEqual(d.unknown, {"mode": "drop", "hold_ms": 10})                                # 第一条 unknown 胜出
        self.assertEqual([i for i, _ in d.fired], [0, 1, 3])
        self.assertEqual(c.status()["counts"][2], 0)                                                # 没生效的不计数
        d2 = c.before("cancel", "binance")
        self.assertTrue(d2.cancel_race)
        d3 = c.before("positions", "binance")
        self.assertEqual(d3.stale_ms, 900)
        self.assertTrue(c.before("account", "binance").zero_once)
        self.assertFalse(c.before("account", "binance").zero_once)
        self.assertEqual(c.fill_delay_ms(), 400)
        self.assertEqual(d.summary()["fired"], [0, 1, 3])
        self.assertEqual(mk([]).fill_delay_ms(), 0)

    def test_scheduled_and_drift(self):
        c = mk([{"kind": "symbol", "at_req": 2, "symbol": "BTCUSDT", "changes": {"tick": 1.0}},
                {"kind": "clock_drift", "at_req": 2, "ms": 4000}], clock_drift_ms=100)
        self.assertEqual(c.drift_ms, 100)
        c.before("time", "okx")
        self.assertEqual(c.scheduled(), [])
        c.before("time", "okx")
        got = c.scheduled()
        self.assertEqual([r["kind"] for r in got], ["symbol", "clock_drift"])
        self.assertEqual(c.drift_ms, 4000)
        self.assertEqual(c.scheduled(), [])                              # 只触发一次
        c.log_scheduled(got[0], "applied", 1)
        self.assertEqual(c.events_written, 1)

    def test_validation(self):
        with self.assertRaises(ValueError):
            mk([{"kind": "nope"}])
        with self.assertRaises(ValueError):
            mk([{"kind": "unknown_result", "mode": "teleport"}])


class TestRuntime(unittest.TestCase):
    def test_update_and_status(self):
        c = mk([{"kind": "latency", "ms": 1}], seed=3)
        c.before("x", "d")
        st = c.update({"add": {"kind": "cancel_race", "op": "cancel"}, "clock_drift_ms": 50})
        self.assertEqual(len(st["config"]["rules"]), 2)
        self.assertEqual(st["req_index"], 0)                           # 重配置重置序号与计数
        self.assertEqual(st["drift_ms"], 50)
        c.update({"rules": [], "seed": 4})
        self.assertEqual(c.cfg["seed"], 4)
        self.assertEqual(c.cfg["rules"], [])

    def test_violations_and_log(self):
        d = tmpdir(self)
        path = os.path.join(d, "chaos.jsonl")
        c = chaos_mod.Chaos({"seed": 1, "rules": [{"kind": "rate_limit", "op": "place", "max": 1, "retry_after_ms": 60000}]},
                            store=store_mod.Store(), log_path=path)
        dec = c.before("place", "bybit")
        c.note_rate_limited("bybit", dec.reject["retry_after_ms"])
        c.log("place", "bybit", dec, "rejected", 123)
        c.before("query", "bybit")                                      # 窗口内再打 → 违规
        c.before("query", "okx")                                        # 别的方言不算
        v = c.status()["violations"]
        self.assertEqual([(x["dialect"], x["op"]) for x in v], [("bybit", "query")])
        self.assertGreater(v[0]["early_by_ms"], 50000)
        with open(path, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f]
        self.assertEqual(rows[0]["injection"], "rate_limit")
        self.assertEqual(rows[0]["executed"], "rejected")
        self.assertEqual(rows[0]["ts"], 123)
        ev = c.store.events(kind="chaos")
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["op"], "place")
        c2 = chaos_mod.Chaos({"rules": []})                             # 没有 store 也能 log
        c2.log("x", "y", chaos_mod.Decision(), "nothing", 1)
        self.assertEqual(c2.events_written, 0)

    def test_load_relative_log_path(self):
        d = tmpdir(self)
        p = os.path.join(d, "scn.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"seed": 2, "rules": [], "log": "events.jsonl"}, f)
        cfg = chaos_mod.load(p)
        self.assertEqual(cfg["log"], os.path.join(d, "events.jsonl"))
        c = chaos_mod.Chaos(cfg)
        self.assertEqual(c.log_path, os.path.join(d, "events.jsonl"))


if __name__ == "__main__":
    unittest.main()
