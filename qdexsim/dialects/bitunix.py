"""Bitunix Futures（/api/v1/futures）最小子集。字段来源：Bitunix OpenAPI 文档「Futures」各章节（见 handler 注释）；
文档站在开发环境不可达，形状凭记忆，本方言可信度整体低于其余四家，逐字段见 qdexsim/SHAPES.md。

鉴权（文档「Common › Signature」，待核）：头 api-key / nonce（32 位随机串，不得重复）/ timestamp（毫秒）/ sign；
两次 SHA-256（不是 HMAC）：digest = SHA256(nonce + timestamp + api_key + queryParams + body)，sign = SHA256(digest + secret)；
queryParams = 按键名升序、key 与 value 直接拼接、无分隔符；body = 原始 JSON 串。时间窗 ±30 秒（待核）。
报文形状 {"code": 0, "data": ..., "msg": "Success"}；业务错 HTTP 200 + code ≠ 0。
"""
from qdexsim.dialects import base
from qdexsim.engine import EngineError

INTERVALS = {"1m": 60000, "3m": 180000, "5m": 300000, "15m": 900000, "30m": 1800000, "1h": 3600000, "4h": 14400000,
             "1d": 86400000}
STATUS = {"NEW": "NEW", "PARTIALLY_FILLED": "PART_FILLED", "FILLED": "FILLED", "CANCELED": "CANCELED", "REJECTED": "CANCELED"}
WINDOW_MS = 30000
ERRORS = {
    "BAD_SIGN": (200, 10007, "Sign signature error"),
    "BAD_TIMESTAMP": (200, 10002, "Parameter Error: timestamp expired"),                     # 待核
    "BAD_KEY": (200, 10003, "api-key can't be empty or invalid"),                             # 待核
    "BAD_NONCE": (200, 10008, "nonce does not comply with the rule"),                        # 待核
    "MISSING_AUTH": (200, 10003, "api-key can't be empty"),
    "NOT_FOUND": (404, 10001, "Network Error"),                                               # 待核
    "BAD_PARAM": (200, 10002, "Parameter Error: {p}"),
    "SYMBOL_UNKNOWN": (200, 20001, "Market not exists"),
    "SYMBOL_DELISTED": (200, 20001, "Market not exists"),
    "SYMBOL_SUSPENDED": (200, 20015, "This futures does not allow trading"),                 # 待核
    "BAD_QTY": (200, 10008, "qty does not comply with the rule"),                             # 待核
    "BAD_PRICE": (200, 10008, "price does not comply with the rule"),                         # 待核
    "MIN_NOTIONAL": (200, 30002, "Minimum order value not met"),                              # 待核
    "DUP_CID": (200, 30008, "Duplicate clientId"),                                            # qdexsim 自定，待核
    "ORDER_NOT_FOUND": (200, 20007, "Order not found, please try it later"),
    "ORDER_NOT_OPEN": (200, 20007, "Order not found, please try it later"),                   # 待核
    "INSUFFICIENT": (200, 20003, "Insufficient balance"),
    "REDUCE_ONLY": (200, 20002, "The current positions do not exist"),                        # 待核
    "BAD_LEVERAGE": (200, 20005, "Invalid leverage"),
    "MARGIN_MODE_BUSY": (200, 20006, "You can't change leverage or margin mode as there are open orders"),
    "RATE_LIMIT": (429, 10006, "Request too frequently"),                                     # 待核（HTTP 码与重试头）
    "SERVER_ERROR": (500, 10001, "Network Error"),                                            # 待核
    "MAINTENANCE": (503, 20012, "Futures not allowed to trade"),                              # 待核
}


class Bitunix(base.Dialect):
    NAME = "bitunix"
    DEFAULT_CREDS = {"api_key": "bitunix-key", "secret": "bitunix-secret", "passphrase": ""}
    ROUTES = (
        ("GET", "/api/v1/futures/market/time", "time", False),             # qdexsim 自加：Bitunix 文档未见服务器时间接口（待核）
        ("GET", "/api/v1/futures/market/trading_pairs", "instruments", False),
        ("GET", "/api/v1/futures/market/tickers", "ticker", False),
        ("GET", "/api/v1/futures/market/kline", "klines", False),
        ("GET", "/api/v1/futures/account", "account", True),
        ("GET", "/api/v1/futures/position/get_pending_positions", "positions", True),
        ("POST", "/api/v1/futures/trade/place_order", "place", True),
        ("POST", "/api/v1/futures/trade/cancel_orders", "cancel", True),
        ("GET", "/api/v1/futures/trade/get_order_detail", "query", True),
        ("GET", "/api/v1/futures/trade/get_pending_orders", "open_orders", True),
        ("GET", "/api/v1/futures/trade/get_history_trades", "fills", True),
        ("POST", "/api/v1/futures/account/change_leverage", "leverage", True),
        ("POST", "/api/v1/futures/account/change_margin_mode", "margin_mode", True),
    )

    def __init__(self, creds=None):
        super().__init__(creds)
        self.seen_nonces = set()

    def ok(self, data, ctx):
        return base.Response(200, {"code": 0, "data": data, "msg": "Success"})

    @staticmethod
    def query_string(params):
        return "".join(k + str(v) for k, v in sorted(params.items()))

    def auth(self, req, ctx):
        key, nonce, ts, sign = (req.header(h) for h in ("api-key", "nonce", "timestamp", "sign"))
        if key is None:
            return self.error("MISSING_AUTH", "", ctx)
        if key != self.creds["api_key"]:
            return self.error("BAD_KEY", "", ctx)
        if nonce is None or ts is None or sign is None:
            return self.error("BAD_PARAM", "nonce/timestamp/sign", ctx)
        digest = base.sha256_hex(nonce + ts + key + self.query_string(req.query_params) + req.body_text)
        if not base.hmac.compare_digest(base.sha256_hex(digest + self.creds["secret"]), sign):
            return self.error("BAD_SIGN", "", ctx)
        try:
            if abs(ctx.now() - int(ts)) > WINDOW_MS:
                return self.error("BAD_TIMESTAMP", "", ctx)
        except ValueError:
            return self.error("BAD_TIMESTAMP", "", ctx)
        if len(nonce) < 8 or nonce in self.seen_nonces:
            return self.error("BAD_NONCE", "", ctx)
        self.seen_nonces.add(nonce)
        return None

    def error(self, kind, msg, ctx, retry_after_ms=None):
        status, code, text = ERRORS.get(kind, ERRORS["SERVER_ERROR"])
        headers = self.rate_limit_headers(retry_after_ms or 1000, ctx) if kind == "RATE_LIMIT" else {}
        return base.Response(status, {"code": code, "data": None, "msg": text.format(p=msg or "")}, headers)

    # ---- 行情 ----
    def h_time(self, req, ctx):
        """qdexsim 自加 GET /api/v1/futures/market/time → {"serverTime": ms}（待核）"""
        return self.ok({"serverTime": ctx.now()}, ctx)

    def h_instruments(self, req, ctx):
        """「Get Trading Pairs」GET /api/v1/futures/market/trading_pairs?symbols="""
        want = [s for s in str(req.get("symbols", "")).split(",") if s]
        rows = []
        for s in ctx.engine.symbols.values():
            if (not want or self.symbol_out(s["symbol"]) in want) and s["status"] != "DELISTED":
                rows.append({"symbol": self.symbol_out(s["symbol"]), "base": s["base"], "quote": s["quote"],
                             "minTradeVolume": self.f(s["min_qty"]), "minBuyPriceOffset": "-0.05", "maxBuyPriceOffset": "0.05",
                             "minSellPriceOffset": "-0.05", "maxSellPriceOffset": "0.05", "maxLimitOrderVolume": "1000",
                             "maxMarketOrderVolume": "100", "basePrecision": s["qty_places"], "quotePrecision": s["price_places"],
                             "maxLeverage": s["max_leverage"], "minLeverage": 1, "defaultLeverage": 20,
                             "makerFee": self.f(ctx.engine.cfg["maker_fee"]), "takerFee": self.f(ctx.engine.cfg["taker_fee"]),
                             "symbolStatus": "OPEN" if s["status"] == "TRADING" else "CLOSE",            # 待核
                             "tickSize": self.f(s["tick"]), "stepSize": self.f(s["step"])})               # 待核：精度字段名
        return self.ok(rows, ctx)

    def h_ticker(self, req, ctx):
        """「Get Tickers」GET /api/v1/futures/market/tickers?symbols="""
        want = [s for s in str(req.get("symbols", "")).split(",") if s]
        rows = []
        for canon in sorted(ctx.engine.mark):
            if not want or self.symbol_out(canon) in want:
                px = self.f(ctx.engine.mark[canon])
                rows.append({"symbol": self.symbol_out(canon), "markPrice": px, "lastPrice": px, "open": px, "last": px,
                             "quoteVol": "0", "baseVol": "0", "high": px, "low": px})
        if want and not rows:
            raise EngineError("SYMBOL_UNKNOWN", want[0])
        return self.ok(rows, ctx)

    def h_klines(self, req, ctx):
        """「Get Kline」GET /api/v1/futures/market/kline?symbol&interval&limit → [{open, high, low, close, quoteVol, baseVol, time, type}]"""
        canon = self.symbol_in(req.get("symbol"))
        ctx.engine.spec(canon)
        ms = INTERVALS.get(req.get("interval", "1m"))
        if ms is None:
            raise EngineError("BAD_PARAM", "interval")
        bars = ctx.candles.get(canon, min(int(req.get("limit", 100)), 200), ms)
        return self.ok([{"open": self.f(b[1]), "high": self.f(b[2]), "low": self.f(b[3]), "close": self.f(b[4]),
                         "quoteVol": self.f(b[5] * b[4]), "baseVol": self.f(b[5]), "time": b[0], "type": "LAST_PRICE"}
                        for b in bars], ctx)

    # ---- 账户 ----
    def h_account(self, req, ctx):
        """「Get Single Account」GET /api/v1/futures/account?marginCoin=USDT"""
        if req.get("marginCoin", "USDT") != "USDT":
            raise EngineError("BAD_PARAM", "marginCoin")
        a = ctx.account_view()
        return self.ok({"marginCoin": "USDT", "available": self.f(a["available"]), "frozen": self.f(a["order_margin"]),
                        "margin": self.f(a["position_margin"]), "transfer": self.f(max(0.0, a["available"])),
                        "positionMode": "ONE_WAY", "crossUnrealizedPNL": self.f(a["upl"]), "isolationUnrealizedPNL": "0",
                        "bonus": "0"}, ctx)

    def h_positions(self, req, ctx):
        """「Get Pending Positions」GET /api/v1/futures/position/get_pending_positions（只返回非零持仓）"""
        want = req.get("symbol")
        rows = []
        for p in ctx.positions_view():
            if p["qty"] == 0 or want not in (None, self.symbol_out(p["symbol"])):
                continue
            rows.append({"positionId": str(abs(hash(p["symbol"])) % 10 ** 9), "symbol": self.symbol_out(p["symbol"]),
                         "qty": self.f(abs(p["qty"])), "entryValue": self.f(abs(p["qty"]) * p["entry"]),
                         "side": "BUY" if p["qty"] > 0 else "SELL",
                         "marginMode": "ISOLATION" if p["margin_mode"] == "isolated" else "CROSS", "positionMode": "ONE_WAY",
                         "leverage": p["leverage"], "fee": "0", "funding": "0", "realizedPNL": self.f(p["realized"]),
                         "margin": self.f(p["margin"]), "unrealizedPNL": self.f(p["upl"]), "liqPrice": self.f(p["liq_price"]),
                         "marginRate": "0", "avgOpenPrice": self.f(p["entry"]), "ctime": str(p["updated"]), "mtime": str(p["updated"])})
        return self.ok(rows, ctx)

    # ---- 交易 ----
    def h_place(self, req, ctx):
        """「Place Order」POST /api/v1/futures/trade/place_order（单向持仓：side BUY/SELL，tradeSide 可省；reduceOnly 布尔）"""
        side = {"BUY": "BUY", "SELL": "SELL"}.get(str(req.get("side")))
        otype = {"MARKET": "MARKET", "LIMIT": "LIMIT"}.get(str(req.get("orderType")))
        canon = base.canon_place(self.symbol_in(req.get("symbol")), side, otype, req.get("qty"), req.get("price"),
                                 req.get("clientId"), base.truthy(req.get("reduceOnly", False)), str(req.get("effect", "GTC")))
        o = base.op_place(ctx, canon)
        return self.ok({"orderId": str(o["id"]), "clientId": o["cid"] or ""}, ctx)

    def h_cancel(self, req, ctx):
        """「Cancel Orders」POST /api/v1/futures/trade/cancel_orders {symbol, orderList: [{orderId | clientId}]}
        → {successList, failureList}（单笔失败不整单报错，待核）"""
        self.symbol_in(req.get("symbol"))
        items = req.get("orderList")
        if not isinstance(items, list) or not items:
            raise EngineError("BAD_PARAM", "orderList")
        ok, bad = [], []
        for it in items:
            oid, cid = base.parse_order_id(it.get("orderId")), it.get("clientId")
            try:
                o = base.op_cancel(ctx, order_id=oid, cid=cid)
                ok.append({"orderId": str(o["id"]), "clientId": o["cid"] or ""})
            except EngineError as e:
                code, text = ERRORS.get(e.kind, ERRORS["SERVER_ERROR"])[1:]
                bad.append({"orderId": str(oid or ""), "clientId": cid or "", "errorMsg": text.format(p=e.msg), "errorCode": code})
        return self.ok({"successList": ok, "failureList": bad}, ctx)

    def h_query(self, req, ctx):
        """「Get Order Detail」GET /api/v1/futures/trade/get_order_detail?orderId|clientId"""
        oid, cid = base.parse_order_id(req.get("orderId")), req.get("clientId")
        if oid is None and cid is None:
            raise EngineError("BAD_PARAM", "orderId or clientId")
        return self.ok(self._order(base.op_query(ctx, order_id=oid, cid=cid), ctx), ctx)

    def h_open_orders(self, req, ctx):
        """「Get Pending Orders」GET /api/v1/futures/trade/get_pending_orders"""
        want = req.get("symbol")
        canon = None if want is None else self.symbol_in(want)
        rows = [self._order(o, ctx) for o in ctx.engine.open_orders(canon)]
        return self.ok({"orderList": rows, "total": len(rows)}, ctx)

    def h_fills(self, req, ctx):
        """「Get History Trades」GET /api/v1/futures/trade/get_history_trades"""
        want = req.get("symbol")
        canon = None if want is None else self.symbol_in(want)
        rows = []
        for f in ctx.fills_view(canon):
            o = ctx.engine.get_order(order_id=f["order_id"])
            p = ctx.engine.position(f["symbol"])
            rows.append({"tradeId": str(f["id"]), "orderId": str(f["order_id"]), "symbol": self.symbol_out(f["symbol"]),
                         "qty": self.f(f["qty"]), "positionMode": "ONE_WAY",
                         "marginMode": "ISOLATION" if p["margin_mode"] == "isolated" else "CROSS", "leverage": p["leverage"],
                         "price": self.f(f["price"]), "side": f["side"], "orderType": o["type"], "effect": o["tif"],
                         "clientId": f["cid"] or "", "reduceOnly": o["reduce_only"], "fee": self.f(f["fee"]),
                         "realizedPNL": self.f(f["realized"]), "ctime": str(f["ts"]), "roleType": "MAKER" if f["maker"] else "TAKER"})
        return self.ok({"tradeList": rows, "total": len(rows)}, ctx)

    def h_leverage(self, req, ctx):
        """「Change Leverage」POST /api/v1/futures/account/change_leverage {symbol, marginCoin, leverage}"""
        canon = self.symbol_in(req.get("symbol"))
        lev = ctx.engine.set_leverage(canon, req.get("leverage"))
        return self.ok({"symbol": self.symbol_out(canon), "marginCoin": "USDT", "leverage": lev}, ctx)

    def h_margin_mode(self, req, ctx):
        """「Change Margin Mode」POST /api/v1/futures/account/change_margin_mode {symbol, marginCoin, marginMode ISOLATION|CROSS}"""
        canon = self.symbol_in(req.get("symbol"))
        mode = {"ISOLATION": "isolated", "CROSS": "cross"}.get(str(req.get("marginMode")))
        if mode is None:
            raise EngineError("BAD_PARAM", "marginMode")
        ctx.engine.set_margin_mode(canon, mode)
        return self.ok({"symbol": self.symbol_out(canon), "marginCoin": "USDT", "marginMode": req.get("marginMode")}, ctx)

    def _order(self, o, ctx):
        p = ctx.engine.position(o["symbol"])
        return {"orderId": str(o["id"]), "symbol": self.symbol_out(o["symbol"]), "qty": self.f(o["qty"]), "tradeQty": self.f(o["filled"]),
                "positionMode": "ONE_WAY", "marginMode": "ISOLATION" if p["margin_mode"] == "isolated" else "CROSS",
                "leverage": p["leverage"], "price": self.f(o["price"] or 0), "side": o["side"], "orderType": o["type"],
                "effect": o["tif"], "clientId": o["cid"] or "", "reduceOnly": o["reduce_only"], "status": STATUS[o["status"]],
                "fee": self.f(o["fee"]), "realizedPNL": self.f(o["realized"]), "avgPrice": self.f(o["avg_price"]),
                "ctime": str(o["created"]), "mtime": str(o["updated"])}
