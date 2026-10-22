"""synth.py：同 seed 同结果、前缀稳定、OHLC 合法、停牌 / 缺口 / 跳跃 / 横盘 / 趋势特征、参数校验、CLI。"""
import json
import os
import tempfile
import unittest

from qdcheck import synth as S
from qdcheck.tests._util import captured

COLS = ("open", "high", "low", "close", "volume")


class TestSynth(unittest.TestCase):
    def setUp(self):
        self.d = S.generate(3000, 7, gap_prob=0.02, jump_prob=0.01)

    def test_same_seed_same_result_and_prefix_stable(self):
        again = S.generate(3000, 7, gap_prob=0.02, jump_prob=0.01)
        self.assertEqual(self.d, again)
        short = S.generate(1234, 7, gap_prob=0.02, jump_prob=0.01)
        for k in COLS:
            self.assertEqual(short[k], self.d[k][:1234])
        other = S.generate(3000, 8, gap_prob=0.02, jump_prob=0.01)
        self.assertNotEqual(other["close"], self.d["close"])
        self.assertEqual(S.generate(0, 1)["close"], [])

    def test_ohlc_valid_and_quantised(self):
        tick = S.DEFAULTS["tick"]
        for o, h, lo, c, v in S.rows(self.d):
            self.assertTrue(0 < lo <= min(o, c) and max(o, c) <= h, (o, h, lo, c))
            for x in (o, h, lo, c):
                self.assertEqual(x / tick, round(x / tick))
            self.assertEqual(v, round(v))
            self.assertGreaterEqual(v, 0.0)
        self.assertEqual(S.ohlcv(self.d).keys(), set(COLS))

    def _segments(self, kind):
        return [(a, b) for a, b, k in self.d["segments"] if k == kind]

    def test_regimes_present_and_have_their_shape(self):
        kinds = {k for _a, _b, k in self.d["segments"]}
        self.assertEqual(kinds, set(S.KINDS))
        c, v = self.d["close"], self.d["volume"]
        for a, b in self._segments("halt"):
            self.assertEqual(set(v[a:b]), {0.0})
            self.assertEqual(len({x for k in ("open", "high", "low", "close") for x in self.d[k][a:b]}), 1)
            self.assertEqual(self.d["open"][a], c[a - 1] if a else S.DEFAULTS["s0"])
        for a, b in self._segments("flat"):
            if b - a >= 20:
                self.assertLess(max(c[a:b]) / min(c[a:b]) - 1, 0.05)
        ups = [c[b - 1] / c[a] for a, b in self._segments("trend_up") if b - a >= 60]
        downs = [c[b - 1] / c[a] for a, b in self._segments("trend_down") if b - a >= 60]
        self.assertTrue(ups and downs)
        self.assertGreater(sum(ups) / len(ups), sum(downs) / len(downs))
        segs = self.d["segments"]
        self.assertEqual((segs[0][0], segs[-1][1]), (0, 3000))
        self.assertTrue(all(x[1] == y[0] and x[2] != y[2] for x, y in zip(segs, segs[1:])))

    def test_events_gap_at_least_3pct_and_jump_8pct(self):
        o, c = self.d["open"], self.d["close"]
        kinds = {k for _i, k in self.d["events"]}
        self.assertEqual(kinds, set(S.EVENT_KINDS))
        for i, kind in self.d["events"]:
            if kind.startswith("gap"):
                r = o[i] / c[i - 1] - 1
                self.assertGreaterEqual(abs(r), 0.03)
                self.assertEqual(r > 0, kind == "gap_up")
            else:
                r = c[i] / o[i] - 1
                self.assertLess(abs(abs(r) - 0.08), 0.002)
                self.assertEqual(r > 0, kind == "jump_up")
        flat_or_halt = {i for a, b, k in self.d["segments"] if k in ("halt", "flat") for i in range(a, b)}
        self.assertFalse(flat_or_halt & {i for i, _k in self.d["events"]})

    def test_forced_segments_and_events(self):
        d = S.generate(200, 3, segments=[(50, 60, "flat"), (150, 10, "halt")], flat_noise=0.0,
                       events={20: "gap_down", 30: "jump_up", 55: "gap_up"}, gap_prob=0.0, jump_prob=0.0)
        self.assertEqual(len(set(d["close"][50:110])), 1)
        self.assertTrue(all(v > 0 for v in d["volume"][50:110]))
        self.assertEqual(set(d["volume"][150:160]), {0.0})
        self.assertEqual(d["events"], [[20, "gap_down"], [30, "jump_up"]])   # 横盘段内的强制事件被忽略
        base = S.generate(200, 3, gap_prob=0.0, jump_prob=0.0)
        self.assertEqual(base["close"][:20], d["close"][:20])
        tiny = S.generate(400, 5, s0=1.5, tick=0.25, vol=0.2, gap_prob=0.2, min_price_ticks=2)
        self.assertGreaterEqual(min(tiny["low"]), 0.5)
        for i, kind in tiny["events"]:
            if kind.startswith("gap"):
                self.assertGreaterEqual(abs(tiny["open"][i] / tiny["close"][i - 1] - 1), 0.03)

    def test_bad_parameters(self):
        bad = [dict(n=-1), dict(n=1.5), dict(n=True), dict(n=5, s0=0), dict(n=5, tick=-1), dict(n=5, seg_min=0),
               dict(n=5, seg_min=10, seg_max=5), dict(n=5, weights={"random": 0}), dict(n=5, weights={"x": 1}),
               dict(n=5, segments=[(0, 0, "flat")]), dict(n=5, segments=[(0, 3, "boom")]),
               dict(n=5, events={1: "crash"}), dict(n=5, events={-1: "gap_up"}), dict(n=5, bogus=1)]
        for kw in bad:
            with self.assertRaises(ValueError, msg=kw):
                S.generate(**kw)

    def test_pick_kind_boundaries(self):
        w = {"random": 1, "halt": 1}
        self.assertEqual(S._pick_kind(0.0, w), "random")
        self.assertEqual(S._pick_kind(0.4999, w), "random")
        self.assertEqual(S._pick_kind(0.5, w), "halt")
        self.assertEqual(S._pick_kind(1.0, w), "halt")

    def test_defaults_and_small_knobs(self):
        self.assertEqual(S.generate(5), S.generate(5, 0))
        grid = [S._pick_kind(i / 1000, S.DEFAULTS["weights"]) for i in range(1000)]
        self.assertEqual({k: grid.count(k) for k in S.KINDS},
                         {"random": 400, "trend_up": 200, "trend_down": 200, "flat": 100, "halt": 100})
        tiny_seg = S.generate(30, 2, seg_min=1, seg_max=1)
        self.assertEqual(sum(b - a for a, b, _k in tiny_seg["segments"]), 30)
        one = S.generate(10, 4, segments=[(3, 1, "halt")], weights={"random": 1})
        self.assertIn([3, 4, "halt"], one["segments"])
        self.assertEqual(one["segments"][0][:2], [0, 3])
        low = S.generate(200, 6, base_volume=0.01, weights={"random": 1})
        self.assertEqual(set(low["volume"]), {1.0})
        tiny = S.generate(400, 5, s0=1.5, tick=0.25, vol=0.2, gap_prob=0.2, min_price_ticks=2)
        for i, kind in tiny["events"]:
            if kind.startswith("gap"):
                r = abs(tiny["open"][i] / tiny["close"][i - 1] - 1)
                self.assertLessEqual(r, S.DEFAULTS["gap_max"] + 0.25 / tiny["close"][i - 1] + 1e-12)
        args = S.build_parser().parse_args([])
        self.assertEqual((args.n, args.seed, args.out), (1000, 0, "-"))

    def test_cli(self):
        with captured() as (out, _err):
            self.assertEqual(S.main(["--n", "5", "--seed", "2"]), 0)
        self.assertEqual(json.loads(out.getvalue())["close"], S.generate(5, 2)["close"])
        path = os.path.join(tempfile.mkdtemp(), "s.json")
        self.assertEqual(S.main(["--n", "3", "--out", path]), 0)
        with open(path) as f:
            self.assertEqual(json.load(f)["spec"]["n"], 3)


if __name__ == "__main__":
    unittest.main()
