"""Donchian 突破：收盘创 entry_n 根新高做多/新低做空，跌破 exit_n 根低点/高点离场；按 ATR 定仓。"""
from qbt.engine import Strategy


class DonchianBreakout(Strategy):
    name = "donchian_breakout"
    DEFAULTS = {"entry_n": 20, "exit_n": 10, "atr_n": 14, "risk": 0.01, "max_size": 1.0}
    PARAM_SPACE = {"entry_n": [10, 20, 55, 100], "exit_n": [5, 10, 20], "atr_n": [10, 14, 20],
                   "risk": [0.005, 0.01, 0.02], "max_size": [0.5, 1.0]}

    def on_start(self, ctx):
        super().on_start(ctx)
        p = self.params
        self.up, self.lo, _ = ctx.indicator("donchian", "high", "low", p["entry_n"], symbol=self.symbol)
        self.xup, self.xlo, _ = ctx.indicator("donchian", "high", "low", p["exit_n"], symbol=self.symbol)
        self.atr = ctx.indicator("atr", "high", "low", "close", p["atr_n"], symbol=self.symbol)
        self.close = ctx.data[self.symbol].close
        self.state = 0

    def _qty(self, ctx, i):
        """风险预算 risk * equity 对应 1 个 ATR 的价格波动；再按 max_size 封顶。"""
        a = self.atr[i]
        price = self.close[i]
        if a is None or a <= 0 or price <= 0 or ctx.equity <= 0:
            return 0.0
        qty = ctx.equity * self.params["risk"] / a
        return min(qty, ctx.equity * self.params["max_size"] / price)

    def on_bar(self, ctx, i):
        if i < 1 or self.up[i - 1] is None or self.xlo[i - 1] is None:
            return
        c = self.close[i]
        if self.state == 0:
            if c > self.up[i - 1]:
                self.state = 1
                ctx.target(self.symbol, self._qty(ctx, i))
            elif c < self.lo[i - 1]:
                self.state = -1
                ctx.target(self.symbol, -self._qty(ctx, i))
        elif self.state == 1 and c < self.xlo[i - 1]:
            self.state = 0
            ctx.target(self.symbol, 0.0)
        elif self.state == -1 and c > self.xup[i - 1]:
            self.state = 0
            ctx.target(self.symbol, 0.0)
