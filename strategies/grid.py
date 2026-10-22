"""网格交易：围绕中枢（SMA）在上下各 levels 档挂限价单，每 recenter_n 根重铺网格；
买入档成交后在上一档挂对应的卖出限价单（反之亦然）；库存超限时停止加仓。"""
from qbt.broker import LIMIT
from qbt.errors import ConfigError
from qbt.engine import Strategy


class GridTrading(Strategy):
    name = "grid"
    DEFAULTS = {"levels": 3, "spacing": 0.01, "qty_frac": 0.1, "recenter_n": 50, "max_inventory": 5}
    PARAM_SPACE = {"levels": [2, 3, 5], "spacing": [0.005, 0.01, 0.02, 0.04], "qty_frac": [0.05, 0.1, 0.2],
                   "recenter_n": [20, 50, 100, 200], "max_inventory": [3, 5, 10]}

    def on_start(self, ctx):
        super().on_start(ctx)
        p = self.params
        if p["levels"] < 1 or not p["spacing"] > 0:                   # qty_frac=0 合法：只是永不下单
            raise ConfigError(f"grid: need levels >= 1 and spacing > 0, got levels={p['levels']} spacing={p['spacing']}")
        self.center = ctx.indicator("sma", "close", p["recenter_n"], symbol=self.symbol)
        self.close = ctx.data[self.symbol].close
        self.orders = []
        self.level_qty = 0.0
        self.inventory = 0

    def _place_grid(self, ctx, i):
        p = self.params
        ctx.cancel_all(self.symbol)
        self.orders = []
        center = self.center[i]
        self.level_qty = self.size_qty(ctx, center, p["qty_frac"]) / p["levels"]
        if self.level_qty <= 0:
            return
        for k in range(1, p["levels"] + 1):
            self.orders.append(ctx.order(self.symbol, 1, self.level_qty, LIMIT, price=center * (1 - k * p["spacing"]),
                                         tag="grid_buy"))
            self.orders.append(ctx.order(self.symbol, -1, self.level_qty, LIMIT, price=center * (1 + k * p["spacing"]),
                                         tag="grid_sell"))

    def on_bar(self, ctx, i):
        p = self.params
        if self.center[i] is None:
            return
        if i % p["recenter_n"] == 0:
            self._place_grid(ctx, i)
            return
        # 对已成交的网格单，在相邻档位挂反向单（库存受限）
        still = []
        for o in self.orders:
            if o.status == "filled":
                self.inventory += o.side
                if abs(self.inventory) < p["max_inventory"]:
                    opp_price = o.price * (1 + p["spacing"]) if o.side > 0 else o.price * (1 - p["spacing"])
                    still.append(ctx.order(self.symbol, -o.side, o.qty, LIMIT, price=opp_price, tag="grid_tp"))
            elif o.status in ("pending", "partial"):
                still.append(o)
        self.orders = still
