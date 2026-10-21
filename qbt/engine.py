"""事件驱动主循环：多策略、多标的，逐根 bar 推进。

时间语义（保证不偷看未来）：
    for i in bars:
        broker.on_bar(i)          # 先用第 i 根 bar 撮合上一根收盘后提交的订单，并以 close[i] 盯市
        for s in strategies:      # 再让策略看到第 i 根（含）之前的数据并下单，订单最早在 i+1 成交
            s.on_bar(ctx, i)

严格模式（strict=True）用守卫列表包裹所有数据列：策略访问 j > i 的元素立即抛 LookaheadError，
用于测试证明策略不偷看；默认模式用裸 list 以换取速度。
"""
from __future__ import annotations

import time

from qbt.broker import Broker, BrokerConfig, MARKET, round_trips
from qbt.data import OHLCV
from qbt.errors import ConfigError, QBTError
from qbt import indicators as _ind


class LookaheadError(QBTError):
    """策略在 bar i 访问了下标 > i 的数据。"""


class GuardedColumn:
    """只允许访问 [0, clock.index] 的列视图（strict 模式）。"""

    __slots__ = ("_col", "_clock")

    def __init__(self, col, clock):
        self._col, self._clock = col, clock

    def __getitem__(self, j):
        if isinstance(j, slice):
            start, stop, step = j.indices(len(self._col))
            if stop - 1 > self._clock.index:
                raise LookaheadError(f"slice up to {stop} at bar {self._clock.index}")
            return self._col[j]
        if j < 0:
            j += len(self._col)
        if j > self._clock.index:
            raise LookaheadError(f"index {j} at bar {self._clock.index}")
        return self._col[j]

    def __len__(self):
        return len(self._col)

    def __iter__(self):
        return iter(self._col[: self._clock.index + 1])


class GuardedData:
    __slots__ = ("symbol", "ts", "open", "high", "low", "close", "volume", "bar_seconds", "_n")

    def __init__(self, data, clock):
        self.symbol, self.bar_seconds, self._n = data.symbol, data.bar_seconds, len(data)
        for col in ("ts", "open", "high", "low", "close", "volume"):
            setattr(self, col, GuardedColumn(getattr(data, col), clock))

    def __len__(self):
        return self._n


class IndicatorCache:
    """进程级指标缓存：键 = (数据指纹, 指标名, 参数)。网格搜索中不同参数组共享同名指标。"""

    def __init__(self, max_elements=8_000_000):
        self.max_elements = max_elements
        self._store = {}
        self._elements = 0
        self.hits = self.misses = 0

    def get(self, data, name, args):
        key = (data.fingerprint(), name, args)
        val = self._store.get(key)
        if val is not None:
            self.hits += 1
            return val
        self.misses += 1
        fn = _ind.REGISTRY.get(name)
        if fn is None:
            raise ConfigError(f"unknown indicator {name!r}")
        cols = [getattr(data, a) if isinstance(a, str) and a in ("open", "high", "low", "close", "volume") else a
                for a in args]
        val = fn(*cols)
        size = sum(len(v) for v in val) if isinstance(val, tuple) else len(val)
        while self._elements + size > self.max_elements and self._store:
            k = next(iter(self._store))
            v = self._store.pop(k)
            self._elements -= sum(len(x) for x in v) if isinstance(v, tuple) else len(v)
        self._store[key] = val
        self._elements += size
        return val

    def clear(self):
        self._store.clear()
        self._elements = 0


INDICATOR_CACHE = IndicatorCache()


class Context:
    """策略可见的接口：数据、账户查询、下单、指标缓存。"""

    __slots__ = ("data", "raw", "broker", "index", "symbols", "cache", "strict")

    def __init__(self, data, broker, strict=False, cache=None):
        self.raw = data
        self.index = -1
        self.strict = strict
        self.data = {s: GuardedData(d, self) for s, d in data.items()} if strict else data
        self.broker = broker
        self.symbols = list(data)
        self.cache = cache or INDICATOR_CACHE

    # ---- 账户 ----
    def position(self, symbol):
        return self.broker.positions[symbol].qty

    @property
    def equity(self):
        return self.broker.equity

    @property
    def cash(self):
        return self.broker.cash

    # ---- 指标（因果、带缓存） ----
    def indicator(self, name, *args, symbol=None):
        d = self.raw[symbol or self.symbols[0]]
        return self.cache.get(d, name, tuple(args))

    # ---- 下单 ----
    def order(self, symbol, side, qty, otype=MARKET, **kw):
        return self.broker.submit(symbol, side, qty, otype, **kw)

    def cancel(self, order):
        return self.broker.cancel(order)

    def cancel_all(self, symbol=None):
        return self.broker.cancel_all(symbol)

    def pending_signed(self, symbol):
        s = 0.0
        for o in self.broker.pending:
            if o.symbol == symbol and o.type == MARKET:
                s += o.side * o.remaining
        return s

    def target(self, symbol, qty, tag=""):
        """把（持仓 + 未成交市价单）调整到目标数量，差额以市价单提交。"""
        delta = qty - self.broker.positions[symbol].qty - self.pending_signed(symbol)
        if delta > 1e-9:
            return self.broker.submit(symbol, 1, delta, MARKET, tag=tag)
        if delta < -1e-9:
            return self.broker.submit(symbol, -1, -delta, MARKET, tag=tag)
        return None


def _check_param_type(strategy, key, value, default):
    """参数类型按默认值归类：bool 只接受 bool；int/float 接受有限实数；str 接受 str。"""
    if isinstance(default, bool):
        ok = isinstance(value, bool)
    elif isinstance(default, (int, float)):
        ok = isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and abs(value) != float("inf")
    else:
        ok = isinstance(value, type(default))
    if not ok:
        raise ConfigError(f"{strategy}: param {key}={value!r} has wrong type (expected like {default!r})")


class Strategy:
    """策略基类。子类覆盖 on_start / on_bar，并声明 PARAM_SPACE（>= 4 维）与 DEFAULTS。"""

    name = "base"
    PARAM_SPACE = {}
    DEFAULTS = {}

    def __init__(self, symbol=None, **params):
        unknown = set(params) - set(self.DEFAULTS)
        if unknown:
            raise ConfigError(f"{self.name}: unknown params {sorted(unknown)}")
        for k, v in params.items():
            _check_param_type(self.name, k, v, self.DEFAULTS[k])
        self.params = {**self.DEFAULTS, **params}
        if symbol is not None and not isinstance(symbol, str):
            raise ConfigError("symbol must be str or None")
        self.symbol = symbol

    def on_start(self, ctx):
        if self.symbol is None:
            self.symbol = ctx.symbols[0]
        if self.symbol not in ctx.symbols:
            raise ConfigError(f"unknown symbol {self.symbol!r}")

    def on_bar(self, ctx, i):
        raise NotImplementedError

    def on_end(self, ctx):
        pass

    def size_qty(self, ctx, price, fraction):
        """按权益比例定数量；价格非正或权益非正返回 0。"""
        if price <= 0 or ctx.equity <= 0:
            return 0.0
        return ctx.equity * fraction / price


class Result:
    """回测结果：权益曲线 + 成交 + 往返交易 + 账户统计。"""

    def __init__(self, engine, equity, elapsed):
        b = engine.broker
        first = engine.data[engine.symbols[0]]
        self.symbols = engine.symbols
        self.n = engine.n
        self.ts = first.ts
        self.bar_seconds = first.bar_seconds
        self.equity = equity
        self.initial_cash = b.initial_cash
        self.final_equity = equity[-1] if equity else b.initial_cash
        self.fills = b.fills
        self.orders = b.orders
        self.trades = round_trips(b.fills)
        self.fees = b.fees_paid
        self.funding = b.funding_paid
        self.liquidations = b.liquidations
        self.bankrupt = b.bankrupt
        self.events = list(b.events)
        self.params = [{"strategy": s.name, "symbol": s.symbol, **s.params} for s in engine.strategies]
        self.elapsed = elapsed
        self.cache_stats = (engine.ctx.cache.hits, engine.ctx.cache.misses)

    def summary(self):
        return {"n": self.n, "initial_cash": self.initial_cash, "final_equity": self.final_equity,
                "fills": len(self.fills), "orders": len(self.orders), "trades": len(self.trades),
                "fees": self.fees, "funding": self.funding, "liquidations": self.liquidations,
                "bankrupt": self.bankrupt, "params": self.params, "elapsed": self.elapsed}


class Engine:
    def __init__(self, data, strategies, broker_config=None, strict=False, cache=None):
        if isinstance(data, OHLCV):
            data = {data.symbol: data}
        if not data:
            raise ConfigError("no data")
        if isinstance(strategies, Strategy):
            strategies = [strategies]
        if not strategies:
            raise ConfigError("no strategies")
        lengths = {len(d) for d in data.values()}
        if len(lengths) != 1:
            raise ConfigError("all symbols must have the same number of bars")
        for d in data.values():
            d.validate()
        self.data = data
        self.symbols = list(data)
        self.n = lengths.pop()
        self.strategies = list(strategies)
        self.broker = Broker(data, broker_config or BrokerConfig())
        self.ctx = Context(data, self.broker, strict=strict, cache=cache)

    def run(self):
        t0 = time.perf_counter()
        ctx, n = self.ctx, self.n
        for s in self.strategies:
            s.on_start(ctx)
        on_bars = [s.on_bar for s in self.strategies]
        broker_on_bar = self.broker.on_bar
        equity = [0.0] * n
        if len(on_bars) == 1:
            ob = on_bars[0]
            for i in range(n):
                equity[i] = broker_on_bar(i)
                ctx.index = i
                ob(ctx, i)
        else:
            for i in range(n):
                equity[i] = broker_on_bar(i)
                ctx.index = i
                for ob in on_bars:
                    ob(ctx, i)
        for s in self.strategies:
            s.on_end(ctx)
        return Result(self, equity, time.perf_counter() - t0)


def run_backtest(data, strategy, broker_config=None, strict=False, cache=None):
    """便捷入口：单策略（或策略列表）回测。"""
    return Engine(data, strategy, broker_config, strict=strict, cache=cache).run()
