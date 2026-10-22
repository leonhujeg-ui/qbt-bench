"""自检用示例（也是给别的系统看的用法样例）。

G2：``BACKTEST_SCHEMA`` + 一个写对了的 ``validate_good`` + 三个故意写坏的 validate：
    validate_keyerror     读必填键前不检查是否存在 -> KeyError
    validate_zerodiv      周期下界写成 >= 0（应为 >= 1），随后 10000 // period -> ZeroDivisionError
    validate_recursion    先用递归逐字符检查 symbol、后检查长度 -> 超长字符串上 RecursionError
受控错误一律是 ``ValidationError``。

G4：determinism harness 的被测对象（全部接收一个可 JSON 序列化的 task dict）：
    set_order_engine      用 set 存“本根有信号的标的”并按 set 迭代顺序依次下单、先到先得地占用现金
                          -> 成交顺序 / 数量随 PYTHONHASHSEED 变化（harness 必须抓到）
    deterministic_engine  同一引擎，只是按 sorted(set) 迭代（harness 必须通过）
    noisy_engine          确定性引擎 + 与哈希相关的相对噪声 task["noise"]（1e-12 应通过，1e-6 应抓到）
    qbt_backtest          本仓 qbt 回测引擎的适配器（真实用例）
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


# ---- G4：determinism harness 的被测对象 ---------------------------------------------------
SYMBOLS = ("BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "DOT", "LTC", "AVAX", "LINK")


def _toy_prices(task):
    import random
    rnd = random.Random(task.get("seed", 1))
    syms = list(task.get("symbols", SYMBOLS))
    prices = {s: [100.0] for s in syms}
    for _ in range(task.get("n_bars", 60) - 1):
        for s in syms:
            prices[s].append(prices[s][-1] * (1.0 + 0.02 * (rnd.random() - 0.5)))
    return syms, prices


def _toy_engine(task, order):
    """极简多标的引擎：每根对“上涨的标的”按 order(信号集合) 的顺序买入，现金先到先得。"""
    syms, prices = _toy_prices(task)
    cash, held, trades = float(task.get("cash", 1000.0)), {s: 0 for s in syms}, []
    for t in range(1, task.get("n_bars", 60)):
        signals = {s for s in syms if prices[s][t] > prices[s][t - 1]}
        for s in order(signals):
            px = prices[s][t]
            qty = int(min(cash * 0.3, 400.0) // px)
            if qty <= 0:
                continue
            cash -= qty * px
            held[s] += qty
            trades.append({"bar": t, "symbol": s, "qty": qty, "price": px})
        for s in syms:                                   # 跌了就清仓
            if held[s] and prices[s][t] < prices[s][t - 1]:
                cash += held[s] * prices[s][t]
                trades.append({"bar": t, "symbol": s, "qty": -held[s], "price": prices[s][t]})
                held[s] = 0
    equity = cash + sum(held[s] * prices[s][-1] for s in syms)
    return {"trades": trades, "final_cash": cash, "final_equity": equity, "n_trades": len(trades)}


def set_order_engine(task):
    return _toy_engine(task, lambda signals: signals)                  # 故意：按 set 的迭代顺序


def deterministic_engine(task):
    return _toy_engine(task, sorted)


def noisy_engine(task):
    out = _toy_engine(task, sorted)
    eps = float(task.get("noise", 0.0)) * (hash("noise-probe") % 1000) / 1000.0
    out["final_equity"] *= 1.0 + eps
    for tr in out["trades"]:
        tr["price"] *= 1.0 + eps
    return out


def qbt_backtest(task):
    """真实用例：task = {"strategy", "params", "n", "seed", "broker": {...}, "data": {...}, "every": k}。"""
    from qbt.broker import BrokerConfig
    from qbt.data import generate
    from qbt.engine import run_backtest
    from strategies import REGISTRY
    data = generate(n=task.get("n", 5000), seed=task.get("seed", 1), **task.get("data", {}))
    res = run_backtest(data, REGISTRY[task.get("strategy", "ma_cross")](**task.get("params", {})),
                       BrokerConfig(**task.get("broker", {})))
    every = task.get("every", 50)
    return {"final_equity": res.final_equity, "equity_sampled": res.equity[::every],
            "fills": [{"bar": f.bar, "side": f.side, "qty": f.qty, "price": f.price, "fee": f.fee, "tag": f.tag}
                      for f in res.fills],
            "trades": [list(t) if isinstance(t, tuple) else t for t in res.trades],
            "fees": res.fees, "funding": res.funding, "liquidations": res.liquidations, "orders": len(res.orders)}
