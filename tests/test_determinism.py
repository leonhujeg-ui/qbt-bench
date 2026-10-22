"""跨进程 / 跨哈希种子一致性：用 qdcheck 的 determinism harness（不改 qdcheck/）。

判据：本进程跑一遍回测，再起两个子进程（PYTHONHASHSEED=0 与 4242）各跑一遍，三份结果做形状 + 容差比较
（整数 / 字符串 / 列表顺序 / dict 键集合精确，浮点相对 1e-9）。dict / set 迭代顺序若泄漏进结果，会在这里暴露。
"""
import os
import unittest

from tests._helpers import scale

try:
    from qdcheck import determinism
except ImportError:                                           # pragma: no cover
    determinism = None


def qbt_grid_target(task):
    """qbt 侧的 harness 目标：网格搜索 + walk-forward 的可 JSON 结果。"""
    from qbt.data import GenSpec
    from qbt import optimizer as O
    t = O.make_task("ma_cross", GenSpec(n=task["n"], seed=task["seed"]).to_dict())
    combos = O.grid({"fast": [5, 10], "slow": [30, 50], "ma": ["sma", "ema"], "size": [0.5], "short": [True]})
    res = O.run_search(t, combos, workers=task.get("workers", 1))
    wf = O.walk_forward(t, combos[:4], n_folds=2, train_frac=0.6, workers=1)
    return {"search": res, "best": O.best(res), "walk_forward": wf}


@unittest.skipIf(determinism is None, "qdcheck 不在仓库里")
class TestHashSeedDeterminism(unittest.TestCase):
    def test_backtest_across_hash_seeds(self):
        rep = determinism.check({"n": scale(3000, 800), "seed": 11, "strategy": "trend_stop",
                                 "broker": {"max_participation": 0.5}}, "qdcheck.examples:qbt_backtest",
                                hash_seeds=(0, 4242), rel_tol=1e-9)
        self.assertTrue(rep["ok"], determinism.format_report(rep))
        self.assertEqual({r["hash_seed"] for r in rep["runs"]} & {"0", "4242"}, {"0", "4242"})

    def test_optimizer_across_hash_seeds(self):
        rep = determinism.check({"n": scale(2000, 600), "seed": 5, "workers": 2}, "tests.test_determinism:qbt_grid_target",
                                hash_seeds=(0, 4242), rel_tol=1e-9)
        self.assertTrue(rep["ok"], determinism.format_report(rep))

    def test_harness_is_not_a_loophole(self):
        """放水判据：把 set 迭代顺序泄漏进结果的引擎、以及依赖 hash() 的 1e-6 噪声都必须被抓住（qdcheck 自带的坏引擎）。"""
        rep = determinism.check({"n_bars": 60}, "qdcheck.examples:set_order_engine", hash_seeds=(0, 4242), rel_tol=1e-9)
        self.assertFalse(rep["ok"], "set 顺序泄漏未被抓住")
        rep = determinism.check({"n_bars": 60, "noise": 1e-6}, "qdcheck.examples:noisy_engine", hash_seeds=(0, 4242), rel_tol=1e-9)
        self.assertFalse(rep["ok"], "1e-6 的哈希相关噪声未被抓住")


if __name__ == "__main__":
    unittest.main()
