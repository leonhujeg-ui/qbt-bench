"""测试公用工具：快速模式开关、容差说明、随机用例生成器。"""
import os
import random
import unittest

# QBT_FAST_TESTS=1：突变测试与模糊测试下跑的精简规模（只缩小“随机用例数/数据长度”，不缩小判据）。
FAST = os.environ.get("QBT_FAST_TESTS") == "1"
# QBT_COVERAGE=1：覆盖率监控下性能阈值无意义，性能测试跳过并在输出里注明。
UNDER_COVERAGE = os.environ.get("QBT_COVERAGE") == "1"

# 浮点容差约定（在各测试里引用并注明理由）：
# ABS_SUM：滚动和/递推与暴力求和的差异来自不同的求和顺序，误差 ~ n * eps * |值|，
#          取 1e-9 的相对容差对价格量级 (1e2) 与窗口 (<=200) 足够，且远小于任何交易决策阈值。
REL_TOL = 1e-9
# ACCOUNTING_TOL：会计恒等式两边按不同顺序累加上百万笔，取 1e-6 绝对容差（相对初始资金 1e5 为 1e-11）。
ACCOUNTING_TOL = 1e-6


def scale(full, fast):
    """按模式返回规模参数。"""
    return fast if FAST else full


def rng(seed):
    return random.Random(seed)


def random_params(space, seed):
    """从参数空间（{name: [choices]}）确定性抽一组参数。"""
    r = random.Random(seed)
    return {k: r.choice(v) for k, v in sorted(space.items())}


class QBTTestCase(unittest.TestCase):
    def assertCloseList(self, a, b, rel=REL_TOL, abs_tol=1e-12, msg=""):
        # abs_tol=1e-12：序列经过 0 时纯相对容差退化为精确相等；1e-12 ≪ 任何非零指标值 × rel，不影响 1e-6 量级的放水判据
        self.assertEqual(len(a), len(b), f"length differs {msg}")
        for i, (x, y) in enumerate(zip(a, b)):
            if x is None or y is None:
                self.assertIs(x, y, f"index {i}: {x!r} vs {y!r} {msg}")
            else:
                self.assertTrue(abs(x - y) <= abs_tol + rel * max(abs(x), abs(y)),
                                f"index {i}: {x!r} vs {y!r} {msg}")
