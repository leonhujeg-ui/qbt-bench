"""换一双眼睛看指标：每个指标挑 1 个变体，按 CONTRACT.md 的公式用 decimal.Decimal（28 位）在 <= 5 根的小样本上
重算一遍，与浮点实现（ref 与 alt）逐点对拍。容差 1e-12·max(1, |值|)（5 根以内的浮点误差在 1e-15 量级）。"""
import decimal
import unittest
from decimal import Decimal as D

from qdcheck import indicators_alt as A
from qdcheck import indicators_ref as R

decimal.getcontext().prec = 28
TOL = D("1e-12")


def bars(close, high=None, low=None, volume=None):
    n = len(close)
    return {"open": list(close), "high": list(high or close), "low": list(low or close), "close": list(close),
            "volume": list(volume or [1000.0] * n)}


def dec(xs):
    return [None if x is None else D(repr(x)) for x in xs]


def d_sma(x, n):
    out = []
    for t in range(len(x)):
        w = x[t - n + 1:t + 1] if t - n + 1 >= 0 else None
        out.append(None if w is None or any(v is None for v in w) else sum(w) / n)
    return out


def d_ema(x, n, kind="ema", seed="sma"):
    """CONTRACT 2.1 的 EMA：alpha = 2/(n+1)（ema）或 1/n（rma）；种子 sma / first。"""
    a = D(2) / (n + 1) if kind == "ema" else D(1) / n
    s = next((i for i, v in enumerate(x) if v is not None), len(x))
    out = [None] * len(x)
    if s >= len(x):
        return out
    if seed == "sma":
        if s + n - 1 >= len(x):
            return out
        e = sum(x[s:s + n]) / n
        out[s + n - 1] = e
        start = s + n
    else:
        e = x[s]
        out[s] = e
        start = s + 1
    for t in range(start, len(x)):
        e = a * x[t] + (1 - a) * e
        out[t] = e
    return out


class DecimalCrossCheck(unittest.TestCase):
    def assertSeries(self, ind, variant, params, data, line, want):
        for impl in (R, A):
            got = impl.compute(ind, data, variant, params)[line]
            self.assertEqual(len(got), len(want), (impl.__name__, ind, line))
            for i, (g, w) in enumerate(zip(got, want)):
                if w is None:
                    self.assertIsNone(g, (impl.__name__, ind, line, i))
                else:
                    self.assertIsNotNone(g, (impl.__name__, ind, line, i))
                    self.assertLessEqual(abs(D(repr(g)) - w), TOL * max(D(1), abs(w)), (impl.__name__, ind, line, i, g, w))

    def test_dema_ema_seed_sma(self):
        c = [1.0, 2.0, 4.0, 3.0, 5.0]
        e1 = d_ema(dec(c), 2)
        e2 = d_ema(e1, 2)
        want = [None if a is None or b is None else 2 * a - b for a, b in zip(e1, e2)]
        self.assertEqual([w is None for w in want], [True, True, False, False, False])
        self.assertSeries("dema", "ma=ema;seed=sma", {"period": 2}, bars(c), "value", want)

    def test_tema_rma_seed_first(self):
        c = [1.0, 2.0, 4.0, 3.0, 5.0]
        e1 = d_ema(dec(c), 3, "rma", "first")
        e2 = d_ema(e1, 3, "rma", "first")
        e3 = d_ema(e2, 3, "rma", "first")
        want = [3 * a - 3 * b + cc for a, b, cc in zip(e1, e2, e3)]
        self.assertSeries("tema", "ma=rma;seed=first", {"period": 3}, bars(c), "value", want)

    def test_trix_sma(self):
        c = [1.0, 2.0, 4.0, 3.0, 5.0]
        e3 = d_sma(d_sma(d_sma(dec(c), 2), 2), 2)
        want = [None] + [None if e3[t] is None or e3[t - 1] is None else 100 * (e3[t] - e3[t - 1]) / e3[t - 1]
                         for t in range(1, 5)]
        self.assertEqual([w is None for w in want], [True, True, True, True, False])
        self.assertSeries("trix", "ma=sma", {"period": 2, "scale": 100.0}, bars(c), "value", want)

    def test_stochrsi_wilder_talib(self):
        c = dec([1.0, 2.0, 1.5, 3.0, 2.5])
        n = 2
        gains = [max(c[t] - c[t - 1], D(0)) for t in range(1, 5)]
        losses = [max(c[t - 1] - c[t], D(0)) for t in range(1, 5)]
        rsi = [None] * 5
        ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
        rsi[n] = 100 - 100 / (1 + ag / al)
        for t in range(n + 1, 5):
            ag = (ag * (n - 1) + gains[t - 1]) / n
            al = (al * (n - 1) + losses[t - 1]) / n
            rsi[t] = 100 - 100 / (1 + ag / al)
        stoch = [None] * 5
        for t in range(n + 1, 5):                       # stoch 窗口 2
            w = rsi[t - 1:t + 1]
            hi, lo = max(w), min(w)
            stoch[t] = D(0) if hi - lo <= D("1e-9") else 100 * (rsi[t] - lo) / (hi - lo)
        p = {"rsi_period": 2, "stoch_period": 2, "k_period": 1, "d_period": 1}
        self.assertEqual([w is None for w in stoch], [True, True, True, False, False])
        self.assertSeries("stochrsi", "flat=talib;rsi=wilder", p, bars([1.0, 2.0, 1.5, 3.0, 2.5]), "k", stoch)
        self.assertSeries("stochrsi", "flat=talib;rsi=wilder", p, bars([1.0, 2.0, 1.5, 3.0, 2.5]), "d", stoch)

    def test_cci_hlc3(self):
        h, lo, c = [2.0, 3.0, 5.0, 4.0, 6.0], [0.5, 1.0, 2.0, 2.5, 3.0], [1.0, 2.0, 4.0, 3.0, 5.0]
        tp = [(D(repr(a)) + D(repr(b)) + D(repr(x))) / 3 for a, b, x in zip(h, lo, c)]
        want = [None, None]
        for t in range(2, 5):
            w = tp[t - 2:t + 1]
            m = sum(w) / 3
            md = sum(abs(v - m) for v in w) / 3
            want.append((tp[t] - m) / (D("0.015") * md))
        self.assertSeries("cci", "src=hlc3;zero_dev=zero", {"period": 3}, bars(c, h, lo), "value", want)

    def test_ao_sma_hl2(self):
        h, lo = [2.0, 3.0, 5.0, 4.0, 6.0], [0.5, 1.0, 2.0, 2.5, 3.0]
        src = [(D(repr(a)) + D(repr(b))) / 2 for a, b in zip(h, lo)]
        fast, slow = d_sma(src, 2), d_sma(src, 3)
        want = [None if f is None or s is None else f - s for f, s in zip(fast, slow)]
        self.assertSeries("ao", "ma=sma;src=hl2", {"fast": 2, "slow": 3}, bars([1.0] * 5, h, lo), "value", want)

    def test_force_ema_seed_sma(self):
        c, v = [1.0, 2.0, 4.0, 3.0, 5.0], [10.0, 20.0, 5.0, 8.0, 12.0]
        cd, vd = dec(c), dec(v)
        raw = [None] + [(cd[t] - cd[t - 1]) * vd[t] for t in range(1, 5)]
        want = d_ema(raw, 2)
        self.assertEqual([w is None for w in want], [True, True, False, False, False])
        self.assertSeries("force", "ma=ema;seed=sma", {"period": 2}, bars(c, volume=v), "value", want)

    def test_kdj_rma_init50(self):
        h, lo, c = [2.0, 3.0, 5.0, 4.0, 6.0], [0.5, 1.0, 2.0, 2.5, 3.0], [1.0, 2.0, 4.0, 3.0, 5.0]
        hd, ld, cd = dec(h), dec(lo), dec(c)
        n, m = 2, 2
        rsv = [None] + [100 * (cd[t] - min(ld[t - 1:t + 1])) / (max(hd[t - 1:t + 1]) - min(ld[t - 1:t + 1]))
                        for t in range(1, 5)]
        k, d = [None] * 5, [None] * 5
        kp, dp = D(50), D(50)
        for t in range(1, 5):
            kp = (rsv[t] + (m - 1) * kp) / m
            dp = (kp + (m - 1) * dp) / m
            k[t], d[t] = kp, dp
        j = [None if a is None else 3 * a - 2 * b for a, b in zip(k, d)]
        p = {"n": 2, "m1": 2, "m2": 2}
        for line, want in (("k", k), ("d", d), ("j", j)):
            self.assertSeries("kdj", "init=50;ma=rma;zero_range=zero", p, bars(c, h, lo), line, want)

    def test_ichimoku_forward_shift_1(self):
        h, lo, c = [2.0, 3.0, 5.0, 4.0, 6.0], [0.5, 1.0, 2.0, 2.5, 3.0], [1.0, 2.0, 4.0, 3.0, 5.0]
        hd, ld, cd = dec(h), dec(lo), dec(c)
        mid = lambda n: [None if t < n - 1 else (max(hd[t - n + 1:t + 1]) + min(ld[t - n + 1:t + 1])) / 2 for t in range(5)]
        tenkan, kijun, base_b = mid(2), mid(3), mid(4)
        base_a = [None if a is None or b is None else (a + b) / 2 for a, b in zip(tenkan, kijun)]
        p = {"tenkan": 2, "kijun": 3, "senkou_b": 4, "displacement": 1}
        want = {"tenkan": tenkan, "kijun": kijun,
                "senkou_a": [None] + base_a[:4],            # 前移 1 根：senkou_a[t] = base_a[t-1]
                "senkou_b": [None] + base_b[:4],
                "chikou": cd[1:] + [None]}                   # 迟行线：chikou[t] = close[t+1]
        self.assertEqual([w is None for w in want["senkou_b"]], [True, True, True, True, False])
        for line, series in want.items():
            self.assertSeries("ichimoku", "shift=forward", p, bars(c, h, lo), line, series)


if __name__ == "__main__":
    unittest.main()
