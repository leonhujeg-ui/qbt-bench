"""自检用示例（也是给别的系统看的用法样例）。

G2：``BACKTEST_SCHEMA`` + 一个写对了的 ``validate_good`` + 三个故意写坏的 validate：
    validate_keyerror     读必填键前不检查是否存在 -> KeyError
    validate_zerodiv      周期下界写成 >= 0（应为 >= 1），随后 10000 // period -> ZeroDivisionError
    validate_recursion    先用递归逐字符检查 symbol、后检查长度 -> 超长字符串上 RecursionError
受控错误一律是 ``ValidationError``。
"""
from __future__ import annotations

import math

BACKTEST_SCHEMA = {"type": "object", "fields": {
    "symbol": {"type": "str", "min_len": 1, "max_len": 16, "charset": "ascii_printable"},
    "period": {"type": "int", "min": 1, "max": 500},
    "side": {"type": "enum", "choices": ["long", "short", "both"]},
    "fee_rate": {"type": "float", "min": 0.0, "max": 0.01, "required": False},
    "leverage": {"type": "float", "min": 1.0, "max": 20.0, "required": False},
    "use_stop": {"type": "bool", "required": False},
    "symbols": {"type": "list", "required": False, "min_len": 0, "max_len": 4,
                "items": {"type": "str", "min_len": 1, "max_len": 8, "charset": "ascii_printable"}},
    "risk": {"type": "object", "required": False, "fields": {
        "max_drawdown": {"type": "float", "min": 0.0, "max": 1.0},
        "stop_loss": {"type": "float", "min": 0.0, "max": 0.5, "required": False},
        "window": {"type": "int", "min": 1, "max": 1000, "required": False}}},
}}
_TOP = ("symbol", "period", "side", "fee_rate", "leverage", "use_stop", "symbols", "risk")
_RISK = ("max_drawdown", "stop_loss", "window")


class ValidationError(Exception):
    """受控错误：参数不合法。"""


def _keys(d, allowed, where):
    if not isinstance(d, dict):
        raise ValidationError(f"{where} must be an object")
    for k in d:
        if not isinstance(k, str) or k not in allowed:
            raise ValidationError(f"{where}: unexpected key {k!r}"[:100])


def _int(v, lo, hi, name):
    if not isinstance(v, int) or isinstance(v, bool):
        raise ValidationError(f"{name} must be an int")
    if not lo <= v <= hi:
        raise ValidationError(f"{name} must be in [{lo}, {hi}]")
    return v


def _num(v, lo, hi, name):
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        raise ValidationError(f"{name} must be a number")
    if isinstance(v, float) and not math.isfinite(v):
        raise ValidationError(f"{name} must be finite")
    if not lo <= v <= hi:
        raise ValidationError(f"{name} must be in [{lo}, {hi}]")
    return float(v) if abs(v) < 1e300 else v


def _str(v, lo, hi, name):
    if not isinstance(v, str):
        raise ValidationError(f"{name} must be a str")
    if not lo <= len(v) <= hi:
        raise ValidationError(f"{name} length must be in [{lo}, {hi}]")
    if not (v.isascii() and v.isprintable()):
        raise ValidationError(f"{name} must be printable ASCII")
    return v


def _require(d, key):
    if key not in d:
        raise ValidationError(f"missing key {key!r}")
    return d[key]


def _rest(cfg, out):
    """side 及全部可选字段（四个 validate 共用的“写对了”的部分）。"""
    side = _require(cfg, "side")
    if not isinstance(side, str) or side not in ("long", "short", "both"):
        raise ValidationError("side must be one of long/short/both")
    out["side"] = side
    if "fee_rate" in cfg:
        out["fee_rate"] = _num(cfg["fee_rate"], 0.0, 0.01, "fee_rate")
    if "leverage" in cfg:
        out["leverage"] = _num(cfg["leverage"], 1.0, 20.0, "leverage")
    if "use_stop" in cfg and not isinstance(cfg["use_stop"], bool):
        raise ValidationError("use_stop must be a bool")
    if "symbols" in cfg:
        syms = cfg["symbols"]
        if not isinstance(syms, list) or len(syms) > 4:
            raise ValidationError("symbols must be a list of at most 4")
        out["symbols"] = [_str(s, 1, 8, "symbols[]") for s in syms]
    if "risk" in cfg:
        risk = cfg["risk"]
        _keys(risk, _RISK, "risk")
        out["risk"] = {"max_drawdown": _num(_require(risk, "max_drawdown"), 0.0, 1.0, "risk.max_drawdown")}
        if "stop_loss" in risk:
            out["risk"]["stop_loss"] = _num(risk["stop_loss"], 0.0, 0.5, "risk.stop_loss")
        if "window" in risk:
            out["risk"]["window"] = _int(risk["window"], 1, 1000, "risk.window")
    return out


def validate_good(cfg):
    """写对了的 validate：任何输入只会返回规范化 dict 或抛 ValidationError。"""
    _keys(cfg, _TOP, "config")
    out = {"symbol": _str(_require(cfg, "symbol"), 1, 16, "symbol"),
           "period": _int(_require(cfg, "period"), 1, 500, "period")}
    return _rest(cfg, out)


def validate_keyerror(cfg):
    """坏例 1：直接下标取必填键。"""
    _keys(cfg, _TOP, "config")
    out = {"period": _int(cfg["period"], 1, 500, "period"), "symbol": _str(cfg["symbol"], 1, 16, "symbol")}
    return _rest(cfg, out)


def validate_zerodiv(cfg):
    """坏例 2：下界 off-by-one，period == 0 时整除报 ZeroDivisionError。"""
    if not isinstance(cfg, dict):
        raise ValidationError("config must be an object")
    period = _require(cfg, "period")
    if not isinstance(period, int) or isinstance(period, bool) or period < 0 or period > 500:
        raise ValidationError("period must be an int in [0, 500]")
    bars_per_period = 10000 // period
    _keys(cfg, _TOP, "config")
    out = {"period": period, "bars_per_period": bars_per_period,
           "symbol": _str(_require(cfg, "symbol"), 1, 16, "symbol")}
    return _rest(cfg, out)


def _all_printable(s, i=0):
    if i == len(s):
        return True
    return s[i].isascii() and s[i].isprintable() and _all_printable(s, i + 1)


def validate_recursion(cfg):
    """坏例 3：递归逐字符检查，且在长度检查之前。"""
    if not isinstance(cfg, dict):
        raise ValidationError("config must be an object")
    sym = _require(cfg, "symbol")
    if not isinstance(sym, str) or not _all_printable(sym):
        raise ValidationError("symbol must be printable ASCII")
    _keys(cfg, _TOP, "config")
    out = {"symbol": _str(sym, 1, 16, "symbol"), "period": _int(_require(cfg, "period"), 1, 500, "period")}
    return _rest(cfg, out)
