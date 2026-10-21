"""合成 K 线数据：几何布朗运动 + 可注入跳跃 / 停牌 / 缺口。

设计要点
- 全部随机数只来自 ``random.Random(seed).random()``（文档保证跨版本可复现），
  正态分布用自实现的 Box-Muller，不依赖 ``random.gauss`` 的内部实现。
- 每根非停牌 bar 消耗固定数量的随机数，使得同 seed 两次生成逐字节相同。
- 列式存储（6 个 list），这是纯 Python 下最快的访问方式。
"""
from __future__ import annotations

import hashlib
import math
import operator
import random
from array import array
from dataclasses import dataclass, asdict

from qbt.errors import DataError

SECONDS_PER_YEAR = 365.25 * 86400.0
_TWO_PI = 2.0 * math.pi


class OHLCV:
    """一个标的的列式 K 线序列。所有列等长；``ts`` 为严格递增的秒级时间戳。"""

    __slots__ = ("symbol", "ts", "open", "high", "low", "close", "volume",
                 "bar_seconds", "_validated", "_fingerprint")

    def __init__(self, symbol, ts, open, high, low, close, volume, bar_seconds=None):
        self.symbol = symbol
        self.ts = list(ts)
        self.open = list(open)
        self.high = list(high)
        self.low = list(low)
        self.close = list(close)
        self.volume = list(volume)
        if bar_seconds is None and len(self.ts) >= 2:
            bar_seconds = self.ts[1] - self.ts[0]
        self.bar_seconds = bar_seconds
        self._validated = False
        self._fingerprint = None

    def __len__(self):
        return len(self.close)

    # ---- 校验 -------------------------------------------------------------
    def validate(self):
        """全量校验，通过后缓存标记；失败抛 DataError。O(n)，用 C 级 map/all。"""
        if self._validated:
            return self
        cols = (self.ts, self.open, self.high, self.low, self.close, self.volume)
        n = len(self.ts)
        if any(len(c) != n for c in cols):
            raise DataError("columns have different lengths")
        if not isinstance(self.symbol, str) or not self.symbol:
            raise DataError("symbol must be a non-empty str")
        if n == 0:
            self._validated = True
            return self
        for name, col in zip(("ts", "open", "high", "low", "close", "volume"), cols):
            _check_finite(name, col)
        if min(self.low) <= 0.0:
            raise DataError("prices must be strictly positive")
        if min(self.volume) < 0.0:
            raise DataError("volume must be non-negative")
        ge, le, lt = operator.ge, operator.le, operator.lt
        if not (all(map(ge, self.high, self.low)) and all(map(ge, self.high, self.open))
                and all(map(ge, self.high, self.close)) and all(map(le, self.low, self.open))
                and all(map(le, self.low, self.close))):
            raise DataError("bar range violated: need low <= open,close <= high")
        if n >= 2 and not all(map(lt, self.ts, self.ts[1:])):
            raise DataError("timestamps must be strictly increasing")
        self._validated = True
        return self

    # ---- 工具 -------------------------------------------------------------
    def slice(self, start, stop):
        """返回 [start, stop) 的副本（不共享列表）。"""
        n = len(self)
        start, stop, _ = slice(start, stop).indices(n)
        out = OHLCV(self.symbol, self.ts[start:stop], self.open[start:stop],
                    self.high[start:stop], self.low[start:stop], self.close[start:stop],
                    self.volume[start:stop], self.bar_seconds)
        out._validated = self._validated
        return out

    def fingerprint(self):
        """内容指纹（sha256 of raw doubles），用于指标缓存键与黄金文件。"""
        if self._fingerprint is None:
            h = hashlib.sha256()
            h.update(self.symbol.encode("utf-8"))
            for col in (self.ts, self.open, self.high, self.low, self.close, self.volume):
                h.update(array("d", [float(x) for x in col]).tobytes())
            self._fingerprint = h.hexdigest()
        return self._fingerprint

    def to_rows(self):
        return list(zip(self.ts, self.open, self.high, self.low, self.close, self.volume))

    def to_csv(self, path):
        with open(path, "w", encoding="utf-8") as f:
            f.write("ts,open,high,low,close,volume\n")
            for row in self.to_rows():
                f.write(",".join(repr(x) for x in row) + "\n")

    @classmethod
    def from_csv(cls, path, symbol="CSV", bar_seconds=None):
        cols = [[] for _ in range(6)]
        with open(path, "r", encoding="utf-8") as f:
            header = f.readline()
            if header.strip() != "ts,open,high,low,close,volume":
                raise DataError("bad csv header")
            for line in f:
                parts = line.strip().split(",")
                if len(parts) != 6:
                    raise DataError("bad csv row")
                cols[0].append(int(float(parts[0])))
                for k in range(1, 6):
                    cols[k].append(float(parts[k]))
        return cls(symbol, *cols, bar_seconds=bar_seconds)


def _check_finite(name, col):
    try:
        if math.isfinite(math.fsum(col)):      # 全有限 <=> fsum 有限（C 级，~20ms/百万）
            return
    except (TypeError, ValueError, OverflowError):
        pass                                  # 慢路径给出精确的错误信息
    for x in col:
        if not isinstance(x, (int, float)) or isinstance(x, bool):
            raise DataError(f"{name}: non-numeric value {x!r}")
        if x != x or x in (math.inf, -math.inf):
            raise DataError(f"{name}: non-finite value {x!r}")


# ---- 生成器 ----------------------------------------------------------------
@dataclass(frozen=True)
class GenSpec:
    """合成数据规格。所有字段可 JSON 序列化，便于在进程间传递。"""
    n: int = 10_000
    seed: int = 0
    symbol: str = "SYN"
    s0: float = 100.0
    mu: float = 0.05          # 年化漂移
    sigma: float = 0.40       # 年化波动率
    bar_seconds: int = 60
    start_ts: int = 1_600_000_000
    jump_prob: float = 0.001
    jump_sigma: float = 0.02
    halt_prob: float = 0.0005
    halt_len: int = 5
    gap_prob: float = 0.002
    gap_sigma: float = 0.005
    base_volume: float = 1000.0
    vol_sigma: float = 0.6
    wick: float = 0.6         # 影线长度相对单根波动的比例

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(**d)

    def check(self):
        if not isinstance(self.n, int) or self.n < 0:
            raise DataError("n must be a non-negative int")
        if self.s0 <= 0 or self.sigma < 0 or self.bar_seconds <= 0:
            raise DataError("s0>0, sigma>=0, bar_seconds>0 required")
        for p in (self.jump_prob, self.halt_prob, self.gap_prob):
            if not 0.0 <= p <= 1.0:
                raise DataError("probabilities must lie in [0,1]")
        if self.halt_len < 1 or self.base_volume <= 0:
            raise DataError("halt_len>=1 and base_volume>0 required")
        return self


def generate(spec=None, **kwargs):
    """按规格生成 OHLCV。``generate(GenSpec(...))`` 或 ``generate(n=..., seed=...)``。"""
    if spec is None:
        spec = GenSpec(**kwargs)
    elif kwargs:
        spec = GenSpec(**{**spec.to_dict(), **kwargs})
    spec.check()
    rng = random.Random(spec.seed)
    rnd = rng.random
    n = spec.n
    dt = spec.bar_seconds / SECONDS_PER_YEAR
    drift = (spec.mu - 0.5 * spec.sigma * spec.sigma) * dt
    vol = spec.sigma * math.sqrt(dt)
    wick = spec.wick * vol
    exp = math.exp
    ts = [spec.start_ts + i * spec.bar_seconds for i in range(n)]
    opens, highs, lows, closes, vols = [], [], [], [], []
    prev_close = spec.s0
    halt_left = 0
    halt_prob, halt_len = spec.halt_prob, spec.halt_len
    gap_prob, gap_sigma = spec.gap_prob, spec.gap_sigma
    jump_prob, jump_sigma = spec.jump_prob, spec.jump_sigma
    base_volume, vol_sigma = spec.base_volume, spec.vol_sigma
    inv_vol = 1.0 / vol if vol > 0 else 0.0
    log, sqrt, cos, sin = math.log, math.sqrt, math.cos, math.sin
    ao, ah, al, ac, av = opens.append, highs.append, lows.append, closes.append, vols.append
    for _ in range(n):
        if halt_left > 0 or rnd() < halt_prob:
            halt_left = (halt_left - 1) if halt_left > 0 else (halt_len - 1)
            o = h = l = c = prev_close
            v = 0.0
        else:
            # 三次 Box-Muller => 六个独立标准正态数 z0..z5（固定消耗 6 个均匀数）
            r0 = sqrt(-2.0 * log(1.0 - rnd())); t0 = _TWO_PI * rnd()
            r1 = sqrt(-2.0 * log(1.0 - rnd())); t1 = _TWO_PI * rnd()
            r2 = sqrt(-2.0 * log(1.0 - rnd())); t2 = _TWO_PI * rnd()
            z0, z1 = r0 * cos(t0), r0 * sin(t0)
            z2, z3 = r1 * cos(t1), r1 * sin(t1)
            z4, z5 = r2 * cos(t2), r2 * sin(t2)
            gap = gap_sigma * z1 if rnd() < gap_prob else 0.0
            jump = jump_sigma * z2 if rnd() < jump_prob else 0.0
            o = prev_close * exp(gap)
            r = drift + vol * z0 + jump
            c = o * exp(r)
            h = (o if o > c else c) * exp(abs(z3) * wick)
            l = (o if o < c else c) * exp(-abs(z4) * wick)
            v = base_volume * exp(vol_sigma * z5) * (1.0 + abs(r) * inv_vol)
        ao(o); ah(h); al(l); ac(c); av(v)
        prev_close = c
    data = OHLCV(spec.symbol, ts, opens, highs, lows, closes, vols, spec.bar_seconds)
    return data.validate()


def generate_universe(symbols, n, seed, **kwargs):
    """多标的：每个标的的 seed 由 (seed, symbol) 确定性派生，时间轴对齐。"""
    out = {}
    for sym in symbols:
        h = hashlib.sha256(f"{seed}:{sym}".encode("utf-8")).digest()
        sub_seed = int.from_bytes(h[:8], "big")
        out[sym] = generate(GenSpec(n=n, seed=sub_seed, symbol=sym, **kwargs))
    return out
