"""CONTRACT.md 与代码一致：向量文件全部符合 vectors/schema.json；校验器能抓篡改；CONTRACT.md 列出了代码里每个枚举值
（Report 字段、乱喂类别、账本行字段 / 检查名、compare 的 reason、determinism 的差异 reason）；退出码与第一行格式。"""
import json
import os
import re
import unittest

from qdcheck import compare as C
from qdcheck import fuzz as F
from qdcheck import ledger as L
from qdcheck import vector_schema as V
from qdcheck.gen_vectors import VECTOR_DIR
from qdcheck.tests._util import REPO

CONTRACT = os.path.join(REPO, "qdcheck", "CONTRACT.md")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _load(name):
    with open(os.path.join(VECTOR_DIR, name), encoding="utf-8") as f:
        return json.load(f)


class TestVectorSchema(unittest.TestCase):
    def test_all_committed_files_are_valid(self):
        rep = V.check_directory()
        self.assertTrue(rep["ok"], {k: v for k, v in rep["files"].items() if v})
        self.assertEqual(rep["checked"], 17)                 # 9 指标 + 7 账本 + manifest

    def test_validator_rejects_tampering(self):
        obj = _load("cci.json")
        good = json.loads(json.dumps(obj))
        self.assertEqual(V.indicator_file_problems(good), [])
        cases = {
            "format": lambda o: o.__setitem__("format", "v2"),
            "extra key": lambda o: o["vectors"][0].__setitem__("note", 1),
            "missing key": lambda o: o["vectors"][0].pop("params"),
            "output length": lambda o: o["vectors"][0]["output"].append(1.0),
            "bad id": lambda o: o["vectors"][0].__setitem__("id", "CCI/x"),
            "unknown param": lambda o: o["vectors"][0]["params"].__setitem__("window", 3),
            "bool as number": lambda o: o["vectors"][0]["output"].__setitem__(-1, True),
            "nan in input": lambda o: o["vectors"][0]["input"]["close"].__setitem__(0, float("nan")),
            "ohlc order": lambda o: o["vectors"][0]["input"]["low"].__setitem__(0, 1e9),
            "count": lambda o: o.__setitem__("count", 1),
            "variant not in header": lambda o: o["vectors"][0].__setitem__("variant", "src=hlc3;zero_dev=maybe"),
            "duplicate id": lambda o: o["vectors"][1].__setitem__("id", o["vectors"][0]["id"]),
            "column length": lambda o: o["vectors"][0]["input"]["volume"].append(1.0),
        }
        for name, edit in cases.items():
            bad = json.loads(json.dumps(obj))
            edit(bad)
            self.assertTrue(V.indicator_file_problems(bad), name)
        led = _load("ledger_long_add_partial_close.json")
        self.assertEqual(V.ledger_file_problems(led), [])
        for name, edit in {
            "bar beyond closes": lambda o: o["records"][0].__setitem__("bar", 99),
            "unsorted": lambda o: o["records"].insert(0, dict(o["records"][-1])),
            "side none on trade": lambda o: o["records"][0].__setitem__("side", "none"),
            "qty 0 on trade": lambda o: o["records"][0].__setitem__("qty", 0),
            "expected column length": lambda o: o["expected"]["equity"].append(1.0),
            "bad mode": lambda o: o["config"].__setitem__("margin_mode", "portfolio"),
            "funding with qty": lambda o: o["records"].append({"bar": 5, "kind": "funding", "side": "none", "qty": 1,
                                                               "price": None, "fee": 0.0, "funding": 1.0}),
        }.items():
            bad = json.loads(json.dumps(led))
            edit(bad)
            self.assertTrue(V.ledger_file_problems(bad), name)
        man = _load("manifest.json")
        self.assertEqual(V.validate(man, "manifest"), [])
        man["tolerance"]["rel_tol"] = 1e-6
        self.assertTrue(V.validate(man, "manifest"))

    def test_number_text_rule(self):
        self.assertEqual(V.number_text_problems('[0.10000000000000001, 100.0, 1e+22, 4.9406564584124654e-324, -0.0, 3, "1.23456789012345678901"]'), [])
        self.assertEqual(len(V.number_text_problems("[0.1]")), 1)                       # 不是 %.17g 写法
        self.assertEqual(len(V.number_text_problems("[0.10000000000000000555]")), 1)    # 20 位
        self.assertEqual(len(V.number_text_problems("[100]")), 0)                       # 整数字面量不受约束
        self.assertEqual(V.fmt_float(0.1), "0.10000000000000001")

    def test_validator_dialect(self):
        schema = {"n": {"type": "object", "required": ["a"], "additionalProperties": False,
                        "properties": {"a": {"type": ["number", "null"], "minimum": 0},
                                       "b": {"type": "array", "items": {"$ref": "leaf"}, "minItems": 1},
                                       "c": {"type": "string", "pattern": "x+"}, "d": {"const": 1}, "e": {"enum": [1, "s"]},
                                       "f": {"type": "integer", "exclusiveMinimum": 0}}},
                  "leaf": {"type": "boolean"}}
        ok = {"a": None, "b": [True], "c": "xx", "d": 1, "e": "s", "f": 1}
        self.assertEqual(V.validate(ok, "n", schema), [])
        for bad in ({}, {"a": -1}, {"a": 1, "z": 1}, {"a": 1, "b": []}, {"a": 1, "b": [1]}, {"a": 1, "c": "xy"},
                    {"a": 1, "d": 2}, {"a": 1, "e": 1.0}, {"a": 1, "e": True}, {"a": True}, {"a": float("inf")},
                    {"a": 1, "f": 0}, {"a": 1, "f": True}):
            self.assertTrue(V.validate(bad, "n", schema), bad)
        self.assertEqual(V.validate({"a": 1, "b": [1, 2, 3]}, "n", schema, limit=2).__len__(), 2)
        from qdcheck.tests._util import captured
        with captured() as (out, _err):
            self.assertEqual(V.main([]), 0)
        self.assertIn("checked 17 files, all valid", out.getvalue())


class TestContractDocument(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = _read(CONTRACT)

    def test_every_enum_in_code_is_documented(self):
        for name in F.Report.FIELDS:
            self.assertIn(f"`{name}`", self.text, f"Report field {name} missing from CONTRACT.md")
        for cat in F.CATEGORIES:
            self.assertIn(f"`{cat}`", self.text, cat)
        for field in L.ROW_FIELDS:
            self.assertIn(field, self.text, field)
        for check in L.CHECKS:
            self.assertIn(f"`{check}`", self.text, check)
        for reason in ("length", "warmup", "type", "value"):
            self.assertIn(reason, self.text)
        for key in ("period", "scale", "rsi_period", "stoch_period", "k_period", "d_period", "range_eps", "constant",
                    "fast", "slow", "n", "m1", "m2", "tenkan", "kijun", "senkou_b", "displacement"):
            self.assertIn(f"`{key}`", self.text)
        self.assertIn("MISMATCH idx=<下标> expected=<期望> actual=<实际> rel=", self.text)
        self.assertIn("--vector", self.text)
        self.assertIn("is_controlled", self.text)
        self.assertIn("PYTHONPATH", self.text)

    def test_ledger_flag_names_are_enumerated(self):
        import ast
        with open(L.__file__, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        used = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "_flag":
                used.add(node.args[1].value)
        self.assertEqual(used, set(L.CHECKS))

    def test_fuzz_report_fields_match_contract_table(self):
        rows = re.findall(r"^\| `([a-z0-9_]+)`(?:, `([a-z0-9_]+)`)? \|", self.text, flags=re.M)
        documented = {a for a, _b in rows} | {b for _a, b in rows if b}
        self.assertTrue(set(F.Report.FIELDS) <= documented, set(F.Report.FIELDS) - documented)

    def test_ichimoku_example_in_contract_matches_code(self):
        from qdcheck import indicators_ref as R
        d = {"open": [1.0, 2.0, 4.0, 3.0, 5.0], "high": [2.0, 3.0, 5.0, 4.0, 6.0], "low": [0.5, 1.0, 2.0, 2.5, 3.0],
             "close": [1.0, 2.0, 4.0, 3.0, 5.0], "volume": [1.0] * 5}
        out = R.compute("ichimoku", d, "shift=forward", {"tenkan": 2, "kijun": 3, "senkou_b": 4, "displacement": 1})
        self.assertEqual(out["tenkan"], [None, 1.75, 3.0, 3.5, 4.25])
        self.assertEqual(out["kijun"], [None, None, 2.75, 3.0, 4.0])
        self.assertEqual(out["senkou_a"], [None, None, None, 2.875, 3.25])
        self.assertEqual(out["senkou_b"], [None, None, None, None, 2.75])
        self.assertEqual(out["chikou"], [2.0, 4.0, 3.0, 5.0, None])
        for token in ("2.875  3.25", "null   2.75", "2.0    4.0    3.0    5.0    null"):
            self.assertIn(token, self.text)

    def test_compare_exit_codes_match_contract(self):
        self.assertIn("**0** 全部一致", self.text)
        self.assertIn("**1** 有不一致", self.text)
        self.assertIn("**2** 输入不合法", self.text)
        self.assertEqual(C.first_line(C.compare_series([1.0], [1.0])), "OK n=1 max_rel=0.000e+00")


ALLOWED = set("""qdcheck re json math os sys time random struct hashlib itertools functools collections dataclasses typing
pathlib subprocess unittest statistics decimal fractions tempfile argparse csv io datetime zlib heapq bisect array contextlib
operator string textwrap copy enum abc traceback signal shutil glob fnmatch logging secrets uuid types inspect warnings platform
resource gc tracemalloc cProfile pstats unicodedata codecs locale threading queue multiprocessing concurrent""".split())
REPO_INTERNAL = {"qbt", "strategies"}             # 本仓自己的回测引擎：只在“真实用例”适配器里、函数内延迟导入
TOOLS_EXTRA = {"ast", "tools", "mutants"} | REPO_INTERNAL   # tools/ 与 tests/：ast（标准库）+ 本仓已有的 tools/cov.py 与 mutants/
NETWORK = {"socket", "urllib", "http", "ssl", "ftplib", "smtplib", "xmlrpc", "asyncio", "select", "selectors"}


class TestDependencyPolicy(unittest.TestCase):
    """零第三方依赖、零出网：库模块与测试只许导入白名单里的标准库；tools/ 另许 ast 与本仓自有工具包。"""

    def _imports(self, path):
        """返回 (模块级导入, 函数内导入) 两个集合。"""
        import ast
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        top, inner = set(), set()
        in_func = set()
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                in_func |= {id(n) for n in ast.walk(fn)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = {node.module.split(".")[0]}
            else:
                continue
            (inner if id(node) in in_func else top).update(names)
        return top, inner

    def test_imports_are_stdlib_only_and_offline(self):
        root = os.path.join(REPO, "qdcheck")
        offenders = {}
        for dirpath, _dirs, files in os.walk(root):
            if "__pycache__" in dirpath or os.path.basename(dirpath) in ("vectors", "results"):
                continue
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                top, inner = self._imports(path)
                is_lib = dirpath == root
                allowed_top = ALLOWED if is_lib else ALLOWED | TOOLS_EXTRA
                allowed_inner = ALLOWED | REPO_INTERNAL if is_lib else ALLOWED | TOOLS_EXTRA
                bad = (top - allowed_top) | (inner - allowed_inner)
                if bad:
                    offenders[os.path.relpath(path, REPO)] = sorted(bad)
                self.assertFalse((top | inner) & NETWORK, path)
        self.assertEqual(offenders, {})
        top, inner = self._imports(os.path.join(root, "examples.py"))
        self.assertEqual(top & REPO_INTERNAL, set())                 # import qdcheck.examples 不需要 qbt
        self.assertEqual(inner & REPO_INTERNAL, REPO_INTERNAL)       # 只有 qbt_backtest() 适配器在函数内导入

    def test_no_network_or_outside_writes_in_library(self):
        root = os.path.join(REPO, "qdcheck")
        for name in sorted(os.listdir(root)):
            if name.endswith(".py"):
                with open(os.path.join(root, name), encoding="utf-8") as f:
                    text = f.read()
                for word in ("socket", "urllib", "http.client", "requests", "ssl"):
                    self.assertNotIn(word + ".", text, (name, word))
                    self.assertNotIn("import " + word, text, (name, word))


if __name__ == "__main__":
    unittest.main()
