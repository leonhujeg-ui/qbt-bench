"""阶段 0 判据：合成数据可复现、前缀一致、结构合法、校验器逐类拒绝非法输入。"""
import math
import os
import tempfile
import unittest

from qbt.data import OHLCV, GenSpec, generate, generate_universe, SECONDS_PER_YEAR
from qbt.errors import DataError
from tests._helpers import QBTTestCase, scale


class TestReproducibility(QBTTestCase):
    def test_same_seed_bytewise_identical(self):
        a = generate(n=scale(20_000, 2_000), seed=7)
        b = generate(n=scale(20_000, 2_000), seed=7)
        self.assertEqual(a.fingerprint(), b.fingerprint())
        for col in ("ts", "open", "high", "low", "close", "volume"):
            self.assertEqual(getattr(a, col), getattr(b, col))   # 精确相等，无容差：确定性要求逐字节一致

    def test_different_seed_differs(self):
        a = generate(n=500, seed=1)
        b = generate(n=500, seed=2)
        self.assertNotEqual(a.close, b.close)
        self.assertNotEqual(a.fingerprint(), b.fingerprint())

    def test_prefix_consistency(self):
        """每根 bar 消耗固定随机数 => 长序列的前缀等于短序列。"""
        short = generate(n=300, seed=11)
        long = generate(n=1000, seed=11)
        self.assertEqual(short.close, long.close[:300])
        self.assertEqual(short.volume, long.volume[:300])
        self.assertEqual(short.ts, long.ts[:300])

    def test_fingerprint_sensitive_to_single_value(self):
        a = generate(n=200, seed=3)
        fp = a.fingerprint()
        b = a.slice(0, 200)
        b.close[150] = b.close[150] + 1e-9
        self.assertNotEqual(fp, b.fingerprint())
        c = a.slice(0, 200)
        c.symbol = "OTHER"
        self.assertNotEqual(fp, c.fingerprint())


class TestStructure(QBTTestCase):
    def setUp(self):
        self.spec = GenSpec(n=scale(50_000, 5_000), seed=42, halt_prob=0.002, gap_prob=0.05, jump_prob=0.01)
        self.d = generate(self.spec)

    def test_validates_and_lengths(self):
        d = self.d
        self.assertEqual(len(d), self.spec.n)
        self.assertTrue(d._validated)
        self.assertIs(d.validate(), d)      # 已校验：走缓存早返回
        d._validated = False
        self.assertIs(d.validate(), d)

    def test_timestamps_regular(self):
        ts = self.d.ts
        self.assertEqual(ts[0], self.spec.start_ts)
        self.assertTrue(all(ts[i + 1] - ts[i] == self.spec.bar_seconds for i in range(len(ts) - 1)))
        self.assertEqual(self.d.bar_seconds, self.spec.bar_seconds)

    def test_bar_geometry(self):
        d = self.d
        for i in range(len(d)):
            self.assertLessEqual(d.low[i], min(d.open[i], d.close[i]))
            self.assertGreaterEqual(d.high[i], max(d.open[i], d.close[i]))
            self.assertGreater(d.low[i], 0.0)
            self.assertGreaterEqual(d.volume[i], 0.0)

    def test_halts_flat_and_zero_volume(self):
        d = self.d
        halts = [i for i in range(1, len(d)) if d.volume[i] == 0.0]
        self.assertGreater(len(halts), 0)
        for i in halts:
            self.assertEqual((d.open[i], d.high[i], d.low[i], d.close[i]), (d.close[i - 1],) * 4)
        # 停牌段长度恰为 halt_len 的倍数（连续段）：首个停牌段长度 == halt_len
        first = halts[0]
        run = 0
        while first + run < len(d) and d.volume[first + run] == 0.0:
            run += 1
        self.assertEqual(run % self.spec.halt_len, 0)
        self.assertGreaterEqual(run, self.spec.halt_len)

    def test_gaps_present_and_absent(self):
        d = self.d
        gaps = sum(1 for i in range(1, len(d)) if d.volume[i] > 0 and d.open[i] != d.close[i - 1])
        self.assertGreater(gaps, 0)
        nog = generate(n=2000, seed=5, gap_prob=0.0, halt_prob=0.0)
        self.assertTrue(all(nog.open[i] == nog.close[i - 1] for i in range(1, len(nog))))

    def test_jumps_increase_dispersion(self):
        base = generate(n=20_000, seed=9, jump_prob=0.0, halt_prob=0.0, gap_prob=0.0)
        jumpy = generate(n=20_000, seed=9, jump_prob=0.5, jump_sigma=0.05, halt_prob=0.0, gap_prob=0.0)

        def var(d):
            r = [math.log(d.close[i] / d.open[i]) for i in range(len(d))]
            m = sum(r) / len(r)
            return sum((x - m) ** 2 for x in r) / len(r)
        self.assertGreater(var(jumpy), 10 * var(base))

    def test_gbm_moments(self):
        """无跳跃/缺口/停牌时，对数收益均值与方差应接近理论值（样本量 2e5，容差 5 个标准误）。"""
        n = scale(200_000, 50_000)
        spec = GenSpec(n=n, seed=1, mu=0.1, sigma=0.5, jump_prob=0.0, halt_prob=0.0, gap_prob=0.0)
        d = generate(spec)
        dt = spec.bar_seconds / SECONDS_PER_YEAR
        r = [math.log(d.close[i] / d.open[i]) for i in range(n)]
        mean = sum(r) / n
        var = sum((x - mean) ** 2 for x in r) / (n - 1)
        exp_mean = (spec.mu - 0.5 * spec.sigma ** 2) * dt
        exp_var = spec.sigma ** 2 * dt
        se_mean = math.sqrt(exp_var / n)
        self.assertLess(abs(mean - exp_mean), 5 * se_mean)
        self.assertLess(abs(var / exp_var - 1.0), 5 * math.sqrt(2.0 / (n - 1)))

    def test_volume_positive_when_trading(self):
        d = self.d
        self.assertTrue(all(v > 0 for v in d.volume if v != 0.0))
        self.assertGreater(sum(d.volume) / len(d), 0.5 * self.spec.base_volume)


class TestValidation(QBTTestCase):
    def _d(self, **kw):
        d = generate(n=50, seed=1, halt_prob=0.0)
        out = d.slice(0, 50)
        out._validated = False
        for k, v in kw.items():
            getattr(out, k)[10] = v
        return out

    def test_rejects_nan_inf(self):
        for bad in (math.nan, math.inf, -math.inf):
            with self.assertRaises(DataError):
                self._d(close=bad).validate()
            with self.assertRaises(DataError):
                self._d(volume=bad).validate()

    def test_rejects_non_positive_price(self):
        with self.assertRaises(DataError):
            self._d(low=0.0).validate()
        with self.assertRaises(DataError):
            self._d(low=-1.0).validate()

    def test_rejects_negative_volume(self):
        with self.assertRaises(DataError):
            self._d(volume=-1e-9).validate()

    def test_rejects_range_violation(self):
        d = self._d()
        d.high[10] = d.low[10] - 1e-6
        with self.assertRaises(DataError):
            d.validate()
        d = self._d()
        d.open[10] = d.high[10] + 1e-6
        with self.assertRaises(DataError):
            d.validate()
        d = self._d()
        d.close[10] = d.low[10] - 1e-6
        with self.assertRaises(DataError):
            d.validate()

    def test_rejects_unordered_or_duplicate_ts(self):
        d = self._d()
        d.ts[10] = d.ts[9]
        with self.assertRaises(DataError):
            d.validate()
        d = self._d()
        d.ts[10] = d.ts[9] - 1
        with self.assertRaises(DataError):
            d.validate()

    def test_rejects_length_mismatch_and_types(self):
        d = self._d()
        d.volume.pop()
        with self.assertRaises(DataError):
            d.validate()
        d = self._d()
        d.close[10] = "100"
        with self.assertRaises(DataError):
            d.validate()
        d = self._d()
        d.close[10] = None
        with self.assertRaises(DataError):
            d.validate()
        d = self._d()
        d.symbol = ""
        with self.assertRaises(DataError):
            d.validate()

    def test_empty_is_valid(self):
        d = OHLCV("E", [], [], [], [], [], [])
        self.assertEqual(len(d.validate()), 0)
        self.assertEqual(len(generate(n=0, seed=1)), 0)

    def test_spec_check(self):
        for kw in ({"n": -1}, {"s0": 0.0}, {"sigma": -0.1}, {"bar_seconds": 0}, {"jump_prob": 1.5},
                   {"halt_prob": -0.1}, {"halt_len": 0}, {"base_volume": 0.0}):
            with self.assertRaises(DataError, msg=str(kw)):
                generate(**kw, seed=1)
        self.assertEqual(GenSpec.from_dict(GenSpec(n=3, seed=4).to_dict()), GenSpec(n=3, seed=4))
        for kw in ({"sigma": math.nan}, {"s0": True}, {"mu": "0.1"}, {"wick": math.inf}, {"seed": 1.5}, {"symbol": ""},
                   {"n": True}):
            with self.assertRaises(DataError, msg=str(kw)):
                generate(**{"n": 5, "seed": 1, **kw})

    def test_generate_override_kwargs(self):
        spec = GenSpec(n=10, seed=1)
        d = generate(spec, seed=2)
        self.assertEqual(d.close, generate(n=10, seed=2).close)


class TestUtilities(QBTTestCase):
    def test_slice_copy_semantics(self):
        d = generate(n=100, seed=1)
        s = d.slice(10, 20)
        self.assertEqual(len(s), 10)
        self.assertEqual(s.close, d.close[10:20])
        s.close[0] = -1.0
        self.assertNotEqual(d.close[10], -1.0)
        self.assertEqual(len(d.slice(90, 500)), 10)
        self.assertEqual(len(d.slice(-5, None)), 5)

    def test_slots_and_slice_validated_flag(self):
        d = generate(n=10, seed=1)
        self.assertFalse(hasattr(d, "__dict__"))                 # __slots__ 契约（列式对象不带实例字典）
        self.assertTrue(d.slice(0, 5)._validated)                # 切片保留已校验标记（不重复 O(n) 校验）
        d._validated = False
        self.assertFalse(d.slice(0, 5)._validated)
        for kw in ({"s0": 0.0}, {"sigma": -0.1}, {"bar_seconds": 0}):
            with self.assertRaises(DataError) as ctx:
                generate(n=5, seed=1, **kw)
            self.assertIn("required", str(ctx.exception))        # 规格检查先于生成，给出精确原因

    def test_csv_roundtrip_exact(self):
        d = generate(n=300, seed=8, halt_prob=0.01)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "d.csv")
            d.to_csv(path)
            back = OHLCV.from_csv(path, symbol=d.symbol, bar_seconds=d.bar_seconds)
            self.assertEqual(back.validate().fingerprint(), d.fingerprint())   # repr 往返 => 逐字节相同
            with open(path, "a", encoding="utf-8") as f:
                f.write("1,2,3\n")
            with self.assertRaises(DataError):
                OHLCV.from_csv(path)
            with open(path, "w", encoding="utf-8") as f:
                f.write("a,b\n")
            with self.assertRaises(DataError):
                OHLCV.from_csv(path)

    def test_rows_and_universe(self):
        d = generate(n=5, seed=1)
        rows = d.to_rows()
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[2], (d.ts[2], d.open[2], d.high[2], d.low[2], d.close[2], d.volume[2]))
        uni = generate_universe(["A", "B"], 100, 3)
        self.assertEqual(sorted(uni), ["A", "B"])
        self.assertEqual(uni["A"].ts, uni["B"].ts)
        self.assertNotEqual(uni["A"].close, uni["B"].close)
        self.assertEqual(uni["A"].symbol, "A")
        again = generate_universe(["A", "B"], 100, 3)
        self.assertEqual(again["B"].fingerprint(), uni["B"].fingerprint())

    def test_bar_seconds_inferred(self):
        d = OHLCV("X", [0, 300, 600], [1, 1, 1], [1, 1, 1], [1, 1, 1], [1, 1, 1], [1, 1, 1])
        self.assertEqual(d.bar_seconds, 300)
        self.assertIsNone(OHLCV("X", [0], [1], [1], [1], [1], [1]).bar_seconds)


class TestGeneratorInternals(QBTTestCase):
    """针对生成器内部的判据：Box-Muller 正态性、影线/成交量分布、默认值与黄金指纹。"""

    def _pure(self, n, seed=1, **kw):
        return generate(GenSpec(n=n, seed=seed, jump_prob=0.0, halt_prob=0.0, gap_prob=0.0, **kw))

    def test_normal_fractions(self):
        """标准正态：P(|z|<1)=0.6827, P(|z|<2)=0.9545，容差 5 个二项标准误。"""
        n = scale(200_000, 50_000)
        spec = GenSpec(n=n, seed=2, mu=0.0, sigma=0.5, bar_seconds=86400, jump_prob=0.0, halt_prob=0.0, gap_prob=0.0)
        d = generate(spec)
        dt = spec.bar_seconds / SECONDS_PER_YEAR
        drift, vol = (spec.mu - 0.5 * spec.sigma ** 2) * dt, spec.sigma * math.sqrt(dt)
        z = [(math.log(d.close[i] / d.open[i]) - drift) / vol for i in range(n)]
        for k, p in ((1.0, 0.682689), (2.0, 0.954500)):
            frac = sum(1 for x in z if abs(x) < k) / n
            self.assertLess(abs(frac - p), 5 * math.sqrt(p * (1 - p) / n), f"k={k} frac={frac}")
        kurt = sum(x ** 4 for x in z) / n / (sum(x * x for x in z) / n) ** 2
        self.assertLess(abs(kurt - 3.0), 5 * math.sqrt(24.0 / n))

    def test_wick_half_normal_moments(self):
        """上/下影线 = wick*|z|，半正态均值 sqrt(2/pi)、方差 1-2/pi；容差 5 个标准误。"""
        n = scale(200_000, 50_000)
        spec = GenSpec(n=n, seed=3, sigma=0.4, wick=0.6, jump_prob=0.0, halt_prob=0.0, gap_prob=0.0)
        d = generate(spec)
        w = spec.wick * spec.sigma * math.sqrt(spec.bar_seconds / SECONDS_PER_YEAR)
        up = [math.log(d.high[i] / max(d.open[i], d.close[i])) / w for i in range(n)]
        dn = [math.log(min(d.open[i], d.close[i]) / d.low[i]) / w for i in range(n)]
        mean_hn, var_hn = math.sqrt(2 / math.pi), 1 - 2 / math.pi
        for arr in (up, dn):
            m = sum(arr) / n
            self.assertLess(abs(m - mean_hn), 5 * math.sqrt(var_hn / n))
            v = sum((x - m) ** 2 for x in arr) / (n - 1)
            self.assertLess(abs(v - var_hn) / var_hn, 5 * math.sqrt(2.0 / n) * 2)   # 半正态峰度更高，放宽 2 倍

    def test_volume_scales_with_move(self):
        d = self._pure(scale(100_000, 20_000), seed=4)
        r = [abs(math.log(d.close[i] / d.open[i])) for i in range(len(d))]
        order = sorted(range(len(d)), key=lambda i: r[i])
        q = len(d) // 4
        small = sum(d.volume[i] for i in order[:q]) / q
        big = sum(d.volume[i] for i in order[-q:]) / q
        self.assertGreater(big, 1.5 * small)

    def test_pinned_same_machine_bytewise(self):
        """同机：进程内两次 + 一个子进程，指纹（sha256 of raw doubles）逐字节相同。"""
        import subprocess
        import sys
        fp = generate(n=2000, seed=123).fingerprint()
        self.assertEqual(generate(n=2000, seed=123).fingerprint(), fp)
        code = "import sys; sys.path.insert(0, %r); from qbt.data import generate; print(generate(n=2000, seed=123).fingerprint())"
        out = subprocess.run([sys.executable, "-c", code % os.path.dirname(os.path.dirname(os.path.abspath(__file__)))],
                             capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(out, fp)

    def test_pinned_cross_machine_within_tolerance(self):
        """跨机：与黄金文件里存的前 200 根逐列对比，时间戳精确、价格/成交量相对 1e-9（libm 的 1 ulp 远小于此）。"""
        import json
        from tests._compare import assert_close, perturb, ShapeMismatch, MONEY_REL
        from tools.make_golden import GOLDEN_PATH
        with open(GOLDEN_PATH, encoding="utf-8") as f:
            pinned = json.load(f)["pinned_data"]
        gen = generate(n=2000, seed=123)
        uni = generate_universe(["A", "B"], 500, 9)["B"]
        for key, d in (("gen_n2000_seed123_head", gen), ("universe_B_n500_seed9_head", uni)):
            g = pinned[key]
            self.assertEqual(d.ts[:len(g["ts"])], g["ts"])
            for col in ("open", "high", "low", "close", "volume"):
                assert_close(getattr(d, col)[:len(g[col])], g[col], MONEY_REL, 0.0, what=f"{key}.{col}")
            with self.assertRaises(ShapeMismatch):                       # 放水判据：改错 1e-6 必红
                assert_close(perturb(g["close"], 1e-6), g["close"], MONEY_REL, 0.0)

    def test_defaults_pinned(self):
        self.assertEqual(GenSpec().to_dict(), {
            "n": 10_000, "seed": 0, "symbol": "SYN", "s0": 100.0, "mu": 0.05, "sigma": 0.40,
            "bar_seconds": 60, "start_ts": 1_600_000_000, "jump_prob": 0.001, "jump_sigma": 0.02,
            "halt_prob": 0.0005, "halt_len": 5, "gap_prob": 0.002, "gap_sigma": 0.005,
            "base_volume": 1000.0, "vol_sigma": 0.6, "wick": 0.6})
        spec = GenSpec(n=5, seed=1, s0=0.5, sigma=0.0)
        self.assertIs(spec.check(), spec)
        d = generate(spec)
        self.assertTrue(all(abs(c - 0.5) < 1e-6 for c in d.close))   # sigma=0：只有漂移，价格几乎不变

    def test_validation_messages(self):
        with self.assertRaises(DataError) as ctx:
            TestValidation._d(self, close="100").validate()
        self.assertIn("non-numeric", str(ctx.exception))
        self.assertIn("'100'", str(ctx.exception))
        for bad, token in ((math.nan, "nan"), (-math.inf, "-inf"), (math.inf, "inf")):
            with self.assertRaises(DataError) as ctx:
                TestValidation._d(self, volume=bad).validate()
            self.assertIn("non-finite", str(ctx.exception))
            self.assertIn(token, str(ctx.exception))

    def test_two_bar_unordered_ts_rejected(self):
        d = OHLCV("X", [10, 9], [1, 1], [1, 1], [1, 1], [1, 1], [1, 1])
        with self.assertRaises(DataError):
            d.validate()
        e = OHLCV("E", [], [], [], [], [], [])
        e.validate()
        self.assertTrue(e._validated)


if __name__ == "__main__":
    unittest.main()
