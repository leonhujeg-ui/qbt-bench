"""带止损的趋势跟随：价格在均线上方且 SuperTrend 多头时做多；
入场后挂 ATR 倍数的止损单与止盈单（止损随价格上移，trailing），任一触发即离场。"""
from qbt.broker import STOP, TAKE_PROFIT
from qbt.engine import Strategy


class TrendFollowStop(Strategy):
    name = "trend_stop"
    DEFAULTS = {"ma_n": 50, "atr_n": 14, "stop_mult": 2.0, "tp_mult": 4.0, "size": 0.5, "trailing": True}
    PARAM_SPACE = {"ma_n": [20, 50, 100, 200], "atr_n": [10, 14, 20], "stop_mult": [1.5, 2.0, 3.0],
                   "tp_mult": [3.0, 4.0, 6.0], "size": [0.25, 0.5, 1.0], "trailing": [True, False]}

    def on_start(self, ctx):
        super().on_start(ctx)
        p = self.params
        self.ma = ctx.indicator("ema", "close", p["ma_n"], symbol=self.symbol)
        self.atr = ctx.indicator("atr", "high", "low", "close", p["atr_n"], symbol=self.symbol)
        _, self.direction = ctx.indicator("supertrend", "high", "low", "close", p["atr_n"], 3.0, symbol=self.symbol)
        self.close = ctx.data[self.symbol].close
        self.stop = self.tp = self.entry = None
        self.stop_price = 0.0
        self.in_trade = False

    def on_bar(self, ctx, i):
        p = self.params
        a, d = self.atr[i], self.direction[i]
        if a is None or d is None or i < p["ma_n"]:
            return
        c = self.close[i]
        if self.in_trade:
            if self.stop.status not in ("pending", "partial") or self.tp.status not in ("pending", "partial"):
                self.in_trade = False                              # 止损或止盈已成交
                ctx.cancel_all(self.symbol)
                ctx.target(self.symbol, 0.0)
            elif p["trailing"] and c - p["stop_mult"] * a > self.stop_price:
                ctx.cancel(self.stop)
                self.stop_price = c - p["stop_mult"] * a
                self.stop = ctx.order(self.symbol, -1, self.stop.remaining, STOP, price=self.stop_price, reduce_only=True)
            return
        entry_open = self.entry is not None and self.entry.status in ("pending", "partial")
        if c > self.ma[i] and d == 1 and ctx.position(self.symbol) == 0.0 and not entry_open:
            qty = self.size_qty(ctx, c, p["size"])
            stop_price = c - p["stop_mult"] * a
            if qty <= 0 or stop_price <= 0:                        # 止损价非正（极端波动）不入场
                return
            self.entry = ctx.order(self.symbol, 1, qty)
            self.stop_price = stop_price
            self.stop = ctx.order(self.symbol, -1, qty, STOP, price=self.stop_price, reduce_only=True)
            self.tp = ctx.order(self.symbol, -1, qty, TAKE_PROFIT, price=c + p["tp_mult"] * a, reduce_only=True)
            self.in_trade = True
