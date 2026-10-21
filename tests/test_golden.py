"""黄金文件回归：固定 seed 数据 + 6 个策略，结果快照必须逐字节一致（repr 精确比较）。

更新方式：python tools/make_golden.py --update，并在提交信息中说明为什么语义变了。
注意：跨机器的 libm（exp/log）最后一位 ulp 差异理论上会改变指纹；本仓库的判据在同一机器上成立。
"""
import json
import os
import unittest

from qbt.broker import BrokerConfig
from qbt.data import GenSpec, generate
from qbt.engine import run_backtest
from qbt import metrics
from strategies import REGISTRY
from tools.make_golden import GOLDEN_PATH, equity_hash, snapshot


class TestGolden(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(GOLDEN_PATH, "r", encoding="utf-8") as f:
            cls.golden = json.load(f)

    def test_snapshot_matches_golden_bytewise(self):
        current = snapshot()
        self.assertEqual(current["data_fingerprint"], self.golden["data_fingerprint"])
        self.assertEqual(sorted(current["strategies"]), sorted(self.golden["strategies"]))
        for name, g in self.golden["strategies"].items():
            c = current["strategies"][name]
            for key in ("equity_sha256", "final_equity", "n_fills", "n_orders", "n_trades", "fees", "first_fills",
                        "last_fill", "metrics"):
                self.assertEqual(c[key], g[key], f"{name}.{key}")

    def test_golden_is_nontrivial(self):
        for name, g in self.golden["strategies"].items():
            self.assertGreater(g["n_fills"], 10, name)
            self.assertNotEqual(g["final_equity"], repr(100_000.0), name)

    def test_golden_file_sanity(self):
        self.assertTrue(os.path.exists(GOLDEN_PATH))
        self.assertEqual(self.golden["spec"]["n"], 3000)
        spec = GenSpec.from_dict(self.golden["spec"])
        self.assertEqual(generate(spec).fingerprint(), self.golden["data_fingerprint"])
        d = generate(spec)
        r = run_backtest(d, REGISTRY["ma_cross"](), BrokerConfig(**self.golden["config"]))
        self.assertEqual(equity_hash(r.equity), self.golden["strategies"]["ma_cross"]["equity_sha256"])
        m = metrics.compute(r.equity, r.ts, r.bar_seconds, r.trades)
        self.assertEqual(repr(m["sharpe"]), self.golden["strategies"]["ma_cross"]["metrics"]["sharpe"])


if __name__ == "__main__":
    unittest.main()
