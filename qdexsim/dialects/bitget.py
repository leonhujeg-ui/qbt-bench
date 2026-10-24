"""Bitget v2 mix（productType=USDT-FUTURES）最小子集。字段来源：Bitget API 文档「Futures」各章节（见 handler 注释）；
文档站在开发环境不可达，形状凭记忆，逐字段可信度见 qdexsim/SHAPES.md。

鉴权（文档「Common › Signature」）：头 ACCESS-KEY / ACCESS-SIGN / ACCESS-TIMESTAMP（毫秒）/ ACCESS-PASSPHRASE；
sign = base64(HMAC-SHA256(secret, timestamp + METHOD + requestPath + ("?" + 查询串) + body))；时间戳与服务器相差超过 30 秒 →
40008（「Request timestamp expired」，窗口大小待核）。报文形状 {"code": "00000", "msg": "success", "requestTime", "data"}；
错误 HTTP 400 + code 字符串（鉴权错亦然）。限频 HTTP 429（待核，文档未写重试头）。
"""
from qdexsim.dialects import base
from qdexsim.engine import EngineError

GRANULARITY = {"1m": 60000, "3m": 180000, "5m": 300000, "15m": 900000, "30m": 1800000, "1H": 3600000, "4H": 14400000,
               "1D": 86400000}
STATE = {"NEW": "live", "PARTIALLY_FILLED": "partially_filled", "FILLED": "filled", "CANCELED": "canceled",
         "REJECTED": "canceled"}
WINDOW_MS = 30000
PRODUCT = "USDT-FUTURES"
ERRORS = {
    "BAD_SIGN": (400, "40009", "sign signature error"),
    "BAD_TIMESTAMP": (400, "40008", "Request timestamp expired"),
    "BAD_KEY": (400, "40006", "Invalid ACCESS_KEY"),
    "BAD_PASSPHRASE": (400, "40012", "Incorrect passphrase"),                                 # 待核
    "MISSING_AUTH": (400, "40001", "ACCESS_KEY cannot be empty"),                              # 待核：缺不同头码不同
    "NOT_FOUND": (404, "40404", "Request URL NOT FOUND"),                                      # 待核
    "BAD_PARAM": (400, "40808", "Parameter verification exception {p}"),
    "SYMBOL_UNKNOWN": (400, "40034", "Parameter {p} does not exist"),
    "SYMBOL_DELISTED": (400, "40034", "Parameter {p} does not exist"),
    "SYMBOL_SUSPENDED": (400, "40020", "Parameter symbol error: not trading"),                # 待核
    "BAD_QTY": (400, "40808", "Parameter verification exception size"),                        # 待核
    "BAD_PRICE": (400, "40808", "Parameter verification exception price"),                     # 待核
    "MIN_NOTIONAL": (400, "45110", "less than the minimum amount 5 USDT"),
    "DUP_CID": (400, "40786", "Duplicate clientOid"),
    "ORDER_NOT_FOUND": (400, "40768", "Order does not exist"),
    "ORDER_NOT_OPEN": (400, "40768", "Order does not exist"),                                  # 待核：已成 / 已撤再撤的码
    "INSUFFICIENT": (400, "40762", "The order amount exceeds the balance"),
    "REDUCE_ONLY": (400, "40774", "No position to reduce"),                                    # 待核
    "BAD_LEVERAGE": (400, "40797", "Exceed the maximum leverage"),                             # 待核
    "MARGIN_MODE_BUSY": (400, "45117", "Position or pending orders exist, cannot change margin mode"),  # 待核
    "RATE_LIMIT": (429, "429", "Too Many Requests"),                                           # 待核
    "SERVER_ERROR": (500, "50001", "System error"),                                            # 待核
    "MAINTENANCE": (503, "40200", "Server upgrade, please try again later"),
}


class Bitget(base.Dialect):
    NAME = "bitget"
    DEFAULT_CREDS = {"api_key": "bitget-key", "secret": "bitget-secret", "passphrase": "bitget-pass"}
    ROUTES = (
        ("GET", "/api/v2/public/time", "time", False),
        ("GET", "/api/v2/mix/market/contracts", "instruments", False),
        ("GET", "/api/v2/mix/market/ticker", "ticker", False),
        ("GET", "/api/v2/mix/market/candles", "klines", False),
        ("GET", "/api/v2/mix/account/account", "account", True),
        ("GET", "/api/v2/mix/position/all-position", "positions", True),
        ("POST", "/api/v2/mix/order/place-order", "place", True),
        ("POST", "/api/v2/mix/order/cancel-order", "cancel", True),
        ("GET", "/api/v2/mix/order/detail", "query", True),
        ("GET", "/api/v2/mix/order/orders-pending", "open_orders", True),
        ("GET", "/api/v2/mix/order/fills", "fills", True),
        ("POST", "/api/v2/mix/account/set-leverage", "leverage", True),
        ("POST", "/api/v2/mix/account/set-margin-mode", "margin_mode", True),
    )

    def ok(self, data, ctx):
        return base.Response(200, {"code": "00000", "msg": "success", "requestTime": ctx.now(), "data": data})

    def auth(self, req, ctx):
        key, sign, ts, pw = (req.header(h) for h in ("access-key", "access-sign", "access-timestamp", "access-passphrase"))
        if key is None or sign is None or ts is None or pw is None:
            return self.error("MISSING_AUTH", "", ctx)
        if key != self.creds["api_key"]:
            return self.error("BAD_KEY", "", ctx)
        if pw != self.creds["passphrase"]:
            return self.error("BAD_PASSPHRASE", "", ctx)
        path = req.path + ("?" + req.query if req.query else "")
        if not base.hmac.compare_digest(base.hmac_b64(self.creds["secret"], ts + req.method + path + req.body_text), sign):
            return self.error("BAD_SIGN", "", ctx)
        try:
            if abs(ctx.now() - int(ts)) > WINDOW_MS:
                return self.error("BAD_TIMESTAMP", "", ctx)
        except ValueError:
            return self.error("BAD_TIMESTAMP", "", ctx)
        return None

    def error(self, kind, msg, ctx, retry_after_ms=None):
        status, code, text = ERRORS.get(kind, ERRORS["SERVER_ERROR"])
        headers = self.rate_limit_headers(retry_after_ms or 1000, ctx) if kind == "RATE_LIMIT" else {}
        return base.Response(status, {"code": code, "msg": text.format(p=msg or ""), "requestTime": ctx.now(),
                                      "data": None}, headers)

    def rate_limit_headers(self, retry_after_ms, ctx):
        return {}                                   # 待核：Bitget 文档未写 429 重试头；适配器用缺省退避

    def _product(self, req):
        if req.get("productType", PRODUCT) != PRODUCT:
            raise EngineError("BAD_PARAM", "productType")

    # ---- 行情 ----
    def h_time(self, req, ctx):
        """「Get Server Time」GET /api/v2/public/time"""
        return self.ok({"serverTime": str(ctx.now())}, ctx)

    def h_instruments(self, req, ctx):
        """「Get All Symbols」GET /api/v2/mix/market/contracts?productType=USDT-FUTURES"""
        self._product(req)
        want = req.get("symbol")
        rows = []
        for s in ctx.engine.symbols.values():
            if want in (None, self.symbol_out(s["symbol"])) and s["status"] != "DELISTED":
                rows.append({"symbol": self.symbol_out(s["symbol"]), "baseCoin": s["base"], "quoteCoin": s["quote"],
                             "buyLimitPriceRatio": "0.05", "sellLimitPriceRatio": "0.05", "feeRateUpRatio": "0.005",
                             "makerFeeRate": self.f(ctx.engine.cfg["maker_fee"]), "takerFeeRate": self.f(ctx.engine.cfg["taker_fee"]),
                             "openCostUpRatio": "0.01", "supportMarginCoins": ["USDT"], "minTradeNum": self.f(s["min_qty"]),
                             "priceEndStep": self.f(s["tick"] * 10 ** s["price_places"]), "volumePlace": str(s["qty_places"]),
                             "pricePlace": str(s["price_places"]), "sizeMultiplier": self.f(s["step"]), "symbolType": "perpetual",
                             "minTradeUSDT": self.f(s["min_notional"]), "maxSymbolOrderNum": "200", "maxProductOrderNum": "400",
                             "maxPositionNum": "150", "symbolStatus": "normal" if s["status"] == "TRADING" else "maintain",  # 待核
                             "offTime": "-1", "limitOpenTime": "-1", "deliveryTime": "", "deliveryStartTime": "",
                             "deliveryPeriod": "", "launchTime": "", "fundInterval": "8", "minLever": "1",
                             "maxLever": str(s["max_leverage"]), "posLimit": "0.05", "maintainTime": ""})
        return self.ok(rows, ctx)

    def h_ticker(self, req, ctx):
        """「Get Ticker」GET /api/v2/mix/market/ticker?productType&symbol → data 是单元素列表"""
        self._product(req)
        canon = self.symbol_in(req.get("symbol"))
        ctx.engine.spec(canon)
        px = self.f(ctx.engine.mark[canon])
        return self.ok([{"symbol": self.symbol_out(canon), "lastPr": px, "askPr": px, "bidPr": px, "bidSz": "0", "askSz": "0",
                         "high24h": px, "low24h": px, "ts": str(ctx.now()), "change24h": "0", "baseVolume": "0",
                         "quoteVolume": "0", "usdtVolume": "0", "openUtc": px, "changeUtc24h": "0", "indexPrice": px,
                         "fundingRate": self.f(ctx.engine.cfg["funding_rate"]), "holdingAmount": "0", "open24h": px,
                         "markPrice": px, "deliveryStartTime": None, "deliveryTime": None, "deliveryStatus": ""}], ctx)

    def h_klines(self, req, ctx):
        """「Get Candlestick Data」GET /api/v2/mix/market/candles → [[ts, o, h, l, c, baseVol, quoteVol]]，旧到新"""
        self._product(req)
        canon = self.symbol_in(req.get("symbol"))
        ctx.engine.spec(canon)
        ms = GRANULARITY.get(req.get("granularity", "1m"))
        if ms is None:
            raise EngineError("BAD_PARAM", "granularity")
        bars = ctx.candles.get(canon, min(int(req.get("limit", 100)), 1000), ms)
        return self.ok([[str(b[0]), self.f(b[1]), self.f(b[2]), self.f(b[3]), self.f(b[4]), self.f(b[5]), self.f(b[5] * b[4])]
                        for b in bars], ctx)

    # ---- 账户 ----
    def h_account(self, req, ctx):
        """「Get Single Account」GET /api/v2/mix/account/account?symbol&productType&marginCoin"""
        self._product(req)
        canon = self.symbol_in(req.get("symbol"))
        p = ctx.engine.position(canon)
        a = ctx.account_view()
        return self.ok({"marginCoin": "USDT", "locked": self.f(a["position_margin"] + a["order_margin"]),
                        "available": self.f(a["available"]), "crossedMaxAvailable": self.f(a["available"]),
                        "isolatedMaxAvailable": self.f(a["available"]), "maxTransferOut": self.f(max(0.0, a["available"])),
                        "accountEquity": self.f(a["equity"]), "usdtEquity": self.f(a["equity"]), "btcEquity": "0",
                        "crossedRiskRate": "0", "crossedMarginLeverage": str(p["leverage"]),
                        "isolatedLongLever": str(p["leverage"]), "isolatedShortLever": str(p["leverage"]),
                        "marginMode": "isolated" if p["margin_mode"] == "isolated" else "crossed", "posMode": "one_way_mode",
                        "unrealizedPL": self.f(a["upl"]), "coupon": "0", "crossedUnrealizedPL": self.f(a["upl"]),
                        "isolatedUnrealizedPL": "0", "assetMode": "single"}, ctx)

    def h_positions(self, req, ctx):
        """「Get All Positions」GET /api/v2/mix/position/all-position?productType&marginCoin（只返回非零持仓）"""
        self._product(req)
        rows = []
        for p in ctx.positions_view():
            if p["qty"] == 0:
                continue
            rows.append({"marginCoin": "USDT", "symbol": self.symbol_out(p["symbol"]), "holdSide": "long" if p["qty"] > 0 else "short",
                         "openDelegateSize": "0", "marginSize": self.f(p["margin"]), "available": self.f(abs(p["qty"])),
                         "locked": "0", "total": self.f(abs(p["qty"])), "leverage": str(p["leverage"]),
                         "achievedProfits": self.f(p["realized"]), "openPriceAvg": self.f(p["entry"]),
                         "marginMode": "isolated" if p["margin_mode"] == "isolated" else "crossed", "posMode": "one_way_mode",
                         "unrealizedPL": self.f(p["upl"]), "liquidationPrice": self.f(p["liq_price"]),
                         "keepMarginRate": self.f(ctx.engine.cfg["mmr"]), "markPrice": self.f(p["mark"]),
                         "breakEvenPrice": self.f(p["entry"]), "totalFee": "0", "deductedFee": "0", "marginRatio": "0",
                         "assetMode": "single", "cTime": str(p["updated"]), "uTime": str(p["updated"])})
        return self.ok(rows, ctx)

    # ---- 交易 ----
    def h_place(self, req, ctx):
        """「Place Order」POST /api/v2/mix/order/place-order（单向持仓：side buy/sell，不传 tradeSide；reduceOnly YES/NO）"""
        self._product(req)
        side = {"buy": "BUY", "sell": "SELL"}.get(str(req.get("side")))
        otype = {"market": "MARKET", "limit": "LIMIT"}.get(str(req.get("orderType")))
        if req.get("marginMode") not in ("crossed", "isolated") or req.get("marginCoin", "USDT") != "USDT":
            raise EngineError("BAD_PARAM", "marginMode/marginCoin")
        reduce_only = str(req.get("reduceOnly", "NO")).upper() == "YES"
        canon = base.canon_place(self.symbol_in(req.get("symbol")), side, otype, req.get("size"), req.get("price"),
                                 req.get("clientOid"), reduce_only, str(req.get("force", "gtc")).upper())
        o = base.op_place(ctx, canon)
        return self.ok({"clientOid": o["cid"] or "", "orderId": str(o["id"])}, ctx)

    def h_cancel(self, req, ctx):
        """「Cancel Order」POST /api/v2/mix/order/cancel-order"""
        self._product(req)
        self.symbol_in(req.get("symbol"))
        oid, cid = base.parse_order_id(req.get("orderId")), req.get("clientOid")
        if oid is None and cid is None:
            raise EngineError("BAD_PARAM", "orderId or clientOid")
        o = base.op_cancel(ctx, order_id=oid, cid=cid)
        return self.ok({"clientOid": o["cid"] or "", "orderId": str(o["id"])}, ctx)

    def h_query(self, req, ctx):
        """「Get Order Details」GET /api/v2/mix/order/detail?symbol&productType&orderId|clientOid"""
        self._product(req)
        self.symbol_in(req.get("symbol"))
        oid, cid = base.parse_order_id(req.get("orderId")), req.get("clientOid")
        if oid is None and cid is None:
            raise EngineError("BAD_PARAM", "orderId or clientOid")
        return self.ok(self._order(base.op_query(ctx, order_id=oid, cid=cid), ctx), ctx)

    def h_open_orders(self, req, ctx):
        """「Get Pending Orders」GET /api/v2/mix/order/orders-pending?productType"""
        self._product(req)
        want = req.get("symbol")
        canon = None if want is None else self.symbol_in(want)
        rows = [self._order(o, ctx) for o in ctx.engine.open_orders(canon)]
        return self.ok({"entrustedList": rows, "endId": str(rows[-1]["orderId"]) if rows else ""}, ctx)

    def h_fills(self, req, ctx):
        """「Get Order Fill Details」GET /api/v2/mix/order/fills?productType"""
        self._product(req)
        want = req.get("symbol")
        canon = None if want is None else self.symbol_in(want)
        rows = []
        for f in ctx.fills_view(canon):
            rows.append({"tradeId": str(f["id"]), "symbol": self.symbol_out(f["symbol"]), "orderId": str(f["order_id"]),
                         "price": self.f(f["price"]), "baseVolume": self.f(f["qty"]),
                         "feeDetail": [{"deduction": "no", "feeCoin": "USDT", "totalDeductionFee": "0", "totalFee": self.f(-f["fee"])}],
                         "side": f["side"].lower(), "quoteVolume": self.f(f["price"] * f["qty"]), "profit": self.f(f["realized"]),
                         "enterPointSource": "API", "tradeSide": "", "posMode": "one_way_mode",
                         "tradeScope": "maker" if f["maker"] else "taker", "cTime": str(f["ts"])})
        return self.ok({"fillList": rows, "endId": rows[-1]["tradeId"] if rows else ""}, ctx)

    def h_leverage(self, req, ctx):
        """「Change Leverage」POST /api/v2/mix/account/set-leverage"""
        self._product(req)
        canon = self.symbol_in(req.get("symbol"))
        lev = ctx.engine.set_leverage(canon, req.get("leverage"))
        p = ctx.engine.position(canon)
        return self.ok({"symbol": self.symbol_out(canon), "marginCoin": "USDT", "longLeverage": str(lev),
                        "shortLeverage": str(lev), "crossMarginLeverage": str(lev),
                        "marginMode": "isolated" if p["margin_mode"] == "isolated" else "crossed"}, ctx)

    def h_margin_mode(self, req, ctx):
        """「Change Margin Mode」POST /api/v2/mix/account/set-margin-mode（isolated / crossed）"""
        self._product(req)
        canon = self.symbol_in(req.get("symbol"))
        mode = {"isolated": "isolated", "crossed": "cross"}.get(str(req.get("marginMode")))
        if mode is None:
            raise EngineError("BAD_PARAM", "marginMode")
        ctx.engine.set_margin_mode(canon, mode)
        p = ctx.engine.position(canon)
        return self.ok({"symbol": self.symbol_out(canon), "marginCoin": "USDT", "longLeverage": str(p["leverage"]),
                        "shortLeverage": str(p["leverage"]), "marginMode": req.get("marginMode")}, ctx)

    def _order(self, o, ctx):
        p = ctx.engine.position(o["symbol"])
        return {"symbol": self.symbol_out(o["symbol"]), "size": self.f(o["qty"]), "orderId": str(o["id"]), "clientOid": o["cid"] or "",
                "baseVolume": self.f(o["filled"]), "fee": self.f(-o["fee"]), "price": self.f(o["price"] or 0),
                "priceAvg": self.f(o["avg_price"]), "state": STATE[o["status"]], "side": o["side"].lower(),
                "force": o["tif"].lower(), "totalProfits": self.f(o["realized"]), "posSide": "net", "marginCoin": "USDT",
                "presetStopSurplusPrice": "", "presetStopLossPrice": "", "quoteVolume": self.f(o["filled"] * o["avg_price"]),
                "orderType": o["type"].lower(), "leverage": str(p["leverage"]),
                "marginMode": "isolated" if p["margin_mode"] == "isolated" else "crossed",
                "reduceOnly": "YES" if o["reduce_only"] else "NO", "enterPointSource": "API", "tradeSide": "",
                "posMode": "one_way_mode", "orderSource": "normal", "cTime": str(o["created"]), "uTime": str(o["updated"])}
