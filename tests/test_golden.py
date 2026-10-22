"""黄金文件回归（格式 v2，跨机器）：固定 seed 数据 + 6 个策略默认参数。

比较口径：整数字段（成交笔数 / 方向 / 根序号 / 订单号 / 订单顺序 / 流动性 / 标签）完全相等；
金额类浮点（权益曲线 / 价格 / 数量 / 费用）相对容差 1e-9；度量类浮点相对容差 5e-7。
理由：跨机器差异只来自 libm（exp/log）的最后 1 ulp，经撮合与累加后仍远小于 1e-9；度量含抵消与滚动漂移，
用规格预算 1e-6 的一半。放水判据：把当前结果人为改错 1e-6 必须红（见 TestGoldenTolerance）。
同一机器两次生成必须逐字节相同（TestGoldenSameMachine）。更新：python3.12 tools/make_golden.py --update。
"""
import json
import os
import unittest

from tests._compare import assert_close, compare, perturb, ShapeMismatch, MONEY_REL, IND_REL
from tools.make_golden import GOLDEN_PATH, snapshot

INT_KEYS = ("n_fills", "n_orders", "n_trades", "order_sequence")
FILL_INT = ("order_id", "symbol", "bar", "side", "liquidity", "tag")
FILL_MONEY = ("qty", "price", "fee")


def _check_strategy(cur, gold, name):
    for key in INT_KEYS:
        d = compare(cur[key], gold[key])
        if d:
            return f"{name}.{key}: {d}"
    d = compare(cur["equity"], gold["equity"], MONEY_REL, MONEY_REL * max(abs(x) for x in gold["equity"]))
    if d:
        return f"{name}.equity: {d}"
    for k in ("fees", "final_equity"):
        d = compare(cur[k], gold[k], MONEY_REL)
        if d:
            return f"{name}.{k}: {d}"
    if len(cur["fills"]) != len(gold["fills"]):
        return f"{name}.fills: length {len(cur['fills'])} != {len(gold['fills'])}"
    for i, (a, b) in enumerate(zip(cur["fills"], gold["fills"])):
        for k in FILL_INT:
            if a[k] != b[k]:
                return f"{name}.fills[{i}].{k}: {a[k]!r} != {b[k]!r}"
        for k in FILL_MONEY:
            d = compare(a[k], b[k], MONEY_REL)
            if d:
                return f"{name}.fills[{i}].{k}: {d}"
    d = compare(cur["metrics"], gold["metrics"], IND_REL, 1e-12)
    return f"{name}.metrics: {d}" if d else None


class TestGolden(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(GOLDEN_PATH, "r", encoding="utf-8") as f:
            cls.golden = json.load(f)
        cls.current = snapshot()

    def test_format_and_provenance(self):
        g = self.golden
        self.assertEqual(g["format"], 2)
        self.assertEqual(g["command"], "python3.12 tools/make_golden.py --update")
        self.assertEqual(g["spec"], self.current["spec"])            # 生成脚本 + seed 与黄金文件一致
        self.assertEqual(g["config"], self.current["config"])
        self.assertEqual(sorted(g["strategies"]), sorted(self.current["strategies"]))
        prov = g["generated_on"]                                        # 跨机对比时要知道黄金来自哪台机器
        self.assertEqual(sorted(prov), ["git_commit", "machine", "platform", "python"])
        for k, v in prov.items():
            self.assertIsInstance(v, str, k)
            self.assertTrue(v, f"generated_on.{k} is empty")

    def test_data_head_within_tolerance(self):
        """数据前 200 根：时间戳整数精确相等，价格 / 成交量相对 1e-9（跨机器 libm 噪声 ~1e-16）。"""
        cur, gold = self.current["data_head"], self.golden["data_head"]
        self.assertEqual(cur["ts"], gold["ts"])
        for col in ("open", "high", "low", "close", "volume"):
            assert_close(cur[col], gold[col], MONEY_REL, 0.0, what=f"data_head.{col}")

    def test_strategies_shape_and_tolerance(self):
        for name, gold in self.golden["strategies"].items():
            d = _check_strategy(self.current["strategies"][name], gold, name)
            self.assertIsNone(d, d)

    def test_golden_is_nontrivial(self):
        for name, g in self.golden["strategies"].items():
            self.assertGreater(g["n_fills"], 10, name)
            self.assertNotEqual(g["final_equity"], 100_000.0, name)
            self.assertEqual(len(g["equity"]), self.golden["spec"]["n"])


class TestGoldenTolerance(unittest.TestCase):
    """放水判据：容差必须能抓住 1e-6 的错，整数字段必须抓住 ±1。"""

    @classmethod
    def setUpClass(cls):
        with open(GOLDEN_PATH, "r", encoding="utf-8") as f:
            cls.golden = json.load(f)

    def test_float_error_1e6_is_red(self):
        gold = self.golden["strategies"]["ma_cross"]
        bad = json.loads(json.dumps(gold))
        bad["equity"] = perturb(bad["equity"], 1e-6)
        self.assertIsNotNone(_check_strategy(bad, gold, "ma_cross"))
        bad = json.loads(json.dumps(gold))
        bad["metrics"] = perturb(bad["metrics"], 1e-6)
        self.assertIsNotNone(_check_strategy(bad, gold, "ma_cross"))
        bad = json.loads(json.dumps(gold))
        bad["fills"][3]["price"] *= 1 + 1e-6
        self.assertIsNotNone(_check_strategy(bad, gold, "ma_cross"))
        ok = json.loads(json.dumps(gold))
        ok["equity"] = perturb(ok["equity"], 1e-12)                    # 机器噪声量级必须绿
        self.assertIsNone(_check_strategy(ok, gold, "ma_cross"))

    def test_integer_fields_exact(self):
        gold = self.golden["strategies"]["ma_cross"]
        for mutate in (lambda b: b.__setitem__("n_fills", b["n_fills"] + 1),
                       lambda b: b["fills"][0].__setitem__("bar", b["fills"][0]["bar"] + 1),
                       lambda b: b["fills"][0].__setitem__("side", -b["fills"][0]["side"]),
                       lambda b: b["order_sequence"].__setitem__(0, b["order_sequence"][1]),
                       lambda b: b["fills"][0].__setitem__("liquidity", "taker" if b["fills"][0]["liquidity"] == "maker" else "maker")):
            bad = json.loads(json.dumps(gold))
            mutate(bad)
            self.assertIsNotNone(_check_strategy(bad, gold, "ma_cross"))


class TestGoldenCrossMachineSimulation(unittest.TestCase):
    def test_robust_to_one_ulp_price_noise(self):
        """模拟另一台机器的 libm 差异：每个价格乘以 (1 ± 2.2e-16) 的交替扰动后，6 个策略的结果仍在容差内、
        整数字段（成交笔数 / 方向 / 根序号 / 订单顺序）完全不变。这是“跨机在容差内对上”的本机可执行版本。"""
        from qbt.broker import BrokerConfig
        from qbt.data import generate
        from qbt.engine import run_backtest
        from qbt import metrics
        from strategies import REGISTRY
        from tools.make_golden import SPEC, CFG, METRIC_KEYS
        with open(GOLDEN_PATH, encoding="utf-8") as f:
            golden = json.load(f)
        d = generate(SPEC).slice(0, SPEC.n)
        eps = 2.220446049250313e-16
        for col in ("open", "high", "low", "close"):
            arr = getattr(d, col)
            for i in range(len(arr)):
                arr[i] *= (1 + eps) if i % 2 else (1 - eps)
        d._validated = False
        d.validate()
        for name, cls in sorted(REGISTRY.items()):
            r = run_backtest(d, cls(), BrokerConfig(**CFG))
            m = metrics.compute(r.equity, r.ts, r.bar_seconds, r.trades)
            cur = {"n_fills": len(r.fills), "n_orders": len(r.orders), "n_trades": len(r.trades), "fees": r.fees,
                   "final_equity": r.final_equity, "equity": list(r.equity), "fills": [f.to_dict() for f in r.fills],
                   "order_sequence": [[o.id, o.created, o.side, o.type, o.status] for o in r.orders],
                   "metrics": {k: m[k] for k in METRIC_KEYS}}
            diff = _check_strategy(cur, golden["strategies"][name], name)
            self.assertIsNone(diff, diff)


class TestGoldenSameMachine(unittest.TestCase):
    def test_two_snapshots_bytewise_identical(self):
        """同一机器：两次快照序列化后逐字节相同（确定性判据，不设容差）。"""
        a, b = snapshot(), snapshot()
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))
        self.assertRaises(ShapeMismatch, assert_close, perturb(a, 1e-6), b, IND_REL)


if __name__ == "__main__":
    unittest.main()
