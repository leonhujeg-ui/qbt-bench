"""方言公共件：请求 / 响应模型、HMAC 工具、方言无关的 13 个操作（op_*）、Dialect 基类（路由 + 鉴权 + 翻译）。

规范操作名（混沌规则与事件日志用的就是这些名字）：
    time instruments ticker klines account positions place cancel query open_orders fills leverage margin_mode

每个 Dialect 子类提供：NAME、ROUTES（(method, path, op, 需鉴权)）、DEFAULT_CREDS、auth(req, ctx)、error(kind, msg, ctx)、
h_<op>(req, ctx)。ctx（server.Context）给出 engine、带漂移的 now()、受混沌影响的视图（positions_view / account_view /
fills_view / cancel）。
"""
import base64
import hashlib
import hmac
import json
import urllib.parse

from qdexsim import common
from qdexsim.engine import EngineError

OPS = ("time", "instruments", "ticker", "klines", "account", "positions", "place", "cancel", "query", "open_orders",
       "fills", "leverage", "margin_mode")
ERROR_KINDS = ("BAD_SIGN", "BAD_TIMESTAMP", "BAD_KEY", "BAD_PASSPHRASE", "BAD_NONCE", "MISSING_AUTH", "NOT_FOUND",
               "BAD_PARAM", "SYMBOL_UNKNOWN", "SYMBOL_SUSPENDED", "SYMBOL_DELISTED", "BAD_QTY", "BAD_PRICE",
               "MIN_NOTIONAL", "DUP_CID", "ORDER_NOT_FOUND", "ORDER_NOT_OPEN", "INSUFFICIENT", "REDUCE_ONLY",
               "BAD_LEVERAGE", "MARGIN_MODE_BUSY", "RATE_LIMIT", "SERVER_ERROR", "MAINTENANCE")


class Request:
    def __init__(self, method, path, query="", headers=None, body=b""):
        self.method, self.path, self.query = method.upper(), path, query or ""
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.body = body or b""
        self.body_text = self.body.decode("utf-8", "replace")
        self.query_params = parse_qs(self.query)
        self.json = None
        ctype = self.headers.get("content-type", "")
        if self.body and "json" in ctype.lower():
            try:
                self.json = json.loads(self.body_text)
            except ValueError:
                self.json = None
        form = parse_qs(self.body_text) if self.body and self.json is None and "json" not in ctype.lower() else {}
        self.params = dict(self.query_params)
        self.params.update(form)
        if isinstance(self.json, dict):
            self.params.update(self.json)

    def header(self, name, default=None):
        return self.headers.get(name.lower(), default)

    def get(self, name, default=None):
        v = self.params.get(name, default)
        return default if v in (None, "") else v


class Response:
    def __init__(self, status=200, body=None, headers=None):
        self.status, self.body, self.headers = status, body, dict(headers or {})

    def bytes(self):
        if self.body is None:
            return b""
        return json.dumps(self.body, separators=(",", ":"), ensure_ascii=False).encode()


def parse_qs(raw):
    out = {}
    for k, v in urllib.parse.parse_qsl(raw or "", keep_blank_values=True):
        out[k] = v
    return out


def strip_param(raw, name):
    """从原始查询串 / 表单串里去掉一个参数（保持其余顺序与原始编码）——Binance 验签要原样拼接。"""
    parts = [p for p in (raw or "").split("&") if p and p.split("=", 1)[0] != name]
    return "&".join(parts)


def hmac_hex(secret, msg):
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


def hmac_b64(secret, msg):
    return base64.b64encode(hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()).decode()


def sha256_hex(s):
    return hashlib.sha256(s.encode()).hexdigest()


def common_prefix(paths):
    """所有路由路径的公共前缀，截到最后一个 "/"：/fapi/、/v5/、/api/v5/、/api/v2/、/api/v1/futures/。"""
    import os.path
    pre = os.path.commonprefix(list(paths))
    return pre[:pre.rfind("/") + 1]


def to_float(v, name="param"):
    try:
        return float(v)
    except (TypeError, ValueError):
        raise EngineError("BAD_PARAM", f"{name}={v!r}") from None


def truthy(v):
    return str(v).lower() in ("true", "1", "yes")


# ---- 方言无关的操作：canon 参数 → 规范结果 ---------------------------------------------------
def op_place(ctx, canon):
    """canon = {symbol, side, type, qty, price, cid, reduce_only}（已翻成规范名 / 规范单位）。"""
    return ctx.engine.place(canon["symbol"], canon["side"], canon["type"], canon["qty"], canon.get("price"),
                            cid=canon.get("cid"), reduce_only=bool(canon.get("reduce_only")), tif=canon.get("tif", "GTC"),
                            origin=ctx.dialect_name)


def op_cancel(ctx, order_id=None, cid=None):
    return ctx.cancel(order_id=order_id, cid=cid)


def op_query(ctx, order_id=None, cid=None):
    return ctx.engine.get_order(order_id=order_id, cid=cid)


def parse_order_id(v):
    if v in (None, ""):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        raise EngineError("ORDER_NOT_FOUND", str(v)) from None


def canon_place(symbol, side, otype, qty, price, cid, reduce_only, tif="GTC"):
    if symbol is None or side is None or otype is None or qty is None:
        raise EngineError("BAD_PARAM", "symbol/side/type/qty required")
    if otype == "LIMIT" and price is None:
        raise EngineError("BAD_PARAM", "price required for LIMIT")
    return {"symbol": symbol, "side": side, "type": otype, "qty": to_float(qty, "qty"),
            "price": None if price is None else to_float(price, "price"), "cid": None if cid in (None, "") else str(cid),
            "reduce_only": bool(reduce_only), "tif": tif}


class Dialect:
    NAME = "base"
    ROUTES = ()
    DEFAULT_CREDS = {"api_key": "key", "secret": "secret", "passphrase": "pass"}
    RECV_WINDOW_MS = 5000
    SYMBOLS = {"BTCUSDT": "BTCUSDT", "ETHUSDT": "ETHUSDT"}          # 规范名 → 方言名

    def __init__(self, creds=None):
        self.creds = dict(self.DEFAULT_CREDS, **(creds or {}))
        self.prefix = common_prefix([r[1] for r in self.ROUTES])
        self._routes = {(m, p): (op, auth) for m, p, op, auth in self.ROUTES}
        self.to_canon = {v: k for k, v in self.SYMBOLS.items()}

    def route(self, req):
        """→ (op, 需鉴权) 或 None。"""
        return self._routes.get((req.method, req.path))

    def owns(self, path):
        return path.startswith(self.prefix)

    def symbol_in(self, name):
        """方言名 → 规范名；不认识的原样返回（engine 会报 SYMBOL_UNKNOWN）。"""
        if name is None:
            raise EngineError("BAD_PARAM", "symbol required")
        return self.to_canon.get(name, name)

    def symbol_out(self, canon):
        return self.SYMBOLS.get(canon, canon)

    def handle(self, op, req, ctx):
        try:
            return getattr(self, "h_" + op)(req, ctx)
        except EngineError as e:
            return self.error(e.kind, e.msg, ctx)

    def auth(self, req, ctx):               # pragma: no cover  子类实现
        raise NotImplementedError

    def error(self, kind, msg, ctx):        # pragma: no cover  子类实现
        raise NotImplementedError

    def rate_limit_headers(self, retry_after_ms, ctx):
        return {"Retry-After": str(max(1, -(-retry_after_ms // 1000)))}

    @staticmethod
    def ts_window_ok(ts_ms, now_ms, recv_window_ms, ahead_ms=1000):
        """统一的时间窗判定：now − recv_window ≤ ts < now + ahead。"""
        return now_ms - recv_window_ms <= ts_ms < now_ms + ahead_ms

    @staticmethod
    def f(x, places=8):
        return common.fnum(x, places)
