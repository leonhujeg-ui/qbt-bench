import os
import unittest

from qdexsim import feed as feed_mod
from qdexsim.tests._util import tmpdir


class TestFeed(unittest.TestCase):
    def test_random_walk_deterministic_and_quantized(self):
        a = feed_mod.Feed({"seed": 3, "vol": 0.01}, ticks={"BTCUSDT": 0.1, "ETHUSDT": 0.01})
        b = feed_mod.Feed({"seed": 3, "vol": 0.01}, ticks={"BTCUSDT": 0.1, "ETHUSDT": 0.01})
        sa = [a.step() for _ in range(20)]
        sb = [b.step() for _ in range(20)]
        self.assertEqual(sa, sb)
        self.assertNotEqual(sa[0][0][1], 60000.0)
        for sym, px, liq in sa[-1]:
            places = 1 if sym == "BTCUSDT" else 2
            self.assertAlmostEqual(px, round(px, places), places=9)
            self.assertGreater(liq, 0)
        c = feed_mod.Feed({"seed": 4, "vol": 0.01})
        self.assertNotEqual([c.step() for _ in range(5)], sa[:5])

    def test_gap_and_drift(self):
        f = feed_mod.Feed({"mode": "fixed", "gaps": [{"at_tick": 2, "symbol": "BTCUSDT", "pct": -0.5}]})
        f.step()
        self.assertEqual(f.prices["BTCUSDT"], 60000.0)
        f.step()
        self.assertEqual(f.prices["BTCUSDT"], 30000.0)
        g = feed_mod.Feed({"mode": "drift_only", "drift": 0.5, "vol": 0.0})
        g.mode = "random_walk"
        g.step()
        self.assertEqual(g.prices["BTCUSDT"], 90000.0)

    def test_fixed_and_set_price(self):
        f = feed_mod.Feed({"mode": "fixed"}, ticks={"BTCUSDT": 0.1})
        self.assertEqual(f.step()[0][1], 60000.0)
        f.set_price("BTCUSDT", 59999.96)
        self.assertEqual(f.prices["BTCUSDT"], 60000.0)
        f.set_price("BTCUSDT", -5)
        self.assertEqual(f.prices["BTCUSDT"], 0.1)
        g = feed_mod.Feed({"mode": "fixed"})
        g.set_price("BTCUSDT", 123.456)
        self.assertEqual(g.prices["BTCUSDT"], 123.456)

    def test_replay_csv(self):
        d = tmpdir(self)
        p = os.path.join(d, "t.csv")
        with open(p, "w", encoding="utf-8") as f:
            f.write("symbol,price,liquidity\nBTCUSDT,61000,0.2\nETHUSDT,3100,\n")
        f1 = feed_mod.Feed({"mode": "replay", "path": p, "loop": True}, ticks={"BTCUSDT": 0.1})
        self.assertEqual(f1.step(), [("BTCUSDT", 61000.0, 0.2)])
        self.assertEqual(f1.step(), [("ETHUSDT", 3100.0, 1.0)])
        self.assertEqual(f1.step()[0][0], "BTCUSDT")                       # loop
        f2 = feed_mod.Feed({"mode": "replay", "path": p, "loop": False})
        f2.step(), f2.step()
        self.assertEqual(f2.step(), [])
        with open(p, "w", encoding="utf-8") as f:
            f.write("symbol,price\n")
        with self.assertRaises(ValueError):
            feed_mod.Feed({"mode": "replay", "path": p})
        with open(p, "w", encoding="utf-8") as f:
            f.write("sym,px\nBTCUSDT,1\n")
        with self.assertRaises(ValueError):
            feed_mod.load_csv(p)


class TestCandles(unittest.TestCase):
    def test_aggregate_and_resample(self):
        c = feed_mod.Candles(bar_ms=1000, max_bars=3)
        c.add("B", 10, 1000, 1)
        c.add("B", 12, 1500, 1)
        c.add("B", 9, 1999, 1)
        c.add("B", 11, 2000, 2)
        self.assertEqual(c.get("B"), [[1000, 10, 12, 9, 9, 3], [2000, 11, 11, 11, 11, 2]])
        c.add("B", 1, 3000), c.add("B", 2, 4000)
        self.assertEqual(len(c.get("B")), 3)                                  # max_bars
        self.assertEqual(c.get("B", limit=1), [[4000, 2, 2, 2, 2, 0.0]])
        self.assertEqual(c.get("B", interval_ms=2000), [[2000, 11, 11, 1, 1, 2], [4000, 2, 2, 2, 2, 0.0]])
        self.assertEqual(c.get("none"), [])
        self.assertEqual(feed_mod.resample([], 5), [])


if __name__ == "__main__":
    unittest.main()
