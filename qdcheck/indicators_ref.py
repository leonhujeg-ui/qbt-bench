"""9 个指标的朴素参考实现：每个输出点都从输入前缀 / 窗口从头算，不用上一点的输出递推，不做任何优化。

复杂度是 O(n^2)（嵌套平滑每层 O(n^2)），只用于生成测试向量和对拍，不要拿来跑回测。

入口::

    compute(indicator, ohlcv, variant=None, params=None, warmup="none") -> {line: [float|None, ...]}

- ``ohlcv``：``{"open","high","low","close","volume"}`` 五列等长 list（只读用到的列）。
- ``variant``：口径字符串 ``"k1=v1;k2=v2"``（键按字母序），全部合法取值见 ``VARIANTS``；None = 该指标第一个变体。
- ``params``：数值参数，缺省取 ``DEFAULT_PARAMS``，多出的键报 ValueError。
- ``warmup``：``"none"`` 预热期 / 未定义点输出 None；``"nan"`` 输出 float('nan')。不进 variant 字符串
  （JSON 向量里一律写 null，compare 把 None/NaN 都当作“无值”）。
- 输出长度恒等于输入长度；输出 i 只依赖输入 [0, i]（Ichimoku 迟行线的 shift=forward* 口径除外，
  它按定义是 chikou[i] = close[i + 位移]）。

平滑口径（ma）：
    sma  简单均值；ema  alpha = 2/(n+1)；rma  Wilder / RMA，alpha = 1/n。
    seed（只对 ema/rma）：sma = 前 n 个有效值的均值作为第 n 个点（TA-Lib 口径，预热 n-1）；
    first = 第一个有效值作为初值（pandas adjust=False，无预热）；
    adjusted = sum((1-a)^k x[t-k]) / sum((1-a)^k)（pandas adjust=True，无预热）。
EMA 的输入序列只允许“前导”缺值；有效段中间出现缺值时该点及之后输出 None（不跨越缺值）。
"""
from __future__ import annotations

import math

INDICATORS = ("dema", "tema", "trix", "stochrsi", "cci", "ao", "force", "kdj", "ichimoku")

_MA_SEEDED = ("ma=ema;seed=sma", "ma=ema;seed=first", "ma=ema;seed=adjusted",
              "ma=rma;seed=sma", "ma=rma;seed=first", "ma=rma;seed=adjusted", "ma=sma")

VARIANTS = {
    "dema": _MA_SEEDED,
    "tema": _MA_SEEDED,
    "trix": _MA_SEEDED,
    "stochrsi": tuple(f"flat={f};rsi={r}" for r in ("wilder", "ema", "sma")
                      for f in ("talib", "neutral", "null", "pine")),
    "cci": tuple(f"src={s};zero_dev={z}" for s in ("hlc3", "close") for z in ("zero", "null")),
    "ao": tuple(f"ma={m};src={s}" for m in ("sma", "ema", "rma") for s in ("hl2", "close")),
    "force": ("ma=ema;seed=sma", "ma=ema;seed=first", "ma=ema;seed=adjusted", "ma=sma", "ma=raw"),
    "kdj": tuple(f"init={i};ma={m};zero_range={z}" for m in ("rma", "ema") for i in ("50", "first")
                 for z in ("zero", "half")) + ("ma=sma;zero_range=zero", "ma=sma;zero_range=half"),
    "ichimoku": ("shift=forward", "shift=forward_tv", "shift=none"),
}

DEFAULT_PARAMS = {
    "dema": {"period": 5},
    "tema": {"period": 5},
    "trix": {"period": 5, "scale": 100.0},
    "stochrsi": {"rsi_period": 14, "stoch_period": 14, "k_period": 3, "d_period": 3,
                 "scale": 100.0, "range_eps": 1e-9},
    "cci": {"period": 20, "constant": 0.015},
    "ao": {"fast": 5, "slow": 34},
    "force": {"period": 13},
    "kdj": {"n": 9, "m1": 3, "m2": 3},
    "ichimoku": {"tenkan": 9, "kijun": 26, "senkou_b": 52, "displacement": 26},
}

LINES = {
    "dema": ("value",), "tema": ("value",), "trix": ("value",), "stochrsi": ("k", "d"),
    "cci": ("value",), "ao": ("value",), "force": ("value",), "kdj": ("k", "d", "j"),
    "ichimoku": ("tenkan", "kijun", "senkou_a", "senkou_b", "chikou"),
}

# Stoch RSI 退化约定：(RSI 在平均涨跌都为 0 时的取值, Stoch 在窗口极差 <= range_eps 时的取值/100*scale)
STOCHRSI_FLAT = {"talib": (0.0, 0.0), "neutral": (50.0, 50.0), "null": (None, None), "pine": (100.0, None)}
_INT_PARAMS = {"period", "rsi_period", "stoch_period", "k_period", "d_period", "fast", "slow", "n", "m1", "m2",
               "tenkan", "kijun", "senkou_b", "displacement"}
_COLUMNS = ("open", "high", "low", "close", "volume")


# ---- 口径字符串与参数 ----------------------------------------------------------
def parse_variant(variant):
    """``"a=1;b=2"`` -> {"a": "1", "b": "2"}。"""
    out = {}
    for part in variant.split(";"):
        key, sep, val = part.partition("=")
        if not sep or not key or not val or key in out:
            raise ValueError(f"malformed variant {variant!r}")
        out[key] = val
    return out


def canonical_variant(variant):
    """键按字母序重排，便于用户随意书写顺序。"""
    knobs = parse_variant(variant)
    return ";".join(f"{k}={knobs[k]}" for k in sorted(knobs))


def resolve(indicator, variant=None, params=None):
    """校验指标名 / 口径 / 参数，返回 (canonical_variant, knobs, full_params)。"""
    if indicator not in VARIANTS:
        raise ValueError(f"unknown indicator {indicator!r}; known: {INDICATORS}")
    if variant is None:
        variant = VARIANTS[indicator][0]
    variant = canonical_variant(variant)
    if variant not in VARIANTS[indicator]:
        raise ValueError(f"unknown variant {variant!r} for {indicator}; known: {VARIANTS[indicator]}")
    full = dict(DEFAULT_PARAMS[indicator])
    for key, val in (params or {}).items():
        if key not in full:
            raise ValueError(f"unknown parameter {key!r} for {indicator}")
        full[key] = val
    for key, val in full.items():
        if key in _INT_PARAMS:
            if not isinstance(val, int) or isinstance(val, bool) or val < 1:
                raise ValueError(f"{indicator}.{key} must be an int >= 1, got {val!r}")
        elif isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val) or val < 0:
            raise ValueError(f"{indicator}.{key} must be a finite non-negative number, got {val!r}")
    return variant, parse_variant(variant), full


def _check_input(ohlcv):
    n = None
    for key in _COLUMNS:
        if key not in ohlcv:
            raise ValueError(f"ohlcv missing column {key!r}")
        col = ohlcv[key]
        if n is None:
            n = len(col)
        if len(col) != n:
            raise ValueError("ohlcv columns must have equal length")
        for v in col:
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise ValueError(f"ohlcv[{key!r}] contains a non-finite / non-numeric value {v!r}")


# ---- 朴素原语 ------------------------------------------------------------------
def _window(x, t, n):
    """x[t-n+1 .. t] 全部有值时返回该窗口，否则 None。"""
    if t - n + 1 < 0:
        return None
    w = x[t - n + 1:t + 1]
    for v in w:
        if v is None:
            return None
    return w


def _mean(w):
    s = 0.0
    for v in w:
        s += v
    return s / len(w)


def _alpha(ma, n):
    return 2.0 / (n + 1) if ma == "ema" else 1.0 / n


def _ema_at(x, t, n, ma, seed):
    """第 t 点的 EMA/RMA：从第一个有效值开始从头递推到 t（不复用 t-1 的结果）。"""
    s = 0
    while s <= t and x[s] is None:
        s += 1
    if s > t:
        return None
    for j in range(s, t + 1):
        if x[j] is None:
            return None
    a = _alpha(ma, n)
    if seed == "sma":
        if t < s + n - 1:
            return None
        e = _mean(x[s:s + n])
        for j in range(s + n, t + 1):
            e = a * x[j] + (1.0 - a) * e
        return e
    if seed == "first":
        e = x[s]
        for j in range(s + 1, t + 1):
            e = a * x[j] + (1.0 - a) * e
        return e
    num = den = 0.0
    w = 1.0
    for j in range(t, s - 1, -1):
        num += w * x[j]
        den += w
        w *= 1.0 - a
    return num / den


def ma_series(x, n, ma, seed="sma"):
    """平滑序列：ma ∈ {sma, ema, rma}；seed 见模块文档。"""
    if ma == "sma":
        out = []
        for t in range(len(x)):
            w = _window(x, t, n)
            out.append(None if w is None else _mean(w))
        return out
    return [_ema_at(x, t, n, ma, seed) for t in range(len(x))]


def _knob_ma(knobs):
    return knobs["ma"], knobs.get("seed", "sma")


# ---- DEMA / TEMA / TRIX --------------------------------------------------------
def dema(d, knobs, p):
    ma, seed = _knob_ma(knobs)
    e1 = ma_series(d["close"], p["period"], ma, seed)
    e2 = ma_series(e1, p["period"], ma, seed)
    return {"value": [None if a is None or b is None else 2.0 * a - b for a, b in zip(e1, e2)]}


def tema(d, knobs, p):
    ma, seed = _knob_ma(knobs)
    e1 = ma_series(d["close"], p["period"], ma, seed)
    e2 = ma_series(e1, p["period"], ma, seed)
    e3 = ma_series(e2, p["period"], ma, seed)
    out = []
    for a, b, c in zip(e1, e2, e3):
        out.append(None if a is None or b is None or c is None else 3.0 * a - 3.0 * b + c)
    return {"value": out}


def trix(d, knobs, p):
    ma, seed = _knob_ma(knobs)
    e3 = d["close"]
    for _ in range(3):
        e3 = ma_series(e3, p["period"], ma, seed)
    out = [None] * len(e3)
    for t in range(1, len(e3)):
        if e3[t] is not None and e3[t - 1] is not None and e3[t - 1] != 0:
            out[t] = p["scale"] * (e3[t] - e3[t - 1]) / e3[t - 1]
    return {"value": out}


# ---- Stoch RSI -----------------------------------------------------------------
def _rsi_at(close, t, n, kind, rsi_flat):
    if t < n:
        return None
    gains = [max(close[j] - close[j - 1], 0.0) for j in range(1, t + 1)]
    losses = [max(close[j - 1] - close[j], 0.0) for j in range(1, t + 1)]
    if kind == "sma":
        ag, al = _mean(gains[t - n:t]), _mean(losses[t - n:t])
    else:
        ag, al = _mean(gains[:n]), _mean(losses[:n])
        a = 2.0 / (n + 1)
        for j in range(n, t):
            if kind == "wilder":
                ag = (ag * (n - 1) + gains[j]) / n
                al = (al * (n - 1) + losses[j]) / n
            else:
                ag = a * gains[j] + (1.0 - a) * ag
                al = a * losses[j] + (1.0 - a) * al
    if al == 0.0:
        return 100.0 if ag > 0.0 else rsi_flat
    return 100.0 - 100.0 / (1.0 + ag / al)


def stochrsi(d, knobs, p):
    rsi_flat, stoch_flat = STOCHRSI_FLAT[knobs["flat"]]
    close, scale = d["close"], p["scale"]
    rsi = [_rsi_at(close, t, p["rsi_period"], knobs["rsi"], rsi_flat) for t in range(len(close))]
    stoch = []
    for t in range(len(close)):
        w = _window(rsi, t, p["stoch_period"])
        if w is None:
            stoch.append(None)
            continue
        hi, lo = max(w), min(w)
        if hi - lo <= p["range_eps"]:
            stoch.append(None if stoch_flat is None else stoch_flat / 100.0 * scale)
        else:
            stoch.append(scale * (rsi[t] - lo) / (hi - lo))
    k = ma_series(stoch, p["k_period"], "sma")
    return {"k": k, "d": ma_series(k, p["d_period"], "sma")}


# ---- CCI / AO / Force ----------------------------------------------------------
def cci(d, knobs, p):
    if knobs["src"] == "hlc3":
        src = [(h + lo + c) / 3.0 for h, lo, c in zip(d["high"], d["low"], d["close"])]
    else:
        src = list(d["close"])
    out = []
    for t in range(len(src)):
        w = _window(src, t, p["period"])
        if w is None:
            out.append(None)
            continue
        m = _mean(w)
        md = _mean([abs(v - m) for v in w])
        if md == 0.0:
            out.append(0.0 if knobs["zero_dev"] == "zero" else None)
        else:
            out.append((src[t] - m) / (p["constant"] * md))
    return {"value": out}


def ao(d, knobs, p):
    if knobs["src"] == "hl2":
        src = [(h + lo) / 2.0 for h, lo in zip(d["high"], d["low"])]
    else:
        src = list(d["close"])
    fast = ma_series(src, p["fast"], knobs["ma"], "sma")
    slow = ma_series(src, p["slow"], knobs["ma"], "sma")
    return {"value": [None if a is None or b is None else a - b for a, b in zip(fast, slow)]}


def force(d, knobs, p):
    c, v = d["close"], d["volume"]
    raw = [None] + [(c[t] - c[t - 1]) * v[t] for t in range(1, len(c))]
    raw = raw[:len(c)]
    if knobs["ma"] == "raw":
        return {"value": raw}
    ma, seed = _knob_ma(knobs)
    return {"value": ma_series(raw, p["period"], ma, seed)}


# ---- KDJ -----------------------------------------------------------------------
def _kdj_smooth(x, t, m, knobs):
    """KDJ 的 K/D 平滑第 t 点（x 只有前导缺值）。rma 即通达信 SMA(X, m, 1)。"""
    s = 0
    while s <= t and x[s] is None:
        s += 1
    if s > t:
        return None
    if knobs["ma"] == "sma":
        w = _window(x, t, m)
        return None if w is None else _mean(w)
    if knobs["init"] == "50":
        y, start = 50.0, s
    else:
        y, start = x[s], s + 1
    for j in range(start, t + 1):
        if knobs["ma"] == "rma":
            y = (x[j] + (m - 1) * y) / m
        else:
            a = 2.0 / (m + 1)
            y = a * x[j] + (1.0 - a) * y
    return y


def kdj(d, knobs, p):
    h, lo, c, n = d["high"], d["low"], d["close"], p["n"]
    rsv = []
    for t in range(len(c)):
        if t < n - 1:
            rsv.append(None)
            continue
        hh, ll = max(h[t - n + 1:t + 1]), min(lo[t - n + 1:t + 1])
        if hh == ll:
            rsv.append(0.0 if knobs["zero_range"] == "zero" else 50.0)
        else:
            rsv.append(100.0 * (c[t] - ll) / (hh - ll))
    k = [_kdj_smooth(rsv, t, p["m1"], knobs) for t in range(len(c))]
    dd = [_kdj_smooth(k, t, p["m2"], knobs) for t in range(len(c))]
    j = [None if a is None or b is None else 3.0 * a - 2.0 * b for a, b in zip(k, dd)]
    return {"k": k, "d": dd, "j": j}


# ---- Ichimoku ------------------------------------------------------------------
def _midpoint(h, lo, t, n):
    if t < n - 1:
        return None
    return (max(h[t - n + 1:t + 1]) + min(lo[t - n + 1:t + 1])) / 2.0


def ichimoku(d, knobs, p):
    h, lo, c = d["high"], d["low"], d["close"]
    n = len(c)
    tenkan = [_midpoint(h, lo, t, p["tenkan"]) for t in range(n)]
    kijun = [_midpoint(h, lo, t, p["kijun"]) for t in range(n)]
    base_a = [None if a is None or b is None else (a + b) / 2.0 for a, b in zip(tenkan, kijun)]
    base_b = [_midpoint(h, lo, t, p["senkou_b"]) for t in range(n)]
    shift = {"forward": p["displacement"], "forward_tv": p["displacement"] - 1, "none": 0}[knobs["shift"]]
    senkou_a = [base_a[t - shift] if t - shift >= 0 else None for t in range(n)]
    senkou_b = [base_b[t - shift] if t - shift >= 0 else None for t in range(n)]
    chikou = [c[t + shift] if t + shift < n else None for t in range(n)]
    return {"tenkan": tenkan, "kijun": kijun, "senkou_a": senkou_a, "senkou_b": senkou_b, "chikou": chikou}


_FUNCS = {"dema": dema, "tema": tema, "trix": trix, "stochrsi": stochrsi, "cci": cci, "ao": ao,
          "force": force, "kdj": kdj, "ichimoku": ichimoku}


def apply_warmup(lines, warmup):
    if warmup == "none":
        return lines
    if warmup != "nan":
        raise ValueError("warmup must be 'none' or 'nan'")
    nan = float("nan")
    return {k: [nan if v is None else v for v in vals] for k, vals in lines.items()}


def compute(indicator, ohlcv, variant=None, params=None, warmup="none"):
    """计算一个指标的全部输出线，返回 {line: list}，每条线长度 = 输入长度。"""
    _variant, knobs, full = resolve(indicator, variant, params)
    _check_input(ohlcv)
    cols = {k: list(ohlcv[k]) for k in _COLUMNS}
    lines = _FUNCS[indicator](cols, knobs, full)
    return apply_warmup({name: lines[name] for name in LINES[indicator]}, warmup)   # 非法 warmup 在这里报 ValueError


def all_variants():
    """[(indicator, variant), ...]，按 INDICATORS 与 VARIANTS 的书写顺序。"""
    return [(ind, v) for ind in INDICATORS for v in VARIANTS[ind]]
