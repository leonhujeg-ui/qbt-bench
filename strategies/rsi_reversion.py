"""RSI 均值回归：RSI < oversold 做多、> overbought 做空，回到 exit 水平或持有超时平仓。"""
from qbt.engine import Strategy


class RSIReversion(Strategy):
    name = "rsi_reversion"
    DEFAULTS = {"n": 14, "oversold": 30.0, "overbought": 70.0, "exit": 50.0, "size": 0.5, "max_hold": 30}
    PARAM_SPACE = {"n": [7, 14, 21, 28], "oversold": [20.0, 25.0, 30.0, 35.0], "overbought": [65.0, 70.0, 75.0, 80.0],
                   "exit": [45.0, 50.0, 55.0], "size": [0.25, 0.5, 1.0], "max_hold": [10, 30, 100]}

    def on_start(self, ctx):
        super().on_start(ctx)
        self.rsi = ctx.indicator("rsi", "close", self.params["n"], symbol=self.symbol)
        self.close = ctx.data[self.symbol].close
        self.state = 0
        self.entry_bar = -1

    def on_bar(self, ctx, i):
        r = self.rsi[i]
        if r is None:
            return
        p = self.params
        if self.state == 0:
            if r < p["oversold"]:
                self.state, self.entry_bar = 1, i
                ctx.target(self.symbol, self.size_qty(ctx, self.close[i], p["size"]))
            elif r > p["overbought"]:
                self.state, self.entry_bar = -1, i
                ctx.target(self.symbol, -self.size_qty(ctx, self.close[i], p["size"]))
        elif self.state == 1 and (r >= p["exit"] or i - self.entry_bar >= p["max_hold"]):
            self.state = 0
            ctx.target(self.symbol, 0.0)
        elif self.state == -1 and (r <= p["exit"] or i - self.entry_bar >= p["max_hold"]):
            self.state = 0
            ctx.target(self.symbol, 0.0)
