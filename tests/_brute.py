"""指标的“暴力/按定义”参考实现：O(n*w) 或逐根重算，只用于对拍，不追求速度。"""
import math


def sma(x, n):
    return [None if i < n - 1 else sum(x[i - n + 1:i + 1]) / n for i in range(len(x))]


def ema_closed_form(x, n):
    """EMA 闭式解：e_i = a*sum_{k=0}^{i-1} (1-a)^k x_{i-k} + (1-a)^i x_0。"""
    a = 2.0 / (n + 1)
    out = []
    for i in range(len(x)):
        acc = (1 - a) ** i * x[0]
        for k in range(i):
            acc += a * (1 - a) ** k * x[i - k]
        out.append(acc)
    return out


def wma(x, n):
    w = list(range(1, n + 1))
    return [None if i < n - 1 else sum(wi * xi for wi, xi in zip(w, x[i - n + 1:i + 1])) / sum(w)
            for i in range(len(x))]


def rolling_max(x, n):
    return [None if i < n - 1 else max(x[i - n + 1:i + 1]) for i in range(len(x))]


def rolling_min(x, n):
    return [None if i < n - 1 else min(x[i - n + 1:i + 1]) for i in range(len(x))]


def rolling_argmax(x, n):
    return [None if i < n - 1 else max(range(i - n + 1, i + 1), key=lambda j: (x[j], j)) for i in range(len(x))]


def rolling_argmin(x, n):
    return [None if i < n - 1 else min(range(i - n + 1, i + 1), key=lambda j: (x[j], -j)) for i in range(len(x))]


def rolling_std(x, n):
    out = []
    for i in range(len(x)):
        if i < n - 1:
            out.append(None)
            continue
        w = x[i - n + 1:i + 1]
        m = sum(w) / n
        out.append(math.sqrt(sum((v - m) ** 2 for v in w) / n))
    return out


def rsi(x, n):
    """按 Wilder 定义逐根重算。"""
    out = [None] * len(x)
    gains = [max(x[i] - x[i - 1], 0.0) for i in range(1, len(x))]
    losses = [max(x[i - 1] - x[i], 0.0) for i in range(1, len(x))]
    if len(x) <= n:
        return out
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    out[n] = _rsi(ag, al)
    for i in range(n + 1, len(x)):
        ag = (ag * (n - 1) + gains[i - 1]) / n
        al = (al * (n - 1) + losses[i - 1]) / n
        out[i] = _rsi(ag, al)
    return out


def _rsi(g, l):
    if l == 0:
        return 100.0 if g > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + g / l)


def true_range(h, l, c):
    return [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]


def wilder(x, n):
    out = [None] * len(x)
    if len(x) < n:
        return out
    s = sum(x[:n]) / n
    out[n - 1] = s
    for i in range(n, len(x)):
        s = (s * (n - 1) + x[i]) / n
        out[i] = s
    return out


def adx(h, l, c, n):
    tr = true_range(h, l, c)
    pdm, mdm = [0.0], [0.0]
    for i in range(1, len(c)):
        up, dn = h[i] - h[i - 1], l[i - 1] - l[i]
        pdm.append(up if up > dn and up > 0 else 0.0)
        mdm.append(dn if dn > up and dn > 0 else 0.0)
    a, p, m = wilder(tr, n), wilder(pdm, n), wilder(mdm, n)
    pdi = [None if v is None else (100 * p[i] / v if v > 0 else 0.0) for i, v in enumerate(a)]
    mdi = [None if v is None else (100 * m[i] / v if v > 0 else 0.0) for i, v in enumerate(a)]
    dx = [None if v is None else (100 * abs(pdi[i] - mdi[i]) / (pdi[i] + mdi[i]) if pdi[i] + mdi[i] > 0 else 0.0)
          for i, v in enumerate(pdi)]
    first = n - 1
    sm = wilder(dx[first:], n)
    return [None] * first + sm, pdi, mdi


def cci(h, l, c, n):
    tp = [(a + b + d) / 3 for a, b, d in zip(h, l, c)]
    out = []
    for i in range(len(tp)):
        if i < n - 1:
            out.append(None)
            continue
        w = tp[i - n + 1:i + 1]
        m = sum(w) / n
        dev = sum(abs(v - m) for v in w) / n
        out.append((tp[i] - m) / (0.015 * dev) if dev > 0 else 0.0)
    return out


def linreg_slope(x, n):
    out = []
    for i in range(len(x)):
        if i < n - 1:
            out.append(None)
            continue
        ys = x[i - n + 1:i + 1]
        xs = list(range(n))
        mx, my = sum(xs) / n, sum(ys) / n
        num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
        den = sum((a - mx) ** 2 for a in xs)
        out.append(num / den)
    return out


def max_drawdown_from_start(x, n):
    out = []
    for i in range(len(x)):
        if i < n - 1:
            out.append(None)
            continue
        best = 0.0
        for j in range(i - n + 1, i + 1):
            peak = max(x[:j + 1])
            best = max(best, 1 - x[j] / peak)
        out.append(best)
    return out


def parabolic_sar(high, low, af0=0.02, step=0.02, mx=0.2):
    """独立实现（与 qbt 的写法不同：显式状态机）。"""
    m = len(high)
    sar, trend = [None] * m, [None] * m
    if m < 2:
        return sar, trend
    long = high[1] > high[0]
    ep = high[1] if long else low[1]
    psar = low[0] if long else high[0]
    af = af0
    for i in range(1, m):
        psar = psar + af * (ep - psar)
        if long:
            psar = min(psar, low[i - 1], low[max(i - 2, 0)])
            if low[i] < psar:
                long, psar, ep, af = False, ep, low[i], af0
            else:
                if high[i] > ep:
                    ep = high[i]
                    af = min(af + step, mx)
        else:
            psar = max(psar, high[i - 1], high[max(i - 2, 0)])
            if high[i] > psar:
                long, psar, ep, af = True, ep, high[i], af0
            else:
                if low[i] < ep:
                    ep = low[i]
                    af = min(af + step, mx)
        sar[i] = psar
        trend[i] = 1 if long else -1
    return sar, trend


def kama(x, n, fast, slow):
    """按定义逐根重算 ER 与自适应平滑。"""
    fsc, ssc = 2.0 / (fast + 1), 2.0 / (slow + 1)
    out = [None] * len(x)
    prev = None
    for i in range(n, len(x)):
        vol = sum(abs(x[j] - x[j - 1]) for j in range(i - n + 1, i + 1))
        er = abs(x[i] - x[i - n]) / vol if vol > 0 else 0.0
        sc = (er * (fsc - ssc) + ssc) ** 2
        prev = x[i] if prev is None else prev + sc * (x[i] - prev)
        out[i] = prev
    return out


def supertrend(high, low, close, n, mult):
    """独立实现：显式 final upper/lower band 规则。"""
    a = wilder(true_range(high, low, close), n)
    m = len(close)
    line, direc = [None] * m, [None] * m
    fu = fl = None
    d = 1
    for i in range(m):
        if a[i] is None:
            continue
        hl2 = (high[i] + low[i]) / 2
        bu, bl = hl2 + mult * a[i], hl2 - mult * a[i]
        if fu is None:
            fu, fl = bu, bl
        else:
            fu = bu if bu < fu or close[i - 1] > fu else fu
            fl = bl if bl > fl or close[i - 1] < fl else fl
        if d == 1 and close[i] < fl:
            d = -1
        elif d == -1 and close[i] > fu:
            d = 1
        line[i] = fl if d == 1 else fu
        direc[i] = d
    return line, direc
