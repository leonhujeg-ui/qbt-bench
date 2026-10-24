"""判据本身：用手搓的服务端真相 + 假客户端，逐个判据验证红 / 绿方向。"""
import unittest

from qdexsim import judges


def order(i, status="NEW", cid=None, side="BUY", qty=0.01, created=1000, origin="binance", symbol="BTCUSDT", otype="LIMIT"):
    return {"id": i, "cid": cid, "symbol": symbol, "side": side, "type": otype, "qty": qty, "price": 1.0, "status": status,
            "filled": qty if status == "FILLED" else 0.0, "created": created, "origin": origin}


def truth(orders, positions=None, wallet=10000.0):
    return {"orders": orders, "fills": [], "positions": [{"symbol": s, "qty": q} for s, q in (positions or {}).items()],
            "account": {"wallet": wallet}}


class Fake:
    """假客户端：records 是它认为的单（键 = local_id / server_id / cid 都能查到），positions / balance 直接给。"""

    def __init__(self, records=(), positions=None, balance=10000.0):
        self.records, self._pos, self._bal = list(records), positions or {}, balance

    def query(self, key):
        for r in self.records:
            if str(key) in (str(r.get("local_id")), str(r.get("server_id")), str(r.get("cid"))):
                return dict(r)
        return None

    def positions(self):
        return self._pos

    def balance(self):
        return self._bal


def rec(local_id, server_id=None, status="NEW", cid=None):
    return {"local_id": local_id, "server_id": server_id, "status": status, "cid": cid}


INTENTS = {"i1": {"symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "qty": 0.01, "expect_placed": True},
           "i2": {"symbol": "BTCUSDT", "side": "SELL", "type": "LIMIT", "qty": 0.02, "expect_placed": True}}


def ctx(orders, client, intents=INTENTS, positions=None, chaos=None, kill=None, wallet=10000.0):
    return judges.JudgeContext(truth(orders, positions, wallet), client, intents, "binance", chaos, kill)


class TestJudges(unittest.TestCase):
    def test_no_duplicate(self):
        c = ctx([order(1), order(2, side="SELL", qty=0.02)], Fake())
        self.assertTrue(judges.judge_no_duplicate(c).ok)
        c = ctx([order(1), order(2), order(3, side="SELL", qty=0.02)], Fake())
        v = judges.judge_no_duplicate(c)
        self.assertFalse(v.ok)
        self.assertEqual(v.numbers["max_per_intent"], 2)
        c = ctx([order(1, origin="system"), order(2)], Fake())             # 系统单（强平）不算
        self.assertTrue(judges.judge_no_duplicate(c).ok)
        c = ctx([order(1), order(2, otype="MARKET")], Fake())               # 类型不同不是同一意图
        self.assertTrue(judges.judge_no_duplicate(c).ok)

    def test_intent_complete(self):
        self.assertTrue(judges.judge_intent_complete(ctx([order(1), order(2, side="SELL", qty=0.02)], Fake())).ok)
        v = judges.judge_intent_complete(ctx([order(1)], Fake()))
        self.assertFalse(v.ok)
        self.assertEqual(v.numbers["missing"], 1)
        intents = dict(INTENTS, i2=dict(INTENTS["i2"], expect_placed=False))
        self.assertTrue(judges.judge_intent_complete(ctx([order(1)], Fake(), intents)).ok)

    def test_no_lost(self):
        c = ctx([order(1, cid="a"), order(2)], Fake([rec("i1", cid="a"), rec("i2", server_id=2)]))
        self.assertTrue(judges.judge_no_lost(c).ok)
        v = judges.judge_no_lost(ctx([order(1, cid="a"), order(2)], Fake([rec("i1", cid="a")])))
        self.assertFalse(v.ok)
        self.assertEqual(v.numbers["lost"], 1)
        self.assertTrue(judges.judge_no_lost(ctx([order(9, origin="system")], Fake())).ok)

    def test_no_phantom(self):
        c = ctx([order(1, cid="a")], Fake([rec("i1", server_id=1), rec("i2", server_id=77, status="REJECTED")]))
        self.assertTrue(judges.judge_no_phantom(c).ok)
        c = ctx([order(1)], Fake([rec("i1", server_id=1), rec("i2", server_id=77, status="FILLED")]))
        v = judges.judge_no_phantom(c)
        self.assertFalse(v.ok)
        self.assertEqual(v.numbers["phantoms"], 1)
        c = ctx([order(1, cid="x")], Fake([rec("i1", server_id=None, cid="x")]))          # 只知道 cid 也算知道
        self.assertTrue(judges.judge_no_phantom(c).ok)

    def test_position_and_balance(self):
        self.assertTrue(judges.judge_position_consistent(ctx([], Fake(positions={"BTCUSDT": 0.01}), positions={"BTCUSDT": 0.01})).ok)
        self.assertTrue(judges.judge_position_consistent(ctx([], Fake(positions={"BTCUSDT": 0.0}), positions={})).ok)
        v = judges.judge_position_consistent(ctx([], Fake(positions={}), positions={"BTCUSDT": 0.01}))
        self.assertFalse(v.ok)
        self.assertEqual(v.numbers["mismatched_symbols"], 1)
        self.assertFalse(judges.judge_position_consistent(ctx([], Fake(positions={"BTCUSDT": 0.0100001}), positions={"BTCUSDT": 0.01})).ok)
        self.assertTrue(judges.judge_balance_consistent(ctx([], Fake(balance=9999.5), wallet=9999.5)).ok)
        self.assertFalse(judges.judge_balance_consistent(ctx([], Fake(balance=0.0), wallet=9999.5)).ok)
        self.assertFalse(judges.judge_balance_consistent(ctx([], Fake(balance=None), wallet=9999.5)).ok)
        self.assertFalse(judges.judge_balance_consistent(ctx([], Fake(balance=9999.5001), wallet=9999.5)).ok)

    def test_cancel_real_and_status(self):
        c = ctx([order(1, "CANCELED"), order(2, "NEW", side="SELL", qty=0.02)], Fake([rec("i1", 1, "CANCELED"), rec("i2", 2, "NEW")]))
        self.assertTrue(judges.judge_cancel_real(c).ok)
        self.assertTrue(judges.judge_status_consistent(c).ok)
        c = ctx([order(1, "NEW"), order(2, "FILLED", side="SELL", qty=0.02)], Fake([rec("i1", 1, "CANCELED"), rec("i2", 2, "CANCELED")]))
        v = judges.judge_cancel_real(c)
        self.assertFalse(v.ok)
        self.assertEqual(v.numbers["still_open"], 1)                      # 已成的不算“仍挂着”
        v = judges.judge_status_consistent(c)
        self.assertFalse(v.ok)
        self.assertEqual(v.numbers["mismatched"], 2)
        c = ctx([order(1, "NEW")], Fake([rec("i1", None, "CANCELED", cid="zz")]))      # 服务端找不到对应单 → 不算仍挂着
        self.assertTrue(judges.judge_cancel_real(c).ok)

    def test_kill_switch(self):
        self.assertFalse(judges.judge_kill_switch(ctx([], Fake())).ok)
        k = {"t_ms": 5000, "grace_ms": 500}
        self.assertTrue(judges.judge_kill_switch(ctx([order(1, "CANCELED", created=4000)], Fake(), kill=k)).ok)
        v = judges.judge_kill_switch(ctx([order(1, "NEW", created=4000)], Fake(), kill=k))
        self.assertFalse(v.ok)
        self.assertEqual(v.numbers["open_orders"], 1)
        v = judges.judge_kill_switch(ctx([order(1, "FILLED", created=5500)], Fake(), kill=k))
        self.assertFalse(v.ok)
        self.assertEqual(v.numbers["late_orders"], 1)
        self.assertTrue(judges.judge_kill_switch(ctx([order(1, "FILLED", created=5499)], Fake(), kill=k)).ok)

    def test_rate_limit(self):
        chaos = {"config": {"rules": [{"kind": "rate_limit"}, {"kind": "latency"}]}, "counts": [2, 5], "violations": []}
        self.assertTrue(judges.judge_rate_limit(ctx([], Fake(), chaos=chaos)).ok)
        v = judges.judge_rate_limit(ctx([], Fake(), chaos=dict(chaos, violations=[{"dialect": "binance", "op": "place"}])))
        self.assertFalse(v.ok)
        self.assertEqual(v.numbers["violations"], 1)
        self.assertTrue(judges.judge_rate_limit(ctx([], Fake(), chaos=dict(chaos, violations=[{"dialect": "okx", "op": "place"}]))).ok)
        self.assertFalse(judges.judge_rate_limit(ctx([], Fake(), chaos=dict(chaos, counts=[0, 5]))).ok)     # 没限频过 → 不算绿

    def test_run_and_verdict(self):
        c = ctx([order(1), order(2, side="SELL", qty=0.02)], Fake([rec("i1", 1), rec("i2", 2)]))
        vs = judges.run(["no_duplicate", "no_lost"], c)
        self.assertEqual([v.name for v in vs], ["no_duplicate", "no_lost"])
        self.assertTrue(all(v.ok for v in vs))
        d = vs[0].to_dict()
        self.assertEqual(set(d), {"judge", "ok", "detail", "numbers"})
        self.assertIn("OK", repr(vs[0]))
        self.assertIn("FAIL", repr(judges.Verdict("x", False)))
        self.assertEqual(set(judges.JUDGES), {"no_duplicate", "intent_complete", "no_lost", "no_phantom", "position_consistent",
                                              "balance_consistent", "cancel_real", "status_consistent", "kill_switch", "rate_limit"})


if __name__ == "__main__":
    unittest.main()
