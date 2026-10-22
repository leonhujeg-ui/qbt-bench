"""G2 判据：覆盖率（2 万次里 NaN / inf / 缺键 / 类型错 各 >= 500）、三个故意写坏的 validate 必须被抓到、
最小复现 <= 原输入 30% 且可独立复现、写对了的 validate 零裸异常且与参考判定逐条一致、性能 <= 20 s、
跨进程 / 跨哈希种子乱喂序列一致、编码可精确还原、schema 语义。"""
import json
import math
import os
import subprocess
import sys
import tempfile
import time
import unittest

from qdcheck import examples as E
from qdcheck import fuzz as F
from qdcheck.tests._util import REPO, UNDER_COVERAGE, captured, scale

SCHEMA = E.BACKTEST_SCHEMA
N = scale(20000, 4000)


def _run(fn, n=N, seed=0, **kw):
    return F.run(fn, SCHEMA, n=n, seed=seed, controlled=E.ValidationError, **kw)


class TestCoverageAndGoodValidate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        t0 = time.perf_counter()
        cls.rep = _run(E.validate_good)
        cls.seconds = time.perf_counter() - t0

    def test_category_coverage(self):
        cats = self.rep["categories"]
        floor = 500 * N // 20000
        for c in ("nan", "inf", "missing_key", "type_wrong"):
            self.assertGreaterEqual(cats[c]["cases"], floor, c)
        for c in F.CATEGORIES:
            self.assertGreater(cats[c]["cases"], 0, c)
            self.assertEqual(cats[c]["cases"], cats[c]["pass"] + cats[c]["controlled"] + cats[c]["bare"])

    def test_good_validate_has_no_bare_exception_and_matches_reference(self):
        rep = self.rep
        self.assertEqual(rep["outcomes"]["bare"], 0)
        self.assertEqual(sum(rep["outcomes"].values()), N)
        self.assertEqual(rep["accepted_invalid"]["count"], 0)
        self.assertEqual(rep["rejected_valid"]["count"], 0)
        self.assertEqual(rep["outcomes"]["pass"], rep["schema_valid_cases"])
        self.assertGreater(rep["outcomes"]["pass"], N // 20)
        self.assertGreater(rep["outcomes"]["controlled"], N // 2)
        self.assertEqual(rep["bare"], [])

    def test_performance(self):
        if UNDER_COVERAGE:
            self.skipTest("timing is meaningless under sys.monitoring")
        self.assertLess(self.seconds * 20000 / N, 20.0)


class TestBrokenValidatesAreCaught(unittest.TestCase):
    def _assert_caught(self, fn, exc_type, max_ratio=0.3):
        rep = _run(fn)
        self.assertGreater(rep["bare_by_type"].get(exc_type, 0), 0, rep["bare_by_type"])
        groups = [g for g in rep["bare"] if g["exc_type"] == exc_type]
        self.assertTrue(groups)
        for g in groups:
            self.assertLessEqual(g["ratio"], max_ratio, g)
            self.assertLess(g["minimal_size"], g["original_size"])
            self.assertEqual(len(g["top3"]), min(3, len(g["top3"])))
            self.assertTrue(g["top3"] and all(" in " in line for line in g["top3"]))
            minimal = F.decode(json.loads(json.dumps(g["minimal_input"])))
            outcome, exc = F.call(fn, minimal, E.ValidationError)
            self.assertEqual((outcome, type(exc).__name__), ("bare", exc_type))
            original = F.make_case(SCHEMA, 0, g["first_case"]).input
            self.assertEqual(F.call(fn, original, E.ValidationError)[0], "bare")
            self.assertEqual(g["original_size"], F.size(original))
        return rep, groups

    def test_keyerror(self):
        _rep, groups = self._assert_caught(E.validate_keyerror, "KeyError")
        self.assertEqual(F.decode(groups[0]["minimal_input"]), {})
        self.assertIn("examples.py", groups[0]["top3"][-1])

    def test_zerodivision(self):
        _rep, groups = self._assert_caught(E.validate_zerodiv, "ZeroDivisionError")
        self.assertEqual(F.decode(groups[0]["minimal_input"]), {"period": 0})
        self.assertIn("10000 // period", groups[0]["top3"][-1])

    def test_recursion_on_long_string(self):
        _rep, groups = self._assert_caught(E.validate_recursion, "RecursionError")
        minimal = F.decode(groups[0]["minimal_input"])
        self.assertEqual(list(minimal), ["symbol"])
        self.assertLess(len(minimal["symbol"]), 3000)
        self.assertGreaterEqual(len(minimal["symbol"]), 900)

        def deeper(k):                               # 在更深的调用栈里也能复现（留了余量）
            return F.call(E.validate_recursion, minimal, E.ValidationError) if k == 0 else deeper(k - 1)
        self.assertEqual(deeper(50)[0], "bare")

    def test_recursion_on_deep_nesting(self):
        def walk(v):
            if isinstance(v, dict):
                for x in v.values():
                    walk(x)
            elif isinstance(v, list):
                for x in v:
                    walk(x)

        def validate(d):
            walk(d)
            return E.validate_good(d)
        rep = _run(validate, n=scale(6000, 3000))
        self.assertGreater(rep["bare_by_type"].get("RecursionError", 0), 0)
        g = rep["bare"][0]
        minimal = F.decode(g["minimal_input"])
        self.assertEqual(F.call(validate, minimal, E.ValidationError)[0], "bare")
        self.assertLess(g["minimal_size"], g["original_size"])


class TestDeterminism(unittest.TestCase):
    def test_same_seed_same_cases(self):
        for i in (0, 1, 17, 999):
            a, b = F.make_case(SCHEMA, 5, i), F.make_case(SCHEMA, 5, i)
            self.assertTrue(F.same_value(a.input, b.input))
            self.assertEqual(a.categories, b.categories)
        same = sum(F.same_value(F.make_case(SCHEMA, 5, i).input, F.make_case(SCHEMA, 6, i).input) for i in range(200))
        self.assertLess(same, 40)
        cases = list(F.iter_cases(SCHEMA, 5, 30))
        self.assertTrue(all(F.same_value(c.input, F.make_case(SCHEMA, 5, c.index).input) for c in cases))

    def test_cases_identical_across_hash_seeds(self):
        code = ("import json,sys; from qdcheck import fuzz as F, examples as E; "
                "print(json.dumps([[list(c.categories), F.encode(c.input)] for c in F.iter_cases(E.BACKTEST_SCHEMA, 3, 300)],"
                " sort_keys=True, ensure_ascii=True))")
        outs = []
        for hs in ("0", "4242"):
            env = dict(os.environ, PYTHONHASHSEED=hs, PYTHONPATH=REPO)
            proc = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True, text=True,
                                  check=True)
            outs.append(json.loads(proc.stdout))
        self.assertEqual(len(outs[0]), 300)
        for (ca, ia), (cb, ib) in zip(*outs):
            self.assertEqual(ca, cb)
            self.assertTrue(F.same_value(F.decode(ia), F.decode(ib)))


class TestEncoding(unittest.TestCase):
    NASTY = [math.nan, math.inf, -math.inf, -0.0, 0.0, 5e-324, 10 ** 400, -(2 ** 64), b"\x00\xff", (1, "a", math.nan),
             "\ud800", "x" * 100000, "ab" * 500 + "a", "\U0001f600" * 70, "$weird", None, True, "",
             {"$float": 1}, {1: "int key", None: "none key", (1, 2): "tuple key", "s": [1, {"t": math.inf}]},
             F.deep_chain("list", 3000, {"leaf": [math.nan]}), F.deep_chain("dict", 1500, "end", "k"),
             {"outer": F.deep_chain("dict", 40, -0.0, "inner")}, [F.deep_chain("list", 33, 1)] * 3,
             F.deep_chain("dict", 64, None, "")]

    def test_round_trip_through_ascii_json(self):
        for v in self.NASTY:
            text = json.dumps(F.encode(v), ensure_ascii=True)
            self.assertTrue(text.isascii())
            self.assertTrue(F.same_value(F.decode(json.loads(text)), v), repr(v)[:80])
        self.assertEqual(F.encode("ab" * 500)["$repeat"], "ab")
        self.assertEqual(F.encode({"k": F.deep_chain("list", 40, 0)})["k"]["depth"], 40)
        self.assertIsInstance(F.encode(F.deep_chain("list", 31, 0)), list)          # 短于 32 层不压缩
        with self.assertRaises(ValueError):
            F.decode({"$what": 1})

    def test_same_value(self):
        self.assertTrue(F.same_value({"a": [math.nan]}, {"a": [math.nan]}))
        self.assertFalse(F.same_value(0.0, -0.0))
        self.assertFalse(F.same_value(1, 1.0))
        self.assertFalse(F.same_value(True, 1))
        self.assertFalse(F.same_value([1, 2], [1]))
        self.assertFalse(F.same_value({"a": 1}, {"b": 1}))
        self.assertFalse(F.same_value({"a": 1}, {"a": 1, "b": 2}))
        self.assertFalse(F.same_value("a", "b"))
        self.assertFalse(F.same_value(1.0, 2.0))
        self.assertTrue(F.same_value({"a": 1, "b": 2}, {"b": 2, "a": 1}))

    def test_key_order_preserved(self):
        obj = {"z": 1, "a": {"y": 2, "b": 3}, 5: "x", "m": [{"q": 1, "c": 2}]}
        self.assertEqual(repr(F.clone(obj)), repr(obj))
        self.assertEqual(repr(F.decode(json.loads(json.dumps(F.encode(obj))))), repr(obj))

    def test_clone_size_and_paths(self):
        deep = F.deep_chain("dict", 5000, [1, 2])
        copy = F.clone(deep)
        self.assertTrue(F.same_value(copy, deep))
        self.assertIsNot(copy, deep)
        self.assertEqual(F.size({"ab": [1, "xy"]}), len(json.dumps({"ab": [1, "xy"]}, separators=(",", ":"))))
        self.assertEqual(F.size("x" * 10), 12)
        self.assertGreater(F.size(10 ** 400), 400)
        self.assertEqual(F.value_paths({"b": {"c": 1}, "a": [5, 6]}), [("a",), ("a", 0), ("a", 1), ("b",), ("b", "c")])
        self.assertEqual(F.value_paths(F.deep_chain("list", 100, 0), max_depth=3), [(0,), (0, 0), (0, 0, 0)])
        obj = {"a": [1, {"b": 2}]}
        self.assertEqual(F.get_at(obj, ("a", 1, "b")), 2)
        self.assertEqual(F.set_at(obj, ("a", 0), 9), {"a": [9, {"b": 2}]})
        self.assertEqual(F.set_at(obj, (), 7), 7)


class TestSchemaSemantics(unittest.TestCase):
    S = {"type": "object", "fields": {
        "i": {"type": "int", "min": 1, "max": 5}, "f": {"type": "float", "min": 0, "max": 1, "required": False},
        "b": {"type": "bool", "required": False}, "e": {"type": "enum", "choices": ["x", 1], "required": False},
        "s": {"type": "str", "min_len": 1, "max_len": 3, "charset": "ascii_printable", "required": False},
        "l": {"type": "list", "items": {"type": "int"}, "max_len": 2, "required": False},
        "o": {"type": "object", "required": False, "fields": {"z": {"type": "int"}}},
        "loose": {"type": "object", "required": False, "extra": "allow", "fields": {}}}}

    def _ok(self, obj):
        return not F.schema_errors(self.S, obj)

    def test_valid_and_invalid(self):
        self.assertTrue(self._ok({"i": 3}))
        self.assertTrue(self._ok({"i": 1, "f": -0.0, "b": False, "e": 1, "s": "abc", "l": [1, 2], "o": {"z": 0},
                                  "loose": {"anything": [1]}}))
        self.assertTrue(self._ok({"i": 5, "f": 1}))
        bad = [None, [], {}, {"i": 0}, {"i": 6}, {"i": True}, {"i": 2.0}, {"i": 2, "f": math.nan}, {"i": 2, "f": 1.5},
               {"i": 2, "f": "0.5"}, {"i": 2, "f": True}, {"i": 2, "b": 1}, {"i": 2, "e": "X"}, {"i": 2, "e": True},
               {"i": 2, "e": 1.0}, {"i": 2, "s": ""}, {"i": 2, "s": "abcd"}, {"i": 2, "s": "é"}, {"i": 2, "s": 1},
               {"i": 2, "l": [1, 2, 3]}, {"i": 2, "l": ["1"]}, {"i": 2, "l": (1,)}, {"i": 2, "o": {}},
               {"i": 2, "o": {"z": 1, "y": 2}}, {"i": 2, "o": []}, {"I": 2}, {"i": 2, 1: 2}, {"i": 10 ** 400},
               {"i": 2, "f": math.inf}]
        for obj in bad:
            self.assertFalse(self._ok(obj), obj)
        self.assertEqual(F.schema_errors(self.S, {"i": 0}), [("$.i", "int out of range")])
        self.assertEqual(len(F.schema_errors(self.S, {"I": 0, "x": 1}, limit=5)), 3)

    def test_check_schema(self):
        F.check_schema(self.S)
        for bad in ({"type": "nope"}, [], {"type": "object"}, {"type": "object", "fields": {1: {"type": "int"}}},
                    {"type": "enum", "choices": []}, {"type": "int", "min": 2, "max": 1},
                    {"type": "list", "items": {"type": "x"}}):
            with self.assertRaises(ValueError, msg=bad):
                F.check_schema(bad)

    def test_valid_instances_are_valid(self):
        import random
        for k in range(300):
            self.assertEqual(F.schema_errors(SCHEMA, F.valid_instance(SCHEMA, random.Random(k))), [])
            self.assertEqual(F.schema_errors(self.S, F.valid_instance(self.S, random.Random(k))), [])


class TestRunner(unittest.TestCase):
    def test_controlled_spec_forms(self):
        def v(d):
            if not isinstance(d, dict) or "period" not in d:
                raise ValueError("bad")
            return d
        as_none = F.run(v, SCHEMA, n=300, seed=2, shrink=False)
        as_cls = F.run(v, SCHEMA, n=300, seed=2, controlled=ValueError, shrink=False)
        as_tuple = F.run(v, SCHEMA, n=300, seed=2, controlled=(KeyError, ValueError), shrink=False)
        as_fn = F.run(v, SCHEMA, n=300, seed=2, controlled=lambda e: str(e) == "bad", shrink=False)
        self.assertGreater(as_none["outcomes"]["bare"], 0)
        self.assertEqual(as_none["outcomes"]["bare"], as_cls["outcomes"]["controlled"])
        for rep in (as_cls, as_tuple, as_fn):
            self.assertEqual(rep["outcomes"]["bare"], 0)
        self.assertEqual(as_none["bare"][0]["shrink_calls"], 0)
        self.assertEqual(as_none["bare"][0]["minimal_size"], as_none["bare"][0]["original_size"])

    def test_base_exceptions_and_builtins(self):
        def exits(d):
            raise SystemExit(3)
        rep = F.run(exits, SCHEMA, n=20, seed=0, shrink=False)
        self.assertEqual(rep["bare_by_type"], {"SystemExit": 20})

        def interrupt(d):
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            F.run(interrupt, SCHEMA, n=5, seed=0)
        rep = F.run(int, SCHEMA, n=50, seed=0, shrink=False, check_accepted=False)
        self.assertGreater(rep["bare_by_type"].get("TypeError", 0), 0)
        self.assertEqual(rep["bare"][0]["top3"], [])
        self.assertIsNone(rep["schema_valid_cases"])

    def test_shrink_budget_and_signature_limit(self):
        def v(d):
            raise ValueError("always")
        rep = F.run(v, SCHEMA, n=200, seed=0, shrink_budget=7)
        self.assertEqual(len(rep["bare"]), 1)
        self.assertLessEqual(rep["bare"][0]["shrink_calls"], 7)
        self.assertEqual((rep["bare"][0]["count"], rep["bare_unrecorded"]), (200, 0))

        def w(d):
            raise ValueError(type(d).__name__ + str(len(d) if isinstance(d, dict) else -1).replace("-", "neg"))
        rep = F.run(w, SCHEMA, n=200, seed=0, shrink=False, max_signatures=1)
        self.assertEqual(len(rep["bare"]), 1)
        self.assertGreater(rep["bare_unrecorded"], 0)
        self.assertEqual(rep["bare"][0]["count"] + rep["bare_unrecorded"], 200)
        self.assertNotEqual(F.message_shape(ValueError("dict")), F.message_shape(ValueError("list")))
        self.assertEqual(F.message_shape(KeyError("a")), F.message_shape(KeyError("b")))

    def test_validate_mutating_its_input_does_not_break_shrinking(self):
        def v(d):
            if isinstance(d, dict):
                d.clear()
            raise RuntimeError("always")
        rep = F.run(v, SCHEMA, n=50, seed=0)
        self.assertEqual(F.decode(rep["bare"][0]["minimal_input"]), {})

    def test_cli_and_report_file(self):
        tmp = tempfile.mkdtemp()
        out = os.path.join(tmp, "r.json")
        schema_path = os.path.join(tmp, "schema.json")
        with open(schema_path, "w") as f:
            json.dump(SCHEMA, f)
        with captured() as (stdout, _err):
            code = F.main(["qdcheck.examples:validate_zerodiv", "--schema", schema_path, "--controlled",
                           "qdcheck.examples:ValidationError", "--n", "800", "--out", out])
            self.assertEqual(code, 0)
            self.assertEqual(F.main(["qdcheck.examples:validate_good", "--schema", "qdcheck.examples:BACKTEST_SCHEMA",
                                     "--n", "100", "--no-shrink"]), 0)
        self.assertIn("ZeroDivisionError", stdout.getvalue())
        with open(out, encoding="ascii") as f:
            rep = json.load(f)
        self.assertEqual(rep["validate"], "qdcheck.examples:validate_zerodiv")
        self.assertEqual(rep["n"], 800)


if __name__ == "__main__":
    unittest.main()
