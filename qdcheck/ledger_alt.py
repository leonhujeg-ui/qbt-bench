"""账本的第二份独立写法：按成交流从头重算，不复用 ledger 的任何状态或计算函数，只用于逐根对拍。

刻意换数据结构与算法：
- 持仓用“批次”列表 [数量, 价格] 表示；平均成本法下的减仓 = 按比例缩小每个批次（pro-rata），
  均价 = sum(批次数量*价格) / sum(批次数量)（fsum）；浮动盈亏 = 逐批次 sum(数量*(收盘-价格))；
- 现金、已实现、手续费、资金费、保险补偿用 Shewchuk 精确累加器（逐笔无舍入误差累积）；
- 逐仓保证金直接取闭式 sum(批次名义额)/杠杆，不做增量；
- 强平合法性、漏强平、强平后余额在这里独立重判。
记录格式与 ledger 相同（ledger.normalize_record 只做格式校验，这里另写一份）。
"""

import math

_SIDE = {"buy": 1, "sell": -1, "none": 0, 1: 1, -1: -1, 0: 0}


class ExactSum:
    """Shewchuk 部分和：add 无舍入误差，value 返回精确和的正确舍入。"""

    __slots__ = ("partials",)

    def __init__(self, x=0.0):
        self.partials = []
        if x:
            self.add(x)

    def add(self, x):
        partials, i = self.partials, 0
        for y in partials:
            if abs(x) < abs(y):
                x, y = y, x
            hi = x + y
            lo = y - (hi - x)
            if lo:
                partials[i] = lo
                i += 1
            x = hi
        partials[i:] = [x]

    def value(self):
        return math.fsum(self.partials)


def _rec(r):
    side = r.get("side", "none")
    return (r["bar"], r.get("kind", "trade"), _SIDE[side], r.get("qty", 0), r.get("price"),
            r.get("fee", 0.0), r.get("funding", 0.0))


def _is_zero(x, ref):
    return x == 0 or abs(x) <= 1e-12 * max(1.0, abs(ref))


class _Book:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lots, self.direction, self.size = [], 0, 0          # 批次、方向、总数量（与方向同号的绝对值）
        self.cash = ExactSum(float(cfg["initial_cash"]))
        self.realized, self.fees, self.funding, self.insurance = ExactSum(), ExactSum(), ExactSum(), ExactSum()
        self.fills = self.liquidations = 0
        self.issues = []

    def cost(self):
        return math.fsum(q * p for q, p in self.lots)

    def avg(self):
        return self.cost() / math.fsum(q for q, _p in self.lots) if self.lots else 0.0

    def signed_qty(self):
        return self.direction * self.size

    def unrealized(self, price):
        return self.direction * math.fsum(q * (price - p) for q, p in self.lots)

    def margin(self):
        return self.cost() / self.cfg["leverage"] if self.cfg["margin_mode"] == "isolated" else 0.0

    def wallet(self):
        w = ExactSum(float(self.cfg["initial_cash"]))
        for acc, sign in ((self.realized, 1), (self.fees, -1), (self.funding, -1)):
            w.add(sign * acc.value())
        return w.value()

    def eff_margin(self):
        return min(self.margin(), self.wallet())

    def pos_equity(self, price):
        if self.cfg["margin_mode"] == "isolated":
            return self.eff_margin() + self.unrealized(price)
        return self.cash.value() + self.signed_qty() * price

    def trade(self, side, q, p):
        self.cash.add(-side * q * p)
        if self.size == 0 or side == self.direction:
            self.lots.append([q, p])
            self.direction, self.size = side, self.size + q
            return
        if _is_zero(q - self.size, self.size):
            closing = self.size
        else:
            closing = min(q, self.size)
        avg = self.avg()
        self.realized.add(self.direction * closing * (p - avg))
        remaining = self.size - closing
        if _is_zero(remaining, self.size):
            leftover = q - closing
            self.lots, self.size = [], 0
            if not _is_zero(leftover, q):
                self.lots, self.direction, self.size = [[leftover, p]], side, leftover
            else:
                self.direction = 0
            return
        factor = remaining / self.size
        self.lots = [[lq * factor, lp] for lq, lp in self.lots]
        self.size = remaining

    def settle(self, p):
        if self.size:
            self.realized.add(self.unrealized(p))
            self.lots = [[self.size, p]]

    def liquidate(self, bar, side, q, p, fee):
        mode, mmr = self.cfg["margin_mode"], self.cfg["mmr"]
        scale = max(1.0, abs(self.cfg["initial_cash"]), abs(self.cash.value()), abs(self.signed_qty() * p))
        if mode != "none":
            pe, mm = self.pos_equity(p), mmr * self.size * p
            if pe - mm > self.cfg["rel_tol"] * max(scale, abs(pe), abs(mm)):
                self.issues.append((bar, "premature_liquidation"))
        if self.size == 0 or side != -self.direction or not _is_zero(q - self.size, self.size):
            self.issues.append((bar, "liquidation_not_full"))
        margin, pnl = self.eff_margin(), self.unrealized(p)
        self.trade(side, q, p)
        self.fees.add(fee)
        self.cash.add(-fee)
        if mode == "isolated":
            gap = margin + pnl - fee
            if gap < 0:
                self.insurance.add(-gap)
                self.realized.add(-gap)
                self.cash.add(-gap)
            if self.cash.value() < -self.cfg["rel_tol"] * scale:
                self.issues.append((bar, "negative_balance_after_liquidation"))


def _liq_price(book, cfg):
    q = book.signed_qty()
    if q == 0 or cfg["margin_mode"] == "none":
        return None
    mmr = cfg["mmr"]
    if cfg["margin_mode"] == "isolated":
        # 解 margin + q*(P - avg) = mmr*|q|*P
        price = (q * book.avg() - book.eff_margin()) / (q - mmr * abs(q))
    else:
        price = -book.cash.value() / (q - mmr * abs(q))
    return price if price > cfg["rel_tol"] * book.avg() else None


def _row(book, cfg, bar, close):
    q, avg = book.signed_qty(), book.avg()
    mv = q * close
    cash = book.cash.value()
    mode = cfg["margin_mode"]
    return {"bar": bar, "close": close, "cash": cash, "qty": q, "avg_price": avg,
            "unrealized": book.unrealized(close), "realized": book.realized.value(), "fees": book.fees.value(),
            "funding": book.funding.value(), "insurance": book.insurance.value(), "equity": cash + mv,
            "market_value": mv, "margin_used": book.margin(), "maint_margin": cfg["mmr"] * abs(q) * close,
            "position_equity": None if mode == "none" else book.pos_equity(close),
            "liq_price": _liq_price(book, cfg), "fills": book.fills, "liquidations": book.liquidations}


def run(records, closes, initial_cash=100_000.0, margin_mode="none", leverage=1.0, mmr=0.005, rel_tol=1e-9):
    """返回 {"rows": [...], "issues": [(bar, check), ...]}。"""
    cfg = {"initial_cash": initial_cash, "margin_mode": margin_mode, "leverage": float(leverage), "mmr": mmr,
           "rel_tol": rel_tol}
    book = _Book(cfg)
    by_bar = {}
    for r in records:
        by_bar.setdefault(r["bar"], []).append(_rec(r))
    rows = []
    for t, close in enumerate(closes):
        for _bar, kind, side, q, p, fee, funding in by_bar.get(t, ()):
            book.funding.add(funding)
            book.cash.add(-funding)
            if kind == "trade":
                book.trade(side, q, p)
                book.fills += 1
            elif kind == "liquidation":
                book.liquidate(t, side, q, p, fee)
                book.fills += 1
                book.liquidations += 1
                continue
            elif kind == "settlement":
                book.settle(p)
            book.fees.add(fee)
            book.cash.add(-fee)
        row = _row(book, cfg, t, close)
        if margin_mode != "none" and q_nonzero(row) and row["position_equity"] < row["maint_margin"] - \
                rel_tol * max(1.0, abs(initial_cash), abs(row["cash"]), abs(row["market_value"])):
            book.issues.append((t, "missed_liquidation"))
        rows.append(row)
    return {"rows": rows, "issues": book.issues}


def q_nonzero(row):
    return row["qty"] != 0
