"""9 个指标的第二份独立写法，只用于和 indicators_ref 逐点对拍（相对容差 1e-9）。

刻意换算法，避免两份实现犯同一个错：
- EMA / RMA / Wilder 用闭式加权和 ``(1-a)^k`` （``pow`` + ``math.fsum``）而不是递推；
  adjusted 口径的分母用等比数列闭式 ``(1 - q^m) / (1 - q)``；
- 简单均值用 ``math.fsum``（精确舍入求和）而不是顺序累加；SMA-RSI 用前缀和；
- 滚动最高 / 最低用单调队列（deque）而不是 max/min 切片；
- RSI 用 ``100 * g / (g + l)`` 而不是 ``100 - 100 / (1 + g / l)``；TRIX 用 ``e3[t] / e3[t-1] - 1``；
- CCI 的零偏差判定用“窗口内全部相等”（ref 用平均绝对偏差 == 0）；
- Ichimoku 位移用切片拼接。
口径字符串与参数的解析复用 ``indicators_ref.resolve``（那是配置，不是计算）。
"""
from __future__ import annotations

import math
from collections import deque

from qdcheck.indicators_ref import LINES, apply_warmup, resolve

# (RSI 0/0 时的值, Stoch 极差为 0 时占 scale 的比例)；独立于 ref 再写一遍
_FLAT = {"talib": (0.0, 0.0), "neutral": (50.0, 0.5), "null": (None, None), "pine": (100.0, None)}


def _lead(x):
    """前导缺值个数；有效段中出现缺值时返回 (s, 第一个中间缺值下标)。"""
    s = next((i for i, v in enumerate(x) if v is not None), len(x))
    gap = next((i for i in range(s, len(x)) if x[i] is None), len(x))
    return s, gap


def _weighted(init, xs, a):
    """y_{-1}=init（init 为 None 时 y_0 = xs[0]），y_k = a*x_k + (1-a)*y_{k-1}，返回 y_last 的闭式值。"""
    q = 1.0 - a
    m = len(xs) - 1
    if init is None:
        terms = [q ** m * xs[0]] + [a * q ** (m - k) * xs[k] for k in range(1, m + 1)]
    else:
        terms = [q ** (m + 1) * init] + [a * q ** (m - k) * xs[k] for k in range(m + 1)]
    return math.fsum(terms)


def _fmean(w):
    return math.fsum(w) / len(w)


def ma(x, n, kind, seed="sma"):
    out = [None] * len(x)
    s, gap = _lead(x)
    if kind == "sma":
        for t in range(len(x)):
            if t - n + 1 >= 0 and all(v is not None for v in x[t - n + 1:t + 1]):
                out[t] = _fmean(x[t - n + 1:t + 1])
        return out
    a = 2.0 / (n + 1) if kind == "ema" else 1.0 / n
    q = 1.0 - a
    for t in range(s, gap):
        if seed == "sma":
            if t >= s + n - 1:
                out[t] = _weighted(None, [_fmean(x[s:s + n])] + x[s + n:t + 1], a)
        elif seed == "first":
            out[t] = _weighted(None, x[s:t + 1], a)
        else:
            m = t - s + 1
            num = math.fsum(q ** k * x[t - k] for k in range(m))
            den = (1.0 - q ** m) / (1.0 - q) if q != 0.0 else 1.0
            out[t] = num / den
    return out


def _ma_seed(knobs):
    return knobs["ma"], knobs.get("seed", "sma")


def dema(d, knobs, p):
    kind, seed = _ma_seed(knobs)
    e1 = ma(d["close"], p["period"], kind, seed)
    e2 = ma(e1, p["period"], kind, seed)
    return {"value": [(a + a - b) if a is not None and b is not None else None for a, b in zip(e1, e2)]}


def tema(d, knobs, p):
    kind, seed = _ma_seed(knobs)
    e1 = ma(d["close"], p["period"], kind, seed)
    e2 = ma(e1, p["period"], kind, seed)
    e3 = ma(e2, p["period"], kind, seed)
    return {"value": [3.0 * (a - b) + c if None not in (a, b, c) else None for a, b, c in zip(e1, e2, e3)]}


def trix(d, knobs, p):
    kind, seed = _ma_seed(knobs)
    e = ma(ma(ma(d["close"], p["period"], kind, seed), p["period"], kind, seed), p["period"], kind, seed)
    prev = [None] + e[:-1]
    return {"value": [p["scale"] * (cur / pv - 1.0) if cur is not None and pv else None
                      for cur, pv in zip(e, prev)][:len(e)]}


def _rsi(close, n, kind, flat_val):
    out = [None] * len(close)
    g = [0.0] + [max(close[i] - close[i - 1], 0.0) for i in range(1, len(close))]
    lo = [0.0] + [max(close[i - 1] - close[i], 0.0) for i in range(1, len(close))]
    pg, pl = [0.0], [0.0]
    for i in range(1, len(close)):
        pg.append(pg[-1] + g[i])
        pl.append(pl[-1] + lo[i])
    a = 1.0 / n if kind == "wilder" else 2.0 / (n + 1)
    for t in range(n, len(close)):
        if kind == "sma":
            ag, al = (pg[t] - pg[t - n]) / n, (pl[t] - pl[t - n]) / n
        else:
            ag = _weighted(None, [_fmean(g[1:n + 1])] + g[n + 1:t + 1], a)
            al = _weighted(None, [_fmean(lo[1:n + 1])] + lo[n + 1:t + 1], a)
        if ag + al == 0.0:
            out[t] = flat_val
        else:
            out[t] = 100.0 * ag / (ag + al)
    return out


def _rolling_extreme(x, n, take_max):
    """单调队列滚动极值；窗口内有 None 则输出 None。"""
    out, dq, last_none = [None] * len(x), deque(), -1
    for t, v in enumerate(x):
        if v is None:
            last_none = t
            dq.clear()
            continue
        while dq and ((x[dq[-1]] <= v) if take_max else (x[dq[-1]] >= v)):
            dq.pop()
        dq.append(t)
        while dq[0] <= t - n:
            dq.popleft()
        if t - n + 1 > last_none and t - n + 1 >= 0:
            out[t] = x[dq[0]]
    return out


def stochrsi(d, knobs, p):
    rsi_flat, stoch_flat = _FLAT[knobs["flat"]]
    rsi = _rsi(d["close"], p["rsi_period"], knobs["rsi"], rsi_flat)
    hi = _rolling_extreme(rsi, p["stoch_period"], True)
    lo = _rolling_extreme(rsi, p["stoch_period"], False)
    st = []
    for r, h, l_ in zip(rsi, hi, lo):
        if h is None:
            st.append(None)
        elif not h - l_ > p["range_eps"]:
            st.append(None if stoch_flat is None else stoch_flat * p["scale"])
        else:
            st.append((r - l_) / (h - l_) * p["scale"])
    k = ma(st, p["k_period"], "sma")
    return {"k": k, "d": ma(k, p["d_period"], "sma")}


def cci(d, knobs, p):
    n = p["period"]
    src = [(a + b + c) / 3.0 for a, b, c in zip(d["high"], d["low"], d["close"])] \
        if knobs["src"] == "hlc3" else list(d["close"])
    out = [None] * len(src)
    for t in range(n - 1, len(src)):
        w = src[t - n + 1:t + 1]
        if len(set(w)) == 1:
            out[t] = 0.0 if knobs["zero_dev"] == "zero" else None
            continue
        m = _fmean(w)
        md = math.fsum(abs(v - m) for v in w) / n
        out[t] = (src[t] - m) / md / p["constant"]
    return {"value": out}


def ao(d, knobs, p):
    src = [(a + b) * 0.5 for a, b in zip(d["high"], d["low"])] if knobs["src"] == "hl2" else list(d["close"])
    fast, slow = ma(src, p["fast"], knobs["ma"]), ma(src, p["slow"], knobs["ma"])
    return {"value": [f - s if f is not None and s is not None else None for f, s in zip(fast, slow)]}


def force(d, knobs, p):
    c, v = d["close"], d["volume"]
    raw = [None if t == 0 else (c[t] - c[t - 1]) * v[t] for t in range(len(c))]
    if knobs["ma"] == "raw":
        return {"value": raw}
    kind, seed = _ma_seed(knobs)
    return {"value": ma(raw, p["period"], kind, seed)}


def _kd(x, m, knobs):
    if knobs["ma"] == "sma":
        return ma(x, m, "sma")
    a = 1.0 / m if knobs["ma"] == "rma" else 2.0 / (m + 1)
    s, gap = _lead(x)
    init = 50.0 if knobs["init"] == "50" else None
    return [None if t < s or t >= gap else _weighted(init, x[s:t + 1], a) for t in range(len(x))]


def kdj(d, knobs, p):
    n = p["n"]
    hh = _rolling_extreme(d["high"], n, True)
    ll = _rolling_extreme(d["low"], n, False)
    rsv = []
    for c, h, l_ in zip(d["close"], hh, ll):
        if h is None:
            rsv.append(None)
        elif h - l_ == 0.0:
            rsv.append(50.0 if knobs["zero_range"] == "half" else 0.0)
        else:
            rsv.append((c - l_) * 100.0 / (h - l_))
    k = _kd(rsv, p["m1"], knobs)
    dd = _kd(k, p["m2"], knobs)
    return {"k": k, "d": dd, "j": [3.0 * a - 2.0 * b if a is not None and b is not None else None
                                   for a, b in zip(k, dd)]}


def _mid(d, n):
    hh, ll = _rolling_extreme(d["high"], n, True), _rolling_extreme(d["low"], n, False)
    return [None if h is None else (h + l_) * 0.5 for h, l_ in zip(hh, ll)]


def ichimoku(d, knobs, p):
    n = len(d["close"])
    tenkan, kijun, base_b = _mid(d, p["tenkan"]), _mid(d, p["kijun"]), _mid(d, p["senkou_b"])
    base_a = [None if a is None or b is None else (a + b) * 0.5 for a, b in zip(tenkan, kijun)]
    off = {"none": 0, "forward_tv": p["displacement"] - 1, "forward": p["displacement"]}[knobs["shift"]]
    lead = [None] * min(off, n)
    senkou_a = (lead + base_a)[:n]
    senkou_b = (lead + base_b)[:n]
    chikou = (list(d["close"][off:]) + [None] * n)[:n]
    return {"tenkan": tenkan, "kijun": kijun, "senkou_a": senkou_a, "senkou_b": senkou_b, "chikou": chikou}


_FUNCS = {"dema": dema, "tema": tema, "trix": trix, "stochrsi": stochrsi, "cci": cci, "ao": ao,
          "force": force, "kdj": kdj, "ichimoku": ichimoku}


def compute(indicator, ohlcv, variant=None, params=None, warmup="none"):
    _variant, knobs, full = resolve(indicator, variant, params)
    cols = {k: list(ohlcv[k]) for k in ("open", "high", "low", "close", "volume")}
    lines = _FUNCS[indicator](cols, knobs, full)
    return apply_warmup({name: lines[name] for name in LINES[indicator]}, warmup)
