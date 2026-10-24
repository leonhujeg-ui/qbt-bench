"""撮合与账户：内存订单簿（只有本账户的挂单）+ 行情源推价撮合，限价单价格时间优先、按每 tick 流动性部分成交；
净持仓（单向模式）、均价、已实现盈亏、maker / taker 手续费、每 8 小时资金费、维持保证金与强平。状态写穿 Store。

规范模型（五个方言都翻译到这里；字段名 = 方言无关的“真相”，判据只看这里）::

    order    = {id, cid, symbol, side BUY|SELL, type MARKET|LIMIT, qty, price|None, reduce_only, tif,
                status NEW|PARTIALLY_FILLED|FILLED|CANCELED|REJECTED, filled, avg_price, fee, realized,
                created, updated, reason}
    fill     = {id, order_id, cid, symbol, side, price, qty, fee, maker, realized, ts, visible_at}
    position = {symbol, qty(带符号), entry, leverage, margin_mode cross|isolated, realized, updated}
    account  = {wallet, upl, equity, position_margin, order_margin, available}

撮合口径（没有对手盘，用“最新价 + 每 tick 流动性”代替整本簿）：
- 市价单：立刻以最新价全部成交（taker）；
- 限价单：下单时若已穿过最新价（买价 ≥ 最新价 / 卖价 ≤ 最新价）→ 以最新价 taker 成交，最多吃掉本 tick 流动性，剩余挂着；
  之后每个 tick 对穿价的挂单按价格优先、时间优先依次以**挂单价** maker 成交，本 tick 流动性用完即止（部分成交由此而来）；
- 只减仓：下单时没有反向持仓 → 拒绝；成交量不超过当时的反向持仓，持仓先被别的单平掉 → 剩余自动撤销。
保证金：起始保证金 = 名义 / 杠杆（持仓与挂单都占），可用 = 钱包 + 全仓未实现 − 持仓占用 − 挂单占用；下单占用 > 可用 → 拒绝。
强平（简化，REPORT 列为“已知简化”）：维持保证金率 mmr 固定 0.5%；逐仓：起始保证金 + 未实现 ≤ 维持保证金 → 以标记价平掉；
全仓：钱包 + 全部未实现 ≤ 全部维持保证金 → 全部平掉；强平按 taker 费。强平价由同一公式反解（见 liq_price）。
资金费：时钟跨过 8 小时边界（UTC 0/8/16 点）时按 pos · mark · rate 结算，多头付（rate > 0）。
"""
import threading

from qdexsim import common, store as store_mod

OPEN = ("NEW", "PARTIALLY_FILLED")
DEFAULT_SYMBOLS = {
    "BTCUSDT": {"symbol": "BTCUSDT", "base": "BTC", "quote": "USDT", "tick": 0.1, "step": 0.001, "min_qty": 0.001,
                "min_notional": 5.0, "max_leverage": 125, "status": "TRADING", "ct_val": 0.01, "price_places": 1,
                "qty_places": 3},
    "ETHUSDT": {"symbol": "ETHUSDT", "base": "ETH", "quote": "USDT", "tick": 0.01, "step": 0.01, "min_qty": 0.01,
                "min_notional": 5.0, "max_leverage": 100, "status": "TRADING", "ct_val": 0.1, "price_places": 2,
                "qty_places": 2},
}
DEFAULT_CONFIG = {"wallet": 10000.0, "maker_fee": 0.0002, "taker_fee": 0.0005, "funding_rate": 0.0001, "mmr": 0.005,
                  "leverage": 10, "margin_mode": "cross", "start": {"BTCUSDT": 60000.0, "ETHUSDT": 3000.0}}


class EngineError(Exception):
    """kind 是方言无关的错误种类；各方言把它翻成自己的错误码与报文形状。"""

    def __init__(self, kind, msg=""):
        super().__init__(f"{kind}: {msg}")
        self.kind, self.msg = kind, msg


class Engine:
    def __init__(self, store=None, clock=None, config=None, symbols=None):
        self.store = store or store_mod.Store(":memory:")
        self.clock = clock or common.now_ms
        self.cfg = dict(DEFAULT_CONFIG, **(config or {}))
        self.lock = threading.RLock()
        self.symbols = {s: dict(v) for s, v in (symbols or DEFAULT_SYMBOLS).items()}
        self.orders, self.by_cid, self.fills, self.positions = {}, {}, [], {}
        self.mark = dict(self.cfg["start"])
        self.wallet = float(self.cfg["wallet"])
        self.next_id = 1000
        self.last_funding = None
        self._load()

    # ---- 持久化 ----------------------------------------------------------------------
    def _load(self):
        st = self.store
        for o in st.load_orders():
            self.orders[o["id"]] = o
            if o["cid"]:
                self.by_cid[o["cid"]] = o["id"]
        self.fills = st.load_fills()
        for p in st.load_positions():
            self.positions[p["symbol"]] = p
        meta = st.get_kv("meta")
        if meta:
            self.wallet, self.next_id, self.last_funding = meta["wallet"], meta["next_id"], meta.get("last_funding")
            self.mark.update(meta.get("mark", {}))
            for s, spec in meta.get("symbols", {}).items():
                self.symbols[s] = spec
        else:
            self._save_meta()

    def _save_meta(self):
        self.store.set_kv("meta", {"wallet": self.wallet, "next_id": self.next_id, "last_funding": self.last_funding,
                                   "mark": self.mark, "symbols": self.symbols})

    def _event(self, kind, **doc):
        return self.store.add_event(self.clock(), kind, doc)

    def _nid(self):
        self.next_id += 1
        return self.next_id

    # ---- 查询 ------------------------------------------------------------------------
    def spec(self, symbol):
        s = self.symbols.get(symbol)
        if s is None:
            raise EngineError("SYMBOL_UNKNOWN", symbol)
        return s

    def get_order(self, order_id=None, cid=None):
        with self.lock:
            if order_id is not None:
                o = self.orders.get(order_id)
            elif cid is not None:
                o = self.orders.get(self.by_cid.get(cid))
            else:
                raise EngineError("BAD_PARAM", "orderId or clientOrderId required")
            if o is None:
                raise EngineError("ORDER_NOT_FOUND", str(order_id or cid))
            return dict(o)

    def open_orders(self, symbol=None):
        with self.lock:
            return [dict(o) for o in self.orders.values() if o["status"] in OPEN and (symbol in (None, o["symbol"]))]

    def all_orders(self, symbol=None):
        with self.lock:
            return [dict(o) for o in self.orders.values() if symbol in (None, o["symbol"])]

    def user_trades(self, symbol=None, visible_before=None):
        """成交明细；visible_before 给出时只返回 visible_at ≤ 它的（“成交推迟出现”混沌的最终一致语义）。"""
        with self.lock:
            return [dict(f) for f in self.fills if symbol in (None, f["symbol"])
                    and (visible_before is None or f["visible_at"] <= visible_before)]

    def position(self, symbol):
        with self.lock:
            p = self.positions.get(symbol) or self._new_position(symbol)
            return self._position_view(p)

    def all_positions(self):
        with self.lock:
            return [self._position_view(p) for p in self.positions.values()]

    def _new_position(self, symbol):
        return {"symbol": symbol, "qty": 0.0, "entry": 0.0, "leverage": self.cfg["leverage"],
                "margin_mode": self.cfg["margin_mode"], "realized": 0.0, "updated": self.clock()}

    def _position_view(self, p):
        mark = self.mark.get(p["symbol"], p["entry"])
        v = dict(p, mark=mark, upl=p["qty"] * (mark - p["entry"]), notional=abs(p["qty"]) * mark)
        v["margin"] = v["notional"] / p["leverage"] if p["qty"] else 0.0
        v["liq_price"] = self.liq_price(p)
        return v

    def liq_price(self, p):
        """逐仓：多 entry·(1 − 1/lev + mmr)，空 entry·(1 + 1/lev − mmr)；全仓：解 wallet + qty·(p − entry) = mmr·|qty|·p。"""
        q, e, mmr = p["qty"], p["entry"], self.cfg["mmr"]
        if q == 0:
            return 0.0
        if p["margin_mode"] == "isolated":
            return max(0.0, e * (1 - 1 / p["leverage"] + mmr) if q > 0 else e * (1 + 1 / p["leverage"] - mmr))
        if q > 0:
            return max(0.0, (q * e - self.wallet) / (q * (1 - mmr)))
        return max(0.0, (self.wallet + abs(q) * e) / (abs(q) * (1 + mmr)))

    def account(self):
        with self.lock:
            upl = sum(p["qty"] * (self.mark.get(p["symbol"], p["entry"]) - p["entry"]) for p in self.positions.values())
            pos_margin = sum(abs(p["qty"]) * self.mark.get(p["symbol"], p["entry"]) / p["leverage"]
                             for p in self.positions.values())
            ord_margin = sum(self._order_margin(o) for o in self.orders.values() if o["status"] in OPEN)
            return {"wallet": self.wallet, "upl": upl, "equity": self.wallet + upl, "position_margin": pos_margin,
                    "order_margin": ord_margin, "available": self.wallet + upl - pos_margin - ord_margin}

    def _order_margin(self, o):
        if o["reduce_only"]:
            return 0.0
        px = o["price"] if o["price"] else self.mark.get(o["symbol"], 0.0)
        lev = (self.positions.get(o["symbol"]) or {}).get("leverage", self.cfg["leverage"])
        return (o["qty"] - o["filled"]) * px / lev

    # ---- 账户设置 --------------------------------------------------------------------
    def set_leverage(self, symbol, leverage):
        spec = self.spec(symbol)
        try:
            lev = int(leverage)
        except (TypeError, ValueError):
            raise EngineError("BAD_LEVERAGE", str(leverage)) from None
        if not 1 <= lev <= spec["max_leverage"]:
            raise EngineError("BAD_LEVERAGE", str(leverage))
        with self.lock, self.store.transaction():
            p = self.positions.setdefault(symbol, self._new_position(symbol))
            p["leverage"], p["updated"] = lev, self.clock()
            self.store.save_position(p)
            self._event("set_leverage", symbol=symbol, leverage=lev)
            return lev

    def set_margin_mode(self, symbol, mode):
        self.spec(symbol)
        if mode not in ("cross", "isolated"):
            raise EngineError("BAD_PARAM", f"margin mode {mode}")
        with self.lock, self.store.transaction():
            p = self.positions.setdefault(symbol, self._new_position(symbol))
            if p["margin_mode"] != mode and (p["qty"] != 0 or self.open_orders(symbol)):
                raise EngineError("MARGIN_MODE_BUSY", symbol)
            p["margin_mode"], p["updated"] = mode, self.clock()
            self.store.save_position(p)
            self._event("set_margin_mode", symbol=symbol, mode=mode)
            return mode

    def set_symbol(self, symbol, **changes):
        """混沌：下架 / 暂停 / 改精度（status TRADING|SUSPENDED|DELISTED, tick, step, min_qty, min_notional）。"""
        with self.lock, self.store.transaction():
            spec = self.spec(symbol)
            spec.update(changes)
            if spec["status"] != "TRADING":
                for o in self.open_orders(symbol):
                    self._cancel_order(self.orders[o["id"]], "SYMBOL_" + spec["status"])
            self._save_meta()
            self._event("symbol_change", symbol=symbol, changes=changes)
            return dict(spec)

    # ---- 下单 / 撤单 -----------------------------------------------------------------
    def place(self, symbol, side, otype, qty, price=None, cid=None, reduce_only=False, tif="GTC", origin=None):
        with self.lock:
            spec = self._validate(symbol, side, otype, qty, price, cid, reduce_only)
            qty = float(qty)
            pos = self.positions.get(symbol) or self._new_position(symbol)
            if reduce_only:
                if pos["qty"] == 0 or (pos["qty"] > 0) == (side == "BUY"):
                    raise EngineError("REDUCE_ONLY", "no position to reduce")
                qty = min(qty, abs(pos["qty"]))
            px = None if otype == "MARKET" else common.round_step(price, spec["tick"])
            now = self.clock()
            o = {"id": self._nid(), "cid": cid, "symbol": symbol, "side": side, "type": otype, "qty": qty, "price": px,
                 "reduce_only": bool(reduce_only), "tif": tif, "status": "NEW", "filled": 0.0, "avg_price": 0.0,
                 "fee": 0.0, "realized": 0.0, "created": now, "updated": now, "reason": None, "origin": origin}
            if not reduce_only and self._order_margin(o) > self.account()["available"] + 1e-9:
                self.next_id -= 1
                raise EngineError("INSUFFICIENT", "available balance")
            with self.store.transaction():
                self.store.save_order(o)                 # UNIQUE(cid) 在库里再挡一次
                self.orders[o["id"]] = o
                if cid:
                    self.by_cid[cid] = o["id"]
                self._event("order_new", order=dict(o))
                mark, liq = self.mark[symbol], spec.get("liquidity", 0.05)
                if otype == "MARKET":
                    self._fill(o, mark, o["qty"], False, now)
                elif self._crosses(o, mark):
                    self._fill(o, mark, min(o["qty"], liq), False, now)
                self._save_meta()
            return dict(o)

    def _validate(self, symbol, side, otype, qty, price, cid, reduce_only):
        spec = self.spec(symbol)
        if spec["status"] != "TRADING":
            raise EngineError("SYMBOL_" + spec["status"], symbol)
        if side not in ("BUY", "SELL") or otype not in ("MARKET", "LIMIT"):
            raise EngineError("BAD_PARAM", f"side={side} type={otype}")
        try:
            qty = float(qty)
            price = None if otype == "MARKET" else float(price)
        except (TypeError, ValueError):
            raise EngineError("BAD_PARAM", "qty/price not numeric") from None
        if qty <= 0 or qty < spec["min_qty"] - 1e-12 or not common.is_multiple(qty, spec["step"]):
            raise EngineError("BAD_QTY", f"qty {qty} step {spec['step']} min {spec['min_qty']}")
        if otype == "LIMIT" and (price <= 0 or not common.is_multiple(price, spec["tick"])):
            raise EngineError("BAD_PRICE", f"price {price} tick {spec['tick']}")
        ref = price if otype == "LIMIT" else self.mark[symbol]
        if not reduce_only and qty * ref < spec["min_notional"] - 1e-9:
            raise EngineError("MIN_NOTIONAL", f"{qty * ref} < {spec['min_notional']}")
        if cid is not None and cid in self.by_cid:
            raise EngineError("DUP_CID", cid)
        return spec

    @staticmethod
    def _crosses(o, mark):
        return o["price"] is not None and ((o["side"] == "BUY" and o["price"] >= mark) or
                                           (o["side"] == "SELL" and o["price"] <= mark))

    def cancel(self, order_id=None, cid=None):
        with self.lock:
            o = self.orders.get(order_id) if order_id is not None else self.orders.get(self.by_cid.get(cid))
            if o is None:
                raise EngineError("ORDER_NOT_FOUND", str(order_id or cid))
            if o["status"] not in OPEN:
                raise EngineError("ORDER_NOT_OPEN", o["status"])
            with self.store.transaction():
                self._cancel_order(o, "USER")
                self._save_meta()
            return dict(o)

    def _cancel_order(self, o, reason):
        o["status"], o["reason"], o["updated"] = "CANCELED", reason, self.clock()
        self.store.save_order(o)
        self._event("order_cancel", order_id=o["id"], cid=o["cid"], reason=reason, filled=o["filled"])

    def cancel_all(self, symbol=None):
        with self.lock, self.store.transaction():
            out = [self._cancel_order(self.orders[o["id"]], "USER") or dict(self.orders[o["id"]])
                   for o in self.open_orders(symbol)]
            self._save_meta()
            return out

    def fill_remaining(self, order_id, maker=True):
        """混沌“撤单竞态”：撤单到达前把这张单剩余部分按挂单价成交掉。"""
        with self.lock:
            o = self.orders.get(order_id)
            if o is None or o["status"] not in OPEN:
                return None
            with self.store.transaction():
                self._fill(o, o["price"] or self.mark[o["symbol"]], o["qty"] - o["filled"], maker, self.clock())
                self._save_meta()
            return dict(o)

    # ---- 成交 ------------------------------------------------------------------------
    def _fill(self, o, price, qty, maker, ts, visible_delay_ms=0, reason="TRADE"):
        pos = self.positions.setdefault(o["symbol"], self._new_position(o["symbol"]))
        if o["reduce_only"]:
            qty = min(qty, abs(pos["qty"]))
            if qty <= 0:
                self._cancel_order(o, "REDUCE_ONLY_NOTHING_LEFT")
                return None
        qty = round(min(qty, o["qty"] - o["filled"]), 10)         # 去掉 0.05 − 0.04 这类浮点尾巴
        if qty <= 0:
            return None
        rate = self.cfg["maker_fee"] if maker else self.cfg["taker_fee"]
        fee = price * qty * rate
        realized = self._apply_position(pos, o["side"], price, qty, ts)
        self.wallet += realized - fee
        o["avg_price"] = (o["avg_price"] * o["filled"] + price * qty) / (o["filled"] + qty)
        o["filled"] = round(o["filled"] + qty, 10)
        o["fee"] += fee
        o["realized"] += realized
        o["status"] = "FILLED" if o["filled"] >= o["qty"] - 1e-12 else "PARTIALLY_FILLED"
        o["updated"] = ts
        f = {"id": self._nid(), "order_id": o["id"], "cid": o["cid"], "symbol": o["symbol"], "side": o["side"],
             "price": price, "qty": qty, "fee": fee, "maker": maker, "realized": realized, "ts": ts,
             "visible_at": ts + visible_delay_ms, "reason": reason}
        self.fills.append(f)
        self.store.save_fill(f)
        self.store.save_order(o)
        self.store.save_position(pos)
        self._event("order_fill", fill=dict(f), status=o["status"])
        return f

    @staticmethod
    def _apply_position(pos, side, price, qty, ts):
        signed = qty if side == "BUY" else -qty
        old = pos["qty"]
        realized = 0.0
        if old == 0 or (old > 0) == (signed > 0):
            pos["entry"] = (abs(old) * pos["entry"] + qty * price) / (abs(old) + qty)
            pos["qty"] = round(old + signed, 10)
        else:
            closing = min(abs(old), qty)
            realized = closing * (price - pos["entry"]) * (1 if old > 0 else -1)
            pos["qty"] = round(old + signed, 10)
            if abs(pos["qty"]) < 1e-12:
                pos["qty"], pos["entry"] = 0.0, 0.0
            elif (pos["qty"] > 0) != (old > 0):
                pos["entry"] = price
        pos["realized"] += realized
        pos["updated"] = ts
        return realized

    # ---- 行情驱动 --------------------------------------------------------------------
    def on_price(self, symbol, price, ts=None, liquidity=None, visible_delay_ms=0):
        """一个 tick：更新标记价 → 撮合穿价挂单（价格 / 时间优先，吃完流动性为止）→ 强平检查 → 资金费。返回本 tick 成交。"""
        with self.lock:
            ts = self.clock() if ts is None else ts
            spec = self.spec(symbol)
            self.mark[symbol] = price
            liq = spec.get("liquidity", 0.05) if liquidity is None else liquidity
            out = []
            with self.store.transaction():
                for o in self._crossing(symbol, price):
                    if liq <= 1e-12:
                        break
                    f = self._fill(o, o["price"], min(liq, o["qty"] - o["filled"]), True, ts, visible_delay_ms)
                    if f:
                        liq -= f["qty"]
                        out.append(f)
                self._check_liquidation(ts)
                self._funding(ts)
                self._save_meta()
            return out

    def _crossing(self, symbol, price):
        buys = [o for o in self.orders.values() if o["symbol"] == symbol and o["status"] in OPEN and o["side"] == "BUY"
                and o["price"] >= price]
        sells = [o for o in self.orders.values() if o["symbol"] == symbol and o["status"] in OPEN and o["side"] == "SELL"
                and o["price"] <= price]
        buys.sort(key=lambda o: (-o["price"], o["created"], o["id"]))
        sells.sort(key=lambda o: (o["price"], o["created"], o["id"]))
        return buys + sells

    def _check_liquidation(self, ts):
        mmr = self.cfg["mmr"]
        total_upl = total_mm = 0.0
        for p in list(self.positions.values()):
            if p["qty"] == 0:
                continue
            mark = self.mark[p["symbol"]]
            upl, notional = p["qty"] * (mark - p["entry"]), abs(p["qty"]) * mark
            if p["margin_mode"] == "isolated":
                if abs(p["qty"]) * p["entry"] / p["leverage"] + upl <= mmr * notional:
                    self._liquidate(p, mark, ts)
            else:
                total_upl += upl
                total_mm += mmr * notional
        if total_mm and self.wallet + total_upl <= total_mm:
            for p in list(self.positions.values()):
                if p["qty"] != 0 and p["margin_mode"] == "cross":
                    self._liquidate(p, self.mark[p["symbol"]], ts)

    def _liquidate(self, p, mark, ts):
        for o in self.open_orders(p["symbol"]):
            self._cancel_order(self.orders[o["id"]], "LIQUIDATION")
        side = "SELL" if p["qty"] > 0 else "BUY"
        o = {"id": self._nid(), "cid": None, "symbol": p["symbol"], "side": side, "type": "MARKET", "qty": abs(p["qty"]),
             "price": None, "reduce_only": True, "tif": "IOC", "status": "NEW", "filled": 0.0, "avg_price": 0.0,
             "fee": 0.0, "realized": 0.0, "created": ts, "updated": ts, "reason": "LIQUIDATION", "origin": "system"}
        self.orders[o["id"]] = o
        self.store.save_order(o)
        self._fill(o, mark, o["qty"], False, ts, reason="LIQUIDATION")
        self.wallet = max(self.wallet, 0.0)
        self._event("liquidation", symbol=p["symbol"], mark=mark, order_id=o["id"])

    def _funding(self, ts):
        slot = ts // common.EIGHT_HOURS_MS
        if self.last_funding is None:
            self.last_funding = slot
            return
        if slot <= self.last_funding:
            return
        self.last_funding = slot
        rate = self.cfg["funding_rate"]
        for p in self.positions.values():
            if p["qty"] != 0:
                pay = p["qty"] * self.mark[p["symbol"]] * rate
                self.wallet -= pay
                self._event("funding", symbol=p["symbol"], payment=pay, rate=rate, slot=slot)

    # ---- 判据用的真相快照 --------------------------------------------------------------
    def snapshot(self):
        with self.lock:
            return {"orders": self.all_orders(), "fills": self.user_trades(), "positions": self.all_positions(),
                    "account": self.account(), "mark": dict(self.mark), "symbols": {k: dict(v) for k, v in self.symbols.items()},
                    "counts": self.store.counts(), "time": self.clock()}
