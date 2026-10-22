"""已提交向量的判据：可在本机重新生成并在 1e-9 内对上；第二份独立实现也能复现；格式 / 覆盖面 / 合成序列特征。"""
import json
import math
import os
import re
import shutil
import tempfile
import unittest

from qdcheck import compare as C
from qdcheck import gen_vectors as G
from qdcheck import indicators_alt as A
from qdcheck import indicators_ref as R
from qdcheck.tests._util import captured, scale

REQUIRED = ("indicator", "variant", "params", "input", "output")
FLOAT_TOKEN = re.compile(r"-?\d+\.\d*(?:e[-+]\d+)?|-?\d+e[-+]\d+")


def _load(ind):
    return C.load_vectors(os.path.join(G.VECTOR_DIR, f"{ind}.json"))


def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)


class TestCommittedVectors(unittest.TestCase):
    def test_regenerates_within_tolerance(self):
        inds = scale(list(R.INDICATORS), ["kdj", "trix", "ichimoku"])
        summary = G.check_all(indicators=inds)
        self.assertTrue(summary["ok"], summary["mismatches"][:5])
        self.assertGreater(summary["values"], 10000 if len(inds) == 9 else 1000)

    def test_independent_implementation_reproduces_committed_outputs(self):
        for ind in scale(list(R.INDICATORS), ["stochrsi", "cci"]):
            for v in _load(ind):
                got = A.compute(ind, v["input"], v["variant"], v["params"])[v["line"]]
                res = C.compare_vector(v, got)
                self.assertTrue(res["ok"], (v["id"], C.format_result(res)))

    def test_coverage_of_series_variants_lines_and_params(self):
        manifest = _read_json(os.path.join(G.VECTOR_DIR, "manifest.json"))
        names = [s["name"] for s in manifest["series"]]
        self.assertGreaterEqual(len(names), 6)
        for need in ("const", "mono_up", "randwalk", "jumps", "longflat", "short1", "short2", "short5"):
            self.assertIn(need, names)
        total = 0
        for ind in R.INDICATORS:
            vecs = _load(ind)
            ids = {v["id"] for v in vecs}
            self.assertEqual(len(ids), len(vecs))
            want = {f"{ind}|{var}|{line}|{name}" + (f"|{tag}" if tag else "") for var in R.VARIANTS[ind]
                    for tag, _o in G.param_sets(ind) for line in R.LINES[ind] for name in names}
            self.assertEqual(ids, want)
            self.assertEqual(manifest["indicators"][ind]["count"], len(vecs))
            total += len(vecs)
            for v in vecs:
                for key in REQUIRED:
                    self.assertIn(key, v)
                n = len(v["input"]["close"])
                self.assertEqual({len(v["input"][k]) for k in ("open", "high", "low", "close", "volume")}, {n})
                self.assertEqual(len(v["output"]), n)
                self.assertTrue(all(x is None or isinstance(x, float) for x in v["output"]))
        self.assertEqual(total, sum(m["count"] for m in manifest["indicators"].values()))

    def test_numbers_written_with_17_significant_digits(self):
        with open(os.path.join(G.VECTOR_DIR, "trix.json"), encoding="utf-8") as f:
            text = f.read()
        tokens = FLOAT_TOKEN.findall(text[text.index("\"vectors\":["):])       # 跳过文件头里的字符串
        self.assertGreater(len(tokens), 10000)
        long_ones = 0
        for tok in tokens[:20000]:
            x = float(tok)
            self.assertEqual(G.fmt_float(x), tok)                  # 文件里的写法就是 %.17g（+ ".0"）
            digits = len(tok.split("e")[0].replace("-", "").replace(".", "").lstrip("0"))
            self.assertLessEqual(digits, 17)
            long_ones += digits == 17
        self.assertGreater(long_ones, 1000)
        self.assertIn("null", text)


class TestWriterAndChecker(unittest.TestCase):
    def test_fmt_float(self):
        self.assertEqual(G.fmt_float(0.1), "0.10000000000000001")
        self.assertEqual(G.fmt_float(100.0), "100.0")
        self.assertEqual(G.fmt_float(-0.0), "-0.0")
        self.assertEqual(G.fmt_float(1e-9), "1.0000000000000001e-09")
        self.assertEqual(G.fmt_float(1e22), "1e+22")
        for x in (math.pi, -1 / 3, 2.0 ** -40, 123456.789):
            self.assertEqual(float(G.fmt_float(x)), x)
        for bad in (math.nan, math.inf, -math.inf):
            with self.assertRaises(ValueError):
                G.fmt_float(bad)

    def test_dumps(self):
        self.assertEqual(G.dumps({"a": [1, 2.0, None, True, False, "xé"], "b": (3,)}),
                         '{"a":[1,2.0,null,true,false,"xé"],"b":[3]}')
        with self.assertRaises(TypeError):
            G.dumps({1, 2})

    def test_write_then_check_detects_tampering(self):
        tmp = tempfile.mkdtemp()
        try:
            counts = G.write_all(tmp)
            manifest = _read_json(os.path.join(tmp, "manifest.json"))
            self.assertEqual({k: v for k, v in counts.items() if not k.startswith("ledger_")},
                             {k: v["count"] for k, v in manifest["indicators"].items()})
            self.assertEqual(sorted(f"ledger_{x['file'][7:-5]}" for x in manifest["ledger"]),
                             sorted(k for k in counts if k.startswith("ledger_")))
            self._tamper_ledger(tmp)
            self.assertTrue(G.check_all(tmp, indicators=["cci", "ao"])["ok"])
            path = os.path.join(tmp, "cci.json")
            obj = _read_json(path)
            v = obj["vectors"][5]
            k = next(i for i, x in enumerate(v["output"]) if x is not None)
            v["output"][k] *= 1 + 1e-6
            obj["vectors"][7]["params"]["period"] = 21
            obj["vectors"][9]["input"]["close"][0] += 1.0
            removed = obj["vectors"].pop(11)["id"]
            obj["vectors"].append(dict(obj["vectors"][0], id="cci|extra"))
            _write_json(path, obj)
            res = G.check_all(tmp, indicators=["cci"])
            self.assertFalse(res["ok"])
            text = "\n".join(res["mismatches"])
            self.assertIn(f"{v['id']}: MISMATCH [value] first index {k}", text)
            self.assertIn("metadata differs", text)
            self.assertIn("input.close: MISMATCH [value] first index 0", text)
            self.assertIn(f"missing on disk: {removed}", text)
            self.assertIn("not regenerated: cci|extra", text)
            self.assertEqual(res["files"]["cci"]["problems"], 5)
            with captured() as (out, _err):
                self.assertEqual(G.main(["--check", "--out", tmp, "--json", os.path.join(tmp, "s.json")]), 1)
            self.assertIn("mismatches 10", out.getvalue())          # 5 处指标向量 + 5 处账本向量
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _tamper_ledger(self, tmp):
        self.assertEqual(G.check_ledger_vectors(tmp)["mismatches"], [])
        edits = {
            "ledger_long_add_partial_close.json": lambda v: v["expected"]["equity"].__setitem__(2, 0.0),
            "ledger_cross_x3_liquidation.json": lambda v: v["config"].__setitem__("mmr", 0.06),
            "ledger_isolated_x20_liquidations.json": lambda v: v["records"][1].__setitem__("price", 91.0),
            "ledger_short_reverse_funding_settlement.json": lambda v: v["closes"].__setitem__(1, 1.0),
        }
        for name, edit in edits.items():
            path = os.path.join(tmp, name)
            vec = _read_json(path)
            edit(vec)
            _write_json(path, vec)
        os.remove(os.path.join(tmp, "ledger_stream_cross_x5.json"))
        res = G.check_ledger_vectors(tmp)
        text = "\n".join(res["mismatches"])
        self.assertIn("ledger_long_add_partial_close: expected.equity bar 2", text)
        self.assertIn("ledger_cross_x3_liquidation: config / record count differs", text)
        self.assertIn("ledger_isolated_x20_liquidations: record 1 differs", text)
        self.assertIn("ledger_short_reverse_funding_settlement: closes: MISMATCH [value] first index 1", text)
        self.assertIn("ledger_stream_cross_x5: missing on disk", text)
        self.assertEqual(len(res["mismatches"]), 5)
        self.assertEqual(res["vectors"], 6)

    def test_main_writes_and_checks(self):
        tmp = tempfile.mkdtemp()
        try:
            with captured() as (out, _err):
                self.assertEqual(G.main(["--out", tmp]), 0)
                self.assertEqual(G.main(["--check", "--out", tmp]), 0)
            self.assertIn('"total": ', out.getvalue())
            self.assertIn("mismatches 0", out.getvalue())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestVectorSeries(unittest.TestCase):
    def setUp(self):
        self.s = dict(G.all_series())

    def test_shapes(self):
        self.assertEqual({len(self.s[k]["close"]) for k in ("short1", "short2", "short5")}, {1, 2, 5})
        self.assertEqual(set(self.s["const"]["close"]), {100.0})
        up, down = self.s["mono_up"]["close"], self.s["mono_down"]["close"]
        self.assertTrue(all(b > a for a, b in zip(up, up[1:])))
        self.assertTrue(all(b < a for a, b in zip(down, down[1:])))
        for name, d in self.s.items():
            for o, h, lo, c in zip(d["open"], d["high"], d["low"], d["close"]):
                self.assertTrue(lo <= min(o, c) <= max(o, c) <= h and lo > 0, name)

    def test_jumps_and_long_flat(self):
        d = self.s["jumps"]
        o, c = d["open"], d["close"]
        self.assertTrue(all(abs(o[i] / c[i - 1] - 1) >= 0.03 for i in (30, 90)))
        self.assertTrue(all(abs(abs(c[i] / o[i] - 1) - 0.08) < 0.001 for i in (60, 120)))
        f = self.s["longflat"]
        self.assertEqual(len(set(f["close"][40:110])), 1)
        self.assertEqual(len(set(f["high"][40:110] + f["low"][40:110])), 1)
        self.assertTrue(all(v > 0 for v in f["volume"][40:110]))
        self.assertEqual(set(f["volume"][125:145]), {0.0})
        self.assertEqual(len(set(f["close"][125:145])), 1)


if __name__ == "__main__":
    unittest.main()
