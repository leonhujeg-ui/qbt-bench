"""行情源：随机游走 / 回放 CSV / 跳空，全部按种子可复现；外加 1 分钟 K 线聚合（给各方言的 K 线接口用）。

配置（JSON，场景文件的 "feed" 节）::

    {"mode": "random_walk", "seed": 1, "interval_ms": 20, "vol": 0.0005,
     "start": {"BTCUSDT": 60000.0, "ETHUSDT": 3000.0},
     "gaps": [{"at_tick": 50, "symbol": "BTCUSDT", "pct": -0.03}],       # 第 50 个 tick 跳空 -3%
     "drift": 0.0, "liquidity": {"BTCUSDT": 0.05}}                        # 每 tick 可成交量（部分成交由此而来）
    {"mode": "replay", "path": "ticks.csv", "loop": true}                 # 列：symbol,price[,liquidity]
    {"mode": "fixed"}                                                     # 价格不动（判据场景里由脚本 "price" 步推动）

随机游走：p_{t+1} = p_t · (1 + drift + vol · z)，z 为 random.Random(seed).gauss(0, 1)，按 tick 精度归整。
"""
import csv
import random

from qdexsim import common

DEFAULT_START = {"BTCUSDT": 60000.0, "ETHUSDT": 3000.0}
DEFAULT_LIQUIDITY = {"BTCUSDT": 0.05, "ETHUSDT": 1.0}


class Feed:
    def __init__(self, cfg=None, ticks=None):
        cfg = dict(cfg or {})
        self.mode = cfg.get("mode", "random_walk")
        self.interval_ms = int(cfg.get("interval_ms", 20))
        self.vol = float(cfg.get("vol", 0.0005))
        self.drift = float(cfg.get("drift", 0.0))
        self.prices = dict(DEFAULT_START, **cfg.get("start", {}))
        self.liquidity = dict(DEFAULT_LIQUIDITY, **cfg.get("liquidity", {}))
        self.ticks = {s: float(t) for s, t in (ticks or {}).items()}
        self.gaps = {(int(g["at_tick"]), g["symbol"]): float(g["pct"]) for g in cfg.get("gaps", [])}
        self.rng = random.Random(int(cfg.get("seed", 0)))
        self.n = 0
        self.rows, self.loop = [], bool(cfg.get("loop", True))
        if self.mode == "replay":
            self.rows = load_csv(cfg["path"])
            if not self.rows:
                raise ValueError("replay feed: empty csv")

    def set_price(self, symbol, price):
        """脚本直接推价（判据场景用：把价格推过限价单）。"""
        self.prices[symbol] = self._quantize(symbol, price)

    def _quantize(self, symbol, price):
        tick = self.ticks.get(symbol, 0.0)
        return max(tick or 1e-9, common.round_step(price, tick)) if tick else max(1e-9, float(price))

    def step(self):
        """前进一个 tick → [(symbol, price, liquidity)]；随机游走每个品种都出一条，回放每次出一行。"""
        self.n += 1
        if self.mode == "replay":
            row = self.rows[(self.n - 1) % len(self.rows)] if self.loop else (
                self.rows[self.n - 1] if self.n - 1 < len(self.rows) else None)
            if row is None:
                return []
            sym, px = row["symbol"], self._quantize(row["symbol"], float(row["price"]))
            self.prices[sym] = px
            liq = float(row.get("liquidity") or self.liquidity.get(sym, 0.05))
            return [(sym, px, liq)]
        out = []
        for sym in sorted(self.prices):
            px = self.prices[sym]
            if self.mode == "random_walk":
                px = px * (1.0 + self.drift + self.vol * self.rng.gauss(0.0, 1.0))
            gap = self.gaps.get((self.n, sym))
            if gap is not None:
                px = px * (1.0 + gap)
            self.prices[sym] = self._quantize(sym, px)
            out.append((sym, self.prices[sym], self.liquidity.get(sym, 0.05)))
        return out


def load_csv(path):
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        if "symbol" not in r or "price" not in r:
            raise ValueError("replay csv needs columns symbol,price")
    return rows


class Candles:
    """按 1 分钟（可配）聚合 tick 成 K 线：[open_ms, open, high, low, close, volume]；最多留 max_bars 根。"""

    def __init__(self, bar_ms=60000, max_bars=500):
        self.bar_ms, self.max_bars = int(bar_ms), int(max_bars)
        self.bars = {}

    def add(self, symbol, price, ts, volume=0.0):
        start = ts - ts % self.bar_ms
        bars = self.bars.setdefault(symbol, [])
        if bars and bars[-1][0] == start:
            b = bars[-1]
            b[2], b[3], b[4], b[5] = max(b[2], price), min(b[3], price), price, b[5] + volume
        else:
            bars.append([start, price, price, price, price, volume])
            del bars[:-self.max_bars]

    def get(self, symbol, limit=100, interval_ms=None):
        bars = self.bars.get(symbol, [])
        if interval_ms and interval_ms != self.bar_ms:
            bars = resample(bars, interval_ms)
        return [list(b) for b in bars[-int(limit):]]


def resample(bars, interval_ms):
    out = []
    for b in bars:
        start = b[0] - b[0] % interval_ms
        if out and out[-1][0] == start:
            o = out[-1]
            o[2], o[3], o[4], o[5] = max(o[2], b[2]), min(o[3], b[3]), b[4], o[5] + b[5]
        else:
            out.append([start] + list(b[1:]))
    return out
