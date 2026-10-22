"""CLI 与工具脚本判据：demo / backtest / optimize 子命令、bench 与 fuzz 的函数级运行、突变器与覆盖率工具自检。"""
import contextlib
import io
import json
import os
import tempfile
import unittest

from qbt import cli
from tests._helpers import QBTTestCase, scale, FAST

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def run_cli(args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.main(args)
    return code, buf.getvalue()


class TestCli(QBTTestCase):
    def test_demo(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out = run_cli(["demo", "--n", str(scale(2000, 600)), "--seed", "2", "--out", tmp])
            self.assertEqual(code, 0)
            self.assertIn("不构成任何投资结论", out)
            import re
            elapsed = re.search(r"总耗时 ([0-9.]+)s", out)
            self.assertIsNotNone(elapsed)
            self.assertLess(float(elapsed.group(1)), 120.0)
            self.assertIn("报告目录", out)
            self.assertIn("最佳夏普", out)
            self.assertIn("strategy", out)                                 # 表头
            self.assertIn("sharpe", out)
            rows = [l for l in out.splitlines() if l.split() and l.split()[0] in cli._registry()]
            self.assertEqual(len(rows), 6)                                 # 每个策略一行
            with open(os.path.join(tmp, "summary.json")) as f:
                summary = json.load(f)
            from qbt.data import GenSpec, generate
            self.assertEqual(cli.DEMO_SPEC_KW, {"halt_prob": 0.002, "gap_prob": 0.01, "jump_prob": 0.002})   # 钉住
            self.assertEqual(cli.DEMO_CFG_KW, {"max_participation": 0.5})
            spec = GenSpec(n=scale(2000, 600), seed=2, **cli.DEMO_SPEC_KW)
            self.assertIn(f"指纹={generate(spec).fingerprint()[:12]}", out)
            from qbt.engine import run_backtest
            from qbt.broker import BrokerConfig
            ref = run_backtest(generate(spec), cli._registry()["ma_cross"](), BrokerConfig(**cli.DEMO_CFG_KW))
            self.assertEqual(summary["ma_cross"]["end_equity"], ref.final_equity)      # demo 配置与独立回测一致
            for row in rows:
                name = row.split()[0]
                self.assertIn(f"{100 * summary[name]['max_drawdown']:7.2f}", row)
                self.assertIn(f"{100 * summary[name]['total_return']:8.2f}", row)
            best = max(summary, key=lambda k: summary[k]["sharpe"])
            self.assertIn(f"最佳夏普: {best}", out)
            with open(os.path.join(tmp, "summary.json")) as f:
                summary = json.load(f)
            self.assertEqual(len(summary), 6)
            self.assertEqual(sorted(summary), sorted(cli._registry()))
            for name in summary:
                for ext in ("txt", "svg", "json"):
                    self.assertTrue(os.path.exists(os.path.join(tmp, f"{name}.{ext}")), f"{name}.{ext}")

    def test_backtest_and_optimize(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out = run_cli(["backtest", "--strategy", "ma_cross", "--params", '{"fast": 5, "slow": 20}',
                                 "--n", "800", "--out", tmp])
            self.assertEqual(code, 0)
            self.assertIn('"fast": 5', out)
            self.assertIn("written:", out)
            from qbt.data import GenSpec, generate
            from qbt.engine import run_backtest
            from qbt.broker import BrokerConfig
            ref = run_backtest(generate(GenSpec(n=800, seed=1)), cli._registry()["ma_cross"](fast=5, slow=20),
                               BrokerConfig(max_participation=0.5))
            self.assertIn(f"期末权益: {ref.final_equity:,.2f}", out)         # backtest 子命令的配置钉住
            self.assertTrue(os.path.exists(os.path.join(tmp, "ma_cross.svg")))
            res = os.path.join(tmp, "opt.json")
            code, out = run_cli(["optimize", "--strategy", "rsi_reversion", "--n", "500", "--random", "3",
                                 "--workers", "1", "--out", res])
            self.assertEqual(code, 0)
            with open(res) as f:
                self.assertEqual(json.load(f)["n_results"], 3)

    def test_test_subcommand_with_pattern(self):
        import contextlib
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(["test", "--pattern", "test_limits.py"])          # 只跑一个小文件，避免递归整套
        self.assertEqual(code, 0)
        self.assertIn("Ran 3 tests", err.getvalue())

    def test_test_subcommand_returns_1_on_failure(self):
        import contextlib
        from unittest import mock

        class FakeResult:
            testsRun = 1
            failures = errors = []
            skipped = []

            @staticmethod
            def wasSuccessful():
                return False

        class FakeRunner:
            def __init__(self, *a, **k):
                pass

            def run(self, suite):
                return FakeResult()
        with mock.patch("unittest.TextTestRunner", FakeRunner), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["test", "--pattern", "test_limits.py"]), 1)

    def test_test_quick_loads_smoke_modules(self):
        """--quick 只装载 QUICK_MODULES 里存在的模块，并设 QBT_FAST_TESTS=1（用假 runner 截获 suite，不真跑）。"""
        from unittest import mock
        seen = {}

        class FakeResult:
            testsRun, failures, errors, skipped = 1, [], [], []

            @staticmethod
            def wasSuccessful():
                return True

        class FakeRunner:
            def __init__(self, *a, **k):
                pass

            def run(self, suite):
                seen["ids"] = [t.id() for t in iter_tests(suite)]
                return FakeResult()

        def iter_tests(suite):
            for t in suite:
                yield from (iter_tests(t) if isinstance(t, unittest.TestSuite) else [t])
        old_fast = os.environ.get("QBT_FAST_TESTS")
        try:
            with mock.patch("unittest.TextTestRunner", FakeRunner), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["test", "--quick"]), 0)
            self.assertEqual(os.environ.get("QBT_FAST_TESTS"), "1")
        finally:
            if old_fast is None:
                os.environ.pop("QBT_FAST_TESTS", None)
            else:
                os.environ["QBT_FAST_TESTS"] = old_fast
        mods = {i.split(".")[1] for i in seen["ids"]}
        self.assertEqual(mods, set(cli.QUICK_MODULES))                   # 每个冒烟模块都装载了、没有多装
        self.assertGreater(len(seen["ids"]), 100)                        # 不是空 suite

    def test_unknown_argument_exits_2(self):
        """非委托子命令带未知参数 → argparse error → 退出码 2（README「退出码」）；委托子命令（optimize）透传。"""
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                cli.main(["test", "--nope"])
        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(cli.build_parser().parse_known_args(["optimize", "--limit", "1"])[1], ["--limit", "1"])

    def test_parser(self):
        ap = cli.build_parser()
        args = ap.parse_args(["demo", "--n", "5"])
        self.assertEqual((args.cmd, args.n), ("demo", 5))
        d = ap.parse_args(["demo"])
        self.assertEqual((d.n, d.seed, d.out), (20_000, 1, os.path.join("out", "demo")))        # 默认值钉住
        b = ap.parse_args(["backtest"])
        self.assertEqual((b.n, b.seed, b.strategy, b.params, b.out), (20_000, 1, "ma_cross", "", None))
        with self.assertRaises(SystemExit):
            ap.parse_args([])


class TestToolScripts(QBTTestCase):
    def test_bench_functions_small(self):
        from bench import bench as B
        from qbt.data import generate
        info = B.machine_info()
        self.assertTrue({"platform", "python", "cpus", "timestamp"} <= set(info))
        d = generate(n=1500, seed=1)
        single = B.bench_single_core(d)
        self.assertTrue(single["identical_rerun"])
        self.assertGreater(single["bars_per_second_cold"], 0)
        g = B.bench_grid(workers=2, serial=True, n_bars=300, n_combos=4)
        self.assertEqual(g["combos"], 4)
        self.assertTrue(g["bytewise_identical"])
        self.assertGreater(g["speedup"], 0)
        if FAST:
            return                                                     # 突变/模糊快速模式：--quick 整跑耗时 ~10s，跳过
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "b.json")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                B.main(["--quick", "--skip-serial", "--workers", "2", "--out", path])
            with open(path) as f:
                rep = json.load(f)
            self.assertTrue(rep["quick"])
            self.assertFalse(rep["pass"]["grid"])                          # quick 模式永不算达标
            self.assertIn("single_core_backtest", rep)

    def test_fuzz_small_run_has_no_crashes(self):
        from fuzz import fuzz as F
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "f.json")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = F.main(["--iterations", str(scale(3000, 800)), "--seed", "11", "--workers", "1", "--out", path])
            with open(path) as f:
                res = json.load(f)
            self.assertEqual(code, 0, res["crash_samples"][:2])
            self.assertEqual(res["crashes"], 0)
            dist = res["distribution"]
            for target in ("engine", "broker", "indicators", "metrics"):
                self.assertTrue(any(k.startswith(target + ":") for k in dist), target)
                self.assertGreater(dist.get(f"{target}:ok", 0), 0, target)
            self.assertGreater(dist.get("engine:DataError", 0), 0)
            self.assertGreater(dist.get("broker:ConfigError", 0) + dist.get("broker:OrderError", 0), 0)

    def test_mutation_generator_self_check(self):
        from mutants import run_mutations as RM
        for module in RM.list_modules():
            if module in ("errors", "__init__"):
                continue
            with open(os.path.join(REPO, "qbt", module + ".py"), encoding="utf-8") as f:
                cands = RM.enumerate_candidates(f.read())
            self.assertGreaterEqual(len(cands), 60, module)
            src, capped = RM.select_mutants(module, 30, 0)
            self.assertEqual(len(capped), 30, module)
            self.assertTrue({"binop", "compare", "const"} <= {c[1] for c in cands}, module)
            self.assertEqual([m["id"] for m in capped], [m["id"] for m in RM.select_mutants(module, 30, 0)[1]])
            for m in capped[:3]:
                mutated, before, after = RM.apply_mutation(src, m["idx"], m["op"], m["extra"])
                compile(mutated, module, "exec")
                self.assertNotEqual(before, after)
        summary = RM.summarize([{"module": "x", "status": "killed"}, {"module": "x", "status": "survived"},
                                {"module": "y", "status": "timeout"}])
        self.assertEqual((summary["total"], summary["caught"], summary["survived"]), (3, 2, 1))
        self.assertAlmostEqual(summary["kill_rate"], 200 / 3, delta=1e-12)   # 100*2/3 的舍入

    def test_coverage_tool_executable_lines(self):
        from tools import cov
        src = '''"""doc"""\nimport os\n\n\ndef f(x):\n    """d"""\n    global G\n    if x:\n        return 1\n    return 2\n\n\n@staticmethod\ndef g():\n    pass  # pragma: no cover\n\n\nif __name__ == "__main__":\n    f(1)\n'''
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "m.py")
            with open(path, "w") as fh:
                fh.write(src)
            lines, deco = cov.executable_lines(path)
        self.assertEqual(lines, {2, 5, 8, 9, 10, 14, 18})
        self.assertEqual(deco, {14: 13})
        self.assertEqual(cov.format_ranges([1, 2, 3, 7, 9, 10]), "1-3,7,9-10")
        self.assertEqual(cov.format_ranges([]), "")


if __name__ == "__main__":
    unittest.main()
