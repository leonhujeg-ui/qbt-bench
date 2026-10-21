"""阶段 1 判据：每个指标一条以上数学判据（暴力对拍 / 闭式解 / 取值范围 / 对称性 / 因果性）。

容差：REL_TOL=1e-9（滚动和 vs 暴力求和，求和顺序不同，误差 ~ n*eps*|x|）；
滚动方差类用 STD_TOL=1e-7（平方和消去误差 ~ n*eps*x^2/var，价格量级 1e2 下足够）。
"""
import math
import random
import unittest

from qbt import indicators as I
from qbt.data import generate
from qbt.errors import ConfigError
from tests import _brute as B
from tests._helpers import QBTTestCase, REL_TOL, scale

STD_TOL = 1e-7
# STD_ABS：退化窗口（真实方差为 0）下，一遍法二阶矩残差 ~eps*dx^2 经 sqrt 放大到 ~1e-8，取 1e-7 绝对容差。
STD_ABS = 1e-7


def _data(n=None, seed=1):
    n = n or scale(3000, 800)
    return generate(n=n, seed=seed, halt_prob=0.003, gap_prob=0.02)


class TestMovingAverages(QBTTestCase):
    def setUp(self):
        self.d = _data()
        self.x = self.d.close

    def test_sma_vs_bruteforce(self):
        for n in (1, 2, 7, 50):
            self.assertCloseList(I.sma(self.x, n), B.sma(self.x, n), msg=f"n={n}")

    def test_sma_warmup_and_constant(self):
        out = I.sma(self.x, 10)
        self.assertEqual(out[:9], [None] * 9)
        self.assertIsNotNone(out[9])
        self.assertCloseList(I.sma([5.0] * 30, 7), [None] * 6 + [5.0] * 24)

    def test_ema_vs_closed_form(self):
        x = self.x[:200]
        for n in (1, 3, 10, 30):
            self.assertCloseList(I.ema(x, n), B.ema_closed_form(x, n), rel=1e-9, msg=f"n={n}")
        self.assertEqual(I.ema([], 5), [])
        self.assertEqual(I.ema([3.0], 5), [3.0])
        self.assertCloseList(I.ema([1.0, 2.0], 1), [1.0, 2.0])          # n=1 => alpha=1 => 跟随

    def test_ema_custom_alpha(self):
        out = I.ema([1.0, 3.0, 5.0], 10, alpha=0.5)
        self.assertCloseList(out, [1.0, 2.0, 3.5])

    def test_wma_vs_bruteforce(self):
        for n in (1, 3, 20):
            self.assertCloseList(I.wma(self.x, n), B.wma(self.x, n), msg=f"n={n}")

    def test_hma_vs_bruteforce_composition(self):
        n = 16
        half, root = n // 2, int(math.sqrt(n))
        a, b = B.wma(self.x, half), B.wma(self.x, n)
        diff = [None if (p is None or q is None) else 2 * p - q for p, q in zip(a, b)]
        start = diff.index(next(v for v in diff if v is not None))
        ref = [None] * start + B.wma(diff[start:], root)
        self.assertCloseList(I.hma(self.x, n), ref)
        self.assertCloseList(I.hma([3.0] * 50, 9)[20:], [3.0] * 30)
        self.assertEqual(I.hma(self.x[:5], 16), [None] * 5)

    def test_dema_tema_lag_on_ramp(self):
        """斜坡输入：EMA 稳态滞后 = (1-a)/a * slope；DEMA/TEMA 对线性趋势渐近无滞后。"""
        n, slope = 10, 0.5
        x = [slope * i for i in range(2000)]
        a = 2.0 / (n + 1)
        e, d, t = I.ema(x, n)[-1], I.dema(x, n)[-1], I.tema(x, n)[-1]
        self.assertAlmostEqual(x[-1] - e, (1 - a) / a * slope, delta=1e-6)
        self.assertLess(abs(x[-1] - d), 1e-6)
        self.assertLess(abs(x[-1] - t), 1e-6)
        self.assertCloseList(I.dema([2.0] * 20, 5), [2.0] * 20)
        self.assertCloseList(I.tema([2.0] * 20, 5), [2.0] * 20)

    def test_kama_properties(self):
        """单调序列 ER=1 => sc=fast_sc^2，KAMA 递推可闭式对拍；随机序列 KAMA 介于窗口极值之间。"""
        x = [float(i) for i in range(60)]
        n, fast, slow = 10, 2, 30
        sc = (2.0 / (fast + 1)) ** 2
        out = I.kama(x, n, fast, slow)
        self.assertEqual(out[:n], [None] * n)
        prev = x[n]
        ref = [None] * n + [prev]
        for i in range(n + 1, 60):
            prev = prev + sc * (x[i] - prev)
            ref.append(prev)
        self.assertCloseList(out, ref)
        k = I.kama(self.x, 10)
        self.assertCloseList(k, B.kama(self.x, 10, 2, 30), rel=1e-8)
        self.assertCloseList(I.kama(self.x, 7, 3, 20), B.kama(self.x, 7, 3, 20), rel=1e-8)
        lo, hi = min(self.x), max(self.x)
        self.assertTrue(all(v is None or lo <= v <= hi for v in k))
        self.assertEqual(I.kama([], 5), [])
        self.assertEqual(I.kama([1.0] * 5, 2)[2:], [1.0, 1.0, 1.0])     # 全平：vol=0 => er=0


class TestOscillators(QBTTestCase):
    def setUp(self):
        self.d = _data()
        self.x = self.d.close

    def test_rsi_vs_definition_and_range(self):
        for n in (2, 14):
            self.assertCloseList(I.rsi(self.x, n), B.rsi(self.x, n), msg=f"n={n}")
        self.assertTrue(all(v is None or 0.0 <= v <= 100.0 for v in I.rsi(self.x, 14)))
        up = [float(i) for i in range(40)]
        self.assertTrue(all(v == 100.0 for v in I.rsi(up, 14)[14:]))
        self.assertTrue(all(v == 0.0 for v in I.rsi(up[::-1], 14)[14:]))
        self.assertTrue(all(v == 50.0 for v in I.rsi([1.0] * 40, 14)[14:]))
        self.assertEqual(I.rsi(self.x, 14)[:14], [None] * 14)

    def test_macd_composition(self):
        line, sig, hist = I.macd(self.x, 12, 26, 9)
        ef, es = B.ema_closed_form(self.x[:150], 12), B.ema_closed_form(self.x[:150], 26)
        self.assertCloseList(line[:150], [a - b for a, b in zip(ef, es)], rel=1e-8)
        self.assertCloseList(sig[:150], B.ema_closed_form(line[:150], 9), rel=1e-8)
        self.assertCloseList(hist, [a - b for a, b in zip(line, sig)])

    def test_roc_momentum_trix(self):
        n = 5
        self.assertCloseList(I.roc(self.x, n), [None] * n + [self.x[i] / self.x[i - n] - 1 for i in range(n, len(self.x))])
        self.assertCloseList(I.momentum(self.x, n), [None] * n + [self.x[i] - self.x[i - n] for i in range(n, len(self.x))])
        self.assertIsNone(I.roc([1.0, 0.0, 2.0], 1)[1 + 1 - 1 + 1 - 1] if False else I.roc([1.0, 0.0, 2.0], 1)[2])  # 除零 => None
        g = 0.01
        x = [math.exp(g * i) for i in range(600)]
        t = I.trix(x, 5)
        self.assertIsNone(t[0])
        self.assertAlmostEqual(t[-1], math.exp(g) - 1.0, delta=1e-9)
        self.assertIsNone(I.trix([1.0, 0.0, 0.0], 1)[2])

    def test_stochastic_and_williams(self):
        d = self.d
        k, dd = I.stochastic(d.high, d.low, d.close, 14, 3)
        hh, ll = B.rolling_max(d.high, 14), B.rolling_min(d.low, 14)
        ref = [None if h is None else (50.0 if h == l else 100.0 * (c - l) / (h - l)) for h, l, c in zip(hh, ll, d.close)]
        self.assertCloseList(k, ref)
        self.assertTrue(all(v is None or 0.0 <= v <= 100.0 for v in k))
        self.assertCloseList(dd, [None] * 13 + B.sma(k[13:], 3))
        w = I.williams_r(d.high, d.low, d.close, 14)
        self.assertCloseList(w, [None if v is None else v - 100.0 for v in k])
        self.assertTrue(all(v is None or -100.0 <= v <= 0.0 for v in w))
        flat = [1.0] * 20
        self.assertEqual(I.stochastic(flat, flat, flat, 5, 3)[0][10], 50.0)
        self.assertEqual(I.williams_r(flat, flat, flat, 5)[10], -50.0)

    def test_aroon_vs_argext(self):
        d, n = self.d, 25
        up, dn = I.aroon(d.high, d.low, n)
        am, an = B.rolling_argmax(d.high, n), B.rolling_argmin(d.low, n)
        self.assertCloseList(up, [None if j is None else 100.0 * (n - (i - j)) / n for i, j in enumerate(am)])
        self.assertCloseList(dn, [None if j is None else 100.0 * (n - (i - j)) / n for i, j in enumerate(an)])
        self.assertTrue(all(v is None or 0.0 < v <= 100.0 for v in up))
        ramp = [float(i) for i in range(50)]
        self.assertTrue(all(v == 100.0 for v in I.aroon(ramp, ramp, 10)[0][9:]))

    def test_cci_vs_bruteforce(self):
        d = self.d
        self.assertCloseList(I.cci(d.high, d.low, d.close, 20), B.cci(d.high, d.low, d.close, 20), rel=1e-8)
        flat = [2.0] * 30
        self.assertEqual(I.cci(flat, flat, flat, 5)[10], 0.0)


class TestExtremesAndChannels(QBTTestCase):
    def setUp(self):
        self.d = _data()
        self.x = self.d.close

    def test_rolling_max_min_vs_bruteforce(self):
        for n in (1, 2, 9, 64):
            self.assertEqual(I.rolling_max(self.x, n), B.rolling_max(self.x, n))   # 精确相等：无算术
            self.assertEqual(I.rolling_min(self.x, n), B.rolling_min(self.x, n))
        with_ties = [1.0, 3.0, 3.0, 2.0, 3.0, 1.0, 1.0]
        self.assertEqual(I.rolling_max(with_ties, 3), B.rolling_max(with_ties, 3))
        self.assertEqual(I.rolling_min(with_ties, 3), B.rolling_min(with_ties, 3))

    def test_donchian(self):
        d = self.d
        up, lo, mid = I.donchian(d.high, d.low, 20)
        self.assertEqual(up, B.rolling_max(d.high, 20))
        self.assertEqual(lo, B.rolling_min(d.low, 20))
        self.assertCloseList(mid, [None if u is None else (u + l) / 2 for u, l in zip(up, lo)])
        self.assertTrue(all(u is None or u >= l for u, l in zip(up, lo)))

    def test_true_range_and_atr(self):
        d = self.d
        tr = I.true_range(d.high, d.low, d.close)
        self.assertCloseList(tr, B.true_range(d.high, d.low, d.close))
        self.assertTrue(all(v >= 0 for v in tr))
        a = I.atr(d.high, d.low, d.close, 14)
        self.assertCloseList(a, B.wilder(tr, 14))
        self.assertTrue(all(v is None or v >= 0 for v in a))
        self.assertEqual(a[:13], [None] * 13)
        self.assertEqual(I.true_range([], [], []), [])
        # 缺口：TR 至少覆盖缺口幅度
        self.assertGreaterEqual(I.true_range([10.0, 20.0], [9.0, 19.0], [9.5, 19.5])[1], 20.0 - 9.5)

    def test_rolling_std_bollinger_zscore(self):
        x = self.x
        for n in (2, 20):
            self.assertCloseList(I.rolling_std(x, n), B.rolling_std(x, n), rel=STD_TOL, abs_tol=STD_ABS)
        up, mid, lo = I.bollinger(x, 20, 2.0)
        self.assertCloseList(mid, B.sma(x, 20))
        sd = B.rolling_std(x, 20)
        self.assertCloseList(up, [None if m is None else m + 2 * s for m, s in zip(mid, sd)], rel=STD_TOL)
        self.assertCloseList([None if u is None else u - m for u, m in zip(up, mid)],
                             [None if l is None else m - l for m, l in zip(mid, lo)], rel=1e-9, msg="symmetry")
        z = I.zscore(x, 20)
        self.assertCloseList(z, [None if m is None else ((v - m) / s if s > 0 else 0.0) for v, m, s in zip(x, mid, sd)],
                             rel=STD_TOL)
        self.assertEqual(I.rolling_std([1.0] * 10, 3)[5], 0.0)
        self.assertEqual(I.zscore([1.0] * 10, 3)[5], 0.0)
        self.assertAlmostEqual(I.rolling_std([1.0, 2.0, 3.0, 4.0], 4, ddof=1)[3], math.sqrt(5.0 / 3.0), delta=1e-12)
        self.assertEqual(I.rolling_std([1.0, 2.0], 1, ddof=1), [None, None])

    def test_keltner(self):
        d = self.d
        up, mid, lo = I.keltner(d.high, d.low, d.close, 20, 2.0, 10)
        self.assertCloseList(mid, I.ema(d.close, 20))
        a = I.atr(d.high, d.low, d.close, 10)
        self.assertCloseList([None if u is None else u - m for u, m in zip(up, mid)],
                             [None if v is None else 2.0 * v for v in a])
        self.assertCloseList([None if l is None else m - l for m, l in zip(mid, lo)],
                             [None if v is None else 2.0 * v for v in a])

    def test_adx_vs_bruteforce_and_range(self):
        d = self.d
        a, p, m = I.adx(d.high, d.low, d.close, 14)
        ra, rp, rm = B.adx(d.high, d.low, d.close, 14)
        self.assertCloseList(a, ra, rel=1e-8)
        self.assertCloseList(p, rp, rel=1e-8)
        self.assertCloseList(m, rm, rel=1e-8)
        for s in (a, p, m):
            self.assertTrue(all(v is None or 0.0 <= v <= 100.0 for v in s))
        hi = [float(i) + 1.0 for i in range(80)]
        lo = [float(i) for i in range(80)]
        ta, tp, tm = I.adx(hi, lo, hi, 14)
        self.assertAlmostEqual(ta[-1], 100.0, delta=1e-9)          # 单边上行：DX 恒为 100
        self.assertAlmostEqual(tm[-1], 0.0, delta=1e-12)
        self.assertEqual(I.adx([], [], [], 14), ([], [], []))
        flat = [3.0] * 40
        fa, fp, fm = I.adx(flat, flat, flat, 5)
        self.assertTrue(all(v == 0.0 for v in fa[8:]) and fp[-1] == 0.0 and fm[-1] == 0.0)   # 全平：DI/DX 定义为 0
        # 极小 DI（0 < +DI + -DI < 1）：DX 仍应为 100 而不是被当作 0
        hi2 = [100.0 + 1e-4 * i for i in range(80)]
        lo2 = [50.0] * 80
        ta2, tp2, _ = I.adx(hi2, lo2, hi2, 14)
        self.assertLess(tp2[-1], 1.0)
        self.assertAlmostEqual(ta2[-1], 100.0, delta=1e-9)


class TestVolumeTrendRisk(QBTTestCase):
    def setUp(self):
        self.d = _data()
        self.x = self.d.close

    def test_obv_vwap(self):
        d = self.d
        o = I.obv(d.close, d.volume)
        ref = [0.0]
        for i in range(1, len(d)):
            s = 1 if d.close[i] > d.close[i - 1] else (-1 if d.close[i] < d.close[i - 1] else 0)
            ref.append(ref[-1] + s * d.volume[i])
        self.assertCloseList(o, ref)
        self.assertEqual(I.obv([1.0, 1.0, 2.0, 2.0, 1.0], [5.0, 5.0, 5.0, 5.0, 5.0]), [0.0, 0.0, 5.0, 5.0, 0.0])
        tp = [(h + l + c) / 3 for h, l, c in zip(d.high, d.low, d.close)]
        cum = I.vwap(d.high, d.low, d.close, d.volume)
        pv = vv = 0.0
        ref = []
        for i in range(len(d)):
            pv += tp[i] * d.volume[i]
            vv += d.volume[i]
            ref.append(pv / vv if vv > 0 else None)
        self.assertCloseList(cum, ref)
        n = 10
        roll = I.vwap(d.high, d.low, d.close, d.volume, n)
        ref = [None if i < n - 1 else (sum(tp[j] * d.volume[j] for j in range(i - n + 1, i + 1))
                                       / sum(d.volume[i - n + 1:i + 1])) for i in range(len(d))]
        self.assertCloseList(roll, ref, rel=1e-8)
        self.assertEqual(I.vwap([1.0], [1.0], [1.0], [0.0]), [None])
        self.assertTrue(all(v is None or min(d.low) <= v <= max(d.high) for v in roll))

    def test_mfi_cmf(self):
        d = self.d
        m = I.mfi(d.high, d.low, d.close, d.volume, 14)
        self.assertTrue(all(v is None or 0.0 <= v <= 100.0 for v in m))
        tp = [(h + l + c) / 3 for h, l, c in zip(d.high, d.low, d.close)]
        ref = [None] * 14
        for i in range(14, len(d)):
            pos = sum(tp[j] * d.volume[j] for j in range(i - 13, i + 1) if tp[j] > tp[j - 1])
            neg = sum(tp[j] * d.volume[j] for j in range(i - 13, i + 1) if tp[j] < tp[j - 1])
            ref.append(100.0 * pos / (pos + neg) if pos + neg > 0 else 50.0)
        self.assertCloseList(m, ref, rel=1e-8)
        c = I.cmf(d.high, d.low, d.close, d.volume, 20)
        self.assertTrue(all(v is None or -1.0 <= v <= 1.0 for v in c))
        mfv = [((cl - l) - (h - cl)) / (h - l) * v if h > l else 0.0 for h, l, cl, v in zip(d.high, d.low, d.close, d.volume)]
        ref = [None if i < 19 else (sum(mfv[i - 19:i + 1]) / sum(d.volume[i - 19:i + 1])
                                    if sum(d.volume[i - 19:i + 1]) > 0 else 0.0) for i in range(len(d))]
        self.assertCloseList(c, ref, rel=1e-8)
        flat = [1.0] * 30
        self.assertEqual(I.mfi(flat, flat, flat, flat, 5)[10], 50.0)
        up = [float(i) for i in range(1, 31)]
        self.assertTrue(all(v == 100.0 for v in I.mfi(up, up, up, flat, 5)[5:]))
        self.assertTrue(all(v == 0.0 for v in I.mfi(up[::-1], up[::-1], up[::-1], flat, 5)[5:]))
        self.assertTrue(all(v == 1.0 for v in I.cmf(up, [u - 1 for u in up], up, flat, 5)[4:]))
        self.assertTrue(all(v == -1.0 for v in I.cmf([u + 1 for u in up], up, up, flat, 5)[4:]))
        self.assertEqual(I.cmf(flat, flat, flat, [0.0] * 30, 5)[10], 0.0)

    def test_ichimoku_lines(self):
        d = self.d
        t, k, sa, sb, ch = I.ichimoku(d.high, d.low, d.close, 9, 26, 52, 26)
        mid = lambda n: [None if h is None else (h + l) / 2 for h, l in zip(B.rolling_max(d.high, n), B.rolling_min(d.low, n))]
        self.assertCloseList(t, mid(9))
        self.assertCloseList(k, mid(26))
        raw_a = [None if (a is None or b is None) else (a + b) / 2 for a, b in zip(t, k)]
        raw_b = mid(52)
        self.assertEqual(sa[:26], [None] * 26)
        self.assertCloseList(sa[26:], raw_a[:-26])
        self.assertCloseList(sb[26:], raw_b[:-26])
        self.assertCloseList(ch, [None] * 26 + [d.close[i] - d.close[i - 26] for i in range(26, len(d))])
        short = I.ichimoku(d.high[:10], d.low[:10], d.close[:10])
        self.assertEqual(short[2], [None] * 10)
        self.assertEqual(short[3], [None] * 10)

    def test_parabolic_sar(self):
        d = self.d
        sar, tr = I.parabolic_sar(d.high, d.low)
        rs, rt = B.parabolic_sar(d.high, d.low)
        self.assertCloseList(sar, rs)
        self.assertEqual(tr, rt)
        for i in range(2, len(d)):
            if tr[i] == tr[i - 1] == 1:
                self.assertLessEqual(sar[i], d.low[i])           # 多头延续：SAR 在当根低点之下
            elif tr[i] == tr[i - 1] == -1:
                self.assertGreaterEqual(sar[i], d.high[i])
            elif tr[i] == 1:
                self.assertLessEqual(sar[i], d.low[i - 1])       # 翻多：SAR = 前一趋势的极值低点
            else:
                self.assertGreaterEqual(sar[i], d.high[i - 1])
        self.assertGreater(tr.count(1), 0)
        self.assertGreater(tr.count(-1), 0)
        self.assertEqual(I.parabolic_sar([1.0], [1.0]), ([None], [None]))
        down = I.parabolic_sar([5.0, 4.0, 3.0], [4.0, 3.0, 2.0])[1]
        self.assertEqual(down[1:], [-1, -1])

    def test_supertrend_properties(self):
        d = self.d
        line, direc = I.supertrend(d.high, d.low, d.close, 10, 3.0)
        a = I.atr(d.high, d.low, d.close, 10)
        for i in range(len(d)):
            if a[i] is None:
                self.assertIsNone(line[i])
                continue
            if direc[i] == 1:
                self.assertGreaterEqual(d.close[i], line[i])
            else:
                self.assertLessEqual(d.close[i], line[i])
        self.assertGreater(direc.count(1), 0)
        self.assertGreater(direc.count(-1), 0)
        rl, rd = B.supertrend(d.high, d.low, d.close, 10, 3.0)
        self.assertCloseList(line, rl)
        self.assertEqual(direc, rd)
        # 翻转时刻：前一根方向不同且收盘穿越带
        flips = [i for i in range(1, len(d)) if direc[i] is not None and direc[i - 1] is not None and direc[i] != direc[i - 1]]
        self.assertGreater(len(flips), 0)

    def test_drawdowns(self):
        x = self.x
        dd = I.rolling_drawdown(x, 20)
        hi = B.rolling_max(x, 20)
        self.assertCloseList(dd, [None if h is None else v / h - 1 for v, h in zip(x, hi)])
        self.assertTrue(all(v is None or v <= 0 for v in dd))
        mdd = I.rolling_max_drawdown(x[:400], 30)
        self.assertCloseList(mdd, B.max_drawdown_from_start(x[:400], 30))
        self.assertTrue(all(v is None or 0 <= v <= 1 for v in mdd))
        self.assertEqual(I.rolling_max_drawdown([1.0, 2.0, 3.0], 2)[2], 0.0)

    def test_linreg_slope(self):
        x = self.x
        for n in (2, 5, 30):
            self.assertCloseList(I.linreg_slope(x, n), B.linreg_slope(x, n), rel=1e-7, abs_tol=1e-9)
        ramp = [0.5 * i + 3 for i in range(100)]
        self.assertTrue(all(abs(v - 0.5) < 1e-9 for v in I.linreg_slope(ramp, 10)[9:]))
        self.assertEqual(I.linreg_slope([1.0, 2.0], 1), [None, None])    # n=1 分母为 0 => None


class TestGenericProperties(QBTTestCase):
    """对注册表里的全部指标做通用判据：长度保持、因果性、空输入、窗口参数校验。"""

    CALLS = {
        "sma": lambda d: (d.close, 10), "ema": lambda d: (d.close, 10), "wma": lambda d: (d.close, 10),
        "hma": lambda d: (d.close, 16), "dema": lambda d: (d.close, 10), "tema": lambda d: (d.close, 10),
        "kama": lambda d: (d.close, 10), "rsi": lambda d: (d.close, 14), "macd": lambda d: (d.close,),
        "roc": lambda d: (d.close, 5), "momentum": lambda d: (d.close, 5), "trix": lambda d: (d.close, 5),
        "rolling_max": lambda d: (d.high, 10), "rolling_min": lambda d: (d.low, 10),
        "donchian": lambda d: (d.high, d.low, 10), "stochastic": lambda d: (d.high, d.low, d.close),
        "williams_r": lambda d: (d.high, d.low, d.close), "aroon": lambda d: (d.high, d.low, 10),
        "true_range": lambda d: (d.high, d.low, d.close), "atr": lambda d: (d.high, d.low, d.close),
        "rolling_std": lambda d: (d.close, 10), "bollinger": lambda d: (d.close,), "zscore": lambda d: (d.close,),
        "keltner": lambda d: (d.high, d.low, d.close), "adx": lambda d: (d.high, d.low, d.close),
        "cci": lambda d: (d.high, d.low, d.close), "obv": lambda d: (d.close, d.volume),
        "vwap": lambda d: (d.high, d.low, d.close, d.volume, 10), "mfi": lambda d: (d.high, d.low, d.close, d.volume),
        "cmf": lambda d: (d.high, d.low, d.close, d.volume), "ichimoku": lambda d: (d.high, d.low, d.close),
        "parabolic_sar": lambda d: (d.high, d.low), "supertrend": lambda d: (d.high, d.low, d.close),
        "rolling_drawdown": lambda d: (d.close, 10), "rolling_max_drawdown": lambda d: (d.close, 10),
        "linreg_slope": lambda d: (d.close, 10),
    }

    @staticmethod
    def _outputs(res):
        return list(res) if isinstance(res, tuple) else [res]

    def test_registry_complete(self):
        self.assertGreaterEqual(len(I.REGISTRY), 25)
        self.assertEqual(set(I.REGISTRY), set(self.CALLS))

    def test_length_preserved_and_empty(self):
        d = _data(300)
        empty = generate(n=0, seed=1)
        for name, fn in I.REGISTRY.items():
            for out in self._outputs(fn(*self.CALLS[name](d))):
                self.assertEqual(len(out), 300, name)
            for out in self._outputs(fn(*self.CALLS[name](empty))):
                self.assertEqual(out, [], name)

    def test_causality_future_tamper(self):
        """篡改 i>=cut 的所有列，输出 [0,cut) 必须逐字节不变。"""
        d = _data(400)
        cut = 250
        tampered = d.slice(0, 400)
        r = random.Random(5)
        for i in range(cut, 400):
            f = r.uniform(0.5, 1.5)
            tampered.open[i] *= f
            tampered.close[i] *= f
            tampered.high[i] = max(tampered.open[i], tampered.close[i]) * 1.01
            tampered.low[i] = min(tampered.open[i], tampered.close[i]) * 0.99
            tampered.volume[i] *= r.uniform(0.1, 3.0)
        for name, fn in I.REGISTRY.items():
            a = self._outputs(fn(*self.CALLS[name](d)))
            b = self._outputs(fn(*self.CALLS[name](tampered)))
            for p, q in zip(a, b):
                self.assertEqual(p[:cut], q[:cut], name)

    def test_window_validation(self):
        bad = (0, -3, 1.5, True, "5")
        one, two = [1.0, 2.0, 3.0], ([1.0, 2.0], [1.0, 2.0], [1.0, 2.0])
        calls = {"sma": (one,), "ema": (one,), "wma": (one,), "hma": (one,), "kama": (one,), "rsi": (one,),
                 "roc": (one,), "momentum": (one,), "rolling_max": (one,), "rolling_min": (one,),
                 "rolling_std": (one,), "linreg_slope": (one,), "rolling_max_drawdown": (one,),
                 "atr": two, "adx": two, "cci": two, "aroon": two[:2], "donchian": two[:2],
                 "mfi": two + (one[:2],), "cmf": two + (one[:2],), "vwap": two + (one[:2],)}
        for name, args in calls.items():
            for b in bad:
                with self.assertRaises(ConfigError, msg=f"{name}({b!r})"):
                    I.REGISTRY[name](*args, b)
        with self.assertRaises(ConfigError):
            I.ichimoku([1.0], [1.0], [1.0], 0)

    def test_default_parameters_pinned(self):
        """默认窗口是 API 的一部分：用签名钉住，防止静默改动。"""
        import inspect
        expected = {"kama": {"n": 10, "fast": 2, "slow": 30}, "rsi": {"n": 14},
                    "macd": {"fast": 12, "slow": 26, "signal": 9}, "stochastic": {"k": 14, "d": 3},
                    "williams_r": {"n": 14}, "aroon": {"n": 25}, "atr": {"n": 14}, "rolling_std": {"ddof": 0},
                    "bollinger": {"n": 20, "k": 2.0}, "zscore": {"n": 20}, "keltner": {"n": 20, "k": 2.0, "atr_n": 10},
                    "adx": {"n": 14}, "cci": {"n": 20}, "vwap": {"n": None}, "mfi": {"n": 14}, "cmf": {"n": 20},
                    "ichimoku": {"tenkan_n": 9, "kijun_n": 26, "senkou_n": 52, "disp": 26},
                    "parabolic_sar": {"af0": 0.02, "af_step": 0.02, "af_max": 0.2},
                    "supertrend": {"n": 10, "mult": 3.0}, "ema": {"alpha": None}}
        for name, defaults in expected.items():
            sig = inspect.signature(I.REGISTRY[name])
            got = {k: p.default for k, p in sig.parameters.items() if p.default is not inspect.Parameter.empty}
            self.assertEqual(got, defaults, name)

    def test_single_bar(self):
        d = _data(1)
        for name, fn in I.REGISTRY.items():
            for out in self._outputs(fn(*self.CALLS[name](d))):
                self.assertEqual(len(out), 1, name)


if __name__ == "__main__":
    unittest.main()
