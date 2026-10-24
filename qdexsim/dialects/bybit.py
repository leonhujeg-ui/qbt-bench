"""Bybit v5（category=linear，USDT 永续）最小子集。字段来源：Bybit API v5 文档（章节名见各 handler 注释）；
文档站在开发环境不可达，形状凭记忆，逐字段可信度见 qdexsim/SHAPES.md。

鉴权（文档「Authentication」）：头 X-BAPI-API-KEY / X-BAPI-TIMESTAMP / X-BAPI-SIGN / X-BAPI-RECV-WINDOW（缺省 5000）；
sign = HMAC-SHA256(secret, timestamp + api_key + recv_window + (GET: 查询串 | POST: 原始请求体))；
时间窗 server_time − recv_window ≤ timestamp < server_time + 1000。
报文形状 {"retCode", "retMsg", "result", "retExtInfo", "time"}；业务错误 HTTP 200 + retCode ≠ 0（含鉴权错，待核）。
限频（文档「Rate Limit」）：retCode 10006，头 X-Bapi-Limit-Status / X-Bapi-Limit / X-Bapi-Limit-Reset-Timestamp；
本仿真按 UID 级限频处理为 HTTP 200 + 10006（IP 级是 HTTP 403，没仿）。
"""
from qdexsim.dialects import base
from qdexsim.engine import EngineError

INTERVALS = {"1": 60000, "3": 180000, "5": 300000, "15": 900000, "30": 1800000, "60": 3600000, "120": 7200000,
             "240": 14400000, "D": 86400000}
STATUS = {"NEW": "New", "PARTIALLY_FILLED": "PartiallyFilled", "FILLED": "Filled", "CANCELED": "Cancelled",
          "REJECTED": "Rejected"}
# kind → (HTTP, retCode, retMsg)
ERRORS = {
    "BAD_SIGN": (200, 10004, "error sign! origin_string[{p}]"),
    "BAD_TIMESTAMP": (200, 10002, "invalid request, please check your server timestamp or recv_window param. "
                                  "req_timestamp[{p}]"),
    "BAD_KEY": (200, 10003, "API key is invalid."),
    "MISSING_AUTH": (200, 10003, "API key is invalid."),                                 # 待核：缺头的码
    "NOT_FOUND": (404, 10001, "path not found"),
    "BAD_PARAM": (200, 10001, "params error: {p}"),
    "SYMBOL_UNKNOWN": (200, 10001, "params error: symbol invalid"),
    "SYMBOL_DELISTED": (200, 10001, "params error: symbol invalid"),
    "SYMBOL_SUSPENDED": (200, 10001, "params error: symbol is not trading"),             # 待核
    "BAD_QTY": (200, 10001, "params error: qty invalid"),                                 # 待核
    "BAD_PRICE": (200, 10001, "params error: price invalid"),                             # 待核
    "MIN_NOTIONAL": (200, 110094, "Order does not meet minimum order value 5USDT"),      # 待核
    "DUP_CID": (200, 110072, "OrderLinkedID is duplicate"),
    "ORDER_NOT_FOUND": (200, 110001, "Order does not exist."),
    "ORDER_NOT_OPEN": (200, 110001, "Order does not exist."),
    "INSUFFICIENT": (200, 110007, "ab not enough for new order"),
    "REDUCE_ONLY": (200, 110017, "Reduce-only rule not satisfied"),
    "BAD_LEVERAGE": (200, 110013, "Leverage cannot be greater than the maximum leverage of the risk limit tier"),  # 待核
    "MARGIN_MODE_BUSY": (200, 110026, "Cross/isolated margin mode is not modified"),      # 待核
    "RATE_LIMIT": (200, 10006, "Too many visits. Exceeded the API Rate Limit."),
    "SERVER_ERROR": (500, 10016, "Server error."),
    "MAINTENANCE": (503, 10016, "System maintenance"),                                    # 待核
}


class Bybit(base.Dialect):
    NAME = "bybit"
    DEFAULT_CREDS = {"api_key": "bybit-key", "secret": "bybit-secret", "passphrase": ""}
    ROUTES = (
        ("GET", "/v5/market/time", "time", False),
        ("GET", "/v5/market/instruments-info", "instruments", False),
        ("GET", "/v5/market/tickers", "ticker", False),
        ("GET", "/v5/market/kline", "klines", False),
        ("GET", "/v5/account/wallet-balance", "account", True),
        ("GET", "/v5/position/list", "positions", True),
        ("POST", "/v5/order/create", "place", True),
        ("POST", "/v5/order/cancel", "cancel", True),
        ("GET", "/v5/order/realtime", "query", True),          # 带 orderId/orderLinkId 查单；不带 = 未成交列表
        ("GET", "/v5/execution/list", "fills", True),
        ("POST", "/v5/position/set-leverage", "leverage", True),
        ("POST", "/v5/position/switch-isolated", "margin_mode", True),
    )

    def ok(self, result, ctx):
        return base.Response(200, {"retCode": 0, "retMsg": "OK", "result": result, "retExtInfo": {}, "time": ctx.now()})

    def auth(self, req, ctx):
        key, ts, sign = req.header("x-bapi-api-key"), req.header("x-bapi-timestamp"), req.header("x-bapi-sign")
        if key != self.creds["api_key"]:
            return self.error("BAD_KEY", "", ctx)
        if ts is None or sign is None:
            return self.error("MISSING_AUTH", "", ctx)
        recv = req.header("x-bapi-recv-window", str(self.RECV_WINDOW_MS))
        payload = ts + key + recv + (req.query if req.method == "GET" else req.body_text)
        if not base.hmac.compare_digest(base.hmac_hex(self.creds["secret"], payload), sign):
            return self.error("BAD_SIGN", payload[:40], ctx)
        try:
            if not self.ts_window_ok(int(ts), ctx.now(), int(recv)):
                return self.error("BAD_TIMESTAMP", ts, ctx)
        except ValueError:
            return self.error("BAD_TIMESTAMP", ts, ctx)
        return None

    def error(self, kind, msg, ctx, retry_after_ms=None):
        status, code, text = ERRORS.get(kind, ERRORS["SERVER_ERROR"])
        headers = self.rate_limit_headers(retry_after_ms or 1000, ctx) if kind == "RATE_LIMIT" else {}
        return base.Response(status, {"retCode": code, "retMsg": text.format(p=msg), "result": {}, "retExtInfo": {},
                                      "time": ctx.now()}, headers)

    def rate_limit_headers(self, retry_after_ms, ctx):
        """「Rate Limit」：X-Bapi-Limit-Reset-Timestamp 是下一次允许请求的毫秒时间戳。"""
        return {"X-Bapi-Limit-Status": "0", "X-Bapi-Limit": "10",
                "X-Bapi-Limit-Reset-Timestamp": str(ctx.now() + retry_after_ms)}

    def _category(self, req):
        if req.get("category", "linear") != "linear":
            raise EngineError("BAD_PARAM", "category must be linear")

    # ---- 行情 ----
    def h_time(self, req, ctx):
        """「Get Bybit Server Time」GET /v5/market/time"""
        now = ctx.now()
        return self.ok({"timeSecond": str(now // 1000), "timeNano": str(now * 1_000_000)}, ctx)

    def h_instruments(self, req, ctx):
        """「Get Instruments Info」GET /v5/market/instruments-info?category=linear"""
        self._category(req)
        want = req.get("symbol")
        rows = [self._instrument(s) for s in ctx.engine.symbols.values()
                if want in (None, self.symbol_out(s["symbol"])) and s["status"] != "DELISTED"]
        return self.ok({"category": "linear", "list": rows, "nextPageCursor": ""}, ctx)

    def _instrument(self, s):
        return {"symbol": self.symbol_out(s["symbol"]), "contractType": "LinearPerpetual",
                "status": "Trading" if s["status"] == "TRADING" else "Closed",                  # 待核：暂停值
                "baseCoin": s["base"], "quoteCoin": s["quote"], "settleCoin": "USDT", "launchTime": "0",
                "priceScale": str(s["price_places"]),
                "leverageFilter": {"minLeverage": "1", "maxLeverage": str(s["max_leverage"]), "leverageStep": "0.01"},
                "priceFilter": {"minPrice": self.f(s["tick"]), "maxPrice": "1000000", "tickSize": self.f(s["tick"])},
                "lotSizeFilter": {"maxOrderQty": "1000", "minOrderQty": self.f(s["min_qty"]), "qtyStep": self.f(s["step"]),
                                  "minNotionalValue": self.f(s["min_notional"])},
                "unifiedMarginTrade": True, "fundingInterval": 480, "copyTrading": "none"}

    def h_ticker(self, req, ctx):
        """「Get Tickers」GET /v5/market/tickers?category=linear"""
        self._category(req)
        want = req.get("symbol")
        rows = []
        for canon in sorted(ctx.engine.mark):
            if want in (None, self.symbol_out(canon)):
                px = self.f(ctx.engine.mark[canon])
                rows.append({"symbol": self.symbol_out(canon), "lastPrice": px, "markPrice": px, "indexPrice": px,
                             "bid1Price": px, "ask1Price": px, "fundingRate": self.f(ctx.engine.cfg["funding_rate"]),
                             "nextFundingTime": str((ctx.now() // 28800000 + 1) * 28800000), "volume24h": "0",
                             "turnover24h": "0", "openInterest": "0"})
        if want is not None and not rows:
            raise EngineError("SYMBOL_UNKNOWN", want)
        return self.ok({"category": "linear", "list": rows}, ctx)

    def h_klines(self, req, ctx):
        """「Get Kline」GET /v5/market/kline → list[[start, o, h, l, c, volume, turnover]]，最新在前"""
        self._category(req)
        canon = self.symbol_in(req.get("symbol"))
        ctx.engine.spec(canon)
        ms = INTERVALS.get(str(req.get("interval", "1")))
        if ms is None:
            raise EngineError("BAD_PARAM", "interval")
        bars = ctx.candles.get(canon, min(int(req.get("limit", 200)), 1000), ms)
        rows = [[str(b[0]), self.f(b[1]), self.f(b[2]), self.f(b[3]), self.f(b[4]), self.f(b[5]), self.f(b[5] * b[4])]
                for b in reversed(bars)]
        return self.ok({"category": "linear", "symbol": self.symbol_out(canon), "list": rows}, ctx)

    # ---- 账户 ----
    def h_account(self, req, ctx):
        """「Get Wallet Balance」GET /v5/account/wallet-balance?accountType=UNIFIED"""
        if req.get("accountType", "UNIFIED") not in ("UNIFIED", "CONTRACT"):
            raise EngineError("BAD_PARAM", "accountType")
        a = ctx.account_view()
        coin = {"coin": "USDT", "equity": self.f(a["equity"]), "walletBalance": self.f(a["wallet"]),
                "availableToWithdraw": self.f(max(0.0, a["available"])), "unrealisedPnl": self.f(a["upl"]),
                "cumRealisedPnl": "0", "totalPositionIM": self.f(a["position_margin"]), "totalOrderIM": self.f(a["order_margin"]),
                "locked": "0", "usdValue": self.f(a["equity"])}
        acct = {"accountType": "UNIFIED", "totalEquity": coin["equity"], "totalWalletBalance": coin["walletBalance"],
                "totalMarginBalance": coin["equity"], "totalAvailableBalance": self.f(a["available"]),
                "totalPerpUPL": coin["unrealisedPnl"], "totalInitialMargin": self.f(a["position_margin"] + a["order_margin"]),
                "totalMaintenanceMargin": "0", "accountIMRate": "0", "accountMMRate": "0", "coin": [coin]}
        return self.ok({"list": [acct]}, ctx)

    def h_positions(self, req, ctx):
        """「Get Position Info」GET /v5/position/list?category=linear&symbol="""
        self._category(req)
        want = req.get("symbol")
        rows = [self._position(p) for p in ctx.positions_view() if want in (None, self.symbol_out(p["symbol"]))]
        if want is not None and not rows:
            canon = self.symbol_in(want)
            rows = [self._position(ctx.engine.position(canon))]
        return self.ok({"category": "linear", "list": rows, "nextPageCursor": ""}, ctx)

    def _position(self, p):
        side = "Buy" if p["qty"] > 0 else "Sell" if p["qty"] < 0 else ""
        return {"positionIdx": 0, "riskId": 1, "riskLimitValue": "2000000", "symbol": self.symbol_out(p["symbol"]),
                "side": side, "size": self.f(abs(p["qty"])), "avgPrice": self.f(p["entry"]),
                "positionValue": self.f(abs(p["qty"]) * p["entry"]), "tradeMode": 1 if p["margin_mode"] == "isolated" else 0,
                "positionStatus": "Normal", "autoAddMargin": 0, "leverage": str(p["leverage"]), "markPrice": self.f(p["mark"]),
                "liqPrice": self.f(p["liq_price"]) if p["qty"] else "", "bustPrice": "", "positionIM": self.f(p["margin"]),
                "positionMM": "0", "unrealisedPnl": self.f(p["upl"]), "cumRealisedPnl": self.f(p["realized"]),
                "createdTime": str(p["updated"]), "updatedTime": str(p["updated"])}

    # ---- 交易 ----
    def h_place(self, req, ctx):
        """「Place Order」POST /v5/order/create"""
        self._category(req)
        side = {"Buy": "BUY", "Sell": "SELL"}.get(str(req.get("side")))
        otype = {"Market": "MARKET", "Limit": "LIMIT"}.get(str(req.get("orderType")))
        canon = base.canon_place(self.symbol_in(req.get("symbol")), side, otype, req.get("qty"), req.get("price"),
                                 req.get("orderLinkId"), base.truthy(req.get("reduceOnly", False)), req.get("timeInForce", "GTC"))
        o = base.op_place(ctx, canon)
        return self.ok({"orderId": str(o["id"]), "orderLinkId": o["cid"] or ""}, ctx)

    def h_cancel(self, req, ctx):
        """「Cancel Order」POST /v5/order/cancel"""
        self._category(req)
        self.symbol_in(req.get("symbol"))
        oid, cid = base.parse_order_id(req.get("orderId")), req.get("orderLinkId")
        if oid is None and cid is None:
            raise EngineError("BAD_PARAM", "orderId or orderLinkId")
        o = base.op_cancel(ctx, order_id=oid, cid=cid)
        return self.ok({"orderId": str(o["id"]), "orderLinkId": o["cid"] or ""}, ctx)

    def h_query(self, req, ctx):
        """「Get Open & Closed Orders」GET /v5/order/realtime：带 orderId / orderLinkId 时查单（查不到 → 空 list，待核），
        不带时返回未成交单列表。"""
        self._category(req)
        sym = req.get("symbol")
        canon = None if sym is None else self.symbol_in(sym)
        oid, cid = base.parse_order_id(req.get("orderId")), req.get("orderLinkId")
        if oid is None and cid is None:
            rows = [self._order(o) for o in ctx.engine.open_orders(canon)]
        else:
            try:
                rows = [self._order(base.op_query(ctx, order_id=oid, cid=cid))]
            except EngineError as e:
                if e.kind != "ORDER_NOT_FOUND":
                    raise
                rows = []
        return self.ok({"category": "linear", "list": rows, "nextPageCursor": ""}, ctx)

    h_open_orders = h_query

    def h_fills(self, req, ctx):
        """「Get Trade History」GET /v5/execution/list"""
        self._category(req)
        sym = req.get("symbol")
        canon = None if sym is None else self.symbol_in(sym)
        rows = []
        for f in ctx.fills_view(canon):
            o = ctx.engine.get_order(order_id=f["order_id"])
            rows.append({"symbol": self.symbol_out(f["symbol"]), "orderId": str(f["order_id"]), "orderLinkId": f["cid"] or "",
                         "side": "Buy" if f["side"] == "BUY" else "Sell", "orderPrice": self.f(o["price"] or 0),
                         "orderQty": self.f(o["qty"]), "leavesQty": self.f(o["qty"] - o["filled"]), "orderType": o["type"].title(),
                         "execFee": self.f(f["fee"]), "execId": str(f["id"]), "execPrice": self.f(f["price"]),
                         "execQty": self.f(f["qty"]), "execType": "Trade", "execValue": self.f(f["price"] * f["qty"]),
                         "execTime": str(f["ts"]), "isMaker": f["maker"],
                         "feeRate": self.f(ctx.engine.cfg["maker_fee" if f["maker"] else "taker_fee"]), "closedSize": "0"})
        return self.ok({"category": "linear", "list": rows, "nextPageCursor": ""}, ctx)

    def h_leverage(self, req, ctx):
        """「Set Leverage」POST /v5/position/set-leverage（buyLeverage 必须等于 sellLeverage）"""
        self._category(req)
        canon = self.symbol_in(req.get("symbol"))
        buy, sell = req.get("buyLeverage"), req.get("sellLeverage")
        if buy is None or buy != sell:
            raise EngineError("BAD_PARAM", "buyLeverage must equal sellLeverage")
        if ctx.engine.position(canon)["leverage"] == base.to_float(buy, "leverage"):
            return base.Response(200, {"retCode": 110043, "retMsg": "Set leverage not modified", "result": {},
                                       "retExtInfo": {}, "time": ctx.now()})
        ctx.engine.set_leverage(canon, int(float(buy)))
        return self.ok({}, ctx)

    def h_margin_mode(self, req, ctx):
        """「Switch Cross/Isolated Margin」POST /v5/position/switch-isolated（tradeMode 0 全仓 / 1 逐仓）"""
        self._category(req)
        canon = self.symbol_in(req.get("symbol"))
        mode = {"0": "cross", "1": "isolated"}.get(str(req.get("tradeMode")))
        if mode is None:
            raise EngineError("BAD_PARAM", "tradeMode")
        if ctx.engine.position(canon)["margin_mode"] == mode:
            return base.Response(200, {"retCode": 110026, "retMsg": "Cross/isolated margin mode is not modified",
                                       "result": {}, "retExtInfo": {}, "time": ctx.now()})
        ctx.engine.set_margin_mode(canon, mode)
        return self.ok({}, ctx)

    def _order(self, o):
        return {"orderId": str(o["id"]), "orderLinkId": o["cid"] or "", "symbol": self.symbol_out(o["symbol"]),
                "price": self.f(o["price"] or 0), "qty": self.f(o["qty"]), "side": "Buy" if o["side"] == "BUY" else "Sell",
                "orderType": o["type"].title(), "orderStatus": STATUS[o["status"]], "cumExecQty": self.f(o["filled"]),
                "cumExecValue": self.f(o["filled"] * o["avg_price"]), "cumExecFee": self.f(o["fee"]),
                "avgPrice": self.f(o["avg_price"]), "leavesQty": self.f(o["qty"] - o["filled"]),
                "leavesValue": self.f((o["qty"] - o["filled"]) * (o["price"] or 0)), "timeInForce": o["tif"],
                "reduceOnly": o["reduce_only"], "closeOnTrigger": False, "positionIdx": 0, "cancelType": "UNKNOWN",
                "rejectReason": "EC_NoError", "stopOrderType": "", "triggerPrice": "", "takeProfit": "", "stopLoss": "",
                "createdTime": str(o["created"]), "updatedTime": str(o["updated"])}
