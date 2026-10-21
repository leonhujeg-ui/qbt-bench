"""风险与收益度量：总收益、年化、夏普、索提诺、最大回撤与回撤时长、胜率、盈亏比、Calmar、VaR/CVaR、月度收益表。

约定
- 输入权益曲线 equity[i] 为第 i 根 bar 收盘后的账户权益；returns[i] = equity[i]/equity[i-1] - 1（i>=1）。
- 年化按 bar_seconds 换算：periods_per_year = 365.25*86400 / bar_seconds。
- 夏普/索提诺的分母为 0 时返回 0.0；无亏损交易时盈亏比为 None（JSON 里无法表示 inf）。
- VaR/CVaR 用历史模拟法（经验分位数，取“下分位”顺序统计量），默认 alpha=0.05，正数表示损失。
"""
from __future__ import annotations

import math
from qbt.data import SECONDS_PER_YEAR


def returns_from_equity(equity):
    out = []
    for i in range(1, len(equity)):
        prev = equity[i - 1]
        out.append(equity[i] / prev - 1.0 if prev > 0 else 0.0)
    return out


def periods_per_year(bar_seconds):
    return SECONDS_PER_YEAR / bar_seconds


def total_return(equity):
    if len(equity) < 1 or equity[0] <= 0:
        return 0.0
    return equity[-1] / equity[0] - 1.0


def cagr(equity, bar_seconds):
    """复合年化收益；权益归零或为负返回 -1.0；不足 1 根 bar 返回 0.0。"""
    n = len(equity)
    if n < 2 or equity[0] <= 0:
        return 0.0
    if equity[-1] <= 0:
        return -1.0
    years = (n - 1) * bar_seconds / SECONDS_PER_YEAR
    ratio = equity[-1] / equity[0]
    if ratio <= 0.0:                                              # 比值下溢到 0：年化 -100%
        return -1.0
    if ratio != ratio:
        return ratio                                              # NaN 输入透传 NaN，不抛异常
    growth = math.log(ratio) / years                              # 用对数避免 x**(1/years) 溢出
    if growth > 700.0:                                            # exp(700) ~ 1e304：截断为有限大数，保持 JSON 可序列化
        return 1e300
    return math.exp(growth) - 1.0


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs, ddof=1):
    """样本标准差；常数序列的浮点残差（均值舍入 ~eps*|x|）视为零波动，避免除以 1e-17。"""
    n = len(xs)
    if n - ddof <= 0:
        return 0.0
    m = _mean(xs)
    var = sum((x - m) * (x - m) for x in xs) / (n - ddof)        # 用乘法而非 **：float 溢出得到 inf 而不是 OverflowError
    scale = 1e-14 * max(abs(x) for x in xs)
    return math.sqrt(var) if var > scale * scale else 0.0


def sharpe(returns, ppy, rf=0.0):
    """年化夏普：mean(r - rf/ppy) / std(r, ddof=1) * sqrt(ppy)。"""
    if len(returns) < 2:
        return 0.0
    ex = [r - rf / ppy for r in returns]
    sd = _std(ex, 1)
    return _mean(ex) / sd * math.sqrt(ppy) if sd > 0 else 0.0


def sortino(returns, ppy, rf=0.0):
    """年化索提诺：下行偏差 = sqrt(mean(min(r - rf/ppy, 0)^2))（对全部样本取均值）。"""
    if len(returns) < 2:
        return 0.0
    ex = [r - rf / ppy for r in returns]
    dd = math.sqrt(sum(min(x, 0.0) * min(x, 0.0) for x in ex) / len(ex))
    return _mean(ex) / dd * math.sqrt(ppy) if dd > 0 else 0.0


def max_drawdown(equity):
    """返回 dict：mdd（>=0 比例）、peak_idx、trough_idx、duration_bars（峰值到恢复或到末尾）、recovered。"""
    peak, peak_idx = -math.inf, 0
    mdd, mdd_peak, mdd_trough = 0.0, 0, 0
    for i, v in enumerate(equity):
        if v > peak:
            peak, peak_idx = v, i
        dd = 1.0 - v / peak if peak > 0 else (0.0 if v >= peak else 1.0)
        if dd > mdd:
            mdd, mdd_peak, mdd_trough = dd, peak_idx, i
    recovered, end = False, len(equity) - 1
    for j in range(mdd_trough + 1, len(equity)):
        if equity[j] >= equity[mdd_peak]:
            recovered, end = True, j
            break
    return {"mdd": mdd, "peak_idx": mdd_peak, "trough_idx": mdd_trough,
            "duration_bars": (end - mdd_peak) if mdd > 0 else 0, "recovered": recovered}


def longest_drawdown_bars(equity):
    """最长水下时长：从峰值 bar 到回到峰值的 bar（或到末尾）的根数；与 max_drawdown 的 duration 口径一致。"""
    peak, start, longest, under = -math.inf, 0, 0, False
    for i, v in enumerate(equity):
        if v >= peak:
            if under:
                longest = max(longest, i - start)
                under = False
            peak, start = v, i
        else:
            under = True
            longest = max(longest, i - start)
    return longest


def trade_stats(trades):
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit, gross_loss = sum(wins), -sum(losses)
    return {
        "n_trades": len(pnls),
        "win_rate": (len(wins) / len(pnls)) if pnls else 0.0,
        "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else None,
        "avg_win": _mean(wins), "avg_loss": _mean(losses),
        "expectancy": _mean(pnls),
        "avg_bars": _mean([t["bars"] for t in trades]),
        "gross_profit": gross_profit, "gross_loss": gross_loss,
    }


def calmar(cagr_value, mdd):
    return (cagr_value / mdd) if mdd > 0 else None


def var_cvar(returns, alpha=0.05):
    """历史 VaR/CVaR（正数 = 损失）。k = ceil(alpha*n) - 1 为下分位顺序统计量下标。"""
    n = len(returns)
    if n == 0:
        return 0.0, 0.0
    s = sorted(returns)
    k = max(0, min(n - 1, math.ceil(alpha * n) - 1))
    var = -s[k]
    cvar = -sum(s[: k + 1]) / (k + 1)
    return var, cvar


def _civil_from_days(days):
    """UTC 天序号（自 1970-01-01）-> (year, month)，Howard Hinnant 的整数算法，无 datetime 开销。"""
    z = days + 719468
    era = (z if z >= 0 else z - 146096) // 146097
    doe = z - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    y = yoe + era * 400
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    m = mp + 3 if mp < 10 else mp - 9
    return (y + 1 if m <= 2 else y, m)


def _day_key(ts):
    return int(ts) // 86400


def _month_key(ts):
    return _civil_from_days(int(ts) // 86400)


def period_returns(equity, ts, key_fn):
    """按 key_fn(ts) 分组，取每组最后一个权益，组间收益 = 本组末 / 上组末 - 1；首组相对首个权益。
    分组键只在“天序号变化”时重新计算（同一天内的 bar 直接沿用），1M 根 1 分钟 bar 只需 ~700 次键计算。"""
    if not equity:
        return []
    groups = []
    last_day, key = None, None
    for e, t in zip(equity, ts):
        day = int(t) // 86400
        if day != last_day:
            last_day, key = day, key_fn(t)
        if groups and groups[-1][0] == key:
            groups[-1][1] = e
        else:
            groups.append([key, e])
    out, prev = [], equity[0]
    for k, e in groups:
        out.append((k, e / prev - 1.0 if prev > 0 else 0.0))
        prev = e
    return out


def monthly_returns(equity, ts):
    return [{"year": k[0], "month": k[1], "ret": r} for k, r in period_returns(equity, ts, _month_key)]


def daily_returns(equity, ts):
    return [r for _k, r in period_returns(equity, ts, _day_key)]


def compute(equity, ts, bar_seconds, trades=(), rf=0.0, alpha=0.05):
    """汇总全部指标为 JSON 可序列化 dict。"""
    rets = returns_from_equity(equity)
    ppy = periods_per_year(bar_seconds)
    dd = max_drawdown(equity)
    c = cagr(equity, bar_seconds)
    var, cvar = var_cvar(rets, alpha)
    daily = daily_returns(equity, ts)
    dvar, dcvar = var_cvar(daily, alpha)
    bars_per_day = 86400.0 / bar_seconds
    out = {
        "bars": len(equity), "start_equity": equity[0] if equity else 0.0, "end_equity": equity[-1] if equity else 0.0,
        "total_return": total_return(equity), "cagr": c,
        "sharpe": sharpe(rets, ppy, rf), "sortino": sortino(rets, ppy, rf),
        "max_drawdown": dd["mdd"], "max_drawdown_bars": dd["duration_bars"],
        "max_drawdown_days": dd["duration_bars"] / bars_per_day, "drawdown_recovered": dd["recovered"],
        "longest_drawdown_bars": longest_drawdown_bars(equity),
        "calmar": calmar(c, dd["mdd"]),
        "var_bar": var, "cvar_bar": cvar, "var_daily": dvar, "cvar_daily": dcvar, "alpha": alpha,
        "volatility_annual": _std(rets, 1) * math.sqrt(ppy) if len(rets) > 1 else 0.0,
        "monthly": monthly_returns(equity, ts),
    }
    out.update(trade_stats(list(trades)))
    return out
