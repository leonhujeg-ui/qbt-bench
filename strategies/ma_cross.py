"""均线交叉：快线上穿慢线做多，下穿做空（或平仓）。"""
from qbt.engine import Strategy


class MACross(Strategy):
    name = "ma_cross"
    DEFAULTS = {"fast": 10, "slow": 30, "ma": "sma", "size": 0.5, "short": True}
    PARAM_SPACE = {"fast": [5, 10, 20, 40], "slow": [30, 50, 100, 200], "ma": ["sma", "ema", "wma"],
                   "size": [0.25, 0.5, 1.0], "short": [True, False]}

    def on_start(self, ctx):
        super().on_start(ctx)
        p = self.params
        if p["fast"] >= p["slow"]:
            self.fast = self.slow = None            # 无效组合：不交易
            return
        self.fast = ctx.indicator(p["ma"], "close", p["fast"], symbol=self.symbol)
        self.slow = ctx.indicator(p["ma"], "close", p["slow"], symbol=self.symbol)
        self.close = ctx.data[self.symbol].close
        self.state = 0

    def on_bar(self, ctx, i):
        slow = self.slow
        if slow is None:
            return
        s = slow[i]
        if s is None:
            return
        f = self.fast[i]
        if f > s:
            if self.state != 1:
                self.state = 1
                ctx.target(self.symbol, self.size_qty(ctx, self.close[i], self.params["size"]))
        elif f < s:
            if self.state != -1:
                self.state = -1
                qty = -self.size_qty(ctx, self.close[i], self.params["size"]) if self.params["short"] else 0.0
                ctx.target(self.symbol, qty)
