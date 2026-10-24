"""Binance USDⓈ-M Futures（/fapi）最小子集。字段来源：Binance Derivatives API 文档「USDⓈ-M Futures」各章节
（章节名写在每个 handler 的注释里）。文档站在本次开发环境不可达，形状全部凭记忆，逐字段可信度见 qdexsim/SHAPES.md。

鉴权（文档「General Info › Signed (TRADE and USER_DATA) Endpoint Security」）：
- 头 X-MBX-APIKEY；参数 timestamp、recvWindow（缺省 5000）、signature；
- signature = HMAC-SHA256(secret, totalParams)，totalParams = 查询串 + 请求体（去掉 signature 参数，保持原始顺序）；
- 时间窗：timestamp < serverTime + 1000 且 serverTime − timestamp ≤ recvWindow（「Timing security」）。
错误报文形状 {"code": <负整数>, "msg": "..."}，HTTP 4xx；限频 429 带 Retry-After（「LIMITS」）。
"""
from qdexsim.dialects import base
from qdexsim.engine import EngineError

INTERVALS = {"1m": 60000, "3m": 180000, "5m": 300000, "15m": 900000, "30m": 1800000, "1h": 3600000, "4h": 14400000,
             "1d": 86400000}
STATUS = {"NEW": "NEW", "PARTIALLY_FILLED": "PARTIALLY_FILLED", "FILLED": "FILLED", "CANCELED": "CANCELED",
          "REJECTED": "REJECTED"}
# kind → (HTTP, code, msg)；「待核」项见 SHAPES.md
ERRORS = {
    "BAD_SIGN": (400, -1022, "Signature for this request is not valid."),
    "BAD_TIMESTAMP": (400, -1021, "Timestamp for this request is outside of the recvWindow."),
    "BAD_KEY": (401, -2015, "Invalid API-key, IP, or permissions for action."),
    "MISSING_AUTH": (400, -1102, "Mandatory parameter '{p}' was not sent, was empty/null, or malformed."),
    "NOT_FOUND": (404, -1121, "Invalid path."),
    "BAD_PARAM": (400, -1102, "Mandatory parameter was not sent, was empty/null, or malformed."),
    "SYMBOL_UNKNOWN": (400, -1121, "Invalid symbol."),
    "SYMBOL_DELISTED": (400, -1121, "Invalid symbol."),
    "SYMBOL_SUSPENDED": (400, -2010, "Symbol is not trading."),                     # 待核
    "BAD_QTY": (400, -1111, "Precision is over the maximum defined for this asset."),
    "BAD_PRICE": (400, -1111, "Precision is over the maximum defined for this asset."),
    "MIN_NOTIONAL": (400, -4164, "Order's notional must be no smaller than 5 (unless you choose reduce only)."),
    "DUP_CID": (400, -4116, "Client order id is not valid."),                      # 待核（重复 newClientOrderId 的码）
    "ORDER_NOT_FOUND": (400, -2013, "Order does not exist."),
    "ORDER_NOT_OPEN": (400, -2011, "Unknown order sent."),
    "INSUFFICIENT": (400, -2019, "Margin is insufficient."),
    "REDUCE_ONLY": (400, -2022, "ReduceOnly Order is rejected."),
    "BAD_LEVERAGE": (400, -4028, "Leverage is not valid"),
    "MARGIN_MODE_BUSY": (400, -4048, "Margin type cannot be changed if there exists position."),
    "RATE_LIMIT": (429, -1003, "Too much request weight used; please use the websocket for live updates to avoid polling the API."),
    "SERVER_ERROR": (500, -1001, "Internal error; unable to process your request. Please try again."),
    "MAINTENANCE": (503, -1016, "This service is no longer available."),            # 待核（维护中的码）
}


class Binance(base.Dialect):
    NAME = "binance"
    DEFAULT_CREDS = {"api_key": "binance-key", "secret": "binance-secret", "passphrase": ""}
    ROUTES = (
        ("GET", "/fapi/v1/time", "time", False),
        ("GET", "/fapi/v1/exchangeInfo", "instruments", False),
        ("GET", "/fapi/v1/ticker/price", "ticker", False),
        ("GET", "/fapi/v1/klines", "klines", False),
        ("GET", "/fapi/v2/account", "account", True),
        ("GET", "/fapi/v2/positionRisk", "positions", True),
        ("POST", "/fapi/v1/order", "place", True),
        ("DELETE", "/fapi/v1/order", "cancel", True),
        ("GET", "/fapi/v1/order", "query", True),
        ("GET", "/fapi/v1/openOrders", "open_orders", True),
        ("GET", "/fapi/v1/userTrades", "fills", True),
        ("POST", "/fapi/v1/leverage", "leverage", True),
        ("POST", "/fapi/v1/marginType", "margin_mode", True),
    )

    # ---- 鉴权 / 错误 ----
    def auth(self, req, ctx):
        if req.header("x-mbx-apikey") != self.creds["api_key"]:
            return self.error("BAD_KEY", "", ctx)
        for p in ("timestamp", "signature"):
            if req.get(p) is None:
                return self.error("MISSING_AUTH", p, ctx)
        payload = base.strip_param(req.query, "signature") + base.strip_param(req.body_text, "signature")
        if not base.hmac.compare_digest(base.hmac_hex(self.creds["secret"], payload), str(req.get("signature"))):
            return self.error("BAD_SIGN", "", ctx)
        try:
            ts, recv = int(req.get("timestamp")), int(req.get("recvWindow", self.RECV_WINDOW_MS))
        except ValueError:
            return self.error("MISSING_AUTH", "timestamp", ctx)
        if not self.ts_window_ok(ts, ctx.now(), recv):
            return self.error("BAD_TIMESTAMP", "", ctx)
        return None

    def error(self, kind, msg, ctx, retry_after_ms=None):
        status, code, text = ERRORS.get(kind, ERRORS["SERVER_ERROR"])
        headers = self.rate_limit_headers(retry_after_ms or 1000, ctx) if kind == "RATE_LIMIT" else {}
        return base.Response(status, {"code": code, "msg": text.format(p=msg)}, headers)

    # ---- 行情 ----
    def h_time(self, req, ctx):
        """「Check Server Time」GET /fapi/v1/time"""
        return base.Response(200, {"serverTime": ctx.now()})

    def h_instruments(self, req, ctx):
        """「Exchange Information」GET /fapi/v1/exchangeInfo"""
        syms = [self._symbol_info(s) for s in ctx.engine.symbols.values() if s["status"] != "DELISTED"]
        return base.Response(200, {"timezone": "UTC", "serverTime": ctx.now(), "futuresType": "U_MARGINED",
                                   "rateLimits": [{"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE",
                                                   "intervalNum": 1, "limit": 2400}],
                                   "exchangeFilters": [], "assets": [{"asset": "USDT", "marginAvailable": True}],
                                   "symbols": syms})

    def _symbol_info(self, s):
        return {"symbol": self.symbol_out(s["symbol"]), "pair": s["symbol"], "contractType": "PERPETUAL",
                "status": "TRADING" if s["status"] == "TRADING" else "BREAK",          # 待核：暂停交易的 status 值
                "baseAsset": s["base"], "quoteAsset": s["quote"], "marginAsset": "USDT",
                "pricePrecision": s["price_places"], "quantityPrecision": s["qty_places"],
                "filters": [{"filterType": "PRICE_FILTER", "minPrice": self.f(s["tick"]), "maxPrice": "1000000",
                             "tickSize": self.f(s["tick"])},
                            {"filterType": "LOT_SIZE", "minQty": self.f(s["min_qty"]), "maxQty": "1000",
                             "stepSize": self.f(s["step"])},
                            {"filterType": "MIN_NOTIONAL", "notional": self.f(s["min_notional"])}],
                "orderTypes": ["LIMIT", "MARKET"], "timeInForce": ["GTC", "IOC", "FOK", "GTX"]}

    def h_ticker(self, req, ctx):
        """「Symbol Price Ticker」GET /fapi/v1/ticker/price"""
        sym = req.get("symbol")
        if sym is None:
            return base.Response(200, [self._ticker(s, ctx) for s in sorted(ctx.engine.mark)])
        return base.Response(200, self._ticker(self.symbol_in(sym), ctx))

    def _ticker(self, canon, ctx):
        ctx.engine.spec(canon)
        return {"symbol": self.symbol_out(canon), "price": self.f(ctx.engine.mark[canon]), "time": ctx.now()}

    def h_klines(self, req, ctx):
        """「Kline/Candlestick Data」GET /fapi/v1/klines → [openTime, o, h, l, c, vol, closeTime, quoteVol, trades, ...]"""
        canon = self.symbol_in(req.get("symbol"))
        ctx.engine.spec(canon)
        ms = INTERVALS.get(req.get("interval", "1m"))
        if ms is None:
            raise EngineError("BAD_PARAM", "interval")
        limit = min(int(req.get("limit", 500)), 1500)
        rows = []
        for b in ctx.candles.get(canon, limit, ms):
            rows.append([b[0], self.f(b[1]), self.f(b[2]), self.f(b[3]), self.f(b[4]), self.f(b[5]), b[0] + ms - 1,
                         self.f(b[5] * b[4]), 0, "0", "0", "0"])
        return base.Response(200, rows)

    # ---- 账户 ----
    def h_account(self, req, ctx):
        """「Account Information V2」GET /fapi/v2/account"""
        a = ctx.account_view()
        positions = [self._account_position(p, ctx) for p in ctx.positions_view()]
        asset = {"asset": "USDT", "walletBalance": self.f(a["wallet"]), "unrealizedProfit": self.f(a["upl"]),
                 "marginBalance": self.f(a["equity"]), "maintMargin": "0", "initialMargin": self.f(a["position_margin"] + a["order_margin"]),
                 "positionInitialMargin": self.f(a["position_margin"]), "openOrderInitialMargin": self.f(a["order_margin"]),
                 "maxWithdrawAmount": self.f(max(0.0, a["available"])), "crossWalletBalance": self.f(a["wallet"]),
                 "crossUnPnl": self.f(a["upl"]), "availableBalance": self.f(a["available"]), "marginAvailable": True,
                 "updateTime": ctx.now()}
        return base.Response(200, {"feeTier": 0, "canTrade": True, "canDeposit": True, "canWithdraw": True,
                                   "updateTime": 0, "totalWalletBalance": asset["walletBalance"],
                                   "totalUnrealizedProfit": asset["unrealizedProfit"], "totalMarginBalance": asset["marginBalance"],
                                   "totalPositionInitialMargin": asset["positionInitialMargin"],
                                   "totalOpenOrderInitialMargin": asset["openOrderInitialMargin"],
                                   "availableBalance": asset["availableBalance"], "maxWithdrawAmount": asset["maxWithdrawAmount"],
                                   "assets": [asset], "positions": positions})

    def _account_position(self, p, ctx):
        return {"symbol": self.symbol_out(p["symbol"]), "initialMargin": self.f(p["margin"]), "maintMargin": self.f(p["notional"] * ctx.engine.cfg["mmr"]),
                "unrealizedProfit": self.f(p["upl"]), "positionInitialMargin": self.f(p["margin"]), "openOrderInitialMargin": "0",
                "leverage": str(p["leverage"]), "isolated": p["margin_mode"] == "isolated", "entryPrice": self.f(p["entry"]),
                "maxNotional": "1000000", "positionSide": "BOTH", "positionAmt": self.f(p["qty"]),
                "notional": self.f(p["qty"] * p["mark"]), "isolatedWallet": "0", "updateTime": p["updated"]}

    def h_positions(self, req, ctx):
        """「Position Information V2」GET /fapi/v2/positionRisk"""
        sym = req.get("symbol")
        canon = None if sym is None else self.symbol_in(sym)
        out = []
        for p in ctx.positions_view():
            if canon in (None, p["symbol"]):
                out.append({"symbol": self.symbol_out(p["symbol"]), "positionAmt": self.f(p["qty"]), "entryPrice": self.f(p["entry"]),
                            "breakEvenPrice": self.f(p["entry"]), "markPrice": self.f(p["mark"]), "unRealizedProfit": self.f(p["upl"]),
                            "liquidationPrice": self.f(p["liq_price"]), "leverage": str(p["leverage"]), "maxNotionalValue": "1000000",
                            "marginType": "isolated" if p["margin_mode"] == "isolated" else "cross", "isolatedMargin": "0",
                            "isAutoAddMargin": "false", "positionSide": "BOTH", "notional": self.f(p["qty"] * p["mark"]),
                            "isolatedWallet": "0", "updateTime": p["updated"]})
        return base.Response(200, out)

    # ---- 交易 ----
    def h_place(self, req, ctx):
        """「New Order (TRADE)」POST /fapi/v1/order"""
        canon = base.canon_place(self.symbol_in(req.get("symbol")), req.get("side"), req.get("type"), req.get("quantity"),
                                 req.get("price"), req.get("newClientOrderId"), base.truthy(req.get("reduceOnly", "false")),
                                 req.get("timeInForce", "GTC"))
        return base.Response(200, self.order_out(base.op_place(ctx, canon)))

    def h_cancel(self, req, ctx):
        """「Cancel Order (TRADE)」DELETE /fapi/v1/order（orderId 或 origClientOrderId）"""
        self.symbol_in(req.get("symbol"))
        oid, cid = base.parse_order_id(req.get("orderId")), req.get("origClientOrderId")
        if oid is None and cid is None:
            raise EngineError("BAD_PARAM", "orderId or origClientOrderId")
        try:
            return base.Response(200, self.order_out(base.op_cancel(ctx, order_id=oid, cid=cid)))
        except EngineError as e:
            if e.kind == "ORDER_NOT_FOUND":
                raise EngineError("ORDER_NOT_OPEN", e.msg) from None       # Binance 撤单查不到 → -2011 Unknown order sent
            raise

    def h_query(self, req, ctx):
        """「Query Order (USER_DATA)」GET /fapi/v1/order"""
        self.symbol_in(req.get("symbol"))
        oid, cid = base.parse_order_id(req.get("orderId")), req.get("origClientOrderId")
        return base.Response(200, self.order_out(base.op_query(ctx, order_id=oid, cid=cid)))

    def h_open_orders(self, req, ctx):
        """「Current All Open Orders (USER_DATA)」GET /fapi/v1/openOrders"""
        sym = req.get("symbol")
        canon = None if sym is None else self.symbol_in(sym)
        return base.Response(200, [self.order_out(o) for o in ctx.engine.open_orders(canon)])

    def h_fills(self, req, ctx):
        """「Account Trade List (USER_DATA)」GET /fapi/v1/userTrades"""
        canon = self.symbol_in(req.get("symbol"))
        out = []
        for f in ctx.fills_view(canon):
            out.append({"buyer": f["side"] == "BUY", "commission": self.f(f["fee"]), "commissionAsset": "USDT", "id": f["id"],
                        "maker": f["maker"], "orderId": f["order_id"], "price": self.f(f["price"]), "qty": self.f(f["qty"]),
                        "quoteQty": self.f(f["price"] * f["qty"]), "realizedPnl": self.f(f["realized"]), "side": f["side"],
                        "positionSide": "BOTH", "symbol": self.symbol_out(f["symbol"]), "time": f["ts"]})
        return base.Response(200, out)

    def h_leverage(self, req, ctx):
        """「Change Initial Leverage (TRADE)」POST /fapi/v1/leverage"""
        canon = self.symbol_in(req.get("symbol"))
        lev = ctx.engine.set_leverage(canon, req.get("leverage"))
        return base.Response(200, {"leverage": lev, "maxNotionalValue": "1000000", "symbol": self.symbol_out(canon)})

    def h_margin_mode(self, req, ctx):
        """「Change Margin Type (TRADE)」POST /fapi/v1/marginType（ISOLATED / CROSSED）"""
        canon = self.symbol_in(req.get("symbol"))
        mode = {"ISOLATED": "isolated", "CROSSED": "cross"}.get(str(req.get("marginType")))
        if mode is None:
            raise EngineError("BAD_PARAM", "marginType")
        if ctx.engine.position(canon)["margin_mode"] == mode:
            return base.Response(400, {"code": -4046, "msg": "No need to change margin type."})
        ctx.engine.set_margin_mode(canon, mode)
        return base.Response(200, {"code": 200, "msg": "success"})

    def order_out(self, o):
        return {"orderId": o["id"], "symbol": self.symbol_out(o["symbol"]), "status": STATUS[o["status"]],
                "clientOrderId": o["cid"] or f"auto{o['id']}", "price": self.f(o["price"] or 0), "avgPrice": self.f(o["avg_price"]),
                "origQty": self.f(o["qty"]), "executedQty": self.f(o["filled"]), "cumQuote": self.f(o["filled"] * o["avg_price"]),
                "timeInForce": o["tif"], "type": o["type"], "reduceOnly": o["reduce_only"], "closePosition": False,
                "side": o["side"], "positionSide": "BOTH", "stopPrice": "0", "workingType": "CONTRACT_PRICE",
                "priceProtect": False, "origType": o["type"], "time": o["created"], "updateTime": o["updated"]}
