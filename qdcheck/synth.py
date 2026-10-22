"""合成 K 线生成器：同 seed 同结果（逐位相同），带停牌 / 缺口 / 单根跳跃 / 长横盘 / 趋势段。

可复现性设计
- 随机数只来自 ``random.Random(seed).random()``（Mersenne Twister，文档保证跨平台、跨版本可复现）。
- 只用 + - * / 和 ``round``，不调用 exp / log / cos 等 libm 函数（libm 在不同平台可能差 1 ulp）；
  正态数用 4 个均匀数之和（Irwin–Hall）近似。所以在任何 IEEE-754 双精度机器上输出逐位相同。
- 价格量化到 ``tick``（默认 1/64，二进制精确），成交量取整数：常数窗口的和 / 均值 / 极差都是精确的，
  指标的退化分支（极差 0、偏差 0）不会被浮点噪声误触发。
- 每根 bar 固定消耗 ``DRAWS_PER_BAR`` 个均匀数，每次换段固定消耗 2 个：前缀稳定
  （``generate(n=100)`` 等于 ``generate(n=200)`` 的前 100 根），用于因果性测试。

输出：``{"open","high","low","close","volume"}`` 五列（等长 list[float]）+ ``segments``
（``[start, stop, kind]``，stop 不含）+ ``events``（``[index, kind]``）+ ``spec``（实际使用的参数）。
所有数据都是合成的，不构成任何交易结论。
"""
from __future__ import annotations

import argparse
import json
import random
import sys

KINDS = ("random", "trend_up", "trend_down", "flat", "halt")
EVENT_KINDS = ("gap_up", "gap_down", "jump_up", "jump_down")
DRAWS_PER_BAR = 17
SQRT3 = 1.7320508075688772          # sqrt(3) 的双精度值，写成字面量以免调用 libm

DEFAULTS = {
    "s0": 100.0,            # 初始价
    "tick": 1.0 / 64.0,     # 价格最小变动
    "vol": 0.01,            # 单根收益率标准差
    "wick": 0.5,            # 影线长度（相对 vol）
    "base_volume": 1000.0,  # 成交量基准
    "trend_drift": 0.3,     # 趋势段每根漂移（相对 vol）
    "flat_noise": 0.1,      # 横盘段噪声（相对 vol）；0 => 价格完全不动（成交量 > 0）
    "flat_pull": 0.2,       # 横盘段向段首价格回归的强度
    "seg_min": 20,          # 自动换段时段长下限
    "seg_max": 200,         # 自动换段时段长上限
    "weights": {"random": 4, "trend_up": 2, "trend_down": 2, "flat": 1, "halt": 1},
    "gap_prob": 0.01,       # 每根出现缺口的概率（停牌 / 横盘段内不注入）
    "jump_prob": 0.005,     # 每根出现单根跳跃的概率
    "gap_min": 0.031,       # 缺口幅度下限（量化后仍保证 >= 3%）
    "gap_max": 0.06,
    "jump_size": 0.08,      # 单根跳跃幅度（close/open - 1 = ±8%）
    "min_price_ticks": 64,  # 价格下限（tick 数）
}
GAP_THRESHOLD = 0.03


def _check_spec(spec, n, segments, events):
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        raise ValueError("n must be a non-negative int")
    for key in ("s0", "tick", "vol", "base_volume"):
        if not spec[key] > 0:
            raise ValueError(f"{key} must be > 0")
    if not 1 <= spec["seg_min"] <= spec["seg_max"]:
        raise ValueError("need 1 <= seg_min <= seg_max")
    weights = spec["weights"]
    if set(weights) - set(KINDS) or not any(weights.get(k, 0) > 0 for k in KINDS):
        raise ValueError(f"weights must be a dict over {KINDS} with a positive entry")
    for start, length, kind in segments:
        if kind not in KINDS or length < 1 or start < 0:
            raise ValueError(f"bad forced segment {(start, length, kind)!r}")
    for idx, kind in events.items():
        if kind not in EVENT_KINDS or idx < 0:
            raise ValueError(f"bad forced event {(idx, kind)!r}")


def _normal(rnd):
    """Irwin–Hall：4 个 U(0,1) 之和减 2，乘 sqrt(3) 得到均值 0、方差 1 的近似正态数。"""
    return (rnd() + rnd() + rnd() + rnd() - 2.0) * SQRT3


def _pick_kind(u, weights):
    total = 0.0
    for k in KINDS:
        total += weights.get(k, 0)
    acc = 0.0
    for k in KINDS:
        acc += weights.get(k, 0)
        if u * total < acc:
            return k
    return KINDS[-1]


def _quant(x, tick):
    return round(x / tick) * tick


class _Regime:
    """自动换段 + 强制段覆盖。强制段只覆盖“种类”，不改变随机数消耗，保证前缀稳定。"""

    def __init__(self, rnd, spec, segments):
        self.rnd, self.spec = rnd, spec
        self.forced = {}
        for start, length, kind in segments:
            for i in range(start, start + length):
                self.forced[i] = kind
        self.left = 0
        self.kind = "random"

    def kind_at(self, i):
        if self.left == 0:
            u_kind, u_len = self.rnd(), self.rnd()
            self.kind = _pick_kind(u_kind, self.spec["weights"])
            lo, hi = self.spec["seg_min"], self.spec["seg_max"]
            self.left = lo + int(u_len * (hi - lo + 1))
        self.left -= 1
        return self.forced.get(i, self.kind)


def _open_price(prev_c, event, u_size, spec):
    tick = spec["tick"]
    if event not in ("gap_up", "gap_down"):
        return prev_c
    g = spec["gap_min"] + (spec["gap_max"] - spec["gap_min"]) * u_size
    sign = 1.0 if event == "gap_up" else -1.0
    o = _quant(prev_c * (1.0 + sign * g), tick)
    while abs(o - prev_c) < GAP_THRESHOLD * prev_c:          # 量化后仍保证缺口 >= 3%
        o += sign * tick
    return o


def _bar(prev_c, kind, event, draws, anchor, spec):
    """返回 (o, h, l, c, v)。draws 为本根预先取好的随机数（固定个数）。"""
    u_size, z_ret, z_hi, z_lo, u_vol = draws
    tick, vol = spec["tick"], spec["vol"]
    floor = spec["min_price_ticks"] * tick
    if kind == "halt":
        return prev_c, prev_c, prev_c, prev_c, 0.0
    if kind == "flat" and spec["flat_noise"] == 0:
        return prev_c, prev_c, prev_c, prev_c, float(round(spec["base_volume"] * (0.5 + u_vol)) or 1)
    o = _open_price(prev_c, event, u_size, spec)
    if event in ("jump_up", "jump_down"):
        r = spec["jump_size"] if event == "jump_up" else -spec["jump_size"]
    elif kind == "flat":
        r = spec["flat_noise"] * vol * z_ret + spec["flat_pull"] * (anchor - o) / o
    else:
        drift = {"trend_up": 1.0, "trend_down": -1.0}.get(kind, 0.0) * spec["trend_drift"] * vol
        r = drift + vol * z_ret
    c = max(_quant(o * (1.0 + r), tick), floor)
    top, bot = (o, c) if o >= c else (c, o)
    h = max(_quant(top * (1.0 + spec["wick"] * vol * abs(z_hi)), tick), top)
    lo = min(_quant(bot * (1.0 - spec["wick"] * vol * abs(z_lo)), tick), bot)
    lo = max(lo, floor)
    v = float(max(1, round(spec["base_volume"] * (0.5 + u_vol) * (1.0 + abs(r) / vol))))
    return o, h, lo, c, v


def _event_for(i, kind, forced_events, u_gap, u_jump, u_sign, spec):
    if kind in ("halt", "flat"):
        return None                                   # 停牌 / 横盘段内不注入事件（强制事件也忽略）
    if i in forced_events:
        return forced_events[i]
    if u_gap < spec["gap_prob"]:
        return "gap_up" if u_sign < 0.5 else "gap_down"
    if u_jump < spec["jump_prob"]:
        return "jump_up" if u_sign < 0.5 else "jump_down"
    return None


def generate(n, seed=0, *, segments=(), events=None, **overrides):
    """生成 n 根合成 K 线。

    segments: 强制段 ``[(start, length, kind), ...]``，kind ∈ KINDS；events: 强制事件 ``{index: kind}``，
    kind ∈ EVENT_KINDS；其余关键字覆盖 ``DEFAULTS``。
    """
    unknown = set(overrides) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"unknown synth parameters: {sorted(unknown)}")
    spec = dict(DEFAULTS, **overrides)
    spec["weights"] = dict(spec["weights"])
    segments = [tuple(s) for s in segments]
    forced_events = {int(k): v for k, v in (events or {}).items()}
    _check_spec(spec, n, segments, forced_events)
    rnd = random.Random(seed).random
    regime = _Regime(rnd, spec, segments)
    cols = {"open": [], "high": [], "low": [], "close": [], "volume": []}
    seg_out, ev_out = [], []
    prev_c = _quant(spec["s0"], spec["tick"])
    anchor, prev_kind = prev_c, None
    for i in range(n):
        kind = regime.kind_at(i)
        u_gap, u_jump, u_sign, u_size, u_vol = rnd(), rnd(), rnd(), rnd(), rnd()
        z_ret, z_hi, z_lo = _normal(rnd), _normal(rnd), _normal(rnd)
        if kind != prev_kind:
            seg_out.append([i, i + 1, kind])
            anchor, prev_kind = prev_c, kind
        else:
            seg_out[-1][1] = i + 1
        event = _event_for(i, kind, forced_events, u_gap, u_jump, u_sign, spec)
        if event is not None:
            ev_out.append([i, event])
        bar = _bar(prev_c, kind, event, (u_size, z_ret, z_hi, z_lo, u_vol), anchor, spec)
        for key, val in zip(("open", "high", "low", "close", "volume"), bar):
            cols[key].append(val)
        prev_c = bar[3]
    cols.update(segments=seg_out, events=ev_out, spec=dict(spec, n=n, seed=seed))
    return cols


def ohlcv(data):
    """只取五列（向量 JSON 的 input 格式）。"""
    return {k: list(data[k]) for k in ("open", "high", "low", "close", "volume")}


def rows(data):
    """转成 [(open, high, low, close, volume), ...] 列表。"""
    return list(zip(data["open"], data["high"], data["low"], data["close"], data["volume"]))


def main(argv=None):
    ap = argparse.ArgumentParser(description="合成 K 线（同 seed 同结果）")
    ap.add_argument("--n", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default="-")
    args = ap.parse_args(argv)
    data = generate(args.n, args.seed)
    text = json.dumps(data, sort_keys=True)
    if args.out == "-":
        sys.stdout.write(text + "\n")
    else:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
