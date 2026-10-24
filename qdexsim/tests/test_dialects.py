"""五个方言：签名 / 时间窗 / 错误码形状 / 13 个操作的报文关键字段 —— 全部进程内（Simulator.handle），不开 socket。"""
import json
import time
import unittest

from qdexsim import adapters, common
from qdexsim.adapters import ExchangeError
from qdexsim.dialects import base, binance, bitget, bitunix, bybit, okx
from qdexsim.tests._util import inproc_adapter, inproc_sim

NAMES = ("binance", "bybit", "okx", "bitget", "bitunix")


def raw(sim, method, path, query="", headers=None, body=b""):
    resp, _d = sim.handle(base.Request(method, path, query, headers, body))
    return resp


class TestRequestModel(unittest.TestCase):
    def test_request_parsing(self):
        r = base.Request("post", "/x", "a=1&b=", {"Content-Type": "application/json"}, b'{"c": 3, "a": 9}')
        self.assertEqual(r.method, "POST")
        self.assertEqual(r.query_params, {"a": "1", "b": ""})
        self.assertEqual(r.params, {"a": 9, "b": "", "c": 3})                  # JSON 覆盖查询串
        self.assertEqual(r.get("b", "dflt"), "dflt")                            # 空串当缺省
        self.assertEqual(r.header("CONTENT-TYPE"), "application/json")
        f = base.Request("POST", "/x", "", {"Content-Type": "application/x-www-form-urlencoded"}, b"k=v&n=2")
        self.assertEqual(f.params, {"k": "v", "n": "2"})
        self.assertIsNone(f.json)
        bad = base.Request("POST", "/x", "", {"Content-Type": "application/json"}, b"{not json")
        self.assertIsNone(bad.json)
        self.assertEqual(base.strip_param("a=1&signature=zz&b=2", "signature"), "a=1&b=2")
        self.assertEqual(base.Response(200, None).bytes(), b"")
        self.assertEqual(base.common_prefix(["/api/v5/a", "/api/v5/b/c"]), "/api/v5/")

    def test_helpers(self):
        self.assertEqual(base.to_float("1.5"), 1.5)
        with self.assertRaises(base.EngineError):
            base.to_float("x", "qty")
        self.assertTrue(base.truthy("TRUE") and base.truthy(1) and base.truthy("yes"))
        self.assertFalse(base.truthy("false") or base.truthy(None))
        self.assertIsNone(base.parse_order_id(""))
        self.assertEqual(base.parse_order_id("12"), 12)
        with self.assertRaises(base.EngineError):
            base.parse_order_id("abc")
        with self.assertRaises(base.EngineError):
            base.canon_place("BTCUSDT", None, "LIMIT", 1, 1, None, False)
        with self.assertRaises(base.EngineError):
            base.canon_place("BTCUSDT", "BUY", "LIMIT", 1, None, None, False)
        c = base.canon_place("BTCUSDT", "BUY", "MARKET", "0.5", None, "", True)
        self.assertEqual((c["qty"], c["cid"], c["reduce_only"], c["price"]), (0.5, None, True, None))
        self.assertTrue(base.Dialect.ts_window_ok(1000, 1000, 5000))
        self.assertTrue(base.Dialect.ts_window_ok(1999, 1000, 5000))
        self.assertFalse(base.Dialect.ts_window_ok(2000, 1000, 5000))
        self.assertFalse(base.Dialect.ts_window_ok(-4001, 1000, 5000))
        self.assertEqual(okx.iso_to_ms(okx.ms_to_iso(1700000000123)), 1700000000123)
        self.assertIsNone(okx.iso_to_ms("garbage"))
        self.assertEqual(bitunix.Bitunix.query_string({"b": "2", "a": 1}), "a1b2")
        d = binance.Binance()
        self.assertEqual(d.symbol_in("DOGEUSDT"), "DOGEUSDT")
        with self.assertRaises(base.EngineError):
            d.symbol_in(None)


class TestRoutingAndAuth(unittest.TestCase):
    def setUp(self):
        self.sim = inproc_sim()

    def test_unknown_paths(self):
        self.assertEqual(raw(self.sim, "GET", "/nothing/here").status, 404)
        r = raw(self.sim, "GET", "/fapi/v9/nope")
        self.assertEqual((r.status, r.body["code"]), (404, -1121))
        r = raw(self.sim, "GET", "/v5/nope")
        self.assertEqual(r.body["retCode"], 10001)
        r = raw(self.sim, "GET", "/api/v5/nope")
        self.assertEqual(r.body["code"], "404")
        r = raw(self.sim, "GET", "/api/v2/nope")
        self.assertEqual(r.body["code"], "40404")
        r = raw(self.sim, "GET", "/api/v1/futures/nope")
        self.assertEqual(r.body["code"], 10001)
        self.assertEqual(raw(self.sim, "GET", "/__admin/nope").status, 404)

    def test_public_endpoints_need_no_auth(self):
        for path, key in (("/fapi/v1/time", "serverTime"), ("/v5/market/time", "result"), ("/api/v5/public/time", "data"),
                          ("/api/v2/public/time", "data"), ("/api/v1/futures/market/time", "data")):
            r = raw(self.sim, "GET", path)
            self.assertEqual(r.status, 200, path)
            self.assertIn(key, r.body)

    def test_binance_auth_errors(self):
        s, now = self.sim, self.sim.now()
        q = f"symbol=BTCUSDT&timestamp={now}"
        sig = base.hmac_hex("binance-secret", q)
        self.assertEqual(raw(s, "GET", "/fapi/v2/account", q + "&signature=" + sig, {"X-MBX-APIKEY": "wrong"}).body["code"], -2015)
        self.assertEqual(raw(s, "GET", "/fapi/v2/account", q + "&signature=bad", {"X-MBX-APIKEY": "binance-key"}).body["code"], -1022)
        self.assertEqual(raw(s, "GET", "/fapi/v2/account", q, {"X-MBX-APIKEY": "binance-key"}).body["code"], -1102)
        old = f"symbol=BTCUSDT&timestamp={now - 6000}"
        r = raw(s, "GET", "/fapi/v2/account", old + "&signature=" + base.hmac_hex("binance-secret", old), {"X-MBX-APIKEY": "binance-key"})
        self.assertEqual((r.status, r.body["code"]), (400, -1021))
        ahead = f"symbol=BTCUSDT&timestamp={now + 1500}"
        r = raw(s, "GET", "/fapi/v2/account", ahead + "&signature=" + base.hmac_hex("binance-secret", ahead), {"X-MBX-APIKEY": "binance-key"})
        self.assertEqual(r.body["code"], -1021)
        wide = f"symbol=BTCUSDT&recvWindow=60000&timestamp={now - 6000}"
        r = raw(s, "GET", "/fapi/v2/account", wide + "&signature=" + base.hmac_hex("binance-secret", wide), {"X-MBX-APIKEY": "binance-key"})
        self.assertEqual(r.status, 200)
        junk = "timestamp=abc"
        r = raw(s, "GET", "/fapi/v2/account", junk + "&signature=" + base.hmac_hex("binance-secret", junk), {"X-MBX-APIKEY": "binance-key"})
        self.assertEqual(r.body["code"], -1102)

    def test_bybit_auth_errors(self):
        s, now = self.sim, str(self.sim.now())
        h = {"X-BAPI-API-KEY": "bybit-key", "X-BAPI-TIMESTAMP": now, "X-BAPI-RECV-WINDOW": "5000"}
        self.assertEqual(raw(s, "GET", "/v5/position/list", "category=linear", dict(h, **{"X-BAPI-API-KEY": "x"})).body["retCode"], 10003)
        self.assertEqual(raw(s, "GET", "/v5/position/list", "category=linear", {"X-BAPI-API-KEY": "bybit-key"}).body["retCode"], 10003)
        self.assertEqual(raw(s, "GET", "/v5/position/list", "category=linear", dict(h, **{"X-BAPI-SIGN": "bad"})).body["retCode"], 10004)
        old = str(int(now) - 6000)
        sig = base.hmac_hex("bybit-secret", old + "bybit-key" + "5000" + "category=linear")
        r = raw(s, "GET", "/v5/position/list", "category=linear", dict(h, **{"X-BAPI-TIMESTAMP": old, "X-BAPI-SIGN": sig}))
        self.assertEqual((r.status, r.body["retCode"]), (200, 10002))
        sig = base.hmac_hex("bybit-secret", "zz" + "bybit-key" + "5000" + "category=linear")
        r = raw(s, "GET", "/v5/position/list", "category=linear", dict(h, **{"X-BAPI-TIMESTAMP": "zz", "X-BAPI-SIGN": sig}))
        self.assertEqual(r.body["retCode"], 10002)
        good = base.hmac_hex("bybit-secret", now + "bybit-key" + "5000" + "category=linear")
        self.assertEqual(raw(s, "GET", "/v5/position/list", "category=linear", dict(h, **{"X-BAPI-SIGN": good})).body["retCode"], 0)

    def test_okx_auth_errors(self):
        s = self.sim
        ts = okx.ms_to_iso(s.now())
        path = "/api/v5/account/balance"
        h = {"OK-ACCESS-KEY": "okx-key", "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": "okx-pass"}
        self.assertEqual(raw(s, "GET", path, "", h).body["code"], "50103")                         # 缺 SIGN
        h["OK-ACCESS-SIGN"] = "bad"
        self.assertEqual(raw(s, "GET", path, "", h).body["code"], "50113")
        self.assertEqual(raw(s, "GET", path, "", dict(h, **{"OK-ACCESS-KEY": "x"})).body["code"], "50111")
        self.assertEqual(raw(s, "GET", path, "", dict(h, **{"OK-ACCESS-PASSPHRASE": "x"})).body["code"], "50105")
        old = okx.ms_to_iso(s.now() - 31000)
        sig = base.hmac_b64("okx-secret", old + "GET" + path)
        r = raw(s, "GET", path, "", dict(h, **{"OK-ACCESS-TIMESTAMP": old, "OK-ACCESS-SIGN": sig}))
        self.assertEqual((r.status, r.body["code"]), (401, "50102"))
        sig = base.hmac_b64("okx-secret", "junk" + "GET" + path)
        self.assertEqual(raw(s, "GET", path, "", dict(h, **{"OK-ACCESS-TIMESTAMP": "junk", "OK-ACCESS-SIGN": sig})).body["code"], "50102")
        good = base.hmac_b64("okx-secret", ts + "GET" + path)
        self.assertEqual(raw(s, "GET", path, "", dict(h, **{"OK-ACCESS-SIGN": good})).body["code"], "0")

    def test_bitget_auth_errors(self):
        s = self.sim
        ts = str(s.now())
        path, q = "/api/v2/mix/position/all-position", "productType=USDT-FUTURES&marginCoin=USDT"
        h = {"ACCESS-KEY": "bitget-key", "ACCESS-TIMESTAMP": ts, "ACCESS-PASSPHRASE": "bitget-pass"}
        self.assertEqual(raw(s, "GET", path, q, h).body["code"], "40001")
        h["ACCESS-SIGN"] = "bad"
        r = raw(s, "GET", path, q, h)
        self.assertEqual((r.status, r.body["code"], r.body["data"]), (400, "40009", None))
        self.assertEqual(raw(s, "GET", path, q, dict(h, **{"ACCESS-KEY": "x"})).body["code"], "40006")
        self.assertEqual(raw(s, "GET", path, q, dict(h, **{"ACCESS-PASSPHRASE": "x"})).body["code"], "40012")
        old = str(s.now() - 31000)
        sig = base.hmac_b64("bitget-secret", old + "GET" + path + "?" + q)
        self.assertEqual(raw(s, "GET", path, q, dict(h, **{"ACCESS-TIMESTAMP": old, "ACCESS-SIGN": sig})).body["code"], "40008")
        sig = base.hmac_b64("bitget-secret", "junk" + "GET" + path + "?" + q)
        self.assertEqual(raw(s, "GET", path, q, dict(h, **{"ACCESS-TIMESTAMP": "junk", "ACCESS-SIGN": sig})).body["code"], "40008")
        good = base.hmac_b64("bitget-secret", ts + "GET" + path + "?" + q)
        self.assertEqual(raw(s, "GET", path, q, dict(h, **{"ACCESS-SIGN": good})).body["code"], "00000")

    def test_bitunix_auth_errors_and_nonce_replay(self):
        s = self.sim
        ts = str(s.now())
        path, q = "/api/v1/futures/account", "marginCoin=USDT"
        self.assertEqual(raw(s, "GET", path, q, {}).body["code"], 10003)
        self.assertEqual(raw(s, "GET", path, q, {"api-key": "x"}).body["code"], 10003)
        h = {"api-key": "bitunix-key", "timestamp": ts}
        self.assertEqual(raw(s, "GET", path, q, h).body["code"], 10002)                            # 缺 nonce/sign
        nonce = "n" * 32

        def sign(nonce_, ts_):
            return base.sha256_hex(base.sha256_hex(nonce_ + ts_ + "bitunix-key" + "marginCoinUSDT" + "") + "bitunix-secret")
        hh = dict(h, nonce=nonce, sign="bad")
        self.assertEqual(raw(s, "GET", path, q, hh).body["code"], 10007)
        old = str(s.now() - 31000)
        self.assertEqual(raw(s, "GET", path, q, dict(h, nonce=nonce, timestamp=old, sign=sign(nonce, old))).body["code"], 10002)
        self.assertEqual(raw(s, "GET", path, q, dict(h, nonce=nonce, timestamp="j", sign=sign(nonce, "j"))).body["code"], 10002)
        self.assertEqual(raw(s, "GET", path, q, dict(h, nonce="short", sign=sign("short", ts))).body["code"], 10008)
        ok = dict(h, nonce=nonce, sign=sign(nonce, ts))
        self.assertEqual(raw(s, "GET", path, q, ok).body["code"], 0)
        self.assertEqual(raw(s, "GET", path, q, ok).body["code"], 10008)                             # nonce 重放


class TestOpsAllDialects(unittest.TestCase):
    """每个方言跑一遍 13 个操作（通过适配器 → 进程内 handle），检查规范结果与关键错误码。"""

    def run_dialect(self, name):
        sim = inproc_sim()
        ad = inproc_adapter(name, sim)
        self.assertGreater(ad.server_time(), 1_600_000_000_000)
        inst = ad.instruments()
        self.assertEqual(sorted(i["symbol"] for i in inst), ["BTCUSDT", "ETHUSDT"])
        self.assertEqual(ad.ticker("BTCUSDT"), 60000.0)
        sim.tick(2)
        self.assertEqual(len(ad.klines("BTCUSDT")), 1)
        self.assertEqual(ad.account()["wallet"], 10000.0)
        self.assertEqual({k: v for k, v in ad.positions().items() if v}, {})
        self.assertEqual(ad.set_leverage("BTCUSDT", 20), 20)
        self.assertEqual(ad.set_margin_mode("BTCUSDT", "isolated"), "isolated")
        self.assertEqual(sim.engine.position("BTCUSDT")["margin_mode"], "isolated")
        self.assertEqual(ad.set_margin_mode("BTCUSDT", "cross"), "cross")
        o = ad.place("BTCUSDT", "BUY", "LIMIT", 0.01, 59000.0, cid=f"{name}-1")
        self.assertEqual(o["cid"], f"{name}-1")
        with self.assertRaises(ExchangeError) as cm:
            ad.place("BTCUSDT", "BUY", "LIMIT", 0.01, 59000.0, cid=f"{name}-1")
        self.assertEqual(cm.exception.kind, "DUP_CID")
        self.assertEqual(len(sim.engine.all_orders()), 1)
        q = ad.query("BTCUSDT", cid=f"{name}-1")
        self.assertEqual((q["status"], q["qty"], q["price"], q["side"], q["type"], q["id"]), ("NEW", 0.01, 59000.0, "BUY", "LIMIT", o["id"]))
        self.assertIsNone(ad.query("BTCUSDT", order_id=424242))
        self.assertEqual([x["id"] for x in ad.open_orders("BTCUSDT")], [o["id"]])
        self.assertEqual(ad.fills("BTCUSDT"), [])
        sim.set_price("BTCUSDT", 58990.0)
        sim.set_price("BTCUSDT", 60000.0)
        q = ad.query("BTCUSDT", order_id=o["id"])
        self.assertEqual((q["status"], q["filled"], q["avg_price"]), ("FILLED", 0.01, 59000.0))
        self.assertEqual(ad.positions(), {"BTCUSDT": 0.01})
        f = ad.fills("BTCUSDT")
        self.assertEqual((len(f), f[0]["qty"], f[0]["price"], f[0]["order_id"], f[0]["side"]), (1, 0.01, 59000.0, o["id"], "BUY"))
        self.assertAlmostEqual(f[0]["fee"], 0.01 * 59000 * 0.0002)
        m = ad.place("BTCUSDT", "SELL", "MARKET", 0.01, reduce_only=True)
        self.assertEqual(ad.query("BTCUSDT", order_id=m["id"])["status"], "FILLED")
        self.assertEqual({k: v for k, v in ad.positions().items() if v}, {})
        o2 = ad.place("BTCUSDT", "BUY", "LIMIT", 0.02, 58000.0, cid=f"{name}-2")
        c = ad.cancel("BTCUSDT", order_id=o2["id"])
        self.assertEqual(c["status"], "CANCELED")
        self.assertEqual(ad.query("BTCUSDT", cid=f"{name}-2")["status"], "CANCELED")
        with self.assertRaises(ExchangeError) as cm:
            ad.cancel("BTCUSDT", order_id=o2["id"])
        self.assertIn(cm.exception.kind, ("ORDER_NOT_FOUND", "ORDER_NOT_OPEN"))
        with self.assertRaises(ExchangeError) as cm:
            ad.cancel("BTCUSDT", order_id=99999)
        self.assertIn(cm.exception.kind, ("ORDER_NOT_FOUND", "ORDER_NOT_OPEN"))
        o3 = ad.place("BTCUSDT", "BUY", "LIMIT", 0.03, 57000.0, cid=f"{name}-3")
        self.assertEqual(ad.cancel("BTCUSDT", cid=f"{name}-3")["id"], o3["id"])
        self.run_rejections(name, sim, ad)
        return sim, ad

    def run_rejections(self, name, sim, ad):
        for kind, args in (("INSUFFICIENT", ("BTCUSDT", "BUY", "LIMIT", 5.0, 59000.0)),
                           ("REDUCE_ONLY", ("BTCUSDT", "SELL", "MARKET", 0.01)),
                           ("BAD_PRICE", ("BTCUSDT", "BUY", "LIMIT", 0.01, 59000.05)),
                           ("BAD_QTY", ("BTCUSDT", "BUY", "LIMIT", 0.0105, 59000.0))):
            with self.assertRaises(ExchangeError) as cm:
                ad.place(*args, reduce_only=(kind == "REDUCE_ONLY"))
            accepted = {kind, "BAD_PARAM"} | ({"BAD_QTY", "BAD_PRICE"} if kind in ("BAD_QTY", "BAD_PRICE") else set())
            self.assertIn(cm.exception.kind, accepted, (name, kind, cm.exception))      # 精度错在多家同码
        with self.assertRaises(ExchangeError):
            ad.set_leverage("BTCUSDT", 999)
        sim.engine.set_symbol("BTCUSDT", status="SUSPENDED")
        with self.assertRaises(ExchangeError) as cm:
            ad.place("BTCUSDT", "BUY", "LIMIT", 0.01, 59000.0)
        self.assertIn(cm.exception.kind, ("SYMBOL_SUSPENDED", "BAD_PARAM"))
        sim.engine.set_symbol("BTCUSDT", status="DELISTED")
        self.assertEqual([i["symbol"] for i in ad.instruments()], ["ETHUSDT"])

    def test_binance(self):
        self.run_dialect("binance")

    def test_bybit(self):
        self.run_dialect("bybit")

    def test_okx(self):
        self.run_dialect("okx")

    def test_bitget(self):
        self.run_dialect("bitget")

    def test_bitunix(self):
        self.run_dialect("bitunix")


class TestShapes(unittest.TestCase):
    """各家报文的形状细节（字段名 / 类型 / 顺序）——SHAPES.md 里写的就是这些。"""

    def setUp(self):
        self.sim = inproc_sim()
        self.ads = {n: inproc_adapter(n, self.sim) for n in NAMES}
        self.sim.tick(1)

    def test_binance_shapes(self):
        ad, s = self.ads["binance"], self.sim
        info = ad.call("instruments", "GET", "/fapi/v1/exchangeInfo", signed=False)
        self.assertEqual(info["symbols"][0]["filters"][0]["filterType"], "PRICE_FILTER")
        self.assertEqual(info["symbols"][0]["contractType"], "PERPETUAL")
        self.assertIsInstance(ad.call("ticker", "GET", "/fapi/v1/ticker/price", signed=False), list)
        k = ad.call("klines", "GET", "/fapi/v1/klines", {"symbol": "BTCUSDT", "interval": "1m"}, signed=False)
        self.assertEqual(len(k[0]), 12)
        self.assertIsInstance(k[0][0], int)
        with self.assertRaises(ExchangeError):
            ad.call("klines", "GET", "/fapi/v1/klines", {"symbol": "BTCUSDT", "interval": "7m"}, signed=False)
        o = ad.call("place", "POST", "/fapi/v1/order", {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.01"})
        self.assertEqual((o["status"], o["positionSide"], o["type"]), ("FILLED", "BOTH", "MARKET"))
        self.assertTrue(o["clientOrderId"].startswith("auto"))
        acct = ad.call("account", "GET", "/fapi/v2/account")
        self.assertEqual(acct["assets"][0]["asset"], "USDT")
        self.assertEqual(acct["positions"][0]["positionAmt"], "0.01")
        pr = ad.call("positions", "GET", "/fapi/v2/positionRisk", {"symbol": "BTCUSDT"})
        self.assertEqual((pr[0]["marginType"], pr[0]["positionSide"]), ("cross", "BOTH"))
        self.assertEqual(ad.call("positions", "GET", "/fapi/v2/positionRisk", {"symbol": "ETHUSDT"}), [])
        r = ad.call("margin_mode", "POST", "/fapi/v1/marginType", {"symbol": "ETHUSDT", "marginType": "ISOLATED"})
        self.assertEqual(r, {"code": 200, "msg": "success"})
        with self.assertRaises(ExchangeError) as cm:
            ad.call("margin_mode", "POST", "/fapi/v1/marginType", {"symbol": "ETHUSDT", "marginType": "ISOLATED"})
        self.assertEqual(cm.exception.code, -4046)
        with self.assertRaises(ExchangeError) as cm:
            ad.call("margin_mode", "POST", "/fapi/v1/marginType", {"symbol": "ETHUSDT", "marginType": "WEIRD"})
        self.assertEqual(cm.exception.code, -1102)
        with self.assertRaises(ExchangeError) as cm:
            ad.call("cancel", "DELETE", "/fapi/v1/order", {"symbol": "BTCUSDT"})
        self.assertEqual(cm.exception.code, -1102)
        t = ad.call("fills", "GET", "/fapi/v1/userTrades", {"symbol": "BTCUSDT"})
        self.assertEqual((t[0]["buyer"], t[0]["maker"], t[0]["commissionAsset"]), (True, False, "USDT"))

    def test_bybit_shapes(self):
        ad = self.ads["bybit"]
        env = ad.t.request(*ad.sign("GET", "/v5/market/time", {}, None, False)[:1], *ad.sign("GET", "/v5/market/time", {}, None, False)[1:3])
        body = json.loads(env[2])
        self.assertEqual(set(body), {"retCode", "retMsg", "result", "retExtInfo", "time"})
        with self.assertRaises(ExchangeError) as cm:
            ad.call("ticker", "GET", "/v5/market/tickers", {"category": "spot", "symbol": "BTCUSDT"}, signed=False)
        self.assertEqual(cm.exception.code, 10001)
        with self.assertRaises(ExchangeError):
            ad.call("ticker", "GET", "/v5/market/tickers", {"category": "linear", "symbol": "DOGEUSDT"}, signed=False)
        k = ad.call("klines", "GET", "/v5/market/kline", {"category": "linear", "symbol": "BTCUSDT", "interval": "1"}, signed=False)
        self.assertEqual(len(k["list"][0]), 7)
        self.assertIsInstance(k["list"][0][0], str)
        with self.assertRaises(ExchangeError):
            ad.call("klines", "GET", "/v5/market/kline", {"category": "linear", "symbol": "BTCUSDT", "interval": "7"}, signed=False)
        with self.assertRaises(ExchangeError):
            ad.call("account", "GET", "/v5/account/wallet-balance", {"accountType": "SPOT"})
        pos = ad.call("positions", "GET", "/v5/position/list", {"category": "linear", "symbol": "BTCUSDT"})["list"]
        self.assertEqual((pos[0]["side"], pos[0]["size"], pos[0]["liqPrice"]), ("", "0", ""))
        ad.place("BTCUSDT", "SELL", "MARKET", 0.01)
        pos = ad.call("positions", "GET", "/v5/position/list", {"category": "linear"})["list"]
        self.assertEqual((pos[0]["side"], pos[0]["size"]), ("Sell", "0.01"))
        with self.assertRaises(ExchangeError) as cm:
            ad.call("leverage", "POST", "/v5/position/set-leverage", body={"category": "linear", "symbol": "BTCUSDT", "buyLeverage": "10", "sellLeverage": "10"})
        self.assertEqual(cm.exception.code, 110043)
        with self.assertRaises(ExchangeError):
            ad.call("leverage", "POST", "/v5/position/set-leverage", body={"category": "linear", "symbol": "BTCUSDT", "buyLeverage": "10", "sellLeverage": "11"})
        with self.assertRaises(ExchangeError) as cm:
            ad.call("margin_mode", "POST", "/v5/position/switch-isolated", body={"category": "linear", "symbol": "BTCUSDT", "tradeMode": 0})
        self.assertEqual(cm.exception.code, 110026)
        with self.assertRaises(ExchangeError):
            ad.call("margin_mode", "POST", "/v5/position/switch-isolated", body={"category": "linear", "symbol": "BTCUSDT", "tradeMode": 7})
        with self.assertRaises(ExchangeError):
            ad.call("cancel", "POST", "/v5/order/cancel", body={"category": "linear", "symbol": "BTCUSDT"})
        ex = ad.call("fills", "GET", "/v5/execution/list", {"category": "linear"})["list"]
        self.assertEqual((ex[0]["execType"], ex[0]["isMaker"], ex[0]["orderType"]), ("Trade", False, "Market"))
        hdr = self.sim.dialects["bybit"].rate_limit_headers(500, type("C", (), {"now": lambda self: 1000})())
        self.assertEqual(hdr["X-Bapi-Limit-Reset-Timestamp"], "1500")

    def test_okx_shapes(self):
        ad = self.ads["okx"]
        with self.assertRaises(ExchangeError):
            ad.call("instruments", "GET", "/api/v5/public/instruments", {"instType": "SPOT"}, signed=False)
        inst = ad.call("instruments", "GET", "/api/v5/public/instruments", {"instType": "SWAP", "instId": "BTC-USDT-SWAP"}, signed=False)
        self.assertEqual((inst[0]["ctVal"], inst[0]["lotSz"], inst[0]["state"], inst[0]["minSz"]), ("0.01", "1", "live", "1"))
        k = ad.call("klines", "GET", "/api/v5/market/candles", {"instId": "BTC-USDT-SWAP", "bar": "1m"}, signed=False)
        self.assertEqual((len(k[0]), k[0][8]), (9, "1"))
        with self.assertRaises(ExchangeError):
            ad.call("klines", "GET", "/api/v5/market/candles", {"instId": "BTC-USDT-SWAP", "bar": "7m"}, signed=False)
        with self.assertRaises(ExchangeError) as cm:          # tdMode 缺
            ad.call("place", "POST", "/api/v5/trade/order", body={"instId": "BTC-USDT-SWAP", "side": "buy", "ordType": "market", "sz": "1"})
        self.assertEqual((cm.exception.code, cm.exception.kind), ("51000", "BAD_PARAM"))
        with self.assertRaises(ExchangeError) as cm:          # tdMode 与持仓模式不一致
            ad.call("place", "POST", "/api/v5/trade/order", body={"instId": "BTC-USDT-SWAP", "tdMode": "isolated", "side": "buy", "ordType": "market", "sz": "1"})
        self.assertEqual(cm.exception.code, "51000")
        r = ad.call("place", "POST", "/api/v5/trade/order", body={"instId": "BTC-USDT-SWAP", "tdMode": "cross", "side": "buy", "ordType": "market", "sz": "2"})
        self.assertEqual((r[0]["sCode"], r[0]["clOrdId"]), ("0", ""))
        self.assertEqual(self.sim.engine.position("BTCUSDT")["qty"], 0.02)                 # 2 张 × 0.01
        pos = ad.call("positions", "GET", "/api/v5/account/positions", {"instType": "SWAP"})
        self.assertEqual((pos[0]["pos"], pos[0]["posSide"], pos[0]["mgnMode"]), ("2", "net", "cross"))
        q = ad.call("query", "GET", "/api/v5/trade/order", {"instId": "BTC-USDT-SWAP", "ordId": r[0]["ordId"]})[0]
        self.assertEqual((q["sz"], q["accFillSz"], q["state"], q["reduceOnly"]), ("2", "2", "filled", "false"))
        with self.assertRaises(ExchangeError) as cm:
            ad.call("query", "GET", "/api/v5/trade/order", {"instId": "BTC-USDT-SWAP"})
        self.assertEqual(cm.exception.code, "51000")
        with self.assertRaises(ExchangeError) as cm:
            ad.call("cancel", "POST", "/api/v5/trade/cancel-order", body={"instId": "BTC-USDT-SWAP"})
        self.assertEqual(cm.exception.code, "51000")
        f = ad.call("fills", "GET", "/api/v5/trade/fills", {"instType": "SWAP"})
        self.assertEqual((f[0]["fillSz"], f[0]["execType"], f[0]["fee"][0]), ("2", "T", "-"))
        with self.assertRaises(ExchangeError):
            ad.call("leverage", "POST", "/api/v5/account/set-leverage", body={"instId": "BTC-USDT-SWAP", "lever": "5", "mgnMode": "weird"})
        self.assertEqual(ad.call("margin_mode", "POST", "/api/v5/account/set-position-mode", body={"posMode": "net_mode"}), [{"posMode": "net_mode"}])
        with self.assertRaises(ExchangeError):
            ad.call("margin_mode", "POST", "/api/v5/account/set-position-mode", body={"posMode": "long_short_mode"})
        d = self.sim.dialects["okx"]
        self.assertEqual(d.order_error("RATE_LIMIT", "", None, None).status, 429)
        self.assertEqual(ad.set_margin_mode("ETHUSDT", "isolated"), "isolated")
        self.assertEqual(self.sim.engine.position("ETHUSDT")["margin_mode"], "isolated")

    def test_bitget_shapes(self):
        ad = self.ads["bitget"]
        with self.assertRaises(ExchangeError):
            ad.call("ticker", "GET", "/api/v2/mix/market/ticker", {"productType": "COIN-FUTURES", "symbol": "BTCUSDT"}, signed=False)
        k = ad.call("klines", "GET", "/api/v2/mix/market/candles", dict(ad.P, symbol="BTCUSDT", granularity="1m"), signed=False)
        self.assertEqual(len(k[0]), 7)
        with self.assertRaises(ExchangeError):
            ad.call("klines", "GET", "/api/v2/mix/market/candles", dict(ad.P, symbol="BTCUSDT", granularity="7m"), signed=False)
        c = ad.call("instruments", "GET", "/api/v2/mix/market/contracts", dict(ad.P, symbol="BTCUSDT"), signed=False)
        self.assertEqual((c[0]["symbolStatus"], c[0]["pricePlace"], c[0]["priceEndStep"], c[0]["sizeMultiplier"]), ("normal", "1", "1", "0.001"))
        with self.assertRaises(ExchangeError) as cm:
            ad.call("place", "POST", "/api/v2/mix/order/place-order", body=dict(ad.P, symbol="BTCUSDT", marginMode="weird", marginCoin="USDT",
                                                                               size="0.01", side="buy", orderType="market"))
        self.assertEqual(cm.exception.code, "40808")
        r = ad.call("place", "POST", "/api/v2/mix/order/place-order", body=dict(ad.P, symbol="BTCUSDT", marginMode="crossed", marginCoin="USDT",
                                                                               size="0.01", side="buy", orderType="market", clientOid="bg1"))
        self.assertEqual(r["clientOid"], "bg1")
        pend = ad.call("open_orders", "GET", "/api/v2/mix/order/orders-pending", ad.P)
        self.assertEqual(pend, {"entrustedList": [], "endId": ""})
        d = ad.call("query", "GET", "/api/v2/mix/order/detail", dict(ad.P, symbol="BTCUSDT", clientOid="bg1"))
        self.assertEqual((d["state"], d["reduceOnly"], d["marginMode"], d["force"]), ("filled", "NO", "crossed", "gtc"))
        with self.assertRaises(ExchangeError):
            ad.call("query", "GET", "/api/v2/mix/order/detail", dict(ad.P, symbol="BTCUSDT"))
        with self.assertRaises(ExchangeError):
            ad.call("cancel", "POST", "/api/v2/mix/order/cancel-order", body=dict(ad.P, symbol="BTCUSDT"))
        f = ad.call("fills", "GET", "/api/v2/mix/order/fills", ad.P)
        self.assertEqual((f["fillList"][0]["tradeScope"], f["fillList"][0]["feeDetail"][0]["feeCoin"]), ("taker", "USDT"))
        pos = ad.call("positions", "GET", "/api/v2/mix/position/all-position", dict(ad.P, marginCoin="USDT"))
        self.assertEqual((pos[0]["holdSide"], pos[0]["total"], pos[0]["marginMode"]), ("long", "0.01", "crossed"))
        acct = ad.call("account", "GET", "/api/v2/mix/account/account", dict(ad.P, symbol="BTCUSDT", marginCoin="USDT"))
        self.assertEqual(acct["marginMode"], "crossed")
        with self.assertRaises(ExchangeError):
            ad.call("margin_mode", "POST", "/api/v2/mix/account/set-margin-mode", body=dict(ad.P, symbol="ETHUSDT", marginCoin="USDT", marginMode="weird"))
        lv = ad.call("leverage", "POST", "/api/v2/mix/account/set-leverage", body=dict(ad.P, symbol="ETHUSDT", marginCoin="USDT", leverage="7"))
        self.assertEqual((lv["longLeverage"], lv["marginMode"]), ("7", "crossed"))
        mm = ad.call("margin_mode", "POST", "/api/v2/mix/account/set-margin-mode", body=dict(ad.P, symbol="ETHUSDT", marginCoin="USDT", marginMode="isolated"))
        self.assertEqual(mm["marginMode"], "isolated")

    def test_bitunix_shapes(self):
        ad = self.ads["bitunix"]
        t = ad.call("ticker", "GET", "/api/v1/futures/market/tickers", {"symbols": "BTCUSDT,ETHUSDT"}, signed=False)
        self.assertEqual([x["symbol"] for x in t], ["BTCUSDT", "ETHUSDT"])
        with self.assertRaises(ExchangeError) as cm:
            ad.call("ticker", "GET", "/api/v1/futures/market/tickers", {"symbols": "DOGEUSDT"}, signed=False)
        self.assertEqual(cm.exception.code, 20001)
        k = ad.call("klines", "GET", "/api/v1/futures/market/kline", {"symbol": "BTCUSDT", "interval": "1m"}, signed=False)
        self.assertEqual(set(k[0]), {"open", "high", "low", "close", "quoteVol", "baseVol", "time", "type"})
        with self.assertRaises(ExchangeError):
            ad.call("klines", "GET", "/api/v1/futures/market/kline", {"symbol": "BTCUSDT", "interval": "7m"}, signed=False)
        with self.assertRaises(ExchangeError):
            ad.call("account", "GET", "/api/v1/futures/account", {"marginCoin": "BTC"})
        ins = ad.call("instruments", "GET", "/api/v1/futures/market/trading_pairs", {"symbols": "BTCUSDT"}, signed=False)
        self.assertEqual((len(ins), ins[0]["symbolStatus"]), (1, "OPEN"))
        r = ad.call("place", "POST", "/api/v1/futures/trade/place_order", body={"symbol": "BTCUSDT", "qty": "0.01", "side": "BUY", "orderType": "MARKET", "clientId": "bu1"})
        self.assertEqual(r["clientId"], "bu1")
        d = ad.call("query", "GET", "/api/v1/futures/trade/get_order_detail", {"clientId": "bu1"})
        self.assertEqual((d["status"], d["marginMode"], d["positionMode"], d["effect"]), ("FILLED", "CROSS", "ONE_WAY", "GTC"))
        with self.assertRaises(ExchangeError):
            ad.call("query", "GET", "/api/v1/futures/trade/get_order_detail", {})
        pos = ad.call("positions", "GET", "/api/v1/futures/position/get_pending_positions", {"symbol": "BTCUSDT"})
        self.assertEqual((pos[0]["side"], pos[0]["qty"]), ("BUY", "0.01"))
        self.assertEqual(ad.call("positions", "GET", "/api/v1/futures/position/get_pending_positions", {"symbol": "ETHUSDT"}), [])
        with self.assertRaises(ExchangeError):
            ad.call("cancel", "POST", "/api/v1/futures/trade/cancel_orders", body={"symbol": "BTCUSDT", "orderList": []})
        c = ad.call("cancel", "POST", "/api/v1/futures/trade/cancel_orders", body={"symbol": "BTCUSDT", "orderList": [{"orderId": "999"}, {"clientId": "bu1"}]})
        self.assertEqual((c["successList"], len(c["failureList"]), c["failureList"][0]["errorCode"]), ([], 2, 20007))
        tr = ad.call("fills", "GET", "/api/v1/futures/trade/get_history_trades", {"symbol": "BTCUSDT"})
        self.assertEqual((tr["total"], tr["tradeList"][0]["roleType"]), (1, "TAKER"))
        with self.assertRaises(ExchangeError):
            ad.call("margin_mode", "POST", "/api/v1/futures/account/change_margin_mode", body={"symbol": "ETHUSDT", "marginCoin": "USDT", "marginMode": "weird"})
        mm = ad.call("margin_mode", "POST", "/api/v1/futures/account/change_margin_mode", body={"symbol": "ETHUSDT", "marginCoin": "USDT", "marginMode": "ISOLATION"})
        self.assertEqual(mm["marginMode"], "ISOLATION")
        po = ad.call("open_orders", "GET", "/api/v1/futures/trade/get_pending_orders", {})
        self.assertEqual(po["total"], 0)


if __name__ == "__main__":
    unittest.main()
