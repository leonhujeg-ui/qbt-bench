"""钉死常量与边界（首轮突变漏网后补的判据）：各家错误码 / 时间窗 / K 线间隔 / K 线行格式 / 缺省参数 / 鉴权路由 / 边界比较。
错误码表全部“凭记忆”（SHAPES.md），这里钉住的是“改它必须是有意的”，不是“它一定对”。"""
import json
import os
import time
import unittest

from qdexsim import adapters, chaos as chaos_mod, clients, common, engine as engine_mod, feed as feed_mod, harness, judges, recon, store as store_mod
from qdexsim.adapters import ExchangeError
from qdexsim.dialects import base, binance, bitget, bitunix, bybit, okx
from qdexsim.tests._util import inproc_adapter, inproc_sim, tmpdir, wire
from qdexsim.tests.test_clients import intent

GOLDEN_ERRORS = {
    "binance": {"BAD_SIGN": (400, -1022), "BAD_TIMESTAMP": (400, -1021), "BAD_KEY": (401, -2015), "MISSING_AUTH": (400, -1102),
                "NOT_FOUND": (404, -1121), "BAD_PARAM": (400, -1102), "SYMBOL_UNKNOWN": (400, -1121), "SYMBOL_DELISTED": (400, -1121),
                "SYMBOL_SUSPENDED": (400, -2010), "BAD_QTY": (400, -1111), "BAD_PRICE": (400, -1111), "MIN_NOTIONAL": (400, -4164),
                "DUP_CID": (400, -4116), "ORDER_NOT_FOUND": (400, -2013), "ORDER_NOT_OPEN": (400, -2011), "INSUFFICIENT": (400, -2019),
                "REDUCE_ONLY": (400, -2022), "BAD_LEVERAGE": (400, -4028), "MARGIN_MODE_BUSY": (400, -4048), "RATE_LIMIT": (429, -1003),
                "SERVER_ERROR": (500, -1001), "MAINTENANCE": (503, -1016)},
    "bybit": {"BAD_SIGN": (200, 10004), "BAD_TIMESTAMP": (200, 10002), "BAD_KEY": (200, 10003), "MISSING_AUTH": (200, 10003),
              "NOT_FOUND": (404, 10001), "BAD_PARAM": (200, 10001), "SYMBOL_UNKNOWN": (200, 10001), "SYMBOL_DELISTED": (200, 10001),
              "SYMBOL_SUSPENDED": (200, 10001), "BAD_QTY": (200, 10001), "BAD_PRICE": (200, 10001), "MIN_NOTIONAL": (200, 110094),
              "DUP_CID": (200, 110072), "ORDER_NOT_FOUND": (200, 110001), "ORDER_NOT_OPEN": (200, 110001), "INSUFFICIENT": (200, 110007),
              "REDUCE_ONLY": (200, 110017), "BAD_LEVERAGE": (200, 110013), "MARGIN_MODE_BUSY": (200, 110026), "RATE_LIMIT": (200, 10006),
              "SERVER_ERROR": (500, 10016), "MAINTENANCE": (503, 10016)},
    "okx": {"BAD_SIGN": (401, "50113"), "BAD_TIMESTAMP": (401, "50102"), "BAD_KEY": (401, "50111"), "BAD_PASSPHRASE": (401, "50105"),
            "MISSING_AUTH": (401, "50103"), "NOT_FOUND": (404, "404"), "BAD_PARAM": (200, "51000"), "SYMBOL_UNKNOWN": (200, "51001"),
            "SYMBOL_DELISTED": (200, "51001"), "SYMBOL_SUSPENDED": (200, "51000"), "BAD_QTY": (200, "51000"), "BAD_PRICE": (200, "51000"),
            "MIN_NOTIONAL": (200, "51020"), "DUP_CID": (200, "51016"), "ORDER_NOT_FOUND": (200, "51603"), "ORDER_NOT_OPEN": (200, "51400"),
            "INSUFFICIENT": (200, "51008"), "REDUCE_ONLY": (200, "51000"), "BAD_LEVERAGE": (200, "51000"), "MARGIN_MODE_BUSY": (200, "51000"),
            "RATE_LIMIT": (429, "50011"), "SERVER_ERROR": (500, "50026"), "MAINTENANCE": (503, "50001")},
    "bitget": {"BAD_SIGN": (400, "40009"), "BAD_TIMESTAMP": (400, "40008"), "BAD_KEY": (400, "40006"), "BAD_PASSPHRASE": (400, "40012"),
               "MISSING_AUTH": (400, "40001"), "NOT_FOUND": (404, "40404"), "BAD_PARAM": (400, "40808"), "SYMBOL_UNKNOWN": (400, "40034"),
               "SYMBOL_DELISTED": (400, "40034"), "SYMBOL_SUSPENDED": (400, "40020"), "BAD_QTY": (400, "40808"), "BAD_PRICE": (400, "40808"),
               "MIN_NOTIONAL": (400, "45110"), "DUP_CID": (400, "40786"), "ORDER_NOT_FOUND": (400, "40768"), "ORDER_NOT_OPEN": (400, "40768"),
               "INSUFFICIENT": (400, "40762"), "REDUCE_ONLY": (400, "40774"), "BAD_LEVERAGE": (400, "40797"), "MARGIN_MODE_BUSY": (400, "45117"),
               "RATE_LIMIT": (429, "429"), "SERVER_ERROR": (500, "50001"), "MAINTENANCE": (503, "40200")},
    "bitunix": {"BAD_SIGN": (200, 10007), "BAD_TIMESTAMP": (200, 10002), "BAD_KEY": (200, 10003), "BAD_NONCE": (200, 10008),
                "MISSING_AUTH": (200, 10003), "NOT_FOUND": (404, 10001), "BAD_PARAM": (200, 10002), "SYMBOL_UNKNOWN": (200, 20001),
                "SYMBOL_DELISTED": (200, 20001), "SYMBOL_SUSPENDED": (200, 20015), "BAD_QTY": (200, 10008), "BAD_PRICE": (200, 10008),
                "MIN_NOTIONAL": (200, 30002), "DUP_CID": (200, 30008), "ORDER_NOT_FOUND": (200, 20007), "ORDER_NOT_OPEN": (200, 20007),
                "INSUFFICIENT": (200, 20003), "REDUCE_ONLY": (200, 20002), "BAD_LEVERAGE": (200, 20005), "MARGIN_MODE_BUSY": (200, 20006),
                "RATE_LIMIT": (429, 10006), "SERVER_ERROR": (500, 10001), "MAINTENANCE": (503, 20012)},
}
MODULES = {"binance": binance, "bybit": bybit, "okx": okx, "bitget": bitget, "bitunix": bitunix}
MIN, H = 60000, 3600000
GOLDEN_INTERVALS = {
    "binance": {"1m": MIN, "3m": 3 * MIN, "5m": 5 * MIN, "15m": 15 * MIN, "30m": 30 * MIN, "1h": H, "4h": 4 * H, "1d": 24 * H},
    "bybit": {"1": MIN, "3": 3 * MIN, "5": 5 * MIN, "15": 15 * MIN, "30": 30 * MIN, "60": H, "120": 2 * H, "240": 4 * H, "D": 24 * H},
    "okx": {"1m": MIN, "3m": 3 * MIN, "5m": 5 * MIN, "15m": 15 * MIN, "30m": 30 * MIN, "1H": H, "2H": 2 * H, "4H": 4 * H, "1D": 24 * H},
    "bitget": {"1m": MIN, "3m": 3 * MIN, "5m": 5 * MIN, "15m": 15 * MIN, "30m": 30 * MIN, "1H": H, "4H": 4 * H, "1D": 24 * H},
    "bitunix": {"1m": MIN, "3m": 3 * MIN, "5m": 5 * MIN, "15m": 15 * MIN, "30m": 30 * MIN, "1h": H, "4h": 4 * H, "1d": 24 * H},
}
INTERVAL_ATTR = {"binance": "INTERVALS", "bybit": "INTERVALS", "okx": "BARS", "bitget": "GRANULARITY", "bitunix": "INTERVALS"}


class TestGoldenTables(unittest.TestCase):
    def test_error_tables(self):
        for name, mod in MODULES.items():
            got = {k: (v[0], v[1]) for k, v in mod.ERRORS.items()}
            self.assertEqual(got, GOLDEN_ERRORS[name], name)

    def test_interval_tables_and_windows(self):
        for name, mod in MODULES.items():
            self.assertEqual(getattr(mod, INTERVAL_ATTR[name]), GOLDEN_INTERVALS[name], name)
        self.assertEqual((okx.WINDOW_MS, bitget.WINDOW_MS, bitunix.WINDOW_MS, base.Dialect.RECV_WINDOW_MS), (30000, 30000, 30000, 5000))
        self.assertEqual(bybit.Bybit().RECV_WINDOW_MS, 5000)

    def test_engine_defaults(self):
        btc, eth = engine_mod.DEFAULT_SYMBOLS["BTCUSDT"], engine_mod.DEFAULT_SYMBOLS["ETHUSDT"]
        self.assertEqual((btc["tick"], btc["step"], btc["min_qty"], btc["min_notional"], btc["max_leverage"], btc["ct_val"], btc["price_places"], btc["qty_places"]),
                         (0.1, 0.001, 0.001, 5.0, 125, 0.01, 1, 3))
        self.assertEqual((eth["tick"], eth["step"], eth["min_qty"], eth["min_notional"], eth["max_leverage"], eth["ct_val"], eth["price_places"], eth["qty_places"]),
                         (0.01, 0.01, 0.01, 5.0, 100, 0.1, 2, 2))
        self.assertEqual(engine_mod.DEFAULT_CONFIG, {"wallet": 10000.0, "maker_fee": 0.0002, "taker_fee": 0.0005, "funding_rate": 0.0001,
                                                     "mmr": 0.005, "leverage": 10, "margin_mode": "cross", "start": {"BTCUSDT": 60000.0, "ETHUSDT": 3000.0}})
        self.assertEqual(engine_mod.Engine().set_leverage("BTCUSDT", 125), 125)
        self.assertEqual((feed_mod.DEFAULT_START, feed_mod.DEFAULT_LIQUIDITY), ({"BTCUSDT": 60000.0, "ETHUSDT": 3000.0}, {"BTCUSDT": 0.05, "ETHUSDT": 1.0}))
        f = feed_mod.Feed()
        self.assertEqual((f.mode, f.interval_ms, f.vol, f.drift, f.liquidity["BTCUSDT"]), ("random_walk", 20, 0.0005, 0.0, 0.05))
        self.assertEqual((base.Response().status, base.Dialect.f(1e-8), base.Dialect.f(1e-8, 7)), (200, "0.00000001", "0"))
        self.assertEqual((adapters.make("binance", "http://127.0.0.1:1").t.timeout, adapters.DEFAULT_RETRY_MS), (0.3, 1000))
        self.assertEqual(chaos_mod.Chaos({"rules": [{"kind": "rate_limit"}]}).before("x", "d").reject["retry_after_ms"], 1000)


class TestKlineRows(unittest.TestCase):
    """每家 K 线行的每一列都钉死（含 quoteVol = vol × close、closeTime = openTime + ms − 1、limit 截断）。"""

    def setUp(self):
        self.sim = inproc_sim()
        self.sim.engine.place("BTCUSDT", "BUY", "LIMIT", 0.02, 59000.0)
        self.t0 = common.now_ms()
        self.sim.set_price("BTCUSDT", 58990.0)                   # 成交 0.02 → 本 bar 成交量 0.02
        self.sim.set_price("BTCUSDT", 60000.0)
        self.bar = self.sim.candles.get("BTCUSDT")[-1]
        for i in range(150):                                      # 造 150 根历史 bar（给 limit 用）
            self.sim.candles.add("ETHUSDT", 3000.0 + i, 1_700_000_000_000 + i * 60000, 1.0)
        self.big = type(self.sim.candles)(max_bars=2000)
        for i in range(1600):
            self.big.add("ETHUSDT", 3000.0, 1_700_000_000_000 + i * 60000, 1.0)

    def ad(self, name):
        return inproc_adapter(name, self.sim)

    def test_binance(self):
        ad = self.ad("binance")
        row = ad.call("klines", "GET", "/fapi/v1/klines", {"symbol": "BTCUSDT", "interval": "1m"}, signed=False)[-1]
        b = self.bar
        self.assertEqual(row, [b[0], common.fnum(b[1]), common.fnum(b[2]), common.fnum(b[3]), common.fnum(b[4]), "0.02", b[0] + 59999,
                               common.fnum(0.02 * b[4]), 0, "0", "0", "0"])
        self.assertEqual(len(ad.klines("ETHUSDT")), 100)
        self.assertEqual(len(ad.call("klines", "GET", "/fapi/v1/klines", {"symbol": "ETHUSDT", "interval": "1m", "limit": 7}, signed=False)), 7)
        self.assertEqual(len(ad.call("klines", "GET", "/fapi/v1/klines", {"symbol": "ETHUSDT", "interval": "1m", "limit": 5000}, signed=False)), 150)
        self.assertEqual(len(ad.call("klines", "GET", "/fapi/v1/klines", {"symbol": "ETHUSDT", "interval": "1m"}, signed=False)), 150)
        self.assertEqual(ad.klines("BTCUSDT")[-1], [b[0], b[1], b[2], b[3], b[4], 0.02])
        self.sim.candles = self.big
        self.assertEqual(len(ad.call("klines", "GET", "/fapi/v1/klines", {"symbol": "ETHUSDT", "interval": "1m"}, signed=False)), 500)
        self.assertEqual(len(ad.call("klines", "GET", "/fapi/v1/klines", {"symbol": "ETHUSDT", "interval": "1m", "limit": 5000}, signed=False)), 1500)

    def test_bybit(self):
        ad = self.ad("bybit")
        row = ad.call("klines", "GET", "/v5/market/kline", {"category": "linear", "symbol": "BTCUSDT", "interval": "1"}, signed=False)["list"][0]
        b = self.bar
        self.assertEqual(row, [str(b[0]), common.fnum(b[1]), common.fnum(b[2]), common.fnum(b[3]), common.fnum(b[4]), "0.02", common.fnum(0.02 * b[4])])
        self.assertEqual(len(ad.klines("ETHUSDT")), 100)
        self.assertEqual(len(ad.call("klines", "GET", "/v5/market/kline", {"category": "linear", "symbol": "ETHUSDT", "interval": "1"}, signed=False)["list"]), 150)
        self.assertEqual(ad.klines("BTCUSDT")[-1], [b[0], b[1], b[2], b[3], b[4], 0.02])
        self.assertEqual([r[0] for r in ad.klines("ETHUSDT", limit=3)], sorted(r[0] for r in ad.klines("ETHUSDT", limit=3)))

    def test_okx(self):
        ad = self.ad("okx")
        row = ad.call("klines", "GET", "/api/v5/market/candles", {"instId": "BTC-USDT-SWAP", "bar": "1m"}, signed=False)[0]
        b = self.bar
        self.assertEqual(row, [str(b[0]), common.fnum(b[1]), common.fnum(b[2]), common.fnum(b[3]), common.fnum(b[4]), "2", "0.02", common.fnum(0.02 * b[4]), "1"])
        self.assertEqual(ad.klines("BTCUSDT")[-1], [b[0], b[1], b[2], b[3], b[4], 0.02])          # 第 6 列是币量 volCcy，不是张数
        self.assertEqual(len(ad.klines("ETHUSDT")), 100)
        self.assertEqual(len(ad.call("klines", "GET", "/api/v5/market/candles", {"instId": "ETH-USDT-SWAP", "bar": "1m", "limit": 999}, signed=False)), 150)

    def test_bitget(self):
        ad = self.ad("bitget")
        row = ad.call("klines", "GET", "/api/v2/mix/market/candles", dict(ad.P, symbol="BTCUSDT", granularity="1m"), signed=False)[-1]
        b = self.bar
        self.assertEqual(row, [str(b[0]), common.fnum(b[1]), common.fnum(b[2]), common.fnum(b[3]), common.fnum(b[4]), "0.02", common.fnum(0.02 * b[4])])
        self.assertEqual(ad.klines("BTCUSDT")[-1], [b[0], b[1], b[2], b[3], b[4], 0.02])
        self.assertEqual(len(ad.klines("ETHUSDT")), 100)
        self.assertEqual(len(ad.call("klines", "GET", "/api/v2/mix/market/candles", dict(ad.P, symbol="ETHUSDT", granularity="1m", limit=5000), signed=False)), 150)
        self.sim.candles = self.big
        self.assertEqual(len(ad.call("klines", "GET", "/api/v2/mix/market/candles", dict(ad.P, symbol="ETHUSDT", granularity="1m", limit=5000), signed=False)), 1000)

    def test_bitunix(self):
        ad = self.ad("bitunix")
        row = ad.call("klines", "GET", "/api/v1/futures/market/kline", {"symbol": "BTCUSDT", "interval": "1m"}, signed=False)[-1]
        b = self.bar
        self.assertEqual(row, {"open": common.fnum(b[1]), "high": common.fnum(b[2]), "low": common.fnum(b[3]), "close": common.fnum(b[4]),
                               "quoteVol": common.fnum(0.02 * b[4]), "baseVol": "0.02", "time": b[0], "type": "LAST_PRICE"})
        self.assertEqual(ad.klines("BTCUSDT")[-1], [b[0], b[1], b[2], b[3], b[4], 0.02])
        self.assertEqual(len(ad.klines("ETHUSDT")), 100)
        self.assertEqual(len(ad.call("klines", "GET", "/api/v1/futures/market/kline", {"symbol": "ETHUSDT", "interval": "1m", "limit": 150}, signed=False)), 150)
        self.assertEqual(len(ad.call("klines", "GET", "/api/v1/futures/market/kline", {"symbol": "ETHUSDT", "interval": "1m", "limit": 9999}, signed=False)), 150)


class TestRoutesAndShapes(unittest.TestCase):
    def setUp(self):
        self.sim = inproc_sim()

    def test_every_signed_route_rejects_bare_requests_and_public_returns_200(self):
        signed_ops = {"account", "positions", "place", "cancel", "query", "open_orders", "fills", "leverage", "margin_mode"}
        for d in self.sim.dialects.values():
            ops = {op for _m, _p, op, _a in d.ROUTES}
            full = signed_ops | {"time", "instruments", "ticker", "klines"}
            self.assertEqual(ops, full - ({"open_orders"} if d.NAME == "bybit" else set()), d.NAME)   # Bybit 查单与未成交同一路由
            for method, path, op, _need_auth in d.ROUTES:
                need_auth = op in signed_ops                                       # 期望值不能来自被测表本身
                body = b'{"category":"linear"}' if method == "POST" else b""
                resp, _ = self.sim.handle(base.Request(method, path, "category=linear&instType=SWAP&productType=USDT-FUTURES",
                                                       {"Content-Type": "application/json"}, body))
                g = GOLDEN_ERRORS[d.NAME]
                if need_auth:                                                 # 裸请求：必须是鉴权错（缺头 / 密钥错），不能执行
                    auth = {g[k] for k in ("MISSING_AUTH", "BAD_KEY", "BAD_SIGN")}
                    code = resp.body.get("retCode", resp.body.get("code"))
                    self.assertIn((resp.status, code), auth, (d.NAME, path, resp.body))
                else:                                                         # 公开接口：缺参可 400，但绝不能是鉴权错
                    self.assertIn(resp.status, (200, 400), (d.NAME, path))
                    body = resp.body if isinstance(resp.body, dict) else {}
                    code = body.get("retCode", body.get("code"))
                    if g["MISSING_AUTH"][1] != g["BAD_PARAM"][1]:                  # Binance 缺参与缺鉴权同码，无法区分
                        self.assertNotEqual(code, g["MISSING_AUTH"][1], (d.NAME, path))
        self.assertEqual(self.sim.handle(base.Request("GET", "/fapi/v1/ticker/price"))[0].status, 200)
        self.assertEqual(self.sim.handle(base.Request("GET", "/fapi/v1/exchangeInfo"))[0].body["futuresType"], "U_MARGINED")

    def test_binance_cancel_error_mapping_and_query_raise(self):
        ad = inproc_adapter("binance", self.sim)
        with self.assertRaises(ExchangeError) as cm:
            ad.cancel("BTCUSDT", order_id=424242)
        self.assertEqual(cm.exception.code, -2011)                                     # 不存在 → Unknown order sent
        o = ad.place("BTCUSDT", "BUY", "LIMIT", 0.01, 59000.0)
        ad.cancel("BTCUSDT", order_id=o["id"])
        with self.assertRaises(ExchangeError) as cm:
            ad.cancel("BTCUSDT", order_id=o["id"])
        self.assertEqual(cm.exception.code, -2011)                                     # 已撤 → 也是 -2011
        with self.assertRaises(ExchangeError) as cm:
            ad.set_margin_mode("DOGEUSDT", "isolated")                                  # 非“不变”的错要抛
        self.assertEqual(cm.exception.kind, "SYMBOL_UNKNOWN")
        bad = inproc_adapter("binance", self.sim, creds={"secret": "wrong"})
        with self.assertRaises(ExchangeError) as cm:
            bad.query("BTCUSDT", order_id=o["id"])                                     # 非 not-found 的错要抛，不能当 None
        self.assertEqual(cm.exception.kind, "BAD_SIGN")

    def test_bybit_details(self):
        ad = inproc_adapter("bybit", self.sim)
        inst = ad.call("instruments", "GET", "/v5/market/instruments-info", {"category": "linear"}, signed=False)["list"][0]
        self.assertEqual(inst["fundingInterval"], 480)
        tk = ad.call("ticker", "GET", "/v5/market/tickers", {"category": "linear", "symbol": "BTCUSDT"}, signed=False)["list"][0]
        nxt = int(tk["nextFundingTime"])
        now = self.sim.now()
        self.assertTrue(now < nxt <= now + 8 * 3600 * 1000 and nxt % 28800000 == 0)
        with self.assertRaises(ExchangeError):
            ad.call("fills", "GET", "/v5/execution/list", {"category": "spot"})
        with self.assertRaises(ExchangeError):
            ad.call("positions", "GET", "/v5/position/list", {"category": "spot"})
        ad.place("BTCUSDT", "BUY", "MARKET", 0.01)
        ex = ad.call("fills", "GET", "/v5/execution/list", {"category": "linear"})["list"][0]
        self.assertEqual((ex["orderPrice"], ex["orderType"]), ("0", "Market"))
        pos = ad.call("positions", "GET", "/v5/position/list", {"category": "linear", "symbol": "BTCUSDT"})["list"][0]
        self.assertEqual((pos["tradeMode"], pos["positionIdx"]), (0, 0))
        o = ad.call("query", "GET", "/v5/order/realtime", {"category": "linear", "symbol": "BTCUSDT"})
        self.assertEqual(o["list"], [])
        ad.set_margin_mode("ETHUSDT", "isolated")
        ad.place("ETHUSDT", "BUY", "MARKET", 0.1)
        pos = ad.call("positions", "GET", "/v5/position/list", {"category": "linear", "symbol": "ETHUSDT"})["list"][0]
        self.assertEqual(pos["tradeMode"], 1)
        oo = ad.place("ETHUSDT", "SELL", "LIMIT", 0.1, 3300.0)
        row = ad.call("open_orders", "GET", "/v5/order/realtime", {"category": "linear"})["list"][0]
        self.assertEqual((row["positionIdx"], row["orderId"]), (0, oo["id"]))

    def test_okx_details(self):
        ad = inproc_adapter("okx", self.sim)
        self.sim.engine.positions["BTCUSDT"] = self.sim.engine._new_position("BTCUSDT")   # BTC 零仓在前、ETH 非零在后
        ad.place("ETHUSDT", "BUY", "MARKET", 0.1)
        self.assertEqual(list(self.sim.engine.positions), ["BTCUSDT", "ETHUSDT"])
        rows = ad.call("positions", "GET", "/api/v5/account/positions", {"instType": "SWAP"})
        self.assertEqual([r["instId"] for r in rows], ["ETH-USDT-SWAP"])              # 零仓跳过、不中断
        self.assertEqual(rows[0]["pos"], "1")
        self.assertEqual(ad.call("positions", "GET", "/api/v5/account/positions", {"instType": "SWAP", "instId": "BTC-USDT-SWAP"}), [])

    def test_bitget_and_bitunix_details(self):
        bg = inproc_adapter("bitget", self.sim)
        self.assertEqual([i["tick"] for i in bg.instruments()], [0.1, 0.01])
        self.sim.chaos.update({"rules": [{"kind": "zero_once", "op": "account", "max": 1}]})
        a = bg.call("account", "GET", "/api/v2/mix/account/account", dict(bg.P, symbol="BTCUSDT", marginCoin="USDT"))
        self.assertEqual((a["maxTransferOut"], a["available"]), ("0", "0"))
        bu = inproc_adapter("bitunix", self.sim)
        with self.assertRaises(ExchangeError) as cm:
            bu.call("query", "GET", "/api/v1/futures/trade/get_order_detail", {})
        self.assertIn("orderId or clientId", cm.exception.msg)
        bu.set_margin_mode("ETHUSDT", "isolated")
        bu.place("ETHUSDT", "BUY", "MARKET", 0.1)
        tr = bu.call("fills", "GET", "/api/v1/futures/trade/get_history_trades", {"symbol": "ETHUSDT"})["tradeList"][0]
        self.assertEqual(tr["marginMode"], "ISOLATION")
        bu.place("BTCUSDT", "BUY", "MARKET", 0.01)
        tr = bu.call("fills", "GET", "/api/v1/futures/trade/get_history_trades", {"symbol": "BTCUSDT"})["tradeList"][0]
        self.assertEqual(tr["marginMode"], "CROSS")


class TestBoundaries(unittest.TestCase):
    def test_engine_boundaries(self):
        e = engine_mod.Engine()
        e.set_symbol("ETHUSDT", min_notional=30.0)
        self.assertEqual(e.place("ETHUSDT", "BUY", "LIMIT", 0.01, 3000.0)["qty"], 0.01)          # 名义恰好 = 最小名义 → 放行
        o = e.place("BTCUSDT", "BUY", "LIMIT", 0.01, 60000.0)                                     # 买价 == 最新价 → 立刻成交
        self.assertEqual(o["status"], "FILLED")
        p = {"symbol": "BTCUSDT", "qty": -0.5, "entry": 60000.0, "leverage": 10, "margin_mode": "isolated"}
        self.assertAlmostEqual(e.liq_price(p), 60000 * (1 + 0.1 - 0.005))                        # 小空头也走空头公式

    def test_feed_boundaries(self):
        d = tmpdir(self)
        p = os.path.join(d, "t.csv")
        with open(p, "w", encoding="utf-8") as f:
            f.write("symbol,price\nBTCUSDT,61000\nETHUSDT,3100\nBTCUSDT,62000\n")
        f3 = feed_mod.Feed({"mode": "replay", "path": p})
        self.assertEqual([f3.step()[0][1] for _ in range(4)], [61000.0, 3100.0, 62000.0, 61000.0])
        f4 = feed_mod.Feed({"mode": "replay", "path": p, "loop": False})
        self.assertEqual([f4.step() for _ in range(4)], [[("BTCUSDT", 61000.0, 0.05)], [("ETHUSDT", 3100.0, 1.0)], [("BTCUSDT", 62000.0, 0.05)], []])
        with open(p, "w", encoding="utf-8") as f:
            f.write("symbol,price\nDOGEUSDT,0.1\n")
        self.assertEqual(feed_mod.Feed({"mode": "replay", "path": p}).step(), [("DOGEUSDT", 0.1, 0.05)])    # 没配流动性 → 0.05
        with open(p, "w", encoding="utf-8") as f:
            f.write("symbol,px\nBTCUSDT,1\n")
        with self.assertRaises(ValueError):
            feed_mod.load_csv(p)
        bars = [[0, 1, 5, 1, 2, 1], [1000, 2, 0.5, 6, 4, 1]]                      # 第二根 high < low 是故意的：钉死列号
        self.assertEqual(feed_mod.resample(bars, 2000), [[0, 1, 5, 1, 4, 2]])

    def test_chaos_boundaries(self):
        c = chaos_mod.Chaos({"seed": 1, "rules": [{"kind": "latency", "op": "*", "ms": 9, "at_req": 2}]})
        self.assertEqual([c.before("x", "d").delay_ms for _ in range(3)], [0, 9, 0])
        c = chaos_mod.Chaos({"rules": []})
        c.note_rate_limited("d", 1000)
        c.before("x", "d")
        self.assertTrue(0 < c.violations[0]["early_by_ms"] <= 1000)
        path = os.path.join(tmpdir(self), "c.jsonl")
        c2 = chaos_mod.Chaos({"rules": [{"kind": "latency", "ms": 1, "note": "中文"}]}, log_path=path)
        c2.log("x", "d", c2.before("x", "d"), "done", 1)
        with open(path, encoding="utf-8") as f:
            self.assertIn("中文", f.read())                                                     # ensure_ascii=False

    def test_judges_recon_server_store_boundaries(self):
        from qdexsim.tests.test_judges import Fake, ctx
        v = judges.judge_no_duplicate(ctx([], Fake(), intents={}))
        self.assertEqual(v.numbers["max_per_intent"], 0)
        self.assertFalse(judges.judge_position_consistent(ctx([], Fake(positions={"ETHUSDT": 1.0}), positions={})).ok)
        self.assertFalse(judges.judge_position_consistent(ctx([], Fake(positions={}), positions={"ETHUSDT": 1.0})).ok)
        rows = recon.normalize([{"cid": "a", "qty": 1.0, "filled": 1.0, "price": None, "status": "FILLED", "ts": 1, "id": "1"},
                                {"cid": "a", "qty": 1.0, "filled": 1.0, "price": 10.0, "status": "FILLED", "ts": 2, "id": ""}])
        self.assertEqual(recon.aggregate(rows)[0]["price"], 5.0)
        rows[0]["qty"] = None
        self.assertEqual(recon.aggregate(rows)[0]["qty"], 1.0)
        local = recon.normalize([{"id": "2", "cid": "a"}])
        remote = recon.normalize([{"id": "1", "cid": "a"}, {"id": "2", "cid": "b"}])
        pairs, only_l, only_r = recon.match(local, remote)
        self.assertEqual(([(l["id"], r["id"]) for l, r in pairs], [r["id"] for r in only_r]), ([("2", "1")], ["2"]))   # cid 优先于订单号
        sim = inproc_sim()
        self.assertIsNone(sim.positions_before(sim.history[-1][0]))                              # 恰好等于最新快照时刻 → 没有旧值
        sim.store.close()
        st = store_mod.Store(os.path.join(tmpdir(self), "w.db"))
        self.assertEqual((st.conn.execute("PRAGMA journal_mode").fetchone()[0], st.conn.execute("PRAGMA synchronous").fetchone()[0]), ("wal", 1))
        self.assertTrue(st.path.endswith("w.db"))
        self.assertEqual(st.add_event(1, "k", {}), 1)
        self.assertEqual(st.add_event(2, "k", {}), 2)
        st.close()


class TestClientsAndHarness(unittest.TestCase):
    def test_correct_client_fetches_status_when_place_returns_none(self):
        sim = inproc_sim()
        c = clients.CorrectClient("bybit", "http://127.0.0.1:1", os.path.join(tmpdir(self), "c"), timeout_s=0.2)
        wire(c.ad, sim)
        c.place(intent("i1"))
        self.assertEqual(c.orders["i1"]["status"], "NEW")                                        # 不等 settle 就知道状态
        self.assertEqual(common.read_json(c.journal_path)["i1"]["server_id"], c.orders["i1"]["server_id"])  # 落盘的也有
        bad = clients.CorrectClient("bybit", "http://127.0.0.1:1", os.path.join(tmpdir(self), "d"), timeout_s=0.2)
        wire(bad.ad, sim)
        bad.ad.positions = lambda: (_ for _ in ()).throw(ExchangeError("INSUFFICIENT"))         # 服务端持仓读不到 → 只能信账本
        bad.orders = {"x": dict(c._rec(intent("x", side="SELL"), cid="x"), status="FILLED", filled=0.03, phase="final")}
        self.assertEqual(bad.positions(), {"BTCUSDT": -0.03})
        good = clients.CorrectClient("bybit", "http://127.0.0.1:1", os.path.join(tmpdir(self), "e"), timeout_s=0.2)
        wire(good.ad, sim)
        sim.set_price("BTCUSDT", 58990.0)
        c.settle()
        good.orders = {"i1": dict(c.orders["i1"])}
        good.positions()
        self.assertFalse(any(e["what"] == "position_adopted_from_server" for e in good.log))     # 账本 == 服务端 → 不采纳

    def test_journal_written_before_send(self):
        sim = inproc_sim()
        c = clients.CorrectClient("binance", "http://127.0.0.1:1", os.path.join(tmpdir(self), "c"), timeout_s=0.2)
        wire(c.ad, sim)
        c._send = lambda rec: (_ for _ in ()).throw(RuntimeError("crash before send"))
        with self.assertRaises(RuntimeError):
            c.place(intent("i1"))
        j = common.read_json(c.journal_path)
        self.assertEqual((j["i1"]["cid"], j["i1"]["phase"]), ("i1", "pending"))               # 先写日志再发

    def test_harness_defaults(self):
        scn = {"name": "t/z", "dialect": "binance", "symbol": "BTCUSDT", "seed": 1, "chaos": {"rules": []}, "judges": [],
               "script": [{"op": "tick"}], "expect": {}}
        run = harness.Run(scn, clients.CorrectClient, tmpdir(self))
        run.start_sim()
        try:
            run.make_client()
            self.assertEqual(run.client.ad.t.timeout, 0.3)                                   # timeout_s 缺省 0.3
            run.step({"op": "tick"})
            self.assertEqual(run.sim.feed.n, 1)                                              # n 缺省 1
        finally:
            run.sim.stop()

    def test_naive_logs_and_restart_rereads_balance(self):
        sim = inproc_sim()
        n = clients.NaiveClient("binance", "http://127.0.0.1:1", os.path.join(tmpdir(self), "n"), timeout_s=0.2)
        wire(n.ad, sim)
        self.assertTrue(any(e["what"] == "balance_read_failed" for e in n.log))                 # 接线前那次读失败被记下
        n.place(intent("bad", qty=100))
        self.assertTrue(any(e["what"] == "rejected" for e in n.log))
        self.assertEqual(n.balance(), 10000.0)
        sim.engine.wallet = 9000.0
        n.restart()
        self.assertEqual(n.balance(), 9000.0)

    def test_harness_errors_and_timing(self):
        scn = {"name": "t/y", "dialect": "binance", "symbol": "BTCUSDT", "seed": 1, "chaos": {"rules": []}, "judges": ["no_duplicate"],
               "script": [{"op": "price"}], "expect": {}}
        r = harness.run_scenario(scn, "correct")
        self.assertIn("harness_error", r)                                                       # 驾驭器自己的步出错不吞

        class Sulky(clients.CorrectClient):
            NAME = "sulky"

            def settle(self):
                raise RuntimeError("no settle")
        r = harness.run_scenario(dict(scn, script=[]), Sulky)
        self.assertEqual([e["op"] for e in r["client_errors"]], ["final_settle"])
        self.assertTrue(0 <= r["seconds"] < 60)
        r = harness.run_scenario(dict(scn, script=[]), "correct", keep_work=True)
        self.assertTrue(os.path.isdir(r["work"]))                                                # keep_work 留下工作目录
        harness.shutil.rmtree(r["work"], ignore_errors=True)
        self.assertNotIn("work", harness.run_scenario(dict(scn, script=[]), "correct"))


if __name__ == "__main__":
    unittest.main()
