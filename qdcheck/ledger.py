"""参考账本：吃“成交流 + 每根收盘价”，逐根给出现金 / 持仓 / 均价 / 浮盈 / 已实现 / 手续费 / 资金费 / 权益，
并逐根检查会计恒等式。单一标的；多头 / 空头 / 反手 / 部分平仓 / 多次加仓；逐仓（isolated，杠杆 1–20）
与全仓（cross）保证金下的保证金占用、维持保证金与强平线。

成交流记录（dict，按 bar 非降序；同一 bar 内按给出的顺序处理，处理完该 bar 的全部记录后以收盘价盯市）::

    {"bar": int, "kind": "trade" | "funding" | "settlement" | "liquidation",
     "side": "buy" | "sell" | "none"（也接受 +1 / -1 / 0）, "qty": 数量 >= 0, "price": 价格 > 0,
     "fee": 手续费（可为负 = 返佣）, "funding": 资金费（正 = 账户支付）}

- trade：按 side/qty/price 成交；平均成本法（加仓更新均价，减仓不改均价，反手时剩余部分以成交价开新仓）。
- funding：只记资金费（qty = 0）。
- settlement：跨日结算——以 price 把浮盈转入已实现，均价重置为 price（现金与权益不变）。
- liquidation：强平成交，必须一次平掉全部持仓。逐仓下该仓位最多亏掉“有效保证金” = min(逐仓保证金, 钱包余额)
  （穿仓部分由保险基金承担，计入已实现与现金），因此强平后账户不欠款（现金 >= 0）。
- 记账口径（“现金法”）：现金 -= side*qty*price + fee + funding；持仓盯市 = qty*close；钱包 = 现金 + qty*均价；
  逐仓保证金 = 开 / 加仓名义额 / 杠杆，减仓按比例释放，settlement 时按结算价重算（恒等于 |qty|*均价/杠杆，作为一致性判据）。

每根 bar 检查（违背记入 violations）::

    I1  权益 == 初始资金 + 已实现 + 浮动 - 手续费 - 资金费
    I2  权益 == 现金 + 持仓盯市
        （这里的“权益”是第三条独立路径：逐根累加 持仓*价格变化 + 当根成交在收盘价上的盈亏 - 费用 + 保险补偿）
    I3  持仓 == 0 时：均价 == 0、浮动 == 0、现金 == 权益、保证金占用 == 0
    I4  逐仓保证金 == |qty|*均价/杠杆；未被强平的持仓在收盘时 仓位权益 >= 维持保证金（否则 missed_liquidation）
    I5  强平记录：强平前 仓位权益 <= 维持保证金（否则 premature_liquidation）、必须全平；强平后持仓 0、均价 0、
        现金 == 权益，逐仓下现金 >= 0（否则 negative_balance_after_liquidation）
仓位权益：逐仓 = 有效保证金 + 浮动；全仓 = 账户权益。维持保证金 = mmr * |qty| * 价格。

别的系统怎么用::

    from qdcheck import ledger
    rep = ledger.check_engine(my_records, my_closes, my_engine_equity, initial_cash=1e5, margin_mode="cross",
                              leverage=3, mmr=0.05)            # 引擎报的权益 vs 参考账本，逐根
    res = ledger.run(records, closes, **config)                 # {"rows", "violations", "warnings", "counters"}
    ledger.check_vector(vec, {"equity": my_ledger_equity})      # 用 qdcheck/vectors/ledger_*.json 测别人的账本
浮点比较：|a-b| <= rel_tol * max(1, 各项的绝对值)，rel_tol 默认 1e-9；数量 |x| <= 1e-12*参考量 视为 0。
"""
from __future__ import annotations

import math

KINDS = ("trade", "funding", "settlement", "liquidation")
MODES = ("none", "isolated", "cross")
_SIDES = {"buy": 1, "sell": -1, "none": 0, 1: 1, -1: -1, 0: 0}
ROW_FIELDS = ("bar", "close", "cash", "qty", "avg_price", "unrealized", "realized", "fees", "funding", "insurance",
              "equity", "market_value", "margin_used", "maint_margin", "position_equity", "liq_price", "fills",
              "liquidations")
INT_FIELDS = ("bar", "fills", "liquidations")
QTY_EPS = 1e-12


class LedgerError(ValueError):
    """成交流或配置不合法。"""


def _finite(name, v, positive=False):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise LedgerError(f"{name} must be a finite number, got {v!r}")
    if positive and v <= 0:
        raise LedgerError(f"{name} must be > 0, got {v!r}")
    return v


def normalize_record(rec):
    """校验一条记录，返回 (bar, side, qty, price, fee, funding, kind)。"""
    if not isinstance(rec, dict):
        raise LedgerError("record must be a dict")
    kind = rec.get("kind", "trade")
    if kind not in KINDS:
        raise LedgerError(f"unknown record kind {kind!r}")
    bar = rec.get("bar")
    if not isinstance(bar, int) or isinstance(bar, bool) or bar < 0:
        raise LedgerError(f"bar must be a non-negative int, got {bar!r}")
    side = rec.get("side", "none")
    if isinstance(side, bool) or side not in _SIDES:
        raise LedgerError(f"side must be buy/sell/none or +1/-1/0, got {side!r}")
    side = _SIDES[side]
    qty = _finite("qty", rec.get("qty", 0))
    fee = _finite("fee", rec.get("fee", 0.0))
    funding = _finite("funding", rec.get("funding", 0.0))
    price = rec.get("price")
    if kind in ("trade", "liquidation"):
        if side == 0 or not qty > 0:
            raise LedgerError(f"{kind} record needs side buy/sell and qty > 0")
        price = _finite("price", price, positive=True)
    elif kind == "settlement":
        price = _finite("price", price, positive=True)
    if kind in ("funding", "settlement") and (side != 0 or qty != 0):
        raise LedgerError(f"{kind} record must have side none and qty 0")
    return bar, side, qty, price, fee, funding, kind


def check_config(initial_cash=100_000.0, margin_mode="none", leverage=1.0, mmr=0.005, rel_tol=1e-9):
    _finite("initial_cash", initial_cash, positive=True)
    if margin_mode not in MODES:
        raise LedgerError(f"margin_mode must be one of {MODES}")
    _finite("leverage", leverage, positive=True)
    if margin_mode == "isolated" and not 1 <= leverage <= 20:
        raise LedgerError("isolated leverage must be in [1, 20]")
    _finite("mmr", mmr)
    if not 0 <= mmr < 1:
        raise LedgerError("mmr must be in [0, 1)")
    _finite("rel_tol", rel_tol)
    return {"initial_cash": initial_cash, "margin_mode": margin_mode, "leverage": leverage, "mmr": mmr,
            "rel_tol": rel_tol}


def snap_qty(x, ref):
    """浮点数量的残差归零：|x| <= 1e-12 * max(1, |ref|) 视为 0（整数数量不受影响）。"""
    return 0 if x != 0 and abs(x) <= QTY_EPS * max(1.0, abs(ref)) else x


def _sign(x):
    return (x > 0) - (x < 0)


class Ledger:
    """增量账本：``apply(record)`` 处理当前 bar 的记录，``mark(bar, close)`` 收盘盯市并检查，返回该 bar 的行。"""

    def __init__(self, initial_cash=100_000.0, margin_mode="none", leverage=1.0, mmr=0.005, rel_tol=1e-9):
        cfg = check_config(initial_cash, margin_mode, leverage, mmr, rel_tol)
        self.cfg = cfg
        self.initial, self.mode, self.lev, self.mmr, self.rel_tol = (
            float(initial_cash), margin_mode, float(leverage), float(mmr), rel_tol)
        self.cash, self.qty, self.avg = float(initial_cash), 0, 0.0
        self.realized = self.fees = self.funding = self.insurance = self.margin = 0.0
        self.fills = self.liquidations = 0
        self.next_bar = 0
        self.violations, self.warnings = [], []
        self.counters = {k: 0 for k in ("open", "add", "reduce", "close", "reverse", "long", "short",
                                        "funding", "settlement", "liquidation", "bankrupt_capped")}
        self._run_equity, self._prev_close, self._prev_qty = float(initial_cash), None, 0
        self._bar_trade_flow, self._bar_costs = [], 0.0     # 当根成交 (带符号数量, 价格) 与费用，用于独立权益路径

    # ---- 工具 ---------------------------------------------------------------------
    def _tol(self, *terms):
        return self.rel_tol * max([1.0, abs(self.initial)] + [abs(t) for t in terms])

    def _flag(self, bar, check, lhs, rhs, detail=""):
        self.violations.append({"bar": bar, "check": check, "lhs": lhs, "rhs": rhs, "detail": detail})

    def wallet(self):
        """钱包余额 = 初始 + 已实现 - 手续费 - 资金费（含保险补偿）= 现金 + qty*均价。"""
        return self.cash + self.qty * self.avg

    def effective_margin(self):
        """逐仓有效保证金 = min(逐仓保证金, 钱包余额)：钱包被资金费 / 结算亏损侵蚀到低于逐仓保证金时，缺口算在仓位上。"""
        return min(self.margin, self.wallet())

    def position_equity(self, price):
        if self.mode == "isolated":
            return self.effective_margin() + self.qty * (price - self.avg)
        return self.cash + self.qty * price

    def maint_margin(self, price):
        return self.mmr * abs(self.qty) * price

    def liq_price(self):
        """仓位权益 == 维持保证金 的价格；无仓 / 无保证金模式 / 不可能触发（<= rel_tol*均价）时返回 None。"""
        q = self.qty
        if q == 0 or self.mode == "none":
            return None
        k = (1.0 - self.mmr) if q > 0 else (1.0 + self.mmr)
        num = (q * self.avg - self.effective_margin()) if self.mode == "isolated" else -self.cash
        p = num / (q * k)
        return p if p > self.rel_tol * self.avg else None       # 数学上为 0 的情形（逐仓 1 倍多头）只剩舍入噪声

    # ---- 成交 ---------------------------------------------------------------------
    def _trade(self, side, q, p):
        Q = self.qty
        if Q == 0 or _sign(Q) == side:
            self.counters["open" if Q == 0 else "add"] += 1
            self.counters["long" if side > 0 else "short"] += Q == 0
            new_abs = abs(Q) + q
            self.avg = (abs(Q) * self.avg + q * p) / new_abs
            self.qty = Q + side * q
            if self.mode == "isolated":
                self.margin += q * p / self.lev
        else:
            closing = min(q, abs(Q))
            if snap_qty(q - abs(Q), Q) == 0:
                closing = abs(Q)
            self.realized += closing * (p - self.avg) * _sign(Q)
            new_q = snap_qty(Q + side * q, Q)
            rest = q - closing
            if new_q == 0:
                self.counters["close"] += 1
                self.qty, self.avg, self.margin = 0, 0.0, 0.0
            elif _sign(new_q) == _sign(Q):
                self.counters["reduce"] += 1
                if self.mode == "isolated":
                    self.margin -= self.margin * closing / abs(Q)
                self.qty = new_q
            else:
                self.counters["reverse"] += 1
                self.counters["long" if side > 0 else "short"] += 1
                self.qty, self.avg = new_q, float(p)
                self.margin = rest * p / self.lev if self.mode == "isolated" else 0.0
        self.cash -= side * q * p
        self._bar_trade_flow.append((side * q, p))

    def _costs(self, fee, funding):
        self.fees += fee
        self.funding += funding
        self.cash -= fee + funding
        self._bar_costs += fee + funding

    def apply(self, rec):
        bar, side, q, p, fee, funding, kind = normalize_record(rec)
        if bar != self.next_bar:
            raise LedgerError(f"record for bar {bar} while the ledger is at bar {self.next_bar}")
        if kind == "trade":
            self._trade(side, q, p)
            self.fills += 1
        elif kind == "settlement":
            self.counters["settlement"] += 1
            if self.qty != 0:
                self.realized += self.qty * (p - self.avg)
                self.avg = float(p)
                if self.mode == "isolated":
                    self.margin = abs(self.qty) * p / self.lev      # 逐仓保证金按结算价重算
        elif kind == "funding":
            self.counters["funding"] += 1
        else:
            self._liquidate(bar, side, q, p, fee)
        self._costs(fee if kind != "liquidation" else 0.0, funding)

    def _liquidate(self, bar, side, q, p, fee):
        Q = self.qty
        if self.mode != "none":
            pe, mm = self.position_equity(p), self.maint_margin(p)
            if pe > mm + self._tol(pe, mm, self.cash, Q * p):
                self._flag(bar, "premature_liquidation", pe, mm, "position equity above maintenance margin")
        if Q == 0 or side != -_sign(Q) or snap_qty(q - abs(Q), Q) != 0:
            self._flag(bar, "liquidation_not_full", q, abs(Q), "liquidation must close the whole position")
        margin_before, pnl = self.effective_margin(), Q * (p - self.avg)
        self._trade(side, q, p)
        self.fills += 1
        self.liquidations += 1
        self.counters["liquidation"] += 1
        self._costs(fee, 0.0)
        if self.mode == "isolated":
            shortfall = -(margin_before + pnl - fee)
            if shortfall > 0:
                self.insurance += shortfall
                self.realized += shortfall
                self.cash += shortfall
                self._bar_costs -= shortfall
                self.counters["bankrupt_capped"] += 1
            if self.cash < -self._tol(self.cash):
                self._flag(bar, "negative_balance_after_liquidation", self.cash, 0.0)
        elif self.cash + self.qty * p < 0:
            self.warnings.append({"bar": bar, "check": "bankrupt", "equity": self.cash + self.qty * p})

    # ---- 收盘 ---------------------------------------------------------------------
    def _step_running(self, close):
        """第三条权益路径：上根持仓 * 价格变化 + 当根成交按收盘价的盈亏 - 当根费用 + 当根保险补偿。"""
        e = self._run_equity - self._bar_costs
        if self._prev_qty != 0:
            e += self._prev_qty * (close - self._prev_close)
        for sq, p in self._bar_trade_flow:
            e += sq * (close - p)
        self._run_equity, self._prev_close, self._prev_qty = e, close, self.qty
        self._bar_trade_flow, self._bar_costs = [], 0.0
        return e

    def mark(self, bar, close):
        if bar != self.next_bar:
            raise LedgerError(f"mark for bar {bar} while the ledger is at bar {self.next_bar}")
        _finite("close", close, positive=True)
        q, avg = self.qty, self.avg
        unreal = q * (close - avg)
        mv = q * close
        equity = self.cash + mv
        run_eq = self._step_running(close)
        decomposition = self.initial + self.realized + unreal - self.fees - self.funding
        tol = self._tol(self.cash, mv, self.realized, self.fees, self.funding, equity, unreal)
        if not abs(run_eq - decomposition) <= tol:
            self._flag(bar, "I1_equity_eq_initial_realized_unrealized_fees_funding", run_eq, decomposition)
        if not abs(run_eq - equity) <= tol:
            self._flag(bar, "I2_equity_eq_cash_plus_mark", run_eq, equity)
        if q == 0 and not (avg == 0 and unreal == 0 and self.margin == 0 and abs(self.cash - equity) <= tol):
            self._flag(bar, "I3_flat_state", [avg, unreal, self.margin], [0.0, 0.0, 0.0])
        pe = self.position_equity(close) if self.mode != "none" else None
        if pe is not None and q != 0 and pe < self.maint_margin(close) - tol:
            self._flag(bar, "I4_missed_liquidation", pe, self.maint_margin(close))
        if self.mode == "isolated" and not abs(self.margin - abs(q) * avg / self.lev) <= tol:
            self._flag(bar, "I4_margin_consistency", self.margin, abs(q) * avg / self.lev)
        self.next_bar += 1
        return {"bar": bar, "close": close, "cash": self.cash, "qty": q, "avg_price": avg, "unrealized": unreal,
                "realized": self.realized, "fees": self.fees, "funding": self.funding, "insurance": self.insurance,
                "equity": equity, "market_value": mv, "margin_used": self.margin,
                "maint_margin": self.maint_margin(close), "position_equity": pe, "liq_price": self.liq_price(),
                "fills": self.fills, "liquidations": self.liquidations}


def run(records, closes, **config):
    """一次性跑完：返回 {"rows", "violations", "warnings", "counters", "config"}。"""
    led = Ledger(**config)
    rows, i, n = [], 0, len(records)
    last_bar = -1
    for rec in records:
        b = rec.get("bar") if isinstance(rec, dict) else None
        if isinstance(b, int) and b < last_bar:
            raise LedgerError("records must be sorted by bar")
        last_bar = b if isinstance(b, int) else last_bar
    for t, close in enumerate(closes):
        while i < n and isinstance(records[i], dict) and records[i].get("bar") == t:
            led.apply(records[i])
            i += 1
        rows.append(led.mark(t, close))
    if i < n:
        raise LedgerError(f"record {i} (bar {records[i].get('bar') if isinstance(records[i], dict) else '?'}) "
                          f"is beyond the last close (n={len(closes)})")
    return {"rows": rows, "violations": led.violations, "warnings": led.warnings, "counters": led.counters,
            "config": led.cfg}


def _money_close(a, b, rel_tol, scale):
    return abs(a - b) <= rel_tol * max(abs(a), abs(b), scale)


def check_engine(records, closes, engine_equity, rel_tol=1e-9, **config):
    """别人的引擎：把它的成交流 + 收盘价喂给参考账本，逐根比较引擎自己报的权益。

    返回 {"ok", "bars", "first_mismatch": {bar, expected, actual} | None, "violations", "max_rel_err"}。
    容差：|a-b| <= rel_tol * max(|a|, |b|, 初始资金)。"""
    res = run(records, closes, rel_tol=rel_tol, **config)
    rows = res["rows"]
    out = {"ok": True, "bars": len(rows), "first_mismatch": None, "violations": res["violations"],
           "counters": res["counters"], "max_rel_err": 0.0}
    if len(engine_equity) != len(rows):
        out.update(ok=False, first_mismatch={"bar": None, "expected": len(rows), "actual": len(engine_equity),
                                             "field": "length"})
        return out
    scale = abs(res["config"]["initial_cash"])
    for row, actual in zip(rows, engine_equity):
        exp = row["equity"]
        out["max_rel_err"] = max(out["max_rel_err"], abs(actual - exp) / max(abs(exp), abs(actual), scale))
        if not _money_close(exp, actual, rel_tol, scale):
            out.update(ok=False, first_mismatch={"bar": row["bar"], "expected": exp, "actual": actual,
                                                 "field": "equity"})
            break
    out["ok"] = out["ok"] and not res["violations"]
    return out


def check_vector(vector, actual_columns, rel_tol=1e-9):
    """ledger_*.json 向量：actual_columns = {字段: [逐根值]}（可只给部分字段，例如只给 equity）。
    整数字段完全相等，None 位置一致，金额字段以 max(|a|,|b|,初始资金) 为尺度、价格字段以 max(|a|,|b|) 为尺度。"""
    exp = vector["expected"]
    scale = abs(vector["config"]["initial_cash"])
    for field, values in actual_columns.items():
        if field not in exp:
            return {"ok": False, "field": field, "bar": None, "detail": "unknown field"}
        want = exp[field]
        if len(values) != len(want):
            return {"ok": False, "field": field, "bar": None, "detail": f"length {len(values)} != {len(want)}"}
        for bar, (e, a) in enumerate(zip(want, values)):
            if field in INT_FIELDS or (field == "qty" and isinstance(e, int)):
                ok = isinstance(a, (int, float)) and not isinstance(a, bool) and a == e
            elif e is None or a is None:
                ok = e is None and a is None
            else:
                ok = isinstance(a, (int, float)) and not isinstance(a, bool) and \
                    _money_close(e, a, rel_tol, 0.0 if field in ("close", "avg_price", "liq_price") else scale)
            if not ok:
                return {"ok": False, "field": field, "bar": bar, "expected": e, "actual": a}
    return {"ok": True, "fields": sorted(actual_columns)}


def columns(rows):
    """行列表 -> {字段: [逐根值]}。"""
    return {f: [r[f] for r in rows] for f in ROW_FIELDS}
