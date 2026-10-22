"""指标的性质判据（对任何 ``fn(ohlcv) -> {line: list}`` 都适用，别的系统可以拿来测自己的实现）。

- ``check_causality``：篡改 i >= cut 的输入，输出 [0, cut) 必须不变（容差 0：同一函数同一前缀的两次计算，
  与平台无关）；
- ``check_length``：每条输出线长度 == 输入长度（含 n = 0）；
- ``expected_warmup``：每个指标 / 变体在“非退化”数据上的前导预热点个数（闭式公式）；
- ``constant_expectation``：常数输入的闭式解；
- ``check_against``：两份实现逐点对拍（默认相对容差 1e-9）。
每个函数返回 ``{"ok": bool, ...}``，失败时带第一处不一致。
"""
from __future__ import annotations

import random

from qdcheck import compare
from qdcheck.indicators_ref import LINES, STOCHRSI_FLAT, parse_variant, resolve

_COLS = ("open", "high", "low", "close", "volume")


def _lines(out):
    return out if isinstance(out, dict) else {"value": out}


def tamper(ohlcv, cut, seed):
    """返回副本：i >= cut 的 K 线换成随机正值（仍满足 low <= open/close <= high，volume >= 0）。"""
    rnd = random.Random(seed).random
    d = {k: list(ohlcv[k]) for k in _COLS}
    for i in range(cut, len(d["close"])):
        o, c = 1.0 + 300.0 * rnd(), 1.0 + 300.0 * rnd()
        d["open"][i], d["close"][i] = o, c
        d["high"][i] = max(o, c) * (1.0 + 0.05 * rnd())
        d["low"][i] = min(o, c) * (1.0 - 0.05 * rnd())
        d["volume"][i] = 5000.0 * rnd()
    return d


def check_causality(fn, ohlcv, cuts, seed=0, exempt=()):
    """exempt：按定义非因果的输出线名（例如前移口径的 chikou），跳过。"""
    base = _lines(fn(ohlcv))
    checked = 0
    for k, cut in enumerate(cuts):
        moved = _lines(fn(tamper(ohlcv, cut, seed * 1000 + k)))
        for line, vals in base.items():
            if line in exempt:
                continue
            res = compare.compare_series(vals[:cut], moved[line][:cut], 0.0, 0.0, "none")
            if not res["ok"]:
                return {"ok": False, "line": line, "cut": cut, "first": res}
            checked += 1
    return {"ok": True, "checked": checked}


def check_length(fn, make_input, lengths):
    for n in lengths:
        out = _lines(fn(make_input(n)))
        for line, vals in out.items():
            if len(vals) != n:
                return {"ok": False, "n": n, "line": line, "got": len(vals)}
    return {"ok": True, "checked": len(lengths)}


def check_against(out_a, out_b, rel_tol=compare.REL_TOL, abs_tol=compare.ABS_TOL):
    a, b = _lines(out_a), _lines(out_b)
    if set(a) != set(b):
        return {"ok": False, "reason": "lines", "a": sorted(a), "b": sorted(b)}
    worst = 0.0
    for line in a:
        res = compare.compare_series(a[line], b[line], rel_tol, abs_tol, "none")
        if not res["ok"]:
            return {"ok": False, "line": line, "first": res}
        worst = max(worst, res["max_tol_ratio"])
    return {"ok": True, "max_tol_ratio": worst}


# ---- 预热期公式 --------------------------------------------------------------------
def _ma_lag(knobs, period):
    """单层平滑在“从第一个有效值开始”的序列上额外增加的预热点数。"""
    if knobs.get("ma") == "sma" or knobs.get("seed", "sma") == "sma":
        return period - 1
    return 0


def expected_warmup(indicator, variant, params=None):
    """非退化数据（无平盘、无零极差）上每条线的前导 null 个数。"""
    _v, knobs, p = resolve(indicator, variant, params)
    if indicator in ("dema", "tema", "trix"):
        layers = {"dema": 2, "tema": 3, "trix": 3}[indicator]
        w = layers * _ma_lag(knobs, p["period"]) + (1 if indicator == "trix" else 0)
        return {"value": w}
    if indicator == "stochrsi":
        k = p["rsi_period"] + p["stoch_period"] - 1 + p["k_period"] - 1
        return {"k": k, "d": k + p["d_period"] - 1}
    if indicator == "cci":
        return {"value": p["period"] - 1}
    if indicator == "ao":
        return {"value": max(p["fast"], p["slow"]) - 1}
    if indicator == "force":
        if knobs["ma"] == "raw":
            return {"value": 1}
        return {"value": 1 + _ma_lag(knobs, p["period"])}
    if indicator == "kdj":
        k = p["n"] - 1 + (p["m1"] - 1 if knobs["ma"] == "sma" else 0)
        d = k + (p["m2"] - 1 if knobs["ma"] == "sma" else 0)
        return {"k": k, "d": d, "j": d}
    off = {"forward": p["displacement"], "forward_tv": p["displacement"] - 1, "none": 0}[knobs["shift"]]
    return {"tenkan": p["tenkan"] - 1, "kijun": p["kijun"] - 1,
            "senkou_a": max(p["tenkan"], p["kijun"]) - 1 + off, "senkou_b": p["senkou_b"] - 1 + off, "chikou": 0}


def leading_nulls(vals):
    k = 0
    while k < len(vals) and compare.is_missing(vals[k]):
        k += 1
    return k


# ---- 常数输入的闭式解 --------------------------------------------------------------
def _kdj_const(knobs, p, n_bars, t, line):
    """常数输入（H=L=C）下 KDJ 第 t 点的闭式值；预热返回 None。"""
    rsv = 50.0 if knobs["zero_range"] == "half" else 0.0
    w = expected_warmup("kdj", ";".join(f"{k}={v}" for k, v in sorted(knobs.items())), p)[line]
    if t < w:
        return None
    if knobs["ma"] == "sma" or knobs["init"] == "first" or rsv == 50.0:
        return rsv
    a = 1.0 / p["m1"] if knobs["ma"] == "rma" else 2.0 / (p["m1"] + 1)
    m = t - (p["n"] - 1)                         # K 的第 m 个点（从 0 起）
    q = 1.0 - a
    k_val = 50.0 * q ** (m + 1)
    d_val = 50.0 * q ** (m + 1) * (1.0 + a * (m + 1))   # m1 == m2 时 D 的闭式（见 REPORT）
    return {"k": k_val, "d": d_val, "j": 3.0 * k_val - 2.0 * d_val}[line]


def constant_expectation(indicator, variant, params, n_bars, c):
    """常数输入（OHLC = c，volume > 0）时每条线的期望值列表（None = 无值）。"""
    v_canon, knobs, p = resolve(indicator, variant, params)
    warm = expected_warmup(indicator, v_canon, p)
    out = {}
    for line in LINES[indicator]:
        w = warm[line]
        if indicator in ("dema", "tema") or indicator == "ichimoku":
            val = c
        elif indicator == "cci":
            val = 0.0 if knobs["zero_dev"] == "zero" else None
        elif indicator == "stochrsi":
            rsi_flat, stoch_flat = STOCHRSI_FLAT[knobs["flat"]]
            val = None if rsi_flat is None or stoch_flat is None else stoch_flat / 100.0 * p["scale"]
        elif indicator == "kdj":
            if p["m1"] != p["m2"]:
                raise ValueError("closed form for kdj implemented for m1 == m2 only")
            out[line] = [_kdj_const(knobs, p, n_bars, t, line) for t in range(n_bars)]
            continue
        else:                                    # trix / ao / force
            val = 0.0
        vals = [None if t < w else val for t in range(n_bars)]
        if indicator == "ichimoku" and line == "chikou":
            off = {"forward": p["displacement"], "forward_tv": p["displacement"] - 1, "none": 0}[knobs["shift"]]
            vals = [c if t + off < n_bars else None for t in range(n_bars)]
        out[line] = vals
    return out


def chikou_exempt(indicator, variant):
    """前移口径的迟行线按定义是 close[t + 位移]，不参与因果性判据（改用位移判据）。"""
    if indicator != "ichimoku":
        return ()
    return ("chikou",) if parse_variant(variant)["shift"] != "none" else ()
