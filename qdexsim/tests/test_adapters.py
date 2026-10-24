import unittest

from qdexsim import adapters
from qdexsim.adapters import ExchangeError, RateLimited, UnknownResult
from qdexsim.dialects import binance, bitunix, bybit, okx
from qdexsim.tests._util import inproc_adapter, inproc_sim


class TestErrorMapping(unittest.TestCase):
    def test_reverse_errors_resolves_ambiguity(self):
        self.assertEqual(adapters.reverse_errors(binance.ERRORS)["-4116"], "DUP_CID")
        self.assertEqual(adapters.reverse_errors(bybit.ERRORS)["10001"], "BAD_PARAM")           # 多义含 BAD_PARAM
        self.assertEqual(adapters.reverse_errors(okx.ERRORS)["51000"], "BAD_PARAM")
        self.assertEqual(adapters.reverse_errors(bitunix.ERRORS)["10008"], "BAD_QTY")            # 多义不含 BAD_PARAM → 优先级
        self.assertEqual(adapters.reverse_errors(bitunix.ERRORS)["20007"], "ORDER_NOT_FOUND")
        self.assertEqual(adapters.reverse_errors({"A": (1, "9", ""), "B": (1, "9", "")})["9"], "A")   # 都不在优先级表 → 首个
        for k in adapters.KIND_PRIORITY:
            pass
        self.assertIn("BAD_PARAM", adapters.KIND_PRIORITY)
        self.assertLess(adapters.KIND_PRIORITY.index("BAD_PARAM"), adapters.KIND_PRIORITY.index("BAD_QTY"))

    def test_kind_of_sniffs_timestamp(self):
        ad = adapters.make("bitunix", "http://127.0.0.1:1")
        self.assertEqual(ad.kind_of(10002, "Parameter Error: timestamp expired"), "BAD_TIMESTAMP")
        self.assertEqual(ad.kind_of(10002, "Parameter Error: qty"), "BAD_PARAM")
        self.assertEqual(ad.kind_of(10007), "BAD_SIGN")
        self.assertEqual(ad.kind_of("unknown-code"), "SERVER_ERROR")

    def test_exception_types(self):
        e = RateLimited(1500, 429, "slow", 429)
        self.assertEqual((e.kind, e.retry_after_ms, e.code), ("RATE_LIMIT", 1500, 429))
        self.assertIsInstance(e, ExchangeError)
        u = UnknownResult("timeout", 504)
        self.assertEqual((u.reason, u.http_status), ("timeout", 504))


class TestTransport(unittest.TestCase):
    def test_only_loopback(self):
        with self.assertRaises(ValueError):
            adapters.Transport("http://example.com:80")
        with self.assertRaises(ValueError):
            adapters.Transport("http://10.0.0.1:8080")
        t = adapters.Transport("http://localhost:1", timeout_s=0.2)
        self.assertEqual((t.host, t.port), ("localhost", 1))
        with self.assertRaises(UnknownResult):
            t.request("GET", "/")                                   # 没人监听 → 连接错误 → 结果未知
        self.assertEqual(adapters.Transport("http://127.0.0.1").port, 80)

    def test_json_helper_and_f(self):
        self.assertIsNone(adapters._json(b"not json"))
        self.assertIsNone(adapters._json(b""))
        self.assertEqual(adapters._json(b"[1]"), [1])
        self.assertEqual(adapters.Adapter.f("x", 7.0), 7.0)
        self.assertEqual(adapters.Adapter.f("1.5"), 1.5)


class TestCheckEdgeCases(unittest.TestCase):
    def test_non_json_bodies(self):
        for name in ("bybit", "okx", "bitget", "bitunix"):
            ad = adapters.make(name, "http://127.0.0.1:1")
            with self.assertRaises(UnknownResult):
                ad.check("time", 200, {}, None)
        bn = adapters.make("binance", "http://127.0.0.1:1")
        with self.assertRaises(ExchangeError) as cm:
            bn.check("time", 400, {}, None)
        self.assertEqual(cm.exception.kind, "SERVER_ERROR")
        with self.assertRaises(RateLimited) as cm:
            bn.check("time", 200, {}, {"code": -1003, "msg": "weight"})
        self.assertEqual(cm.exception.retry_after_ms, 1000)
        bg = adapters.make("bitget", "http://127.0.0.1:1")
        with self.assertRaises(RateLimited) as cm:
            bg.check("time", 429, {"retry-after": "3"}, {"code": "429"})
        self.assertEqual(cm.exception.retry_after_ms, 3000)
        bu = adapters.make("bitunix", "http://127.0.0.1:1")
        with self.assertRaises(RateLimited) as cm:
            bu.check("time", 200, {}, {"code": 10006, "msg": "x"})
        self.assertEqual(cm.exception.retry_after_ms, adapters.DEFAULT_RETRY_MS)
        by = adapters.make("bybit", "http://127.0.0.1:1")
        with self.assertRaises(RateLimited) as cm:
            by.check("time", 200, {}, {"retCode": 10006, "retMsg": "x"})
        self.assertEqual(cm.exception.retry_after_ms, adapters.DEFAULT_RETRY_MS)
        ok = adapters.make("okx", "http://127.0.0.1:1")
        with self.assertRaises(RateLimited):
            ok.check("time", 200, {}, {"code": "50011", "msg": ""})
        with self.assertRaises(ExchangeError) as cm:
            ok.check("place", 200, {}, {"code": "1", "data": []})
        self.assertEqual(cm.exception.kind, "SERVER_ERROR")

    def test_call_5xx_semantics(self):
        sim = inproc_sim()
        ad = inproc_adapter("okx", sim)
        sim.chaos.update({"rules": [{"kind": "server_error", "op": "time", "max": 1, "status": 500}]})
        with self.assertRaises(ExchangeError) as cm:          # 读操作上的交易所形状 5xx → 可重试的业务错
            ad.server_time()
        self.assertEqual(cm.exception.kind, "SERVER_ERROR")
        sim.chaos.update({"rules": [{"kind": "server_error", "op": "place", "max": 1, "status": 500}]})
        with self.assertRaises(UnknownResult):               # 写操作上的 5xx → 结果未知
            ad.place("BTCUSDT", "BUY", "LIMIT", 0.01, 59000.0)

    def test_okx_contract_size_refresh_and_tdmode(self):
        sim = inproc_sim()
        ad = inproc_adapter("okx", sim)
        sim.engine.set_symbol("ETHUSDT", ct_val=1.0)
        ad.instruments()
        self.assertEqual(ad.ct["ETHUSDT"], 1.0)
        ad.set_margin_mode("ETHUSDT", "isolated")
        o = ad.place("ETHUSDT", "BUY", "MARKET", 1.0)
        self.assertEqual(ad.query("ETHUSDT", order_id=o["id"])["qty"], 1.0)
        self.assertEqual(sim.engine.position("ETHUSDT")["qty"], 1.0)
        self.assertEqual(ad.set_leverage("ETHUSDT", 3), 3)

    def test_sync_time(self):
        sim = inproc_sim(chaos_cfg={"clock_drift_ms": 60000})
        ad = inproc_adapter("bitget", sim)
        with self.assertRaises(ExchangeError) as cm:
            ad.account()
        self.assertEqual(cm.exception.kind, "BAD_TIMESTAMP")
        off = ad.sync_time()
        self.assertGreater(off, 50000)
        self.assertEqual(ad.account()["wallet"], 10000.0)


if __name__ == "__main__":
    unittest.main()
