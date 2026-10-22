"""compare.py 的判定规则：长度、无值位置、相对 / 绝对容差边界、类型、第一处不一致的下标与两边的值、CLI。"""
import json
import math
import os
import tempfile
import unittest

from qdcheck import compare as C
from qdcheck.tests._util import captured

NAN = float("nan")


class TestCompareSeries(unittest.TestCase):
    def test_equal_and_within_tolerance(self):
        e = [None, None, 1.0, 100.0, -3.5, 0.0]
        self.assertTrue(C.compare_series(e, [None, None, 1.0, 100.0, -3.5, 0.0])["ok"])
        res = C.compare_series(e, [NAN, None, 1.0 * (1 + 9e-10), 100.0 * (1 - 9e-10), -3.5, 5e-13])
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["checked"], 4)
        self.assertAlmostEqual(res["max_rel_err"], 9e-10, delta=1e-12)
        self.assertEqual(res["max_abs_err"], 5e-13)
        self.assertLess(res["max_tol_ratio"], 1.0)

    def test_error_statistics(self):
        res = C.compare_series([0.5], [0.5 * (1 + 5e-10)])
        self.assertAlmostEqual(res["max_rel_err"], 5e-10, delta=1e-13)          # |值| < 1 也计入相对误差
        self.assertEqual(res["max_abs_err"], 0.0)
        res = C.compare_series([0.5], [0.4], rel_tol=0.5, abs_tol=0.25)         # 边界：rel*scale == abs_tol
        self.assertAlmostEqual(res["max_rel_err"], 0.2, delta=1e-12)
        self.assertEqual(res["max_abs_err"], 0.0)
        res = C.compare_series([1.0], [1.0 + 9e-10])
        self.assertAlmostEqual(res["max_tol_ratio"], 0.9, delta=1e-6)
        res = C.compare_series([0.0], [5e-13])
        self.assertAlmostEqual(res["max_tol_ratio"], 0.5, delta=1e-9)
        self.assertEqual((res["max_rel_err"], res["max_abs_err"]), (0.0, 5e-13))

    def test_value_mismatch_reports_first_index_and_both_values(self):
        e = [1.0, 2.0, 3.0, 4.0]
        a = [1.0, 2.0, 3.0 * (1 + 2e-9), 0.0]
        res = C.compare_series(e, a)
        self.assertFalse(res["ok"])
        self.assertEqual((res["reason"], res["index"], res["expected"], res["actual"]), ("value", 2, 3.0, a[2]))
        self.assertIn("first index 2", C.format_result(res))
        self.assertIn("rel_err 2.000e-09", res["detail"])

    def test_tolerance_is_relative_with_absolute_floor(self):
        self.assertTrue(C.compare_series([1e6], [1e6 + 9e-4])["ok"])
        self.assertFalse(C.compare_series([1e6], [1e6 + 2e-3])["ok"])
        self.assertTrue(C.compare_series([0.0], [9e-13])["ok"])
        self.assertFalse(C.compare_series([0.0], [2e-12])["ok"])
        self.assertTrue(C.compare_series([1e-3], [1e-3 + 0.9e-12], 1e-9, 0.0)["ok"])
        self.assertFalse(C.compare_series([1e-3], [1e-3 + 1.5e-12], 1e-9, 0.0)["ok"])
        self.assertTrue(C.compare_series([1e-3], [1e-3 + 1.5e-12], 1e-9, 2e-12)["ok"])
        self.assertTrue(C.compare_series([1.0], [1.0 + 1e-6], rel_tol=1e-5)["ok"])
        self.assertTrue(C.compare_series([0.0, 0.0], [0.0, 0.0], 0.0, 0.0)["ok"])
        self.assertFalse(C.compare_series([1.0], [1.0 + 2.3e-16], 0.0, 0.0)["ok"])

    def test_length_mismatch(self):
        res = C.compare_series([1.0, 2.0, 3.0], [1.0, 2.0])
        self.assertEqual((res["ok"], res["reason"], res["index"], res["expected"], res["actual"]),
                         (False, "length", 2, 3, 2))
        self.assertIn("length differs", C.format_result(res))

    def test_warmup_positions_must_match(self):
        res = C.compare_series([None, 1.0], [0.0, 1.0])
        self.assertEqual((res["reason"], res["index"], res["actual"]), ("warmup", 0, 0.0))
        res = C.compare_series([None, 1.0], [None, None])
        self.assertEqual((res["reason"], res["index"]), ("warmup", 1))
        res = C.compare_series([None, 1.0], [None, NAN])
        self.assertEqual((res["reason"], res["index"]), ("warmup", 1))

    def test_warmup_representation_modes(self):
        self.assertTrue(C.compare_series([None], [NAN], warmup="either")["ok"])
        self.assertTrue(C.compare_series([None], [None], warmup="none")["ok"])
        self.assertFalse(C.compare_series([None], [NAN], warmup="none")["ok"])
        self.assertTrue(C.compare_series([None], [NAN], warmup="nan")["ok"])
        self.assertFalse(C.compare_series([None], [None], warmup="nan")["ok"])
        self.assertFalse(C.compare_series([None], [0.0], warmup="nan")["ok"])
        with self.assertRaises(ValueError):
            C.compare_series([None], [None], warmup="zero")

    def test_types_and_non_finite(self):
        for bad in (True, "1.0", [1.0], {"v": 1}):
            res = C.compare_series([1.0], [bad])
            self.assertEqual((res["ok"], res["reason"], res["index"]), (False, "type", 0), bad)
        self.assertEqual(C.compare_series([1.0], [math.inf])["reason"], "value")
        self.assertEqual(C.compare_series([1.0], [-math.inf])["reason"], "value")
        self.assertTrue(C.compare_series([2.0], [2])["ok"])
        res = C.compare_series([1.0], 1.0)
        self.assertEqual((res["ok"], res["reason"]), (False, "type"))
        self.assertTrue(C.compare_series([1.0], (1.0,))["ok"])

    def test_helpers(self):
        self.assertTrue(C.is_missing(None) and C.is_missing(NAN))
        self.assertFalse(C.is_missing(0.0) or C.is_missing(math.inf))
        self.assertEqual(C.rel_err(0.0, 0.0), 0.0)
        self.assertEqual(C.rel_err(2.0, 1.0), 0.5)
        self.assertEqual(C.rel_err(-1.0, 1.0), 2.0)
        self.assertTrue(C.format_result(C.compare_series([1.0], [1.0])).startswith("OK (1 values"))


def _vec(vid, out):
    return {"id": vid, "indicator": "x", "variant": "v", "line": "value", "series": "s", "params": {},
            "input": {}, "output": out}


class TestCompareFile(unittest.TestCase):
    def setUp(self):
        self.obj = {"vectors": [_vec("a", [None, 1.0]), _vec("b", [2.0, 3.0]), _vec("c", [5.0])]}

    def test_summary_counts_and_failures(self):
        rep = C.compare_file(self.obj, {"a": [None, 1.0], "b": [2.0, 4.0], "zzz": [1.0]})
        self.assertEqual((rep["total"], rep["checked"], rep["passed"], rep["failed"]), (3, 2, 1, 1))
        self.assertEqual((rep["missing"], rep["unknown"]), (["c"], ["zzz"]))
        self.assertEqual((rep["failures"][0]["id"], rep["failures"][0]["index"]), ("b", 1))

    def test_file_level_statistics(self):
        exact = C.compare_file(self.obj, {"a": [None, 1.0], "b": [2.0, 3.0], "c": [5.0]})
        self.assertEqual((exact["max_rel_err"], exact["max_abs_err"], exact["max_tol_ratio"]), (0.0, 0.0, 0.0))
        near = C.compare_file(self.obj, {"b": [2.0, 3.0 * (1 + 4e-10)], "c": [5.0 * (1 + 2e-10)]})
        self.assertAlmostEqual(near["max_rel_err"], 4e-10, delta=1e-13)
        self.assertAlmostEqual(near["max_tol_ratio"], 0.4, delta=1e-4)

    def test_load_layouts(self):
        self.assertEqual(len(C.load_vectors(self.obj)), 3)
        self.assertEqual(len(C.load_vectors(self.obj["vectors"])), 3)
        self.assertEqual(len(C.load_vectors(self.obj["vectors"][0])), 1)
        with self.assertRaises(ValueError):
            C.load_vectors({"nothing": 1})
        noid = {"indicator": "dema", "variant": "ma=sma", "output": [1.0]}
        self.assertEqual(C.vector_id(noid), "dema|ma=sma|value|")
        self.assertEqual(C.compare_vector(noid, [1.0])["id"], "dema|ma=sma|value|")

    def _files(self, actual):
        tmp = tempfile.mkdtemp()
        vp, ap = os.path.join(tmp, "v.json"), os.path.join(tmp, "a.json")
        with open(vp, "w") as f:
            json.dump(self.obj, f)
        with open(ap, "w") as f:
            json.dump(actual, f)
        return tmp, vp, ap

    def test_cli_exit_codes_and_first_mismatch(self):
        tmp, vp, ap = self._files({"a": [None, 1.0], "b": [2.0, 3.5], "c": [5.0]})
        rp = os.path.join(tmp, "rep.json")
        with captured() as (out, _err):
            code = C.main([vp, ap, "--json", rp])
        self.assertEqual(code, 1)
        self.assertIn("b: MISMATCH [value] first index 1: expected 3.0, actual 3.5", out.getvalue())
        with open(rp) as f:
            self.assertEqual(json.load(f)["failed"], 1)
        tmp, vp, ap = self._files({"a": [None, 1.0], "b": [2.0, 3.0]})
        with captured() as (out, _err):
            self.assertEqual(C.main([vp, ap]), 0)
            self.assertEqual(C.main([vp, ap, "--strict"]), 1)
        self.assertIn("checked 2/3  passed 2  failed 0  missing 1", out.getvalue())

    def test_cli_bare_array_and_errors(self):
        tmp, vp, ap = self._files([2.0, 3.0])
        with captured() as (out, err):
            self.assertEqual(C.main([vp, ap, "--id", "b"]), 0)
            self.assertEqual(C.main([vp, ap]), 2)                      # 多条向量 + 裸数组 + 没给 --id
            self.assertEqual(C.main([vp, os.path.join(tmp, "missing.json")]), 2)
            self.assertEqual(C.main([vp, ap, "--id", "nope"]), 1)      # 一条都没比上 => 失败
        self.assertIn("pass --id", err.getvalue())
        single = {"vectors": [_vec("only", [1.0, None])]}
        with open(vp, "w") as f:
            json.dump(single, f)
        with open(ap, "w") as f:
            f.write("[1.0, NaN]")
        with captured():
            self.assertEqual(C.main([vp, ap]), 0)
            self.assertEqual(C.main([vp, ap, "--warmup", "none"]), 1)
            self.assertEqual(C.main([vp, ap, "--rel", "0", "--abs", "0"]), 0)


if __name__ == "__main__":
    unittest.main()
