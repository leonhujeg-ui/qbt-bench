import unittest

from qdexsim import clients, harness, scenarios


def scn(script, judges=("no_duplicate",), chaos=None, **kw):
    return dict({"name": "t/x", "dialect": "binance", "symbol": "BTCUSDT", "seed": 1, "timeout_s": 0.3, "chaos": chaos or {"rules": []},
                 "script": list(script), "judges": list(judges), "expect": {"naive": "red", "correct": "green"}}, **kw)


class TestHarness(unittest.TestCase):
    def test_script_ops(self):
        s = scn([{"op": "place", "intent": "i1", "side": "BUY", "type": "LIMIT", "qty": 0.01, "offset": -100},
                 {"op": "tick", "n": 2}, {"op": "price", "price": 59800}, {"op": "price", "offset": 0}, {"op": "sleep", "ms": 1},
                 {"op": "chaos", "patch": {"add": {"kind": "latency", "ms": 1}}}, {"op": "symbol", "changes": {"tick": 1.0}},
                 {"op": "settle"}, {"op": "restart_server", "down_ms": 1}, {"op": "cancel", "intent": "i1"},
                 {"op": "place", "intent": "i2", "side": "SELL", "type": "MARKET", "qty": 0.01, "reduce_only": True}],
                judges=["no_duplicate", "no_lost", "position_consistent", "status_consistent"], engine={"taker_fee": 0.0})
        r = harness.run_scenario(s, "correct")
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["server_orders"], r["server_fills"]), (2, 2))
        self.assertGreaterEqual(r["requests"], 2)                       # 服务端重启后计数从零起
        self.assertEqual(r["client_errors"], [])
        self.assertEqual(r["client"], "correct")
        self.assertGreater(r["chaos_events"], 0)

    def test_unknown_op_is_harness_error(self):
        r = harness.run_scenario(scn([{"op": "teleport"}]), "correct")
        self.assertFalse(r["ok"])
        self.assertIn("unknown script op", r["harness_error"])

    def test_client_exceptions_are_recorded_not_fatal(self):
        class Broken(clients.CorrectClient):
            NAME = "broken"

            def place(self, intent):
                raise RuntimeError("nope")

            def kill_switch(self):
                raise RuntimeError("no kill")
        s = scn([{"op": "place", "intent": "i1", "side": "BUY", "type": "LIMIT", "qty": 0.01}, {"op": "kill_switch"}],
                judges=["intent_complete", "kill_switch"])
        r = harness.run_scenario(s, Broken)
        self.assertFalse(r["ok"])
        self.assertEqual([e["op"] for e in r["client_errors"]], ["place", "kill_switch"])
        self.assertEqual(r["client"], "broken")

    def test_missing_optional_hooks(self):
        class Minimal(clients.NaiveClient):
            NAME = "minimal"
            restart = property(lambda self: (_ for _ in ()).throw(AttributeError("restart")))
        s = scn([{"op": "restart_client"}], judges=["no_duplicate"])
        r = harness.run_scenario(s, Minimal)
        self.assertEqual(r["client_errors"][0]["op"], "restart_client")

    def test_run_suite_summary(self):
        scns = [scenarios.load("binance/baseline")]
        seen = []
        res = harness.run_suite(scns, client_names=("naive", "correct"), progress=seen.append)
        self.assertEqual(len(seen), 2)
        self.assertEqual(res["summary"]["correct"], {"green": 1, "red": 0, "expected_mismatch": []})
        self.assertEqual(res["summary"]["naive"]["red"], 1)
        self.assertIn("machine", res)
        bad = dict(scenarios.load("binance/baseline"), expect={"naive": "green", "correct": "green"})
        res = harness.run_suite([bad], client_names=("naive",))
        self.assertEqual(res["summary"]["naive"]["expected_mismatch"], ["binance/baseline"])

    def test_price_offset_and_base(self):
        s = scn([{"op": "place", "intent": "i1", "side": "BUY", "type": "LIMIT", "qty": 0.01, "offset": -5}], price_base=50000.0,
                judges=["intent_complete"])
        r = harness.run_scenario(s, "correct")
        self.assertTrue(r["ok"])


if __name__ == "__main__":
    unittest.main()
