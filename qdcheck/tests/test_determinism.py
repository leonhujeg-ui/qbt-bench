"""G4 判据：依赖 set 迭代顺序的假引擎必须被抓到、确定性假引擎必须通过、容差语义（1e-12 噪声通过 / 1e-6 抓到）、
哈希种子确实生效、结构化比较的每条规则与路径格式、错误处理、CLI、qbt 回测引擎真实用例。"""
import json
import math
import os
import tempfile
import unittest

from qdcheck import determinism as D
from qdcheck.tests._util import captured, scale

TOY = {"n_bars": 60}


def _pair(rep, a, b):
    return next(p for p in rep["pairs"] if (p["a"], p["b"]) == (a, b))


class TestFakeEngines(unittest.TestCase):
    def test_set_order_engine_is_caught(self):
        rep = D.check(TOY, "qdcheck.examples:set_order_engine")
        self.assertFalse(rep["ok"])
        self.assertTrue(rep["hash_seeds_effective"])
        pair = _pair(rep, "PYTHONHASHSEED=0", "PYTHONHASHSEED=4242")
        self.assertFalse(pair["ok"])
        self.assertEqual(pair["first_diff"]["reason"], "value")
        self.assertTrue(any(d["path"].startswith("trades[") for d in pair["diffs"]), pair["diffs"])
        self.assertEqual(len(rep["runs"]), 3)
        self.assertIn("DIFFERENT", D.format_report(rep))
        self.assertIn("also ", D.format_report(rep))

    def test_deterministic_engine_passes(self):
        rep = D.check(TOY, "qdcheck.examples:deterministic_engine")
        self.assertTrue(rep["ok"], D.format_report(rep))
        self.assertEqual(len(rep["pairs"]), 3)
        self.assertTrue(all(p["ok"] and p["leaves"] > 100 for p in rep["pairs"]))
        self.assertEqual({r["seed_env"] for r in rep["runs"][1:]}, {"0", "4242"})
        self.assertIn("identical", D.format_report(rep))

    def test_tolerance_on_hash_dependent_noise(self):
        self.assertTrue(D.check(dict(TOY, noise=1e-12), "qdcheck.examples:noisy_engine")["ok"])
        rep = D.check(dict(TOY, noise=1e-6), "qdcheck.examples:noisy_engine")
        self.assertFalse(rep["ok"])
        self.assertEqual(_pair(rep, "PYTHONHASHSEED=0", "PYTHONHASHSEED=4242")["first_diff"]["path"], "final_equity")
        loose = D.check(dict(TOY, noise=1e-6), "qdcheck.examples:noisy_engine", rel_tol=1e-5)
        self.assertTrue(loose["ok"])

    def test_same_seed_twice_is_not_effective(self):
        rep = D.check(TOY, "qdcheck.examples:deterministic_engine", hash_seeds=(7, 7), in_process=False)
        self.assertFalse(rep["hash_seeds_effective"])
        self.assertFalse(rep["ok"])
        self.assertEqual(len(rep["pairs"]), 1)


class TestDiff(unittest.TestCase):
    def test_equal_and_tolerance(self):
        self.assertEqual(D.diff({"a": [1, 2.0, "x", None, True]}, {"a": [1, 2.0, "x", None, True]}), (None, 5))
        self.assertIsNone(D.diff(1.0, 1.0 + 9e-10)[0])
        self.assertEqual(D.diff(1.0, 1.0 + 2e-9)[0]["reason"], "value")
        self.assertIsNone(D.diff(1.0, 1.0 + 2e-9, rel_tol=1e-8)[0])
        self.assertIsNone(D.diff(math.nan, math.nan)[0])
        self.assertIsNotNone(D.diff(math.nan, 1.0)[0])
        self.assertIsNone(D.diff(math.inf, math.inf)[0])
        self.assertIsNotNone(D.diff(math.inf, -math.inf)[0])
        self.assertIsNotNone(D.diff(0.0, 1e-300)[0])
        self.assertIsNone(D.diff(0.0, 1e-300, abs_tol=1e-200)[0])

    def test_types_are_strict(self):
        self.assertEqual(D.diff(1, 1.0)[0]["reason"], "type int != float")
        self.assertEqual(D.diff(True, 1)[0]["reason"], "type bool != int")
        self.assertEqual(D.diff(None, 0)[0]["reason"], "type null != int")
        self.assertEqual(D.diff([1], (1,))[0], None)
        self.assertEqual(D.diff("1", 1)[0]["reason"], "type str != int")
        self.assertEqual(D.diff({"a": 1}, [1])[0]["reason"], "type dict != list")

    def test_paths_and_first_difference_order(self):
        a = {"trades": [{"price": 1.0, "qty": 1}] * 20, "z": 1}
        b = {"trades": [{"price": 1.0, "qty": 1}] * 17 + [{"price": 2.0, "qty": 1}] * 3, "z": 2}
        first, _n = D.diff(a, b)
        self.assertEqual((first["path"], first["a"], first["b"]), ("trades[17].price", 1.0, 2.0))
        found, _n = D.diff_all(a, b, limit=10)
        self.assertEqual([d["path"] for d in found],
                         ["trades[17].price", "trades[18].price", "trades[19].price", "z"])
        self.assertEqual(len(D.diff_all(a, b, limit=2)[0]), 2)
        self.assertEqual(D.diff({"a b": {"1x": 1}}, {"a b": {"1x": 2}})[0]["path"], '["a b"]["1x"]')
        self.assertEqual(D.diff(1, 2)[0]["path"], "<root>")
        self.assertEqual(D.diff([[1, 2]], [[1, 3]])[0]["path"], "[0][1]")
        self.assertEqual(D.format_path(("x", 3, "y", "名字")), 'x[3].y["名字"]')

    def test_lists_and_dicts(self):
        first, _n = D.diff({"t": [1, 2, 3]}, {"t": [1, 2]})
        self.assertEqual((first["path"], first["reason"]), ("t", "length 3 != 2"))
        first, _n = D.diff({"t": [1, 9, 3]}, {"t": [1, 2]})
        self.assertEqual(first["path"], "t[1]")                     # 先报共同前缀里的差异，再报长度
        first, _n = D.diff({"a": 1, "b": 2}, {"a": 1, "c": 2})
        self.assertEqual((first["path"], first["reason"]), ("b", "key only in a"))
        first, _n = D.diff({"a": 1}, {"a": 1, "c": 2})
        self.assertEqual((first["path"], first["reason"]), ("c", "key only in b"))
        self.assertIsNone(D.diff({"a": 1, "b": 2}, {"b": 2, "a": 1})[0])
        self.assertEqual(D.diff({"a": 1, "b": 2}, {"b": 2, "a": 1}, dict_order=True)[0]["reason"],
                         "dict key order differs")
        found, _n = D.diff_all({"x": [1, 2, 3], "y": 5}, {"x": [9], "y": 6}, limit=10)
        self.assertEqual([d["reason"] for d in found], ["value", "length 3 != 1", "value"])
        self.assertTrue(isinstance(D.diff_all([list(range(300))], [list(range(301))])[0][0]["a"], int))
        long_a, long_b = ["x" * 300], ["y" * 300]
        self.assertLessEqual(len(D.diff(long_a, long_b)[0]["a"]), 302)


class TestReportDetails(unittest.TestCase):
    def test_nan_results_survive_json_round_trip(self):
        self.assertTrue(math.isnan(D.run_target("builtins:float", "nan")))
        self.assertEqual(D.run_target("builtins:float", "inf"), math.inf)

    def test_timing_fields_with_fake_clock(self):
        from unittest import mock
        ticks = iter([5.0, 6.23456])
        with mock.patch.object(D.time, "perf_counter", lambda: next(ticks)):
            rep = D.check(TOY, "qdcheck.examples:deterministic_engine", hash_seeds=())
        self.assertEqual(rep["runs"][0]["seconds"], 1.235)
        ticks = iter([5.0, 6.23456])
        with mock.patch.object(D.time, "perf_counter", lambda: next(ticks)):
            out = D.run_child("qdcheck.examples:deterministic_engine", TOY, 0)
        self.assertEqual(out["seconds"], 1.235)

    def test_short_values_and_key_lists(self):
        self.assertEqual(D.diff("a" * 198, "b" * 198)[0]["a"], "a" * 198)        # repr 恰 200 字符：原样
        self.assertEqual(D.diff("a" * 199, "b" * 199)[0]["a"], repr("a" * 199)[:200])
        a = {f"k{i:02d}": i for i in range(25)}
        b = dict(reversed(list(a.items())))
        first = D.diff(a, b, dict_order=True)[0]
        self.assertEqual((len(first["a"]), first["a"][0], first["b"][0]), (20, "k00", "k24"))

    def test_defaults(self):
        import inspect
        self.assertEqual(inspect.signature(D.check).parameters["rel_tol"].default, 1e-9)
        self.assertEqual(inspect.signature(D.check).parameters["hash_seeds"].default, (0, 4242))
        self.assertEqual(inspect.signature(D.diff).parameters["rel_tol"].default, 1e-9)
        args = D.build_parser().parse_args(["m:f"])
        self.assertEqual((args.seeds, args.rel, args.task, args.dict_order), ([0, 4242], 1e-9, "{}", False))


class TestMoreDetails(unittest.TestCase):
    def test_more_defaults(self):
        import inspect
        self.assertEqual(inspect.signature(D.run_child).parameters["timeout"].default, 600.0)
        self.assertEqual(inspect.signature(D.floats_equal).parameters["abs_tol"].default, 0.0)
        self.assertEqual(inspect.signature(D.diff_all).parameters["rel_tol"].default, 1e-9)
        self.assertEqual(inspect.signature(D.diff_all).parameters["limit"].default, 10)
        self.assertFalse(D.floats_equal(0.0, 0.5, 1e-9))
        self.assertTrue(D.floats_equal(0.0, 0.5, 1e-9, 0.5))

    def test_collection_continues_after_key_and_order_differences(self):
        found, _n = D.diff_all({"a": {"x": 1}, "b": 1}, {"a": {"y": 1}, "b": 2})
        self.assertEqual([(d["path"], d["reason"]) for d in found], [("a.x", "key only in a"), ("b", "value")])
        found, _n = D.diff_all({"a": {"p": 1, "q": 2}, "b": 1}, {"a": {"q": 2, "p": 1}, "b": 2}, dict_order=True)
        self.assertEqual([d["path"] for d in found], ["a", "b"])

    def test_child_stderr_tail_is_3000_chars(self):
        with self.assertRaises(D.HarnessError) as ctx:
            D.run_child("builtins:exec", "raise ValueError('x' * 5000)", 0)
        head, sep, tail = str(ctx.exception).partition("):\n")
        self.assertEqual((sep, len(tail)), ("):\n", 3000))
        self.assertIn("PYTHONHASHSEED=0 failed (exit 1", head)


class TestErrors(unittest.TestCase):
    def test_bad_targets_tasks_and_results(self):
        for spec in ("nocolon", ":f", "m:", "qdcheck.examples:nope", "qdcheck.examples:SYMBOLS"):
            with self.assertRaises((D.HarnessError, AttributeError), msg=spec):
                D.load_target(spec)
        with self.assertRaises(D.HarnessError):
            D.check({"x": {1, 2}}, "qdcheck.examples:deterministic_engine")
        with self.assertRaises(D.HarnessError):
            D.run_target("builtins:set", [1, 2])
        with self.assertRaises(D.HarnessError) as ctx:
            D.run_child("qdcheck.examples:validate_good", {"bad": 1}, 0)
        self.assertIn("ValidationError", str(ctx.exception))
        with self.assertRaises(D.HarnessError) as ctx:
            D.run_child("time:sleep", 5, 0, timeout=0.5)
        self.assertIn("timed out", str(ctx.exception))

    def test_child_entry_point_in_process(self):
        import io
        import sys
        stdin, stdout = sys.stdin, sys.stdout
        sys.stdin = io.StringIO(json.dumps({"target": "qdcheck.examples:deterministic_engine", "task": TOY}))
        sys.stdout = io.StringIO()
        try:
            D._child_main()
            out = json.loads(sys.stdout.getvalue())
        finally:
            sys.stdin, sys.stdout = stdin, stdout
        self.assertEqual(out["result"], D.run_target("qdcheck.examples:deterministic_engine", TOY))
        self.assertEqual(out["probe"], hash(D.PROBE))
        self.assertEqual(D.diff(b"a", "a")[0]["reason"], "type bytes != str")

    def test_cli(self):
        tmp = tempfile.mkdtemp()
        task_path, out = os.path.join(tmp, "task.json"), os.path.join(tmp, "rep.json")
        with open(task_path, "w") as f:
            json.dump(TOY, f)
        with captured() as (stdout, stderr):
            self.assertEqual(D.main(["qdcheck.examples:deterministic_engine", "--task", "@" + task_path,
                                     "--json", out]), 0)
            self.assertEqual(D.main(["qdcheck.examples:set_order_engine", "--task", '{"n_bars": 60}',
                                     "--seeds", "0", "4242", "--dict-order"]), 1)
            self.assertEqual(D.main(["qdcheck.examples:nope"]), 2)
            self.assertEqual(D.main(["qdcheck.examples:deterministic_engine", "--task", "{not json"]), 2)
            self.assertEqual(D.main(["qdcheck.examples:deterministic_engine", "--task", "@/nonexistent.json"]), 2)
        self.assertIn("OK: qdcheck.examples:deterministic_engine", stdout.getvalue())
        self.assertIn("error:", stderr.getvalue())
        with open(out) as f:
            self.assertTrue(json.load(f)["ok"])


class TestQbtRealCase(unittest.TestCase):
    def test_qbt_backtests_are_identical_across_processes_and_hash_seeds(self):
        for strategy in scale(("ma_cross", "grid", "trend_stop"), ("ma_cross",)):
            task = {"strategy": strategy, "n": scale(8000, 3000), "seed": 2,
                    "broker": {"max_leverage": 3.0, "funding_interval": 60, "funding_rate": 0.0001}}
            rep = D.check(task, "qdcheck.examples:qbt_backtest")
            self.assertTrue(rep["ok"], D.format_report(rep))
            self.assertGreater(rep["pairs"][0]["leaves"], 200)


if __name__ == "__main__":
    unittest.main()
