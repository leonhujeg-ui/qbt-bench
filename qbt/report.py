"""纯文本报告 + 标准库生成的 SVG 权益曲线（含回撤阴影）。"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone

from qbt import metrics as M

_METRIC_ROWS = [
    ("总收益", "total_return", "pct"), ("年化收益 (CAGR)", "cagr", "pct"), ("年化波动", "volatility_annual", "pct"),
    ("夏普", "sharpe", "f2"), ("索提诺", "sortino", "f2"), ("Calmar", "calmar", "f2"),
    ("最大回撤", "max_drawdown", "pct"), ("最大回撤时长 (bar)", "max_drawdown_bars", "int"),
    ("最大回撤时长 (天)", "max_drawdown_days", "f2"), ("最长水下 (bar)", "longest_drawdown_bars", "int"),
    ("VaR 95% (单根)", "var_bar", "pct"), ("CVaR 95% (单根)", "cvar_bar", "pct"),
    ("VaR 95% (日)", "var_daily", "pct"), ("CVaR 95% (日)", "cvar_daily", "pct"),
    ("交易次数", "n_trades", "int"), ("胜率", "win_rate", "pct"), ("盈亏比 (PF)", "profit_factor", "f2"),
    ("平均盈利", "avg_win", "f2"), ("平均亏损", "avg_loss", "f2"), ("期望 / 笔", "expectancy", "f2"),
    ("平均持有 (bar)", "avg_bars", "f1"),
]


def fmt(v, kind):
    if v is None:
        return "n/a"
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return "n/a"
    if kind == "pct":
        return f"{100.0 * v:.2f}%"
    if kind == "int":
        return f"{int(v)}"
    if kind == "f1":
        return f"{v:.1f}"
    return f"{v:.2f}"


def _date(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def text_report(result, m=None, title="qbt backtest report"):
    """生成纯文本报告：参数、账户、指标表、月度收益表。"""
    m = m or M.compute(result.equity, result.ts, result.bar_seconds, result.trades)
    lines = [title, "=" * len(title), ""]
    for p in result.params:
        lines.append("策略: " + json.dumps(p, ensure_ascii=False, sort_keys=True))
    if result.ts:
        lines.append(f"区间: {_date(result.ts[0])} .. {_date(result.ts[-1])}  ({result.n} bars, bar={result.bar_seconds}s)")
    lines += [f"初始资金: {result.initial_cash:,.2f}   期末权益: {result.final_equity:,.2f}",
              f"成交: {len(result.fills)}   订单: {len(result.orders)}   手续费: {result.fees:,.2f}   "
              f"资金费: {result.funding:,.2f}   强平: {result.liquidations}   破产: {result.bankrupt}", "",
              "指标", "-" * 40]
    for label, key, kind in _METRIC_ROWS:
        lines.append(f"{label:<22s} {fmt(m.get(key), kind):>14s}")
    lines += ["", "月度收益", "-" * 40, _monthly_table(m.get("monthly", []))]
    return "\n".join(lines) + "\n"


def _monthly_table(monthly):
    by_year = {}
    for row in monthly:
        by_year.setdefault(row["year"], {})[row["month"]] = row["ret"]
    header = "年份  " + " ".join(f"{k:>7d}" for k in range(1, 13))
    out = [header]
    for year in sorted(by_year):
        cells = [f"{100 * by_year[year][k]:7.2f}" if k in by_year[year] else f"{'':>7s}" for k in range(1, 13)]
        out.append(f"{year}  " + " ".join(cells))
    return "\n".join(out) if monthly else "(无)"


def equity_svg(equity, ts=None, width=960, height=420, title="Equity"):
    """SVG：权益折线（上 70%）+ 回撤面积（下 30%），坐标轴刻度。纯字符串拼接，确定性输出。"""
    n = len(equity)
    pad_l, pad_r, pad_t, pad_b = 70, 20, 30, 30
    w, h = width - pad_l - pad_r, height - pad_t - pad_b
    h_eq, h_dd = int(h * 0.68), h - int(h * 0.68) - 10
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
             f'<rect width="{width}" height="{height}" fill="white"/>',
             f'<text x="{pad_l}" y="20" font-family="monospace" font-size="14">{title}</text>']
    if n == 0:
        parts.append(f'<text x="{pad_l}" y="{height // 2}" font-family="monospace" font-size="12">no data</text></svg>')
        return "\n".join(parts)
    lo, hi = min(equity), max(equity)
    if hi == lo:
        hi = lo + 1.0
    sx = w / max(n - 1, 1)
    pts = [f"{pad_l + i * sx:.1f},{pad_t + (hi - v) / (hi - lo) * h_eq:.1f}" for i, v in enumerate(equity)]
    peak, dd = -math.inf, []
    for v in equity:
        peak = v if v > peak else peak
        dd.append(1.0 - v / peak if peak > 0 else 0.0)
    max_dd = max(dd) or 1.0
    y0 = pad_t + h_eq + 10
    area = [f"{pad_l:.1f},{y0:.1f}"] + [f"{pad_l + i * sx:.1f},{y0 + d / max_dd * h_dd:.1f}" for i, d in enumerate(dd)]
    area.append(f"{pad_l + (n - 1) * sx:.1f},{y0:.1f}")
    parts.append(f'<polygon points="{" ".join(area)}" fill="#e57373" fill-opacity="0.6" stroke="none"/>')
    parts.append(f'<polyline points="{" ".join(pts)}" fill="none" stroke="#1565c0" stroke-width="1.2"/>')
    parts.extend(_axes(equity, ts, lo, hi, pad_l, pad_t, w, h_eq, y0, h_dd, max_dd, n, sx))
    parts.append("</svg>")
    return "\n".join(parts)


def _axes(equity, ts, lo, hi, pad_l, pad_t, w, h_eq, y0, h_dd, max_dd, n, sx):
    out = [f'<line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{y0 + h_dd}" stroke="#444"/>',
           f'<line x1="{pad_l}" y1="{pad_t + h_eq}" x2="{pad_l + w}" y2="{pad_t + h_eq}" stroke="#444"/>']
    for k in range(5):
        v = lo + (hi - lo) * k / 4
        y = pad_t + (hi - v) / (hi - lo) * h_eq
        out.append(f'<text x="{pad_l - 6}" y="{y + 4:.1f}" text-anchor="end" font-family="monospace" font-size="10">{v:,.0f}</text>')
        out.append(f'<line x1="{pad_l - 3}" y1="{y:.1f}" x2="{pad_l}" y2="{y:.1f}" stroke="#444"/>')
    out.append(f'<text x="{pad_l - 6}" y="{y0 + h_dd + 4}" text-anchor="end" font-family="monospace" font-size="10">'
               f'-{100 * max_dd:.1f}%</text>')
    ticks = min(6, n)
    for k in range(ticks):
        i = round((n - 1) * k / max(ticks - 1, 1))
        x = pad_l + i * sx
        label = _date(ts[i]) if ts else str(i)
        out.append(f'<line x1="{x:.1f}" y1="{pad_t + h_eq}" x2="{x:.1f}" y2="{pad_t + h_eq + 4}" stroke="#444"/>')
        out.append(f'<text x="{x:.1f}" y="{pad_t + h_eq + 15}" text-anchor="middle" font-family="monospace" font-size="10">{label}</text>')
    return out


def write_report(result, out_dir, name, title=None):
    """写出 <name>.txt / <name>.svg / <name>.json，返回路径 dict。"""
    os.makedirs(out_dir, exist_ok=True)
    m = M.compute(result.equity, result.ts, result.bar_seconds, result.trades)
    paths = {k: os.path.join(out_dir, f"{name}.{k}") for k in ("txt", "svg", "json")}
    with open(paths["txt"], "w", encoding="utf-8") as f:
        f.write(text_report(result, m, title or f"qbt report: {name}"))
    with open(paths["svg"], "w", encoding="utf-8") as f:
        f.write(equity_svg(result.equity, result.ts, title=title or name))
    with open(paths["json"], "w", encoding="utf-8") as f:
        json.dump({"summary": result.summary(), "metrics": m}, f, indent=1, sort_keys=True, ensure_ascii=False)
    return paths
