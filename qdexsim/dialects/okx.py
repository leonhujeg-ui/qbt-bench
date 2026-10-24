"""OKX v5（instType=SWAP，USDT 本位永续，如 BTC-USDT-SWAP）最小子集。字段来源：OKX API v5 文档（章节名见 handler 注释）；
文档站在开发环境不可达，形状凭记忆，逐字段可信度见 qdexsim/SHAPES.md。

鉴权（文档「REST Authentication › Making Requests」）：头 OK-ACCESS-KEY / OK-ACCESS-SIGN / OK-ACCESS-TIMESTAMP（ISO 8601，如
2020-12-08T09:08:57.715Z）/ OK-ACCESS-PASSPHRASE；sign = base64(HMAC-SHA256(secret, timestamp + method + requestPath + body))，
requestPath 含查询串；时间戳与服务器时间相差 30 秒以上 → 50102（「Timestamp request expired」）。
报文形状 {"code": "0", "msg": "", "data": [...]}；鉴权错 HTTP 401；下单 / 撤单的业务错在 data[0].sCode / sMsg，外层 code "1"。
张数：sz 以**张**计，1 张 = ctVal 个币（BTC-USDT-SWAP 0.01 BTC，ETH-USDT-SWAP 0.1 ETH）；engine 规范量 = sz × ctVal。
"""
import datetime

from qdexsim.dialects import base
from qdexsim.engine import EngineError

BARS = {"1m": 60000, "3m": 180000, "5m": 300000, "15m": 900000, "30m": 1800000, "1H": 3600000, "2H": 7200000,
        "4H": 14400000, "1D": 86400000}
STATE = {"NEW": "live", "PARTIALLY_FILLED": "partially_filled", "FILLED": "filled", "CANCELED": "canceled",
         "REJECTED": "canceled"}
WINDOW_MS = 30000
# kind → (HTTP, code, msg)；下单 / 撤单的业务错用 ORDER_SCODES 包进 data[0]
ERRORS = {
    "BAD_SIGN": (401, "50113", "Invalid Sign"),
    "BAD_TIMESTAMP": (401, "50102", "Timestamp request expired"),
    "BAD_KEY": (401, "50111", "Invalid OK-ACCESS-KEY"),
    "BAD_PASSPHRASE": (401, "50105", "OK-ACCESS-PASSPHRASE incorrect"),                      # 待核
    "MISSING_AUTH": (401, "50103", "Request header 'OK-ACCESS-KEY' cannot be empty"),        # 待核：缺不同头的码不同
    "NOT_FOUND": (404, "404", "Not Found"),
    "BAD_PARAM": (200, "51000", "Parameter {p} error"),
    "SYMBOL_UNKNOWN": (200, "51001", "Instrument ID doesn't exist"),
    "SYMBOL_DELISTED": (200, "51001", "Instrument ID doesn't exist"),
    "SYMBOL_SUSPENDED": (200, "51000", "Instrument is suspended"),                           # 待核
    "BAD_QTY": (200, "51000", "Parameter sz error"),                                          # 待核
    "BAD_PRICE": (200, "51000", "Parameter px error"),                                        # 待核
    "MIN_NOTIONAL": (200, "51020", "Order amount should be greater than the min available amount"),
    "DUP_CID": (200, "51016", "Client Order ID already exists"),                              # 待核
    "ORDER_NOT_FOUND": (200, "51603", "Order does not exist"),
    "ORDER_NOT_OPEN": (200, "51400", "Cancellation failed as the order does not exist"),     # 待核（51401/51402 分已撤 / 已成）
    "INSUFFICIENT": (200, "51008", "Order failed. Insufficient account balance"),
    "REDUCE_ONLY": (200, "51000", "Parameter reduceOnly error"),                              # 待核
    "BAD_LEVERAGE": (200, "51000", "Parameter lever error"),                                  # 待核
    "MARGIN_MODE_BUSY": (200, "51000", "Parameter mgnMode error: position exists"),          # 待核
    "RATE_LIMIT": (429, "50011", "Rate limit reached. Please refer to API documentation and throttle requests accordingly."),
    "SERVER_ERROR": (500, "50026", "System error, please try again later."),
    "MAINTENANCE": (503, "50001", "Service temporarily unavailable, please try again later."),
}
ORDER_SCODES = ("BAD_PARAM", "SYMBOL_UNKNOWN", "SYMBOL_DELISTED", "SYMBOL_SUSPENDED", "BAD_QTY", "BAD_PRICE",
                "MIN_NOTIONAL", "DUP_CID", "ORDER_NOT_OPEN", "INSUFFICIENT", "REDUCE_ONLY")


def iso_to_ms(s):
    try:
        return int(datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=datetime.timezone.utc)
                   .timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def ms_to_iso(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
        f"{ms % 1000:03d}Z"


class OKX(base.Dialect):
    NAME = "okx"
    DEFAULT_CREDS = {"api_key": "okx-key", "secret": "okx-secret", "passphrase": "okx-pass"}
    SYMBOLS = {"BTCUSDT": "BTC-USDT-SWAP", "ETHUSDT": "ETH-USDT-SWAP"}
    ROUTES = (
        ("GET", "/api/v5/public/time", "time", False),
        ("GET", "/api/v5/public/instruments", "instruments", False),
        ("GET", "/api/v5/market/ticker", "ticker", False),
        ("GET", "/api/v5/market/candles", "klines", False),
        ("GET", "/api/v5/account/balance", "account", True),
        ("GET", "/api/v5/account/positions", "positions", True),
        ("POST", "/api/v5/trade/order", "place", True),
        ("POST", "/api/v5/trade/cancel-order", "cancel", True),
        ("GET", "/api/v5/trade/order", "query", True),
        ("GET", "/api/v5/trade/orders-pending", "open_orders", True),
        ("GET", "/api/v5/trade/fills", "fills", True),
        ("POST", "/api/v5/account/set-leverage", "leverage", True),
        ("POST", "/api/v5/account/set-position-mode", "margin_mode", True),   # 见 h_margin_mode 注释
    )

    def ok(self, data, ctx, **extra):
        return base.Response(200, dict({"code": "0", "msg": "", "data": data}, **extra))

    def auth(self, req, ctx):
        key, sign, ts, pw = (req.header(h) for h in ("ok-access-key", "ok-access-sign", "ok-access-timestamp",
                                                     "ok-access-passphrase"))
        if key is None or sign is None or ts is None or pw is None:
            return self.error("MISSING_AUTH", "", ctx)
        if key != self.creds["api_key"]:
            return self.error("BAD_KEY", "", ctx)
        if pw != self.creds["passphrase"]:
            return self.error("BAD_PASSPHRASE", "", ctx)
        path = req.path + ("?" + req.query if req.query else "")
        if not base.hmac.compare_digest(base.hmac_b64(self.creds["secret"], ts + req.method + path + req.body_text), sign):
            return self.error("BAD_SIGN", "", ctx)
        ms = iso_to_ms(ts)
        if ms is None or abs(ctx.now() - ms) > WINDOW_MS:
            return self.error("BAD_TIMESTAMP", "", ctx)
        return None

    def error(self, kind, msg, ctx, retry_after_ms=None):
        status, code, text = ERRORS.get(kind, ERRORS["SERVER_ERROR"])
        text = text.format(p=msg or "")
        headers = self.rate_limit_headers(retry_after_ms or 1000, ctx) if kind == "RATE_LIMIT" else {}
        return base.Response(status, {"code": code, "msg": text, "data": []}, headers)

    def order_error(self, kind, msg, cid, ctx):
        """下单 / 撤单的业务错：外层 code "1"，data[0].sCode（「Place Order › Response Parameters」）。"""
        if kind not in ORDER_SCODES:
            return self.error(kind, msg, ctx)
        _s, code, text = ERRORS[kind]
        return base.Response(200, {"code": "1", "msg": "Operation failed.", "data": [
            {"clOrdId": cid or "", "ordId": "", "tag": "", "sCode": code, "sMsg": text.format(p=msg or "")}]})

    def rate_limit_headers(self, retry_after_ms, ctx):
        return {}                                   # 待核：OKX 文档未写 429 的重试头；适配器用缺省退避

    def _ct(self, ctx, canon):
        return ctx.engine.spec(canon)["ct_val"]

    # ---- 行情 ----
    def h_time(self, req, ctx):
        """「Get system time」GET /api/v5/public/time"""
        return self.ok([{"ts": str(ctx.now())}], ctx)

    def h_instruments(self, req, ctx):
        """「Get instruments」GET /api/v5/public/instruments?instType=SWAP"""
        if req.get("instType") != "SWAP":
            raise EngineError("BAD_PARAM", "instType")
        want = req.get("instId")
        rows = []
        for s in ctx.engine.symbols.values():
            if want in (None, self.symbol_out(s["symbol"])) and s["status"] != "DELISTED":
                rows.append({"instType": "SWAP", "instId": self.symbol_out(s["symbol"]), "uly": f"{s['base']}-USDT",
                             "instFamily": f"{s['base']}-USDT", "baseCcy": "", "quoteCcy": "", "settleCcy": "USDT",
                             "ctVal": self.f(s["ct_val"]), "ctMult": "1", "ctValCcy": s["base"], "ctType": "linear",
                             "state": "live" if s["status"] == "TRADING" else "suspend", "tickSz": self.f(s["tick"]),
                             "lotSz": "1", "minSz": self.f(max(1.0, round(s["min_qty"] / s["ct_val"]))),
                             "lever": str(s["max_leverage"]), "listTime": "0", "expTime": ""})
        return self.ok(rows, ctx)

    def h_ticker(self, req, ctx):
        """「Get ticker」GET /api/v5/market/ticker?instId="""
        canon = self.symbol_in(req.get("instId"))
        ctx.engine.spec(canon)
        px = self.f(ctx.engine.mark[canon])
        return self.ok([{"instType": "SWAP", "instId": self.symbol_out(canon), "last": px, "lastSz": "0", "askPx": px,
                         "askSz": "0", "bidPx": px, "bidSz": "0", "open24h": px, "high24h": px, "low24h": px,
                         "volCcy24h": "0", "vol24h": "0", "ts": str(ctx.now()), "sodUtc0": px, "sodUtc8": px}], ctx)

    def h_klines(self, req, ctx):
        """「Get candlesticks」GET /api/v5/market/candles → [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]，最新在前"""
        canon = self.symbol_in(req.get("instId"))
        ct = self._ct(ctx, canon)
        ms = BARS.get(req.get("bar", "1m"))
        if ms is None:
            raise EngineError("BAD_PARAM", "bar")
        bars = ctx.candles.get(canon, min(int(req.get("limit", 100)), 300), ms)
        rows = [[str(b[0]), self.f(b[1]), self.f(b[2]), self.f(b[3]), self.f(b[4]), self.f(b[5] / ct), self.f(b[5]),
                 self.f(b[5] * b[4]), "1"] for b in reversed(bars)]
        return self.ok(rows, ctx)

    # ---- 账户 ----
    def h_account(self, req, ctx):
        """「Get balance」GET /api/v5/account/balance"""
        a = ctx.account_view()
        det = {"ccy": "USDT", "eq": self.f(a["equity"]), "cashBal": self.f(a["wallet"]), "availBal": self.f(a["available"]),
               "availEq": self.f(a["available"]), "frozenBal": self.f(a["position_margin"] + a["order_margin"]),
               "ordFrozen": self.f(a["order_margin"]), "upl": self.f(a["upl"]), "uTime": str(ctx.now())}
        return self.ok([{"totalEq": det["eq"], "isoEq": "0", "adjEq": det["eq"], "availEq": det["availEq"],
                         "ordFroz": det["ordFrozen"], "imr": self.f(a["position_margin"]), "mmr": "0", "mgnRatio": "",
                         "notionalUsd": "0", "uTime": det["uTime"], "details": [det]}], ctx)

    def h_positions(self, req, ctx):
        """「Get positions」GET /api/v5/account/positions?instType=SWAP（只返回非零持仓）"""
        want = req.get("instId")
        rows = []
        for p in ctx.positions_view():
            if p["qty"] == 0 or want not in (None, self.symbol_out(p["symbol"])):
                continue
            ct = self._ct(ctx, p["symbol"])
            rows.append({"instType": "SWAP", "instId": self.symbol_out(p["symbol"]), "mgnMode": p["margin_mode"],
                         "posId": str(1), "posSide": "net", "pos": self.f(p["qty"] / ct), "availPos": self.f(p["qty"] / ct),
                         "avgPx": self.f(p["entry"]), "upl": self.f(p["upl"]), "uplRatio": "0", "lever": str(p["leverage"]),
                         "liqPx": self.f(p["liq_price"]), "markPx": self.f(p["mark"]), "margin": self.f(p["margin"]),
                         "mmr": self.f(p["notional"] * ctx.engine.cfg["mmr"]), "notionalUsd": self.f(p["notional"]),
                         "cTime": str(p["updated"]), "uTime": str(p["updated"])})
        return self.ok(rows, ctx)

    # ---- 交易 ----
    def h_place(self, req, ctx):
        """「Place order」POST /api/v5/trade/order（sz 为张数，tdMode cross|isolated 须与当前持仓保证金模式一致）"""
        cid = req.get("clOrdId")
        try:
            canon_sym = self.symbol_in(req.get("instId"))
            ct = self._ct(ctx, canon_sym)
            if req.get("tdMode") not in ("cross", "isolated"):
                raise EngineError("BAD_PARAM", "tdMode")
            if req.get("tdMode") != ctx.engine.position(canon_sym)["margin_mode"]:
                raise EngineError("BAD_PARAM", "tdMode mismatch with position margin mode")
            side = {"buy": "BUY", "sell": "SELL"}.get(str(req.get("side")))
            otype = {"market": "MARKET", "limit": "LIMIT"}.get(str(req.get("ordType")))
            sz = base.to_float(req.get("sz"), "sz")
            canon = base.canon_place(canon_sym, side, otype, sz * ct, req.get("px"), cid, base.truthy(req.get("reduceOnly", False)))
            o = base.op_place(ctx, canon)
        except EngineError as e:
            return self.order_error(e.kind, e.msg, cid, ctx)
        now = str(ctx.now())
        return self.ok([{"clOrdId": o["cid"] or "", "ordId": str(o["id"]), "tag": "", "sCode": "0", "sMsg": ""}], ctx,
                       inTime=now, outTime=now)

    def h_cancel(self, req, ctx):
        """「Cancel order」POST /api/v5/trade/cancel-order"""
        cid = req.get("clOrdId")
        try:
            self.symbol_in(req.get("instId"))
            oid = base.parse_order_id(req.get("ordId"))
            if oid is None and cid is None:
                raise EngineError("BAD_PARAM", "ordId or clOrdId")
            o = base.op_cancel(ctx, order_id=oid, cid=cid)
        except EngineError as e:
            kind = "ORDER_NOT_OPEN" if e.kind == "ORDER_NOT_FOUND" else e.kind
            return self.order_error(kind, e.msg, cid, ctx)
        return self.ok([{"clOrdId": o["cid"] or "", "ordId": str(o["id"]), "sCode": "0", "sMsg": ""}], ctx)

    def h_query(self, req, ctx):
        """「Get order details」GET /api/v5/trade/order?instId&ordId|clOrdId"""
        self.symbol_in(req.get("instId"))
        oid, cid = base.parse_order_id(req.get("ordId")), req.get("clOrdId")
        if oid is None and cid is None:
            raise EngineError("BAD_PARAM", "ordId or clOrdId")
        return self.ok([self._order(base.op_query(ctx, order_id=oid, cid=cid), ctx)], ctx)

    def h_open_orders(self, req, ctx):
        """「Get order List」GET /api/v5/trade/orders-pending?instType=SWAP"""
        want = req.get("instId")
        canon = None if want is None else self.symbol_in(want)
        return self.ok([self._order(o, ctx) for o in ctx.engine.open_orders(canon)], ctx)

    def h_fills(self, req, ctx):
        """「Get transaction details (last 3 days)」GET /api/v5/trade/fills?instType=SWAP"""
        want = req.get("instId")
        canon = None if want is None else self.symbol_in(want)
        rows = []
        for f in ctx.fills_view(canon):
            ct = self._ct(ctx, f["symbol"])
            rows.append({"instType": "SWAP", "instId": self.symbol_out(f["symbol"]), "tradeId": str(f["id"]),
                         "ordId": str(f["order_id"]), "clOrdId": f["cid"] or "", "billId": str(f["id"]), "tag": "",
                         "fillPx": self.f(f["price"]), "fillSz": self.f(f["qty"] / ct), "side": f["side"].lower(),
                         "posSide": "net", "execType": "M" if f["maker"] else "T", "feeCcy": "USDT", "fee": self.f(-f["fee"]),
                         "ts": str(f["ts"]), "fillTime": str(f["ts"]), "fillPnl": self.f(f["realized"])})
        return self.ok(rows, ctx)

    def h_leverage(self, req, ctx):
        """「Set leverage」POST /api/v5/account/set-leverage {instId, lever, mgnMode}"""
        canon = self.symbol_in(req.get("instId"))
        mode = req.get("mgnMode")
        if mode not in ("cross", "isolated"):
            raise EngineError("BAD_PARAM", "mgnMode")
        lev = ctx.engine.set_leverage(canon, req.get("lever"))
        if ctx.engine.position(canon)["margin_mode"] != mode:
            ctx.engine.set_margin_mode(canon, mode)
        return self.ok([{"lever": str(lev), "mgnMode": mode, "instId": self.symbol_out(canon), "posSide": "net"}], ctx)

    def h_margin_mode(self, req, ctx):
        """OKX 没有“按品种切换保证金模式”的接口：保证金模式随 set-leverage 的 mgnMode / 下单的 tdMode 走。
        这里把 POST /api/v5/account/set-position-mode 当作规范操作 margin_mode 的载体：只接受 posMode=net_mode
        （「Set position mode」）；真正改模式用 h_leverage。SHAPES.md 标「qdexsim 映射，非一一对应」。"""
        if req.get("posMode") != "net_mode":
            raise EngineError("BAD_PARAM", "posMode (only net_mode is simulated)")
        return self.ok([{"posMode": "net_mode"}], ctx)

    def _order(self, o, ctx):
        ct = self._ct(ctx, o["symbol"])
        p = ctx.engine.position(o["symbol"])
        return {"instType": "SWAP", "instId": self.symbol_out(o["symbol"]), "ordId": str(o["id"]), "clOrdId": o["cid"] or "",
                "tag": "", "px": self.f(o["price"] or 0), "sz": self.f(o["qty"] / ct), "ordType": o["type"].lower(),
                "side": o["side"].lower(), "posSide": "net", "tdMode": p["margin_mode"], "accFillSz": self.f(o["filled"] / ct),
                "fillPx": self.f(o["avg_price"]), "fillSz": self.f(o["filled"] / ct), "avgPx": self.f(o["avg_price"]),
                "state": STATE[o["status"]], "lever": str(p["leverage"]), "fee": self.f(-o["fee"]), "feeCcy": "USDT",
                "pnl": self.f(o["realized"]), "reduceOnly": "true" if o["reduce_only"] else "false", "tgtCcy": "",
                "cTime": str(o["created"]), "uTime": str(o["updated"])}
