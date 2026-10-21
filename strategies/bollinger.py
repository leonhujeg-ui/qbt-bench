"""布林带均值回归：收盘跌破下轨做多、突破上轨做空，|z| 回到 exit_z 以内或持有超时平仓。"""
from qbt.engine import Strategy


class BollingerReversion(Strategy):
    name = "bollinger_reversion"
    DEFAULTS = {"n": 20, "k": 2.0, "exit_z": 0.5, "size": 0.5, "max_hold": 50}
    PARAM_SPACE = {"n": [10, 20, 40, 80], "k": [1.5, 2.0, 2.5, 3.0], "exit_z": [0.0, 0.5, 1.0],
                   "size": [0.25, 0.5, 1.0], "max_hold": [20, 50, 200]}

    def on_start(self, ctx):
        super().on_start(ctx)
        p = self.params
        self.z = ctx.indicator("zscore", "close", p["n"], symbol=self.symbol)
        self.close = ctx.data[self.symbol].close
        self.state = 0
        self.entry_bar = -1

    def on_bar(self, ctx, i):
        z = self.z[i]
        if z is None:
            return
        p = self.params
        if self.state == 0:
            if z < -p["k"]:
                self.state, self.entry_bar = 1, i
                ctx.target(self.symbol, self.size_qty(ctx, self.close[i], p["size"]))
            elif z > p["k"]:
                self.state, self.entry_bar = -1, i
                ctx.target(self.symbol, -self.size_qty(ctx, self.close[i], p["size"]))
        elif abs(z) <= p["exit_z"] or i - self.entry_bar >= p["max_hold"]:
            self.state = 0
            ctx.target(self.symbol, 0.0)
