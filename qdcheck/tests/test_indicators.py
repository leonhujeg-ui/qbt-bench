"""G1 判据：参考实现的因果性 / 长度保持 / 预热期公式 / 常数闭式解 / 线性与周期 1 恒等式 / 手算样例，
以及与第二份独立写法（indicators_alt）逐点对拍（相对容差 1e-9）。"""
import math
import unittest
from fractions import Fraction as F

from qdcheck import compare as C
from qdcheck import gen_vectors as G
from qdcheck import indicators_alt as A
from qdcheck import indicators_ref as R
from qdcheck import props as P
from qdcheck import synth
from qdcheck.tests._util import scale

SERIES = dict(G.all_series())


def _bars(close, high=None, low=None, volume=None):
    n = len(close)
    return {"open": list(close), "high": list(high or close), "low": list(low or close), "close": list(close),
            "volume": list(volume or [1000.0] * n)}


def _approx(test, got, want, tol=1e-12):
    test.assertEqual(len(got), len(want))
    for i, (g, w) in enumerate(zip(got, want)):
        if w is None:
            test.assertIsNone(g, f"index {i}")
        else:
            test.assertIsNotNone(g, f"index {i}")
            test.assertTrue(abs(g - float(w)) <= tol * max(1.0, abs(float(w))), f"index {i}: {g} vs {float(w)}")


def _frac_ema(x, n, alpha, seed):
    """有理数精确递推（第三份写法，只用于手算样例）。"""
    out = [None] * len(x)
    a = F(alpha)
    if seed == "sma":
        if len(x) < n:
            return out
        e = sum(F(v) for v in x[:n]) / n
        out[n - 1] = e
        for t in range(n, len(x)):
            e = a * F(x[t]) + (1 - a) * e
            out[t] = e
        return out
    e = F(x[0])
    out[0] = e
    for t in range(1, len(x)):
        e = a * F(x[t]) + (1 - a) * e
        out[t] = e
    return out


class TestResolve(unittest.TestCase):
    def test_variant_strings_are_canonical_and_listed(self):
        for ind in R.INDICATORS:
            self.assertGreaterEqual(len(R.VARIANTS[ind]), 2)
            for v in R.VARIANTS[ind]:
                self.assertEqual(R.canonical_variant(v), v)
        self.assertEqual(R.resolve("dema", "seed=first;ma=ema")[0], "ma=ema;seed=first")
        self.assertEqual(R.resolve("dema")[0], R.VARIANTS["dema"][0])
        self.assertEqual(len(R.all_variants()), sum(len(v) for v in R.VARIANTS.values()))

    def test_bad_names_and_params_raise_value_error(self):
        bad = [("nope", None, None), ("dema", "ma=wma", None), ("dema", "ma", None), ("dema", "ma=ema;ma=sma", None),
               ("dema", "=x", None), ("dema", None, {"period": 0}), ("dema", None, {"period": True}),
               ("dema", None, {"period": 2.5}), ("dema", None, {"bogus": 1}), ("trix", None, {"scale": float("nan")}),
               ("trix", None, {"scale": -1.0}), ("cci", None, {"constant": "0.015"}), ("trix", None, {"scale": True})]
        for ind, var, params in bad:
            with self.assertRaises(ValueError, msg=(ind, var, params)):
                R.resolve(ind, var, params)

    def test_bad_input_raises_value_error(self):
        good = _bars([1.0, 2.0])
        for mutate in (lambda d: d.pop("volume"), lambda d: d["high"].append(3.0),
                       lambda d: d["close"].__setitem__(0, float("nan")), lambda d: d["low"].__setitem__(1, True),
                       lambda d: d["open"].__setitem__(0, "1")):
            d = {k: list(v) for k, v in good.items()}
            mutate(d)
            with self.assertRaises(ValueError):
                R.compute("dema", d)
        with self.assertRaises(ValueError):
            R.compute("dema", good, warmup="zero")
        with self.assertRaises(ValueError):
            R.apply_warmup({"value": [None]}, "zero")

    def test_warmup_nan_mode(self):
        d = SERIES["randwalk"]
        for ind in R.INDICATORS:
            a = R.compute(ind, d)
            b = R.compute(ind, d, warmup="nan")
            c = A.compute(ind, d, warmup="nan")
            for line in R.LINES[ind]:
                for x, y, z in zip(a[line], b[line], c[line]):
                    if x is None:
                        self.assertTrue(math.isnan(y) and math.isnan(z))
                    else:
                        self.assertEqual(x, y)


class TestHandComputed(unittest.TestCase):
    x = [1.0, 2.0, 4.0, 3.0, 5.0, 8.0, 6.0]

    def test_ema_seeds_against_exact_rationals(self):
        for n in (1, 2, 3):
            for ma, alpha in (("ema", F(2, n + 1)), ("rma", F(1, n))):
                for seed in ("sma", "first"):
                    _approx(self, R.ma_series(self.x, n, ma, seed), _frac_ema(self.x, n, alpha, seed))
                    _approx(self, A.ma(self.x, n, ma, seed), _frac_ema(self.x, n, alpha, seed))
                q = 1 - alpha
                adj = [sum(q ** k * F(self.x[t - k]) for k in range(t + 1)) / sum(q ** k for k in range(t + 1))
                       for t in range(len(self.x))]
                _approx(self, R.ma_series(self.x, n, ma, "adjusted"), adj)
                _approx(self, A.ma(self.x, n, ma, "adjusted"), adj)
        sma3 = [None, None] + [sum(F(v) for v in self.x[t - 2:t + 1]) / 3 for t in range(2, 7)]
        _approx(self, R.ma_series(self.x, 3, "sma"), sma3)

    def test_ema_does_not_cross_interior_gaps(self):
        x = [None, 1.0, 2.0, None, 3.0, 4.0]
        for impl in (R.ma_series, A.ma):
            self.assertEqual(impl(x, 2, "ema", "first")[3:], [None, None, None])
            self.assertEqual(impl(x, 2, "ema", "sma")[:3], [None, None, 1.5])
            self.assertEqual(impl(x, 2, "sma")[4:], [None, 3.5])
        self.assertEqual(R.ma_series([None, None], 2, "ema", "first"), [None, None])

    def test_stochrsi_small_periods(self):
        d = _bars([1.0, 2.0, 1.0, 3.0, 2.0, 4.0])
        p = {"rsi_period": 2, "stoch_period": 2, "k_period": 1, "d_period": 1}
        for impl in (R, A):
            out = impl.compute("stochrsi", d, "flat=talib;rsi=wilder", p)
            _approx(self, out["k"], [None, None, None, 100.0, 0.0, 100.0])
            _approx(self, out["d"], [None, None, None, 100.0, 0.0, 100.0])
        # SMA-RSI：t=2 窗口变化 (+1,-1) => 50；t=3 (-1,+2) => 66.67；t=4 (+2,-1) => 66.67；t=5 (-1,+2) => 66.67
        out = R.compute("stochrsi", d, "flat=pine;rsi=sma", dict(p, k_period=1))
        _approx(self, out["k"], [None, None, None, 100.0, None, None])

    def test_cci_ao_force_small(self):
        d = _bars([1.0, 2.0, 3.0, 4.0], volume=[5.0, 2.0, 4.0, 1.0])
        for impl in (R, A):
            _approx(self, impl.compute("cci", d, "src=close;zero_dev=zero", {"period": 3})["value"],
                    [None, None, 100.0, 100.0])
            _approx(self, impl.compute("force", d, "ma=raw")["value"], [None, 2.0, 4.0, 1.0])
            _approx(self, impl.compute("force", d, "ma=sma", {"period": 2})["value"], [None, None, 3.0, 2.5])
        d2 = _bars([1.0, 2.0, 4.0, 8.0])
        for impl in (R, A):
            _approx(self, impl.compute("ao", d2, "ma=sma;src=close", {"fast": 2, "slow": 3})["value"],
                    [None, None, F(2, 3), F(4, 3)])
            hl = impl.compute("ao", _bars([5.0] * 4, high=[2.0, 4.0, 8.0, 16.0], low=[0.0] * 4),
                              "ma=sma;src=hl2", {"fast": 2, "slow": 3})["value"]
            _approx(self, hl, [None, None, F(2, 3), F(4, 3)])

    def test_kdj_small(self):
        d = _bars([9.0, 10.0, 11.0, 10.0], high=[10.0, 11.0, 12.0, 11.0], low=[8.0, 9.0, 10.0, 9.0])
        p = {"n": 3, "m1": 3, "m2": 3}
        k2 = (F(75) + 2 * 50) / 3
        d2 = (k2 + 2 * 50) / 3
        k3 = (F(100, 3) + 2 * k2) / 3
        d3 = (k3 + 2 * d2) / 3
        for impl in (R, A):
            out = impl.compute("kdj", d, "init=50;ma=rma;zero_range=zero", p)
            _approx(self, out["k"], [None, None, k2, k3])
            _approx(self, out["d"], [None, None, d2, d3])
            _approx(self, out["j"], [None, None, 3 * k2 - 2 * d2, 3 * k3 - 2 * d3])
            first = impl.compute("kdj", d, "init=first;ma=ema;zero_range=zero", p)
            ke3 = F(1, 2) * F(100, 3) + F(1, 2) * 75
            _approx(self, first["k"], [None, None, 75, ke3])
            _approx(self, first["d"], [None, None, 75, F(1, 2) * ke3 + F(1, 2) * 75])
            sma = impl.compute("kdj", d, "ma=sma;zero_range=half", {"n": 3, "m1": 2, "m2": 2})
            _approx(self, sma["k"], [None, None, None, (F(75) + F(100, 3)) / 2])

    def test_ichimoku_shifts(self):
        h = [float(v) for v in (3, 5, 4, 6, 8, 7, 9)]
        lo = [float(v) for v in (1, 2, 2, 3, 5, 4, 6)]
        c = [float(v) for v in (2, 4, 3, 5, 7, 6, 8)]
        d = _bars(c, h, lo)
        p = {"tenkan": 2, "kijun": 3, "senkou_b": 4, "displacement": 2}
        tenkan = [None] + [(max(h[t - 1:t + 1]) + min(lo[t - 1:t + 1])) / 2 for t in range(1, 7)]
        kijun = [None, None] + [(max(h[t - 2:t + 1]) + min(lo[t - 2:t + 1])) / 2 for t in range(2, 7)]
        base_a = [None, None] + [(tenkan[t] + kijun[t]) / 2 for t in range(2, 7)]
        base_b = [None] * 3 + [(max(h[t - 3:t + 1]) + min(lo[t - 3:t + 1])) / 2 for t in range(3, 7)]
        for impl in (R, A):
            none = impl.compute("ichimoku", d, "shift=none", p)
            fwd = impl.compute("ichimoku", d, "shift=forward", p)
            tv = impl.compute("ichimoku", d, "shift=forward_tv", p)
            self.assertEqual(none["tenkan"], tenkan)
            self.assertEqual(none["kijun"], kijun)
            self.assertEqual(none["senkou_a"], base_a)
            self.assertEqual(fwd["senkou_a"], [None, None] + base_a[:5])
            self.assertEqual(fwd["senkou_b"], [None, None] + base_b[:5])
            self.assertEqual(tv["senkou_b"], [None] + base_b[:6])
            self.assertEqual(none["chikou"], c)
            self.assertEqual(fwd["chikou"], c[2:] + [None, None])
            self.assertEqual(tv["chikou"], c[1:] + [None])

    def test_period_one_is_identity(self):
        d = SERIES["jumps"]
        c = d["close"]
        pct = [None] + [100.0 * (c[t] / c[t - 1] - 1.0) for t in range(1, len(c))]
        for variant in R.VARIANTS["dema"]:
            for impl in (R, A):
                _approx(self, impl.compute("dema", d, variant, {"period": 1})["value"], c, 1e-12)
                _approx(self, impl.compute("tema", d, variant, {"period": 1})["value"], c, 1e-12)
                _approx(self, impl.compute("trix", d, variant, {"period": 1})["value"], pct, 1e-9)

    def test_dema_tema_track_linear_input_exactly(self):
        """线性输入 x_t = a + b t：SMA 种子的 EMA 与 SMA 都恰好滞后 b(n-1)/2，故 DEMA = TEMA = x（预热后）。"""
        d = SERIES["mono_up"]
        for variant in ("ma=ema;seed=sma", "ma=sma"):
            for ind in ("dema", "tema"):
                for impl in (R, A):
                    out = impl.compute(ind, d, variant)["value"]
                    w = P.expected_warmup(ind, variant)["value"]
                    _approx(self, out, [None] * w + d["close"][w:], 1e-12)
        rma = R.compute("dema", d, "ma=rma;seed=sma")["value"]
        w = P.expected_warmup("dema", "ma=rma;seed=sma")["value"]
        self.assertGreater(abs(rma[w] - d["close"][w]), 0.5)          # RMA 的 SMA 种子与稳态滞后不同，预热后有暂态


class TestProperties(unittest.TestCase):
    def test_causality_every_variant(self):
        names = scale(["randwalk", "jumps", "longflat"], ["jumps", "longflat"])
        for ind, variant in R.all_variants():
            fn = (lambda d, ind=ind, variant=variant: R.compute(ind, d, variant))
            for name in names:
                n = len(SERIES[name]["close"])
                res = P.check_causality(fn, SERIES[name], [10, n // 3, n // 2, n - 5], seed=len(name),
                                        exempt=P.chikou_exempt(ind, variant))
                self.assertTrue(res["ok"], (ind, variant, name, res))

    def test_causality_detects_lookahead(self):
        leaky = lambda d: {"value": [d["close"][min(t + 1, len(d["close"]) - 1)] for t in range(len(d["close"]))]}
        res = P.check_causality(leaky, SERIES["randwalk"], [20, 60])
        self.assertFalse(res["ok"])
        self.assertEqual((res["line"], res["cut"], res["first"]["index"]), ("value", 20, 19))

    def test_chikou_forward_is_exact_displacement(self):
        d = SERIES["randwalk"]
        c = d["close"]
        for variant, off in (("shift=forward", 26), ("shift=forward_tv", 25)):
            ch = R.compute("ichimoku", d, variant)["chikou"]
            self.assertEqual(ch, [c[t + off] if t + off < len(c) else None for t in range(len(c))])
        self.assertEqual(P.chikou_exempt("ichimoku", "shift=none"), ())
        self.assertEqual(P.chikou_exempt("dema", "ma=sma"), ())

    def test_length_preserved_including_empty(self):
        base = synth.ohlcv(synth.generate(40, 3))
        make = lambda n: {k: v[:n] for k, v in base.items()}
        for ind, variant in R.all_variants():
            for impl in (R, A):
                res = P.check_length(lambda d: impl.compute(ind, d, variant), make, [0, 1, 2, 3, 5, 8, 13, 40])
                self.assertTrue(res["ok"], (ind, variant, res))
        bad = P.check_length(lambda d: {"value": d["close"][:-1] if d["close"] else []}, make, [0, 3])
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["n"], 3)

    def test_warmup_positions_match_formula(self):
        for name in ("randwalk", "trend"):
            d = SERIES[name]
            for ind, variant in R.all_variants():
                for tag, override in G.param_sets(ind):
                    out = R.compute(ind, d, variant, override)
                    want = P.expected_warmup(ind, variant, override)
                    for line, vals in out.items():
                        self.assertEqual(P.leading_nulls(vals), want[line], (name, ind, variant, tag, line))
                        tail = vals[want[line]:]
                        if ind == "ichimoku" and line == "chikou":          # 前移口径的迟行线末尾 off 个点无值
                            tail = tail[:len(tail) - P.leading_nulls(vals[::-1])]
                        self.assertNotIn(None, tail, (name, ind, variant, line))

    def test_constant_input_closed_forms(self):
        for c, n in ((100.0, 80), (37.25, 120)):
            d = {"open": [c] * n, "high": [c] * n, "low": [c] * n, "close": [c] * n, "volume": [500.0] * n}
            for ind, variant in R.all_variants():
                for impl in (R, A):
                    want = P.constant_expectation(ind, variant, None, n, c)
                    got = impl.compute(ind, d, variant)
                    for line in R.LINES[ind]:
                        res = C.compare_series(want[line], got[line], 1e-9, 1e-9, "none")
                        self.assertTrue(res["ok"], (impl.__name__, ind, variant, line, C.format_result(res)))
        with self.assertRaises(ValueError):
            P.constant_expectation("kdj", R.VARIANTS["kdj"][0], {"m2": 4}, 10, 1.0)

    def test_constant_kdj_init50_is_not_constant(self):
        """init=50 + 零极差取 0：K 从 50 按 (1-a)^m 衰减，闭式解本身要能区分口径。"""
        d = _bars([5.0] * 30)
        k = R.compute("kdj", d, "init=50;ma=rma;zero_range=zero")["k"]
        self.assertAlmostEqual(k[8], 50.0 * 2 / 3, delta=1e-12)
        self.assertAlmostEqual(k[29], 50.0 * (2 / 3) ** 22, delta=1e-12)


class TestCrossCheck(unittest.TestCase):
    def _cross(self, data, label):
        worst = 0.0
        for ind, variant in R.all_variants():
            for _tag, override in G.param_sets(ind):
                res = P.check_against(R.compute(ind, data, variant, override), A.compute(ind, data, variant, override))
                self.assertTrue(res["ok"], (label, ind, variant, override, res))
                worst = max(worst, res["max_tol_ratio"])
        return worst

    def test_vector_series(self):
        names = scale(sorted(SERIES), ["jumps", "longflat", "short5"])
        for name in names:
            self.assertLess(self._cross(SERIES[name], name), 1.0)

    def test_extra_random_series_with_all_features(self):
        for seed in range(scale(4, 1)):
            data = synth.ohlcv(synth.generate(200, 900 + seed, gap_prob=0.05, jump_prob=0.03, seg_min=15, seg_max=60,
                                              flat_noise=0.0 if seed % 2 else 0.1))
            self._cross(data, seed)

    def test_check_against_reports_mismatch(self):
        self.assertFalse(P.check_against({"a": [1.0]}, {"b": [1.0]})["ok"])
        res = P.check_against([1.0, 2.0], [1.0, 2.0 * (1 + 1e-7)])
        self.assertFalse(res["ok"])
        self.assertEqual(res["first"]["index"], 1)
        self.assertTrue(P.check_against([1.0, 2.0], [1.0, 2.0 * (1 + 1e-10)])["ok"])


if __name__ == "__main__":
    unittest.main()
