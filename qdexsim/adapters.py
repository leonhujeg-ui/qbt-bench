"""客户端侧的方言适配器：按各家规则签名、把各家报文翻回规范模型、把各家错误归一成三类异常。参考客户端与判据驾驭器都用它。

    ad = adapters.make("okx", "http://127.0.0.1:PORT", creds=None, timeout_s=0.3)
    ad.place("BTCUSDT", "BUY", "LIMIT", 0.01, 59000.0, cid="i1")   → {"id", "cid", "status"|None}
    ad.query("BTCUSDT", cid="i1")                                   → 规范订单 | None（不存在）
    ad.cancel("BTCUSDT", order_id=...) / ad.open_orders(sym) / ad.fills(sym) / ad.positions() / ad.account()
    ad.set_leverage(sym, 10) / ad.set_margin_mode(sym, "cross") / ad.server_time() / ad.sync_time()

异常：
- UnknownResult：请求**可能已执行**（超时 / 断连 / 5xx 网关形状 / 写操作上的任何 5xx）；
- RateLimited(retry_after_ms)：各家限频报文（Binance 429+Retry-After、Bybit retCode 10006+X-Bapi-Limit-Reset-Timestamp、
  OKX 429/50011、Bitget 429、Bitunix 429/10006；没有重试头的方言用 DEFAULT_RETRY_MS）；
- ExchangeError(kind)：业务错，kind 为方言无关的种类（DUP_CID / ORDER_NOT_FOUND / BAD_TIMESTAMP / ...），由各家错误码反查。
只连 127.0.0.1 / localhost（构造时校验，别处不开 socket）。
"""
import http.client
import json
import socket
import time
import urllib.parse
import uuid

from qdexsim import common
from qdexsim.dialects import base, binance, bitget, bitunix, bybit, okx

DEFAULT_RETRY_MS = 1000
WRITE_OPS = ("place", "cancel", "leverage", "margin_mode")


class ExchangeError(Exception):
    def __init__(self, kind, code=None, msg="", http_status=None):
        super().__init__(f"{kind} ({code}): {msg}")
        self.kind, self.code, self.msg, self.http_status = kind, code, msg, http_status


class RateLimited(ExchangeError):
    def __init__(self, retry_after_ms, code=None, msg="", http_status=None):
        super().__init__("RATE_LIMIT", code, msg, http_status)
        self.retry_after_ms = int(retry_after_ms)


class UnknownResult(Exception):
    """超时 / 断连 / 网关 5xx：服务端可能已经执行了这次请求。"""

    def __init__(self, reason, http_status=None):
        super().__init__(reason)
        self.reason, self.http_status = reason, http_status


# 同一个错误码在一家交易所里常对应多种情况（Bybit 10001 / OKX 51000 / Bitget 40808 / Bitunix 10008 都是“参数错”），客户端
# 反查时按这个优先级取 kind：业务错优先、能区分的优先，笼统的 BAD_PARAM 排在精度错之前（客户端对 BAD_PARAM 会刷新规则重试一次）。
KIND_PRIORITY = ("DUP_CID", "ORDER_NOT_FOUND", "ORDER_NOT_OPEN", "INSUFFICIENT", "REDUCE_ONLY", "MIN_NOTIONAL", "SYMBOL_UNKNOWN",
                 "SYMBOL_SUSPENDED", "SYMBOL_DELISTED", "BAD_LEVERAGE", "MARGIN_MODE_BUSY", "BAD_PARAM", "BAD_QTY", "BAD_PRICE",
                 "BAD_TIMESTAMP", "BAD_SIGN", "BAD_KEY", "BAD_PASSPHRASE", "BAD_NONCE", "MISSING_AUTH", "RATE_LIMIT", "MAINTENANCE",
                 "SERVER_ERROR", "NOT_FOUND")


def reverse_errors(table):
    """方言 ERRORS（kind → (http, code, msg)）反查成 code → kind：一个码只对应一种 kind 时直接取；多义时若含 BAD_PARAM 取
    BAD_PARAM（客户端对它“刷新规则重试一次，没变化就终局”），否则按 KIND_PRIORITY 取第一个。"""
    kinds = {}
    for kind, (_h, code, _m) in table.items():
        kinds.setdefault(str(code), []).append(kind)
    out = {}
    for code, ks in kinds.items():
        if len(ks) == 1:
            out[code] = ks[0]
        elif "BAD_PARAM" in ks:
            out[code] = "BAD_PARAM"
        else:
            out[code] = next((k for k in KIND_PRIORITY if k in ks), ks[0])
    return out


class Transport:
    def __init__(self, base_url, timeout_s=0.3):
        u = urllib.parse.urlsplit(base_url)
        if u.hostname not in ("127.0.0.1", "localhost"):
            raise ValueError("qdexsim adapters only talk to 127.0.0.1")
        self.host, self.port, self.timeout = u.hostname, u.port or 80, timeout_s
        self.calls = 0

    def request(self, method, path, headers=None, body=b""):
        """→ (status, headers dict(lower), body bytes)；超时 / 断连 → UnknownResult。"""
        self.calls += 1
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            conn.request(method, path, body=body or None, headers=dict(headers or {}))
            r = conn.getresponse()
            data = r.read()
            return r.status, {k.lower(): v for k, v in r.getheaders()}, data
        except (socket.timeout, TimeoutError) as e:
            raise UnknownResult(f"timeout: {e}") from None
        except (ConnectionError, http.client.HTTPException, OSError) as e:
            raise UnknownResult(f"connection: {e!r}") from None
        finally:
            conn.close()


def _json(data):
    try:
        return json.loads(data) if data else None
    except ValueError:
        return None


class Adapter:
    """方言无关的接口；子类填 NAME / 签名 / 路径 / 翻译。"""
    NAME = "base"
    DIALECT = None
    STATUS_IN = {}

    def __init__(self, base_url, creds=None, timeout_s=0.3):
        self.t = Transport(base_url, timeout_s)
        self.creds = dict(self.DIALECT.DEFAULT_CREDS, **(creds or {}))
        self.offset_ms = 0
        self.d = self.DIALECT(self.creds)
        self.codes = reverse_errors(self.ERRORS)

    # ---- 时间 ----
    def ts(self):
        return common.now_ms() + self.offset_ms

    def sync_time(self):
        self.offset_ms = self.server_time() - common.now_ms()
        return self.offset_ms

    def sym(self, canon):
        return self.d.symbol_out(canon)

    def canon(self, name):
        return self.d.symbol_in(name)

    # ---- 通用调用 ----
    def call(self, op, method, path, query=None, body=None, signed=True):
        """发一个请求，归一错误；→ 解析后的 JSON body。"""
        query = {k: v for k, v in (query or {}).items() if v is not None}
        body = None if body is None else {k: v for k, v in body.items() if v is not None}
        method_, path_, headers, raw = self.sign(method, path, query, body, signed)
        status, hdrs, data = self.t.request(method_, path_, headers, raw)
        js = _json(data)
        if status >= 500 and (js is None or op in WRITE_OPS):
            raise UnknownResult(f"http {status}", status)
        return self.check(op, status, hdrs, js)

    def status_in(self, s):
        return self.STATUS_IN.get(s, s)

    def kind_of(self, code, msg=""):
        """错误码 → kind；多义码再嗅一下报文（"timestamp" → BAD_TIMESTAMP：Bitunix 的时间窗错与参数错同码）。"""
        kind = self.codes.get(str(code), "SERVER_ERROR")
        if kind == "BAD_PARAM" and "timestamp" in str(msg).lower():
            return "BAD_TIMESTAMP"
        return kind

    @staticmethod
    def f(v, default=0.0):
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    # ---- 子类需实现 ----
    def sign(self, method, path, query, body, signed):      # pragma: no cover
        raise NotImplementedError

    def check(self, op, status, hdrs, js):                  # pragma: no cover
        raise NotImplementedError


# =============================================================================================
class BinanceAdapter(Adapter):
    NAME, DIALECT, ERRORS = "binance", binance.Binance, binance.ERRORS
    STATUS_IN = {"NEW": "NEW", "PARTIALLY_FILLED": "PARTIALLY_FILLED", "FILLED": "FILLED", "CANCELED": "CANCELED",
                 "EXPIRED": "CANCELED", "REJECTED": "REJECTED"}

    def sign(self, method, path, query, body, signed):
        q = dict(query)
        headers = {"X-MBX-APIKEY": self.creds["api_key"]}
        if signed:
            q["timestamp"], q["recvWindow"] = self.ts(), 5000
            qs = urllib.parse.urlencode(q)
            qs += "&signature=" + base.hmac_hex(self.creds["secret"], qs)
        else:
            qs = urllib.parse.urlencode(q)
        return method, path + ("?" + qs if qs else ""), headers, b""

    def check(self, op, status, hdrs, js):
        if status == 429 or (isinstance(js, dict) and js.get("code") == -1003):
            raise RateLimited(int(hdrs.get("retry-after", "1")) * 1000, -1003, "", status)
        if status >= 400:
            code = js.get("code") if isinstance(js, dict) else None
            raise ExchangeError(self.kind_of(code, js.get("msg", "") if js else ""), code, js.get("msg", "") if js else "", status)
        return js

    def server_time(self):
        return int(self.call("time", "GET", "/fapi/v1/time", signed=False)["serverTime"])

    def instruments(self):
        return [{"symbol": self.canon(s["symbol"]), "status": s["status"], "tick": self.f(_filter(s, "PRICE_FILTER")["tickSize"]),
                 "step": self.f(_filter(s, "LOT_SIZE")["stepSize"]), "min_qty": self.f(_filter(s, "LOT_SIZE")["minQty"]),
                 "min_notional": self.f(_filter(s, "MIN_NOTIONAL")["notional"])}
                for s in self.call("instruments", "GET", "/fapi/v1/exchangeInfo", signed=False)["symbols"]]

    def ticker(self, symbol):
        return self.f(self.call("ticker", "GET", "/fapi/v1/ticker/price", {"symbol": self.sym(symbol)}, signed=False)["price"])

    def klines(self, symbol, interval="1m", limit=100):
        rows = self.call("klines", "GET", "/fapi/v1/klines", {"symbol": self.sym(symbol), "interval": interval, "limit": limit}, signed=False)
        return [[r[0]] + [self.f(x) for x in r[1:6]] for r in rows]

    def account(self):
        a = self.call("account", "GET", "/fapi/v2/account")
        return {"wallet": self.f(a["totalWalletBalance"]), "available": self.f(a["availableBalance"]), "equity": self.f(a["totalMarginBalance"])}

    def positions(self):
        return {self.canon(p["symbol"]): self.f(p["positionAmt"]) for p in self.call("positions", "GET", "/fapi/v2/positionRisk")}

    def place(self, symbol, side, otype, qty, price=None, cid=None, reduce_only=False):
        q = {"symbol": self.sym(symbol), "side": side, "type": otype, "quantity": common.fnum(qty), "newClientOrderId": cid,
             "reduceOnly": "true" if reduce_only else None}
        if otype == "LIMIT":
            q.update(price=common.fnum(price), timeInForce="GTC")
        o = self.call("place", "POST", "/fapi/v1/order", q)
        return {"id": str(o["orderId"]), "cid": o.get("clientOrderId"), "status": self.status_in(o["status"])}

    def cancel(self, symbol, order_id=None, cid=None):
        o = self.call("cancel", "DELETE", "/fapi/v1/order", {"symbol": self.sym(symbol), "orderId": order_id, "origClientOrderId": cid})
        return self._order(o)

    def query(self, symbol, order_id=None, cid=None):
        try:
            return self._order(self.call("query", "GET", "/fapi/v1/order", {"symbol": self.sym(symbol), "orderId": order_id,
                                                                            "origClientOrderId": cid}))
        except ExchangeError as e:
            if e.kind == "ORDER_NOT_FOUND":
                return None
            raise

    def open_orders(self, symbol=None):
        return [self._order(o) for o in self.call("open_orders", "GET", "/fapi/v1/openOrders", {"symbol": symbol and self.sym(symbol)})]

    def fills(self, symbol):
        return [{"id": str(f["id"]), "order_id": str(f["orderId"]), "symbol": self.canon(f["symbol"]), "side": f["side"],
                 "price": self.f(f["price"]), "qty": self.f(f["qty"]), "fee": self.f(f["commission"]), "ts": f["time"]}
                for f in self.call("fills", "GET", "/fapi/v1/userTrades", {"symbol": self.sym(symbol)})]

    def set_leverage(self, symbol, leverage):
        return int(self.call("leverage", "POST", "/fapi/v1/leverage", {"symbol": self.sym(symbol), "leverage": leverage})["leverage"])

    def set_margin_mode(self, symbol, mode):
        try:
            self.call("margin_mode", "POST", "/fapi/v1/marginType", {"symbol": self.sym(symbol), "marginType": "ISOLATED" if mode == "isolated" else "CROSSED"})
        except ExchangeError as e:
            if e.code != -4046:
                raise
        return mode

    def _order(self, o):
        return {"id": str(o["orderId"]), "cid": o.get("clientOrderId"), "symbol": self.canon(o["symbol"]), "side": o["side"],
                "type": o["type"], "qty": self.f(o["origQty"]), "price": self.f(o["price"]), "filled": self.f(o["executedQty"]),
                "avg_price": self.f(o["avgPrice"]), "status": self.status_in(o["status"]), "reduce_only": bool(o.get("reduceOnly")),
                "created": o.get("time"), "updated": o.get("updateTime")}


def _filter(s, name):
    return next(f for f in s["filters"] if f["filterType"] == name)


# =============================================================================================
class BybitAdapter(Adapter):
    NAME, DIALECT, ERRORS = "bybit", bybit.Bybit, bybit.ERRORS
    STATUS_IN = {"New": "NEW", "PartiallyFilled": "PARTIALLY_FILLED", "Filled": "FILLED", "Cancelled": "CANCELED",
                 "Rejected": "REJECTED", "Untriggered": "NEW", "Deactivated": "CANCELED"}

    def sign(self, method, path, query, body, signed):
        qs = urllib.parse.urlencode(query)
        raw = json.dumps(body, separators=(",", ":")).encode() if body is not None else b""
        headers = {"Content-Type": "application/json"}
        if signed:
            ts, recv = str(self.ts()), "5000"
            payload = ts + self.creds["api_key"] + recv + (qs if method == "GET" else raw.decode())
            headers.update({"X-BAPI-API-KEY": self.creds["api_key"], "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": recv,
                            "X-BAPI-SIGN": base.hmac_hex(self.creds["secret"], payload), "X-BAPI-SIGN-TYPE": "2"})
        return method, path + ("?" + qs if qs else ""), headers, raw

    def check(self, op, status, hdrs, js):
        if not isinstance(js, dict):
            raise UnknownResult(f"http {status} non-json", status)
        rc = js.get("retCode", 0)
        if rc == 10006 or status == 429:
            reset = hdrs.get("x-bapi-limit-reset-timestamp")
            wait = max(0, int(reset) - self.ts()) if reset else DEFAULT_RETRY_MS
            raise RateLimited(wait, rc, js.get("retMsg", ""), status)
        if rc != 0:
            raise ExchangeError(self.kind_of(rc, js.get("retMsg", "")), rc, js.get("retMsg", ""), status)
        return js["result"]

    def server_time(self):
        return int(self.call("time", "GET", "/v5/market/time", signed=False)["timeNano"]) // 1_000_000

    def instruments(self):
        return [{"symbol": self.canon(s["symbol"]), "status": s["status"], "tick": self.f(s["priceFilter"]["tickSize"]),
                 "step": self.f(s["lotSizeFilter"]["qtyStep"]), "min_qty": self.f(s["lotSizeFilter"]["minOrderQty"]),
                 "min_notional": self.f(s["lotSizeFilter"].get("minNotionalValue"))}
                for s in self.call("instruments", "GET", "/v5/market/instruments-info", {"category": "linear"}, signed=False)["list"]]

    def ticker(self, symbol):
        return self.f(self.call("ticker", "GET", "/v5/market/tickers", {"category": "linear", "symbol": self.sym(symbol)}, signed=False)["list"][0]["lastPrice"])

    def klines(self, symbol, interval="1", limit=100):
        rows = self.call("klines", "GET", "/v5/market/kline", {"category": "linear", "symbol": self.sym(symbol), "interval": interval,
                                                               "limit": limit}, signed=False)["list"]
        return [[int(r[0])] + [self.f(x) for x in r[1:6]] for r in reversed(rows)]

    def account(self):
        a = self.call("account", "GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED"})["list"][0]
        return {"wallet": self.f(a["totalWalletBalance"]), "available": self.f(a["totalAvailableBalance"]), "equity": self.f(a["totalEquity"])}

    def positions(self):
        out = {}
        for p in self.call("positions", "GET", "/v5/position/list", {"category": "linear"})["list"]:
            size = self.f(p["size"])
            out[self.canon(p["symbol"])] = size if p["side"] == "Buy" else -size if p["side"] == "Sell" else 0.0
        return out

    def place(self, symbol, side, otype, qty, price=None, cid=None, reduce_only=False):
        b = {"category": "linear", "symbol": self.sym(symbol), "side": side.title(), "orderType": otype.title(),
             "qty": common.fnum(qty), "orderLinkId": cid, "reduceOnly": True if reduce_only else None}
        if otype == "LIMIT":
            b.update(price=common.fnum(price), timeInForce="GTC")
        r = self.call("place", "POST", "/v5/order/create", body=b)
        return {"id": str(r["orderId"]), "cid": r.get("orderLinkId") or None, "status": None}

    def cancel(self, symbol, order_id=None, cid=None):
        r = self.call("cancel", "POST", "/v5/order/cancel", body={"category": "linear", "symbol": self.sym(symbol),
                                                                   "orderId": order_id and str(order_id), "orderLinkId": cid})
        return {"id": str(r["orderId"]), "cid": r.get("orderLinkId") or None, "status": "CANCELED"}

    def query(self, symbol, order_id=None, cid=None):
        rows = self.call("query", "GET", "/v5/order/realtime", {"category": "linear", "symbol": self.sym(symbol),
                                                                 "orderId": order_id, "orderLinkId": cid})["list"]
        return self._order(rows[0]) if rows else None

    def open_orders(self, symbol=None):
        rows = self.call("open_orders", "GET", "/v5/order/realtime", {"category": "linear", "symbol": symbol and self.sym(symbol)})["list"]
        return [self._order(o) for o in rows]

    def fills(self, symbol):
        rows = self.call("fills", "GET", "/v5/execution/list", {"category": "linear", "symbol": self.sym(symbol)})["list"]
        return [{"id": f["execId"], "order_id": f["orderId"], "symbol": self.canon(f["symbol"]), "side": f["side"].upper(),
                 "price": self.f(f["execPrice"]), "qty": self.f(f["execQty"]), "fee": self.f(f["execFee"]), "ts": int(f["execTime"])}
                for f in rows]

    def set_leverage(self, symbol, leverage):
        try:
            self.call("leverage", "POST", "/v5/position/set-leverage", body={"category": "linear", "symbol": self.sym(symbol),
                                                                              "buyLeverage": str(leverage), "sellLeverage": str(leverage)})
        except ExchangeError as e:
            if e.code != 110043:
                raise
        return int(leverage)

    def set_margin_mode(self, symbol, mode):
        try:
            self.call("margin_mode", "POST", "/v5/position/switch-isolated", body={
                "category": "linear", "symbol": self.sym(symbol), "tradeMode": 1 if mode == "isolated" else 0,
                "buyLeverage": "10", "sellLeverage": "10"})
        except ExchangeError as e:
            if e.code != 110026:
                raise
        return mode

    def _order(self, o):
        return {"id": str(o["orderId"]), "cid": o.get("orderLinkId") or None, "symbol": self.canon(o["symbol"]),
                "side": o["side"].upper(), "type": o["orderType"].upper(), "qty": self.f(o["qty"]), "price": self.f(o["price"]),
                "filled": self.f(o["cumExecQty"]), "avg_price": self.f(o["avgPrice"]), "status": self.status_in(o["orderStatus"]),
                "reduce_only": bool(o.get("reduceOnly")), "created": int(o["createdTime"]), "updated": int(o["updatedTime"])}


# =============================================================================================
class OKXAdapter(Adapter):
    NAME, DIALECT, ERRORS = "okx", okx.OKX, okx.ERRORS
    STATUS_IN = {"live": "NEW", "partially_filled": "PARTIALLY_FILLED", "filled": "FILLED", "canceled": "CANCELED",
                 "mmp_canceled": "CANCELED"}

    def __init__(self, base_url, creds=None, timeout_s=0.3):
        super().__init__(base_url, creds, timeout_s)
        self.ct = {"BTCUSDT": 0.01, "ETHUSDT": 0.1}          # 启动时按文档缺省；instruments() 会刷新
        self.margin_mode = {}

    def sign(self, method, path, query, body, signed):
        qs = urllib.parse.urlencode(query)
        full = path + ("?" + qs if qs else "")
        raw = json.dumps(body, separators=(",", ":")).encode() if body is not None else b""
        headers = {"Content-Type": "application/json"}
        if signed:
            ts = okx.ms_to_iso(self.ts())
            headers.update({"OK-ACCESS-KEY": self.creds["api_key"], "OK-ACCESS-TIMESTAMP": ts,
                            "OK-ACCESS-PASSPHRASE": self.creds["passphrase"],
                            "OK-ACCESS-SIGN": base.hmac_b64(self.creds["secret"], ts + method + full + raw.decode())})
        return method, full, headers, raw

    def check(self, op, status, hdrs, js):
        if status == 429 or (isinstance(js, dict) and js.get("code") == "50011"):
            raise RateLimited(DEFAULT_RETRY_MS, "50011", "", status)
        if not isinstance(js, dict):
            raise UnknownResult(f"http {status} non-json", status)
        code = js.get("code")
        if code == "1":
            d0 = (js.get("data") or [{}])[0]
            raise ExchangeError(self.kind_of(d0.get("sCode"), d0.get("sMsg", "")), d0.get("sCode"), d0.get("sMsg", ""), status)
        if code != "0":
            raise ExchangeError(self.kind_of(code, js.get("msg", "")), code, js.get("msg", ""), status)
        return js["data"]

    def server_time(self):
        return int(self.call("time", "GET", "/api/v5/public/time", signed=False)[0]["ts"])

    def instruments(self):
        out = []
        for s in self.call("instruments", "GET", "/api/v5/public/instruments", {"instType": "SWAP"}, signed=False):
            canon, ct = self.canon(s["instId"]), self.f(s["ctVal"], 1.0)
            self.ct[canon] = ct
            out.append({"symbol": canon, "status": s["state"], "tick": self.f(s["tickSz"]), "step": self.f(s["lotSz"]) * ct,
                        "min_qty": self.f(s["minSz"]) * ct, "min_notional": 0.0})
        return out

    def ticker(self, symbol):
        return self.f(self.call("ticker", "GET", "/api/v5/market/ticker", {"instId": self.sym(symbol)}, signed=False)[0]["last"])

    def klines(self, symbol, interval="1m", limit=100):
        rows = self.call("klines", "GET", "/api/v5/market/candles", {"instId": self.sym(symbol), "bar": interval, "limit": limit}, signed=False)
        return [[int(r[0])] + [self.f(x) for x in r[1:5]] + [self.f(r[6])] for r in reversed(rows)]

    def account(self):
        a = self.call("account", "GET", "/api/v5/account/balance")[0]
        det = a["details"][0] if a["details"] else {"cashBal": 0, "availBal": 0}
        return {"wallet": self.f(det["cashBal"]), "available": self.f(det["availBal"]), "equity": self.f(a["totalEq"])}

    def positions(self):
        out = {}
        for p in self.call("positions", "GET", "/api/v5/account/positions", {"instType": "SWAP"}):
            canon = self.canon(p["instId"])
            out[canon] = self.f(p["pos"]) * self.ct.get(canon, 1.0)
        return out

    def _td(self, symbol):
        return self.margin_mode.get(symbol, "cross")

    def place(self, symbol, side, otype, qty, price=None, cid=None, reduce_only=False):
        b = {"instId": self.sym(symbol), "tdMode": self._td(symbol), "side": side.lower(), "ordType": otype.lower(),
             "sz": common.fnum(qty / self.ct[symbol]), "px": common.fnum(price) if otype == "LIMIT" else None, "clOrdId": cid,
             "reduceOnly": True if reduce_only else None}
        r = self.call("place", "POST", "/api/v5/trade/order", body=b)[0]
        return {"id": r["ordId"], "cid": r.get("clOrdId") or None, "status": None}

    def cancel(self, symbol, order_id=None, cid=None):
        r = self.call("cancel", "POST", "/api/v5/trade/cancel-order", body={"instId": self.sym(symbol), "ordId": order_id and str(order_id),
                                                                             "clOrdId": cid})[0]
        return {"id": r["ordId"], "cid": r.get("clOrdId") or None, "status": "CANCELED"}

    def query(self, symbol, order_id=None, cid=None):
        try:
            rows = self.call("query", "GET", "/api/v5/trade/order", {"instId": self.sym(symbol), "ordId": order_id, "clOrdId": cid})
        except ExchangeError as e:
            if e.kind == "ORDER_NOT_FOUND":
                return None
            raise
        return self._order(rows[0])

    def open_orders(self, symbol=None):
        rows = self.call("open_orders", "GET", "/api/v5/trade/orders-pending", {"instType": "SWAP", "instId": symbol and self.sym(symbol)})
        return [self._order(o) for o in rows]

    def fills(self, symbol):
        rows = self.call("fills", "GET", "/api/v5/trade/fills", {"instType": "SWAP", "instId": self.sym(symbol)})
        return [{"id": f["tradeId"], "order_id": f["ordId"], "symbol": self.canon(f["instId"]), "side": f["side"].upper(),
                 "price": self.f(f["fillPx"]), "qty": self.f(f["fillSz"]) * self.ct[self.canon(f["instId"])],
                 "fee": -self.f(f["fee"]), "ts": int(f["ts"])} for f in rows]

    def set_leverage(self, symbol, leverage):
        r = self.call("leverage", "POST", "/api/v5/account/set-leverage", body={"instId": self.sym(symbol), "lever": str(leverage),
                                                                                "mgnMode": self._td(symbol)})[0]
        return int(float(r["lever"]))

    def set_margin_mode(self, symbol, mode):
        """OKX 用 set-leverage 的 mgnMode 改模式（见 dialects/okx.py h_margin_mode 注释）；之后下单 tdMode 跟随。"""
        lev = 10
        try:
            lev = int(float(self.call("leverage", "POST", "/api/v5/account/set-leverage", body={
                "instId": self.sym(symbol), "lever": "10", "mgnMode": mode})[0]["lever"]))
        finally:
            self.margin_mode[symbol] = mode
        return mode if lev else mode

    def _order(self, o):
        canon = self.canon(o["instId"])
        ct = self.ct.get(canon, 1.0)
        return {"id": o["ordId"], "cid": o.get("clOrdId") or None, "symbol": canon, "side": o["side"].upper(),
                "type": o["ordType"].upper(), "qty": self.f(o["sz"]) * ct, "price": self.f(o["px"]), "filled": self.f(o["accFillSz"]) * ct,
                "avg_price": self.f(o["avgPx"]), "status": self.status_in(o["state"]), "reduce_only": o.get("reduceOnly") == "true",
                "created": int(o["cTime"]), "updated": int(o["uTime"])}


# =============================================================================================
class BitgetAdapter(Adapter):
    NAME, DIALECT, ERRORS = "bitget", bitget.Bitget, bitget.ERRORS
    STATUS_IN = {"live": "NEW", "partially_filled": "PARTIALLY_FILLED", "filled": "FILLED", "canceled": "CANCELED"}
    P = {"productType": "USDT-FUTURES"}

    def __init__(self, base_url, creds=None, timeout_s=0.3):
        super().__init__(base_url, creds, timeout_s)
        self.margin_mode = {}

    def sign(self, method, path, query, body, signed):
        qs = urllib.parse.urlencode(query)
        full = path + ("?" + qs if qs else "")
        raw = json.dumps(body, separators=(",", ":")).encode() if body is not None else b""
        headers = {"Content-Type": "application/json", "locale": "en-US"}
        if signed:
            ts = str(self.ts())
            headers.update({"ACCESS-KEY": self.creds["api_key"], "ACCESS-TIMESTAMP": ts, "ACCESS-PASSPHRASE": self.creds["passphrase"],
                            "ACCESS-SIGN": base.hmac_b64(self.creds["secret"], ts + method.upper() + full + raw.decode())})
        return method, full, headers, raw

    def check(self, op, status, hdrs, js):
        if status == 429:
            raise RateLimited(int(hdrs["retry-after"]) * 1000 if "retry-after" in hdrs else DEFAULT_RETRY_MS, "429", "", status)
        if not isinstance(js, dict):
            raise UnknownResult(f"http {status} non-json", status)
        code = str(js.get("code"))
        if code != "00000":
            raise ExchangeError(self.kind_of(code, js.get("msg", "")), code, js.get("msg", ""), status)
        return js["data"]

    def server_time(self):
        return int(self.call("time", "GET", "/api/v2/public/time", signed=False)["serverTime"])

    def instruments(self):
        return [{"symbol": self.canon(s["symbol"]), "status": s["symbolStatus"], "tick": self.f(s["priceEndStep"]) / 10 ** int(s["pricePlace"]),
                 "step": self.f(s["sizeMultiplier"]), "min_qty": self.f(s["minTradeNum"]), "min_notional": self.f(s["minTradeUSDT"])}
                for s in self.call("instruments", "GET", "/api/v2/mix/market/contracts", self.P, signed=False)]

    def ticker(self, symbol):
        return self.f(self.call("ticker", "GET", "/api/v2/mix/market/ticker", dict(self.P, symbol=self.sym(symbol)), signed=False)[0]["lastPr"])

    def klines(self, symbol, interval="1m", limit=100):
        rows = self.call("klines", "GET", "/api/v2/mix/market/candles", dict(self.P, symbol=self.sym(symbol), granularity=interval, limit=limit), signed=False)
        return [[int(r[0])] + [self.f(x) for x in r[1:6]] for r in rows]

    def account(self, symbol="BTCUSDT"):
        a = self.call("account", "GET", "/api/v2/mix/account/account", dict(self.P, symbol=self.sym(symbol), marginCoin="USDT"))
        return {"wallet": self.f(a["accountEquity"]) - self.f(a["unrealizedPL"]), "available": self.f(a["available"]), "equity": self.f(a["accountEquity"])}

    def positions(self):
        out = {}
        for p in self.call("positions", "GET", "/api/v2/mix/position/all-position", dict(self.P, marginCoin="USDT")):
            total = self.f(p["total"])
            out[self.canon(p["symbol"])] = total if p["holdSide"] == "long" else -total
        return out

    def place(self, symbol, side, otype, qty, price=None, cid=None, reduce_only=False):
        b = dict(self.P, symbol=self.sym(symbol), marginMode="isolated" if self.margin_mode.get(symbol) == "isolated" else "crossed",
                 marginCoin="USDT", size=common.fnum(qty), side=side.lower(), orderType=otype.lower(), force="gtc", clientOid=cid,
                 reduceOnly="YES" if reduce_only else "NO", price=common.fnum(price) if otype == "LIMIT" else None)
        r = self.call("place", "POST", "/api/v2/mix/order/place-order", body=b)
        return {"id": r["orderId"], "cid": r.get("clientOid") or None, "status": None}

    def cancel(self, symbol, order_id=None, cid=None):
        r = self.call("cancel", "POST", "/api/v2/mix/order/cancel-order", body=dict(self.P, symbol=self.sym(symbol),
                                                                                    orderId=order_id and str(order_id), clientOid=cid))
        return {"id": r["orderId"], "cid": r.get("clientOid") or None, "status": "CANCELED"}

    def query(self, symbol, order_id=None, cid=None):
        try:
            o = self.call("query", "GET", "/api/v2/mix/order/detail", dict(self.P, symbol=self.sym(symbol), orderId=order_id, clientOid=cid))
        except ExchangeError as e:
            if e.kind == "ORDER_NOT_FOUND":
                return None
            raise
        return self._order(o)

    def open_orders(self, symbol=None):
        r = self.call("open_orders", "GET", "/api/v2/mix/order/orders-pending", dict(self.P, symbol=symbol and self.sym(symbol)))
        return [self._order(o) for o in r["entrustedList"]]

    def fills(self, symbol):
        r = self.call("fills", "GET", "/api/v2/mix/order/fills", dict(self.P, symbol=self.sym(symbol)))
        return [{"id": f["tradeId"], "order_id": f["orderId"], "symbol": self.canon(f["symbol"]), "side": f["side"].upper(),
                 "price": self.f(f["price"]), "qty": self.f(f["baseVolume"]), "fee": -self.f(f["feeDetail"][0]["totalFee"]),
                 "ts": int(f["cTime"])} for f in r["fillList"]]

    def set_leverage(self, symbol, leverage):
        r = self.call("leverage", "POST", "/api/v2/mix/account/set-leverage", body=dict(self.P, symbol=self.sym(symbol), marginCoin="USDT",
                                                                                      leverage=str(leverage)))
        return int(float(r["longLeverage"]))

    def set_margin_mode(self, symbol, mode):
        self.call("margin_mode", "POST", "/api/v2/mix/account/set-margin-mode", body=dict(self.P, symbol=self.sym(symbol), marginCoin="USDT",
                                                                                        marginMode="isolated" if mode == "isolated" else "crossed"))
        self.margin_mode[symbol] = mode
        return mode

    def _order(self, o):
        return {"id": o["orderId"], "cid": o.get("clientOid") or None, "symbol": self.canon(o["symbol"]), "side": o["side"].upper(),
                "type": o["orderType"].upper(), "qty": self.f(o["size"]), "price": self.f(o["price"]), "filled": self.f(o["baseVolume"]),
                "avg_price": self.f(o["priceAvg"]), "status": self.status_in(o["state"]), "reduce_only": o.get("reduceOnly") == "YES",
                "created": int(o["cTime"]), "updated": int(o["uTime"])}


# =============================================================================================
class BitunixAdapter(Adapter):
    NAME, DIALECT, ERRORS = "bitunix", bitunix.Bitunix, bitunix.ERRORS
    STATUS_IN = {"INIT": "NEW", "NEW": "NEW", "PART_FILLED": "PARTIALLY_FILLED", "FILLED": "FILLED", "CANCELED": "CANCELED"}

    def sign(self, method, path, query, body, signed):
        qs = urllib.parse.urlencode(query)
        raw = json.dumps(body, separators=(",", ":")).encode() if body is not None else b""
        headers = {"Content-Type": "application/json", "language": "en-US"}
        if signed:
            nonce, ts = uuid.uuid4().hex, str(self.ts())
            digest = base.sha256_hex(nonce + ts + self.creds["api_key"] + bitunix.Bitunix.query_string(query) + raw.decode())
            headers.update({"api-key": self.creds["api_key"], "nonce": nonce, "timestamp": ts,
                            "sign": base.sha256_hex(digest + self.creds["secret"])})
        return method, path + ("?" + qs if qs else ""), headers, raw

    def check(self, op, status, hdrs, js):
        if status == 429 or (isinstance(js, dict) and js.get("code") == 10006):
            raise RateLimited(int(hdrs["retry-after"]) * 1000 if "retry-after" in hdrs else DEFAULT_RETRY_MS, 10006, "", status)
        if not isinstance(js, dict):
            raise UnknownResult(f"http {status} non-json", status)
        if js.get("code") != 0:
            raise ExchangeError(self.kind_of(js.get("code"), js.get("msg", "")), js.get("code"), js.get("msg", ""), status)
        return js["data"]

    def server_time(self):
        return int(self.call("time", "GET", "/api/v1/futures/market/time", signed=False)["serverTime"])

    def instruments(self):
        return [{"symbol": self.canon(s["symbol"]), "status": s["symbolStatus"], "tick": self.f(s.get("tickSize")),
                 "step": self.f(s.get("stepSize")), "min_qty": self.f(s["minTradeVolume"]), "min_notional": 0.0}
                for s in self.call("instruments", "GET", "/api/v1/futures/market/trading_pairs", signed=False)]

    def ticker(self, symbol):
        return self.f(self.call("ticker", "GET", "/api/v1/futures/market/tickers", {"symbols": self.sym(symbol)}, signed=False)[0]["lastPrice"])

    def klines(self, symbol, interval="1m", limit=100):
        rows = self.call("klines", "GET", "/api/v1/futures/market/kline", {"symbol": self.sym(symbol), "interval": interval, "limit": limit}, signed=False)
        return [[int(r["time"]), self.f(r["open"]), self.f(r["high"]), self.f(r["low"]), self.f(r["close"]), self.f(r["baseVol"])] for r in rows]

    def account(self):
        a = self.call("account", "GET", "/api/v1/futures/account", {"marginCoin": "USDT"})
        wallet = self.f(a["available"]) + self.f(a["frozen"]) + self.f(a["margin"]) - self.f(a["crossUnrealizedPNL"])
        return {"wallet": wallet, "available": self.f(a["available"]), "equity": wallet + self.f(a["crossUnrealizedPNL"])}

    def positions(self):
        out = {}
        for p in self.call("positions", "GET", "/api/v1/futures/position/get_pending_positions"):
            out[self.canon(p["symbol"])] = self.f(p["qty"]) if p["side"] == "BUY" else -self.f(p["qty"])
        return out

    def place(self, symbol, side, otype, qty, price=None, cid=None, reduce_only=False):
        b = {"symbol": self.sym(symbol), "qty": common.fnum(qty), "side": side, "orderType": otype, "effect": "GTC", "clientId": cid,
             "reduceOnly": bool(reduce_only), "price": common.fnum(price) if otype == "LIMIT" else None}
        r = self.call("place", "POST", "/api/v1/futures/trade/place_order", body=b)
        return {"id": str(r["orderId"]), "cid": r.get("clientId") or None, "status": None}

    def cancel(self, symbol, order_id=None, cid=None):
        item = {"orderId": str(order_id)} if order_id is not None else {"clientId": cid}
        r = self.call("cancel", "POST", "/api/v1/futures/trade/cancel_orders", body={"symbol": self.sym(symbol), "orderList": [item]})
        if r["failureList"]:
            f = r["failureList"][0]
            raise ExchangeError(self.kind_of(f["errorCode"], f["errorMsg"]), f["errorCode"], f["errorMsg"], 200)
        s = r["successList"][0]
        return {"id": str(s["orderId"]), "cid": s.get("clientId") or None, "status": "CANCELED"}

    def query(self, symbol, order_id=None, cid=None):
        try:
            o = self.call("query", "GET", "/api/v1/futures/trade/get_order_detail", {"orderId": order_id, "clientId": cid})
        except ExchangeError as e:
            if e.kind == "ORDER_NOT_FOUND":
                return None
            raise
        return self._order(o)

    def open_orders(self, symbol=None):
        r = self.call("open_orders", "GET", "/api/v1/futures/trade/get_pending_orders", {"symbol": symbol and self.sym(symbol)})
        return [self._order(o) for o in r["orderList"]]

    def fills(self, symbol):
        r = self.call("fills", "GET", "/api/v1/futures/trade/get_history_trades", {"symbol": self.sym(symbol)})
        return [{"id": f["tradeId"], "order_id": f["orderId"], "symbol": self.canon(f["symbol"]), "side": f["side"],
                 "price": self.f(f["price"]), "qty": self.f(f["qty"]), "fee": self.f(f["fee"]), "ts": int(f["ctime"])} for f in r["tradeList"]]

    def set_leverage(self, symbol, leverage):
        return int(self.call("leverage", "POST", "/api/v1/futures/account/change_leverage", body={"symbol": self.sym(symbol), "marginCoin": "USDT",
                                                                                              "leverage": int(leverage)})["leverage"])

    def set_margin_mode(self, symbol, mode):
        self.call("margin_mode", "POST", "/api/v1/futures/account/change_margin_mode", body={
            "symbol": self.sym(symbol), "marginCoin": "USDT", "marginMode": "ISOLATION" if mode == "isolated" else "CROSS"})
        return mode

    def _order(self, o):
        return {"id": str(o["orderId"]), "cid": o.get("clientId") or None, "symbol": self.canon(o["symbol"]), "side": o["side"],
                "type": o["orderType"], "qty": self.f(o["qty"]), "price": self.f(o["price"]), "filled": self.f(o["tradeQty"]),
                "avg_price": self.f(o["avgPrice"]), "status": self.status_in(o["status"]), "reduce_only": bool(o.get("reduceOnly")),
                "created": int(o["ctime"]), "updated": int(o["mtime"])}


ADAPTERS = {a.NAME: a for a in (BinanceAdapter, BybitAdapter, OKXAdapter, BitgetAdapter, BitunixAdapter)}


def make(dialect, base_url, creds=None, timeout_s=0.3):
    return ADAPTERS[dialect](base_url, creds, timeout_s)
