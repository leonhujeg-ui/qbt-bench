"""撮合与账户：市价/限价/止损/止盈/冰山订单、maker/taker 手续费、滑点、成交量参与上限、
保证金与爆仓、资金费率。

时间语义：订单在第 i-1 根收盘后提交，在第 i 根 bar 上撮合（市价按开盘价 + 滑点；
限价/止损按触发规则在 [low, high] 内成交），绝不使用第 i 根之后的任何信息。

会计恒等式（每根 bar 收盘后都成立，tests 逐根验证）：
    equity == cash + sum(qty * close)                  （签名持仓）
    equity == assets() - liabilities()                 （现金 + 持仓市值 - 负债）
    equity == initial_cash + realized + unrealized - fees - funding
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict

from qbt.errors import ConfigError, OrderError

MARKET, LIMIT, STOP, TAKE_PROFIT, ICEBERG = "market", "limit", "stop", "take_profit", "iceberg"
ORDER_TYPES = (MARKET, LIMIT, STOP, TAKE_PROFIT, ICEBERG)
PENDING, PARTIAL, FILLED, CANCELLED, REJECTED = "pending", "partial", "filled", "cancelled", "rejected"


def _real(name, v, allow_bool=False):
    """配置字段必须是有限实数（bool 只在 allow_bool 时接受），否则 ConfigError 而不是 TypeError。"""
    if isinstance(v, bool):
        if not allow_bool:
            raise ConfigError(f"{name} must be a real number, got bool")
    elif not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ConfigError(f"{name} must be a finite real number, got {v!r}")


@dataclass(frozen=True)
class BrokerConfig:
    cash: float = 100_000.0
    maker_fee: float = 0.0002          # 成交额比例
    taker_fee: float = 0.0005
    slippage: float = 0.0001           # 市价/止损单的固定滑点（价格比例）
    impact: float = 0.0                # 冲击滑点：impact * (成交量 / bar 成交量)
    max_participation: float = 0.1     # 单笔订单每根 bar 最多吃掉的成交量比例
    max_leverage: float = 1.0          # 总名义敞口 <= 权益 * max_leverage
    maintenance_margin: float = 0.05   # 权益 < 维持保证金率 * 总名义敞口 => 爆仓
    liquidation_fee: float = 0.01      # 爆仓罚金（名义额比例）
    funding_rate: float = 0.0          # 每个资金周期按名义额收取（多头付正费率）
    funding_interval: int = 0          # 以 bar 计；0 = 关闭
    allow_short: bool = True

    def check(self):
        for name in ("cash", "maker_fee", "taker_fee", "slippage", "impact", "liquidation_fee", "max_participation",
                     "max_leverage", "maintenance_margin", "funding_rate"):
            _real(name, getattr(self, name))
        if self.cash <= 0:
            raise ConfigError("cash must be positive and finite")
        for name in ("maker_fee", "taker_fee", "slippage", "impact", "liquidation_fee"):
            v = getattr(self, name)
            if v < 0 or v >= 1:
                raise ConfigError(f"{name} must be in [0,1)")
        if not (0 < self.max_participation <= 1):
            raise ConfigError("max_participation must be in (0,1]")
        if self.max_leverage <= 0:
            raise ConfigError("max_leverage must be positive")
        if not (0 <= self.maintenance_margin < 1):
            raise ConfigError("maintenance_margin must be in [0,1)")
        if abs(self.funding_rate) >= 1:
            raise ConfigError("funding_rate must be finite and |rate| < 1")
        if not isinstance(self.funding_interval, int) or isinstance(self.funding_interval, bool) or self.funding_interval < 0:
            raise ConfigError("funding_interval must be a non-negative int")
        if not isinstance(self.allow_short, bool):
            raise ConfigError("allow_short must be bool")
        return self

    def to_dict(self):
        return asdict(self)


class Order:
    __slots__ = ("id", "symbol", "side", "qty", "remaining", "type", "price", "display", "status",
                 "created", "expires", "reduce_only", "tag", "reason")

    def __init__(self, oid, symbol, side, qty, otype, price, display, created, expires, reduce_only, tag):
        self.id, self.symbol, self.side, self.qty, self.remaining = oid, symbol, side, qty, qty
        self.type, self.price, self.display = otype, price, display
        self.status, self.created, self.expires = PENDING, created, expires
        self.reduce_only, self.tag, self.reason = reduce_only, tag, ""

    def to_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}


class Fill:
    __slots__ = ("order_id", "symbol", "bar", "side", "qty", "price", "fee", "liquidity", "tag")

    def __init__(self, order_id, symbol, bar, side, qty, price, fee, liquidity, tag):
        self.order_id, self.symbol, self.bar, self.side = order_id, symbol, bar, side
        self.qty, self.price, self.fee, self.liquidity, self.tag = qty, price, fee, liquidity, tag

    def to_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}


class Position:
    __slots__ = ("symbol", "qty", "avg_price", "realized")

    def __init__(self, symbol):
        self.symbol, self.qty, self.avg_price, self.realized = symbol, 0.0, 0.0, 0.0


def _validate_order(symbol, side, qty, otype, price, display):
    if side not in (1, -1):
        raise OrderError(f"side must be +1/-1, got {side!r}")
    if not isinstance(qty, (int, float)) or isinstance(qty, bool) or not math.isfinite(qty) or qty <= 0:
        raise OrderError(f"qty must be a positive finite number, got {qty!r}")
    if otype not in ORDER_TYPES:
        raise OrderError(f"unknown order type {otype!r}")
    if otype != MARKET:
        if not isinstance(price, (int, float)) or isinstance(price, bool) or not math.isfinite(price) or price <= 0:
            raise OrderError(f"{otype} order needs a positive finite price, got {price!r}")
    if otype == ICEBERG:
        if not isinstance(display, (int, float)) or isinstance(display, bool) or not math.isfinite(display) or display <= 0:
            raise OrderError("iceberg order needs positive finite display qty")
    if not isinstance(symbol, str):
        raise OrderError("symbol must be str")


class Broker:
    """单账户、多标的撮合器。``data`` 为 {symbol: OHLCV}，各标的时间轴对齐。"""

    def __init__(self, data, config=None):
        self.cfg = (config or BrokerConfig()).check()
        self.data = data
        self.cash = float(self.cfg.cash)
        self.initial_cash = self.cash
        self.positions = {s: Position(s) for s in data}
        self._rows = [(s, self.positions[s], data[s].close) for s in data]   # 热循环用：避免每根 bar 查 dict/属性链
        self.pending = []
        self.orders = []
        self.fills = []
        self.events = []
        self.fees_paid = 0.0
        self.funding_paid = 0.0
        self.equity = self.cash
        self.bar = 0               # 引擎总是先调用 on_bar(0) 再让策略下单；盯市下标即当前 bar
        self.bankrupt = False
        self.liquidations = 0
        self._next_id = 1

    # ---- 下单 / 撤单 ----------------------------------------------------------------
    def submit(self, symbol, side, qty, otype=MARKET, price=None, display=None, expires=None,
               reduce_only=False, tag=""):
        _validate_order(symbol, side, qty, otype, price, display)
        if symbol not in self.positions:
            raise OrderError(f"unknown symbol {symbol!r}")
        order = Order(self._next_id, symbol, side, float(qty), otype, price, display, self.bar,
                      expires, reduce_only, tag)
        self._next_id += 1
        self.orders.append(order)
        if self.bankrupt:
            return self._reject(order, "bankrupt")
        if not self.cfg.allow_short and side < 0 and not reduce_only:
            pos = self.positions[symbol].qty
            if qty > pos + 1e-12:
                return self._reject(order, "short not allowed")
        self.pending.append(order)
        return order

    def cancel(self, order):
        if order.status in (PENDING, PARTIAL):
            order.status = CANCELLED
            self.pending.remove(order)
            return True
        return False

    def cancel_all(self, symbol=None):
        n = 0
        for o in list(self.pending):
            if symbol is None or o.symbol == symbol:
                self.cancel(o)
                n += 1
        return n

    def _reject(self, order, reason):
        order.status, order.reason = REJECTED, reason
        return order

    # ---- 每根 bar 推进 ----------------------------------------------------------------
    def on_bar(self, i):
        """撮合挂单 -> 资金费率 -> 以收盘价盯市 -> 爆仓检查。返回收盘权益。"""
        self.bar = i
        if self.pending:
            self._match_pending(i)
        cfg = self.cfg
        if cfg.funding_interval and (i + 1) % cfg.funding_interval == 0:
            self._apply_funding(i)
        equity = self.cash
        gross = 0.0
        for _sym, pos, close in self._rows:
            q = pos.qty
            if q:
                v = q * close[i]
                equity += v
                gross += v if v > 0 else -v
        self.equity = equity
        if gross > 0 and equity < cfg.maintenance_margin * gross:
            self._liquidate(i)
        return self.equity

    def _match_pending(self, i):
        for order in list(self.pending):
            if order.expires is not None and i > order.expires:
                order.status, order.reason = CANCELLED, "expired"
                self.pending.remove(order)
                continue
            d = self.data[order.symbol]
            qty, price, liquidity = self._match(order, d.open[i], d.high[i], d.low[i], d.volume[i])
            if qty <= 0:
                continue
            if not self.cfg.allow_short and order.side < 0:
                held = self.positions[order.symbol].qty          # 成交时再次封顶：多笔挂单可能已把多头卖光
                if held <= 1e-12:
                    self.pending.remove(order)
                    self._reject(order, "short not allowed")
                    continue
                qty = qty if qty < held else held
            if not self._margin_ok(order, qty, price):
                self.pending.remove(order)
                self._reject(order, "margin")
                continue
            self._apply_fill(order, i, qty, price, liquidity)
            if order.remaining <= 1e-12:
                order.remaining, order.status = 0.0, FILLED
                self.pending.remove(order)
            else:
                order.status = PARTIAL

    def _match(self, order, o, h, l, v):
        """返回 (成交量, 成交价, 流动性类型)；不成交返回 (0, None, None)。"""
        cap = self.cfg.max_participation * v
        if cap <= 0:
            return 0.0, None, None
        qty = order.remaining if order.remaining < cap else cap
        if order.type == ICEBERG and order.display < qty:
            qty = order.display
        side, t = order.side, order.type
        if t == MARKET:
            return qty, o * (1.0 + side * self._slip(qty, v)), "taker"
        p = order.price
        if t in (LIMIT, ICEBERG, TAKE_PROFIT):
            if side > 0 and l <= p:
                return qty, (o if o < p else p), "maker"
            if side < 0 and h >= p:
                return qty, (o if o > p else p), "maker"
            return 0.0, None, None
        if side > 0 and h >= p:                        # 买入止损：价格向上触及
            return qty, (o if o > p else p) * (1.0 + self._slip(qty, v)), "taker"
        if side < 0 and l <= p:                        # 卖出止损
            return qty, (o if o < p else p) * (1.0 - self._slip(qty, v)), "taker"
        return 0.0, None, None

    def _slip(self, qty, volume):
        return self.cfg.slippage + self.cfg.impact * (qty / volume)

    def _margin_ok(self, order, qty, price):
        pos = self.positions[order.symbol].qty
        new_pos = pos + order.side * qty
        if abs(new_pos) <= abs(pos) + 1e-12:          # 减仓永远允许
            return True
        if order.reduce_only:
            return False
        gross = 0.0
        for sym, p in self.positions.items():
            if sym != order.symbol and p.qty:
                gross += abs(p.qty * self.data[sym].close[self.bar])
        gross += abs(new_pos * price)
        return gross <= self.equity * self.cfg.max_leverage + 1e-9

    def _apply_fill(self, order, i, qty, price, liquidity):
        rate = self.cfg.maker_fee if liquidity == "maker" else self.cfg.taker_fee
        fee = qty * price * rate
        self.cash -= order.side * qty * price + fee
        self.fees_paid += fee
        self._update_position(self.positions[order.symbol], order.side * qty, price)
        order.remaining -= qty
        self.fills.append(Fill(order.id, order.symbol, i, order.side, qty, price, fee, liquidity, order.tag))

    @staticmethod
    def _update_position(pos, signed_qty, price):
        q = pos.qty
        if q == 0.0 or (q > 0) == (signed_qty > 0):
            new_q = q + signed_qty
            pos.avg_price = (abs(q) * pos.avg_price + abs(signed_qty) * price) / abs(new_q)
            pos.qty = new_q
            return
        closing = min(abs(q), abs(signed_qty))
        pos.realized += (price - pos.avg_price) * closing * (1.0 if q > 0 else -1.0)
        new_q = q + signed_qty
        if abs(new_q) <= 1e-12:
            pos.qty, pos.avg_price = 0.0, 0.0
        elif (new_q > 0) != (q > 0):                    # 反手：剩余部分按成交价开新仓
            pos.qty, pos.avg_price = new_q, price
        else:
            pos.qty = new_q

    def _apply_funding(self, i):
        rate = self.cfg.funding_rate
        for sym, pos in self.positions.items():
            if pos.qty:
                pay = pos.qty * self.data[sym].close[i] * rate
                self.cash -= pay
                self.funding_paid += pay

    def _liquidate(self, i):
        """以收盘价强平全部持仓，收 taker 费 + 罚金，撤销全部挂单。"""
        cfg = self.cfg
        for sym, pos in self.positions.items():
            if pos.qty:
                price = self.data[sym].close[i]
                qty, side = abs(pos.qty), (-1 if pos.qty > 0 else 1)
                fee = qty * price * (cfg.taker_fee + cfg.liquidation_fee)
                self.cash -= side * qty * price + fee
                self.fees_paid += fee
                self._update_position(pos, side * qty, price)
                self.fills.append(Fill(0, sym, i, side, qty, price, fee, "taker", "liquidation"))
        for o in self.pending:
            o.status, o.reason = CANCELLED, "liquidation"
        self.pending.clear()
        self.liquidations += 1
        self.equity = self.cash
        self.events.append(("liquidation", i, self.equity))
        if self.equity <= 0:
            self.bankrupt = True
            self.events.append(("bankrupt", i, self.equity))

    # ---- 查询 ---------------------------------------------------------------------------
    def position(self, symbol):
        return self.positions[symbol].qty

    def assets(self):
        """正现金 + 多头市值。"""
        a = self.cash if self.cash > 0 else 0.0
        for sym, pos in self.positions.items():
            if pos.qty > 0:
                a += pos.qty * self.data[sym].close[self.bar]
        return a

    def liabilities(self):
        """借入现金（负现金）+ 空头市值（借入证券）。"""
        li = -self.cash if self.cash < 0 else 0.0
        for sym, pos in self.positions.items():
            if pos.qty < 0:
                li += -pos.qty * self.data[sym].close[self.bar]
        return li

    def realized_pnl(self):
        return sum(p.realized for p in self.positions.values())

    def unrealized_pnl(self):
        i = self.bar
        return sum(p.qty * (self.data[s].close[i] - p.avg_price) for s, p in self.positions.items() if p.qty)

    def gross_exposure(self):
        i = self.bar
        return sum(abs(p.qty * self.data[s].close[i]) for s, p in self.positions.items() if p.qty)


def round_trips(fills):
    """把成交流按标的拼成往返交易（持仓从 0 到非 0 开仓，回到 0 或反手时平仓）。
    返回 dict 列表：symbol, entry_bar, exit_bar, side, qty（持仓峰值）, entry_price, exit_price, pnl（含手续费）, bars。"""
    trades = []
    state = {}
    for f in fills:
        st = state.get(f.symbol)
        signed = f.side * f.qty
        if st is None:
            state[f.symbol] = {"qty": signed, "avg": f.price, "entry": f.bar, "fees": f.fee, "pnl": 0.0, "peak": f.qty}
            continue
        if (st["qty"] > 0) == (signed > 0):
            st["avg"] = (abs(st["qty"]) * st["avg"] + f.qty * f.price) / (abs(st["qty"]) + f.qty)
            st["qty"] += signed
            st["peak"] = max(st["peak"], abs(st["qty"]))
            st["fees"] += f.fee
            continue
        closing = min(abs(st["qty"]), f.qty)
        st["pnl"] += (f.price - st["avg"]) * closing * (1.0 if st["qty"] > 0 else -1.0)
        st["fees"] += f.fee
        remaining = st["qty"] + signed
        if abs(remaining) <= 1e-12 or (remaining > 0) != (st["qty"] > 0):
            trades.append({"symbol": f.symbol, "entry_bar": st["entry"], "exit_bar": f.bar,
                           "side": 1 if st["qty"] > 0 else -1, "qty": st["peak"], "entry_price": st["avg"],
                           "exit_price": f.price, "pnl": st["pnl"] - st["fees"], "bars": f.bar - st["entry"]})
            if abs(remaining) <= 1e-12:
                del state[f.symbol]                      # 平仓：下一笔成交重新开仓
            else:
                state[f.symbol] = {"qty": remaining, "avg": f.price, "entry": f.bar, "fees": 0.0, "pnl": 0.0,
                                   "peak": abs(remaining)}
        else:
            st["qty"] = remaining
    return trades
