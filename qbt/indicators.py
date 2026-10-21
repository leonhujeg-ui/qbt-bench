"""技术指标库：全部 O(n) 单遍或滚动窗口实现（滚动和、Wilder 递推、单调队列）。

约定
- 输入为等长 list[float]，输出为等长 list，预热期为 None（显式，不用 NaN 以免静默传播）。
- 每个函数都是因果的：输出 i 只依赖输入 [0, i]（Ichimoku 的先行/迟行线见函数说明）。
- 窗口参数 n 必须为正整数，否则抛 ConfigError。
"""
from __future__ import annotations

import math
from collections import deque

from qbt.errors import ConfigError


def _check_window(n, name="window"):
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        raise ConfigError(f"{name} must be a positive int, got {n!r}")


# ---- 移动平均族 --------------------------------------------------------------
def sma(x, n):
    """简单移动平均：滚动和，O(1)/bar。"""
    _check_window(n)
    out = [None] * len(x)
    s = 0.0
    for i, v in enumerate(x):
        s += v
        if i >= n:
            s -= x[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(x, n, alpha=None):
    """指数移动平均：alpha=2/(n+1)，以首值为种子。"""
    _check_window(n)
    a = 2.0 / (n + 1) if alpha is None else alpha
    out = [None] * len(x)
    if not x:
        return out
    prev = x[0]
    out[0] = prev
    for i in range(1, len(x)):
        prev = prev + a * (x[i] - prev)
        out[i] = prev
    return out


def wma(x, n):
    """线性加权移动平均：权重 1..n。维护 窗口和 s 与 加权和 w，w_new = w_old + n*x_new - s_old。"""
    _check_window(n)
    out = [None] * len(x)
    s = w = 0.0
    denom = n * (n + 1) / 2.0
    for i, v in enumerate(x):
        if i >= n:
            w = w - s + n * v
            s = s - x[i - n] + v
        else:
            w += (i + 1) * v
            s += v
        if i >= n - 1:
            out[i] = w / denom
    return out


def hma(x, n):
    """Hull MA = WMA(2*WMA(n/2) - WMA(n), sqrt(n))。"""
    _check_window(n)
    half, root = max(1, n // 2), max(1, int(math.sqrt(n)))
    a, b = wma(x, half), wma(x, n)
    diff = [None if (p is None or q is None) else 2.0 * p - q for p, q in zip(a, b)]
    out = [None] * len(x)
    start = next((i for i, v in enumerate(diff) if v is not None), len(x))
    if start < len(x):
        tail = wma(diff[start:], root)
        out[start:] = tail
    return out


def dema(x, n):
    e1 = ema(x, n)
    e2 = ema(e1, n)
    return [2.0 * a - b for a, b in zip(e1, e2)]


def tema(x, n):
    e1 = ema(x, n)
    e2 = ema(e1, n)
    e3 = ema(e2, n)
    return [3.0 * a - 3.0 * b + c for a, b, c in zip(e1, e2, e3)]


def kama(x, n=10, fast=2, slow=30):
    """Kaufman 自适应均线：ER = |x_i - x_{i-n}| / sum|dx|（滚动和）。"""
    _check_window(n)
    fast_sc, slow_sc = 2.0 / (fast + 1), 2.0 / (slow + 1)
    out = [None] * len(x)
    if not x:
        return out
    vol = 0.0
    prev = None
    for i in range(len(x)):
        if i >= 1:
            vol += abs(x[i] - x[i - 1])
        if i > n:
            vol -= abs(x[i - n] - x[i - n - 1])
        if i < n:
            continue
        er = abs(x[i] - x[i - n]) / vol if vol > 0 else 0.0
        sc = (er * (fast_sc - slow_sc) + slow_sc) ** 2
        prev = x[i] if prev is None else prev + sc * (x[i] - prev)
        out[i] = prev
    return out


# ---- 动量 / 振荡 ---------------------------------------------------------------
def rsi(x, n=14):
    """RSI，Wilder 平滑（首值用简单平均作种子）。"""
    _check_window(n)
    out = [None] * len(x)
    gain = loss = 0.0
    for i in range(1, len(x)):
        d = x[i] - x[i - 1]
        g, l = (d, 0.0) if d > 0 else (0.0, -d)
        if i <= n:
            gain += g
            loss += l
            if i == n:
                gain /= n
                loss /= n
                out[i] = _rsi_value(gain, loss)
        else:
            gain = (gain * (n - 1) + g) / n
            loss = (loss * (n - 1) + l) / n
            out[i] = _rsi_value(gain, loss)
    return out


def _rsi_value(gain, loss):
    if loss == 0.0:
        return 100.0 if gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + gain / loss)


def macd(x, fast=12, slow=26, signal=9):
    """返回 (macd, signal, hist)。"""
    ef, es = ema(x, fast), ema(x, slow)
    line = [a - b for a, b in zip(ef, es)]
    sig = ema(line, signal)
    hist = [a - b for a, b in zip(line, sig)]
    return line, sig, hist


def roc(x, n):
    """变动率：x_i / x_{i-n} - 1。"""
    _check_window(n)
    return [None if i < n or x[i - n] == 0 else x[i] / x[i - n] - 1.0 for i in range(len(x))]


def momentum(x, n):
    _check_window(n)
    return [None if i < n else x[i] - x[i - n] for i in range(len(x))]


def trix(x, n):
    e3 = ema(ema(ema(x, n), n), n)
    return [None if i < 1 or e3[i - 1] == 0 else e3[i] / e3[i - 1] - 1.0 for i in range(len(x))]


# ---- 滚动极值（单调队列，均摊 O(1)） ------------------------------------------------
def rolling_max(x, n):
    _check_window(n)
    out = [None] * len(x)
    dq = deque()
    for i, v in enumerate(x):
        while dq and x[dq[-1]] <= v:
            dq.pop()
        dq.append(i)
        if dq[0] <= i - n:
            dq.popleft()
        if i >= n - 1:
            out[i] = x[dq[0]]
    return out


def rolling_min(x, n):
    _check_window(n)
    out = [None] * len(x)
    dq = deque()
    for i, v in enumerate(x):
        while dq and x[dq[-1]] >= v:
            dq.pop()
        dq.append(i)
        if dq[0] <= i - n:
            dq.popleft()
        if i >= n - 1:
            out[i] = x[dq[0]]
    return out


def _rolling_argext(x, n, is_max):
    """返回窗口内极值的下标（最近者优先），用于 Aroon。"""
    out = [None] * len(x)
    dq = deque()
    for i, v in enumerate(x):
        while dq and ((x[dq[-1]] <= v) if is_max else (x[dq[-1]] >= v)):
            dq.pop()
        dq.append(i)
        if dq[0] <= i - n:
            dq.popleft()
        if i >= n - 1:
            out[i] = dq[0]
    return out


def donchian(high, low, n):
    """Donchian 通道：(upper, lower, middle)。"""
    up, lo = rolling_max(high, n), rolling_min(low, n)
    mid = [None if u is None else (u + l) / 2.0 for u, l in zip(up, lo)]
    return up, lo, mid


def stochastic(high, low, close, k=14, d=3):
    """随机指标 (%K, %D)，%K 在 [0,100]。"""
    hh, ll = rolling_max(high, k), rolling_min(low, k)
    kk = [None] * len(close)
    for i in range(len(close)):
        if hh[i] is None:
            continue
        rng = hh[i] - ll[i]
        kk[i] = 50.0 if rng == 0 else 100.0 * (close[i] - ll[i]) / rng
    return kk, _sma_skip_none(kk, d)


def williams_r(high, low, close, n=14):
    hh, ll = rolling_max(high, n), rolling_min(low, n)
    out = [None] * len(close)
    for i in range(len(close)):
        if hh[i] is None:
            continue
        rng = hh[i] - ll[i]
        out[i] = -50.0 if rng == 0 else -100.0 * (hh[i] - close[i]) / rng
    return out


def aroon(high, low, n=25):
    """Aroon up/down = 100 * (n - bars since window high/low) / n。"""
    _check_window(n)
    am, an = _rolling_argext(high, n, True), _rolling_argext(low, n, False)
    up = [None if j is None else 100.0 * (n - (i - j)) / n for i, j in enumerate(am)]
    dn = [None if j is None else 100.0 * (n - (i - j)) / n for i, j in enumerate(an)]
    return up, dn


def _sma_skip_none(x, n):
    """对带 None 预热的序列做 SMA：从首个非 None 处开始。"""
    out = [None] * len(x)
    start = next((i for i, v in enumerate(x) if v is not None), len(x))
    if start < len(x):
        out[start:] = sma(x[start:], n)
    return out


# ---- 波动 / 通道 ---------------------------------------------------------------
def true_range(high, low, close):
    out = [high[0] - low[0]] if close else []
    for i in range(1, len(close)):
        pc = close[i - 1]
        out.append(max(high[i] - low[i], abs(high[i] - pc), abs(low[i] - pc)))
    return out


def atr(high, low, close, n=14):
    """ATR：真实波幅的 Wilder 平滑（前 n 根简单平均作种子）。"""
    _check_window(n)
    tr = true_range(high, low, close)
    return _wilder(tr, n)


def _wilder(x, n):
    out = [None] * len(x)
    s = 0.0
    for i, v in enumerate(x):
        if i < n:
            s += v
            if i == n - 1:
                s /= n
                out[i] = s
        else:
            s = (s * (n - 1) + v) / n
            out[i] = s
    return out


def rolling_std(x, n, ddof=0):
    """滚动标准差（默认总体）。用 West 滑窗更新（均值 + 二阶中心矩），
    误差相对于方差本身而非 x^2，避免“平方和减均值平方”的灾难性消去。"""
    _check_window(n)
    out = [None] * len(x)
    if n - ddof <= 0:
        return out
    mean = m2 = 0.0
    for i, v in enumerate(x):
        if i < n:
            k = i + 1
            delta = v - mean
            mean += delta / k
            m2 += delta * (v - mean)
        elif (i + 1) % n == 0:          # 每 n 根从窗口精确重算，消除漂移（均摊 O(1)/bar）
            win = x[i - n + 1:i + 1]
            mean = sum(win) / n
            m2 = sum((w - mean) * (w - mean) for w in win)
        else:
            u = x[i - n]
            old_mean = mean
            mean += (v - u) / n
            m2 += (v - u) * (v - mean + u - old_mean)
        if i >= n - 1:
            var = m2 / (n - ddof)
            out[i] = math.sqrt(var) if var > 0 else 0.0
    return out


def bollinger(x, n=20, k=2.0):
    """布林带 (upper, middle, lower)，带宽对称：upper - mid == mid - lower。"""
    mid, sd = sma(x, n), rolling_std(x, n)
    up = [None if m is None else m + k * s for m, s in zip(mid, sd)]
    lo = [None if m is None else m - k * s for m, s in zip(mid, sd)]
    return up, mid, lo


def zscore(x, n=20):
    mid, sd = sma(x, n), rolling_std(x, n)
    return [None if m is None else ((v - m) / s if s > 0 else 0.0) for v, m, s in zip(x, mid, sd)]


def keltner(high, low, close, n=20, k=2.0, atr_n=10):
    """Keltner 通道：EMA(close) ± k * ATR。"""
    mid, a = ema(close, n), atr(high, low, close, atr_n)
    up = [None if v is None else m + k * v for m, v in zip(mid, a)]
    lo = [None if v is None else m - k * v for m, v in zip(mid, a)]
    return up, mid, lo


def adx(high, low, close, n=14):
    """ADX：返回 (adx, +DI, -DI)，全部在 [0,100]。"""
    _check_window(n)
    m = len(close)
    tr = true_range(high, low, close)
    plus_dm = [0.0] * m
    minus_dm = [0.0] * m
    for i in range(1, m):
        up, dn = high[i] - high[i - 1], low[i - 1] - low[i]
        if up > dn and up > 0:
            plus_dm[i] = up
        elif dn > up and dn > 0:
            minus_dm[i] = dn
    atr_s, pdm_s, mdm_s = _wilder(tr, n), _wilder(plus_dm, n), _wilder(minus_dm, n)
    pdi, mdi, dx = [None] * m, [None] * m, [None] * m
    for i in range(m):
        if atr_s[i] is None:
            continue
        a = atr_s[i]
        pdi[i] = 100.0 * pdm_s[i] / a if a > 0 else 0.0
        mdi[i] = 100.0 * mdm_s[i] / a if a > 0 else 0.0
        tot = pdi[i] + mdi[i]
        dx[i] = 100.0 * abs(pdi[i] - mdi[i]) / tot if tot > 0 else 0.0
    start = n - 1
    out = [None] * m
    if start < m:
        out[start:] = _wilder(dx[start:], n)
    return out, pdi, mdi


def cci(high, low, close, n=20):
    """CCI = (TP - SMA(TP)) / (0.015 * 平均绝对偏差)。平均绝对偏差围绕当前窗口均值，
    需要 O(n_window) 的内层循环（窗口有界，整体 O(N*w)），不做从头重算。"""
    _check_window(n)
    tp = [(h + l + c) / 3.0 for h, l, c in zip(high, low, close)]
    m = sma(tp, n)
    out = [None] * len(tp)
    for i in range(n - 1, len(tp)):
        mean = m[i]
        dev = 0.0
        for j in range(i - n + 1, i + 1):
            dev += abs(tp[j] - mean)
        dev /= n
        out[i] = (tp[i] - mean) / (0.015 * dev) if dev > 0 else 0.0
    return out


# ---- 成交量 --------------------------------------------------------------------
def obv(close, volume):
    out = [0.0] * len(close)
    for i in range(1, len(close)):
        if close[i] > close[i - 1]:
            out[i] = out[i - 1] + volume[i]
        elif close[i] < close[i - 1]:
            out[i] = out[i - 1] - volume[i]
        else:
            out[i] = out[i - 1]
    return out


def vwap(high, low, close, volume, n=None):
    """VWAP：n=None 为累计，否则滚动窗口。成交量和为 0 时输出 None。"""
    tp = [(h + l + c) / 3.0 for h, l, c in zip(high, low, close)]
    out = [None] * len(tp)
    pv = vv = 0.0
    for i in range(len(tp)):
        pv += tp[i] * volume[i]
        vv += volume[i]
        if n is not None:
            _check_window(n)
            if i >= n:
                pv -= tp[i - n] * volume[i - n]
                vv -= volume[i - n]
            if i < n - 1:
                continue
        out[i] = pv / vv if vv > 0 else None
    return out


def mfi(high, low, close, volume, n=14):
    """资金流量指数，[0,100]。"""
    _check_window(n)
    tp = [(h + l + c) / 3.0 for h, l, c in zip(high, low, close)]
    pos = [0.0] * len(tp)
    neg = [0.0] * len(tp)
    for i in range(1, len(tp)):
        flow = tp[i] * volume[i]
        if tp[i] > tp[i - 1]:
            pos[i] = flow
        elif tp[i] < tp[i - 1]:
            neg[i] = flow
    ps, ns = sma(pos, n), sma(neg, n)
    out = [None] * len(tp)
    for i in range(n, len(tp)):
        tot = ps[i] + ns[i]
        v = 100.0 * ps[i] / tot if tot > 0 else 50.0
        out[i] = max(0.0, min(100.0, v))      # 数学上有界，钳制滚动和的浮点噪声
    return out


def cmf(high, low, close, volume, n=20):
    """Chaikin Money Flow，[-1,1]。"""
    _check_window(n)
    mfv = []
    for h, l, c, v in zip(high, low, close, volume):
        rng = h - l
        mfv.append(((c - l) - (h - c)) / rng * v if rng > 0 else 0.0)
    a, b = sma(mfv, n), sma(volume, n)
    out = [None if p is None else (p / q if q > 0 else 0.0) for p, q in zip(a, b)]
    return [None if v is None else max(-1.0, min(1.0, v)) for v in out]


# ---- 趋势系统 -------------------------------------------------------------------
def ichimoku(high, low, close, tenkan_n=9, kijun_n=26, senkou_n=52, disp=26):
    """一目均衡表五线，全部因果化：
    senkou_a/b[i] = 在 i-disp 处算得的值（即“前移 disp 根”后落在 i 上的值）；
    chikou[i] = close[i] - close[i-disp]（迟行线与 disp 根前价格的差，>0 即“迟行线在价格上方”）。"""
    for n in (tenkan_n, kijun_n, senkou_n, disp):
        _check_window(n)
    tenkan = _midline(high, low, tenkan_n)
    kijun = _midline(high, low, kijun_n)
    raw_a = [None if (t is None or k is None) else (t + k) / 2.0 for t, k in zip(tenkan, kijun)]
    raw_b = _midline(high, low, senkou_n)
    m = len(close)
    senkou_a = [None] * disp + raw_a[:m - disp] if m > disp else [None] * m
    senkou_b = [None] * disp + raw_b[:m - disp] if m > disp else [None] * m
    chikou = [None if i < disp else close[i] - close[i - disp] for i in range(m)]
    return tenkan, kijun, senkou_a, senkou_b, chikou


def _midline(high, low, n):
    hh, ll = rolling_max(high, n), rolling_min(low, n)
    return [None if h is None else (h + l) / 2.0 for h, l in zip(hh, ll)]


def parabolic_sar(high, low, af0=0.02, af_step=0.02, af_max=0.2):
    """抛物线 SAR（Wilder），返回 (sar, trend) trend=+1 多 / -1 空。"""
    m = len(high)
    sar, trend = [None] * m, [None] * m
    if m < 2:
        return sar, trend
    up = high[1] > high[0]
    ep = high[1] if up else low[1]
    cur = low[0] if up else high[0]
    af = af0
    for i in range(1, m):
        cur = cur + af * (ep - cur)
        if up:
            cur = min(cur, low[i - 1], low[i - 2] if i >= 2 else low[i - 1])
            if low[i] < cur:
                up, cur, ep, af = False, ep, low[i], af0
            elif high[i] > ep:
                ep, af = high[i], min(af + af_step, af_max)
        else:
            cur = max(cur, high[i - 1], high[i - 2] if i >= 2 else high[i - 1])
            if high[i] > cur:
                up, cur, ep, af = True, ep, high[i], af0
            elif low[i] < ep:
                ep, af = low[i], min(af + af_step, af_max)
        sar[i], trend[i] = cur, (1 if up else -1)
    return sar, trend


def supertrend(high, low, close, n=10, mult=3.0):
    """SuperTrend：返回 (line, direction)，direction=+1 多(价在线上)/-1 空。"""
    a = atr(high, low, close, n)
    m = len(close)
    line, direc = [None] * m, [None] * m
    upper = lower = None
    d = 1
    for i in range(m):
        if a[i] is None:
            continue
        hl2 = (high[i] + low[i]) / 2.0
        bu, bl = hl2 + mult * a[i], hl2 - mult * a[i]
        if upper is None:
            upper, lower = bu, bl
        else:
            upper = bu if (bu < upper or close[i - 1] > upper) else upper
            lower = bl if (bl > lower or close[i - 1] < lower) else lower
        if d == 1 and close[i] < lower:
            d = -1
        elif d == -1 and close[i] > upper:
            d = 1
        line[i], direc[i] = (lower if d == 1 else upper), d
    return line, direc


# ---- 风险 / 统计 -----------------------------------------------------------------
def rolling_drawdown(x, n):
    """相对滚动窗口最高点的回撤（<= 0）。"""
    hi = rolling_max(x, n)
    return [None if h is None else (v / h - 1.0 if h > 0 else 0.0) for v, h in zip(x, hi)]


def rolling_max_drawdown(x, n):
    """窗口内的最大回撤（>= 0）：窗口内 (峰值-谷值)/峰值 的最大值；
    用“自序列起点的累计峰值回撤”在窗口上取滚动最大，O(n)。"""
    _check_window(n)
    peak = -math.inf
    dd = []
    for v in x:
        peak = v if v > peak else peak
        dd.append(1.0 - v / peak if peak > 0 else 0.0)
    return rolling_max(dd, n)


def linreg_slope(x, n):
    """滚动线性回归斜率（横轴 0..n-1）：维护 sum(y) 与 sum(i*y)，O(1)/bar；
    每 n 根从窗口精确重算一次以消除浮点漂移（均摊仍是 O(1)/bar）。"""
    _check_window(n)
    out = [None] * len(x)
    sx = (n - 1) * n / 2.0
    sxx = (n - 1) * n * (2 * n - 1) / 6.0
    denom = n * sxx - sx * sx
    sy = sxy = 0.0
    for i in range(n - 1, len(x)):
        if (i + 1 - n) % n == 0:        # 窗口首次就绪及其后每 n 根：从窗口精确重算
            win = x[i - n + 1:i + 1]
            sy = sum(win)
            sxy = sum(j * y for j, y in enumerate(win))
        else:
            u, v = x[i - n], x[i]
            sxy = sxy - sy + u + (n - 1) * v   # 旧项下标整体减 1，去掉下标 -1 的项，加入新项
            sy += v - u
        if denom > 0:
            out[i] = (n * sxy - sx * sy) / denom
    return out


REGISTRY = {
    "sma": sma, "ema": ema, "wma": wma, "hma": hma, "dema": dema, "tema": tema, "kama": kama,
    "rsi": rsi, "macd": macd, "roc": roc, "momentum": momentum, "trix": trix,
    "rolling_max": rolling_max, "rolling_min": rolling_min, "donchian": donchian,
    "stochastic": stochastic, "williams_r": williams_r, "aroon": aroon,
    "true_range": true_range, "atr": atr, "rolling_std": rolling_std, "bollinger": bollinger,
    "zscore": zscore, "keltner": keltner, "adx": adx, "cci": cci,
    "obv": obv, "vwap": vwap, "mfi": mfi, "cmf": cmf,
    "ichimoku": ichimoku, "parabolic_sar": parabolic_sar, "supertrend": supertrend,
    "rolling_drawdown": rolling_drawdown, "rolling_max_drawdown": rolling_max_drawdown,
    "linreg_slope": linreg_slope,
}
