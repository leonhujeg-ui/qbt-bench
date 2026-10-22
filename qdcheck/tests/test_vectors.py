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
        """全部指标向量 + 账本向量与入库文件对比（快速模式也跑满：约 2.5 s，是生成代码里所有常数的判据）。"""
        summary = G.check_all()
        self.assertTrue(summary["ok"], summary["mismatches"][:5])
        self.assertEqual(summary["vectors"], 1260)
        self.assertGreater(summary["values"], 90000)
        self.assertEqual((summary["ledger"]["vectors"], summary["ledger"]["bars"]), (7, 1227))
        self.assertEqual(summary["ledger"]["bars"], sum(len(v["closes"]) for v in G.ledger_vectors()))

    def test_file_headers(self):
        for ind in R.INDICATORS:
            obj = _read_json(os.path.join(G.VECTOR_DIR, f"{ind}.json"))
            self.assertEqual((obj["format"], obj["indicator"], obj["seed"]), (G.FORMAT, ind, G.SEED))
            self.assertEqual(obj["count"], len(obj["vectors"]))
            self.assertEqual((obj["variants"], obj["lines"]), (list(R.VARIANTS[ind]), list(R.LINES[ind])))
        with open(os.path.join(G.VECTOR_DIR, "manifest.json"), encoding="utf-8") as f:
            self.assertEqual(f.read(), json.dumps(G.manifest(), indent=1, ensure_ascii=False) + "\n")
        led = _read_json(os.path.join(G.VECTOR_DIR, "ledger_cross_x3_liquidation.json"))
        self.assertEqual((led["format"], led["name"]), (G.LEDGER_FORMAT, "cross_x3_liquidation"))
        self.assertEqual(set(led["expected"]), set(G.ledger.ROW_FIELDS))

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
            with open(os.path.join(tmp, "manifest.json"), encoding="utf-8") as f:     # 刚写出的文件（不是入库的）
                self.assertEqual(f.read(), json.dumps(G.manifest(), indent=1, ensure_ascii=False) + "\n")
            for ind in R.INDICATORS:
                head = _read_json(os.path.join(tmp, f"{ind}.json"))
                self.assertEqual((head["count"], head["format"], head["indicator"]), (len(head["vectors"]), G.FORMAT, ind))
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

    def test_inconsistent_ledger_scenario_is_refused(self):
        from unittest import mock
        bad = [("bad", "提前且部分强平", {"initial_cash": 10_000.0, "margin_mode": "isolated", "leverage": 20, "mmr": 0.005},
                [100.0, 99.0], [{"bar": 0, "kind": "trade", "side": "buy", "qty": 1000, "price": 100.0},
                                {"bar": 1, "kind": "liquidation", "side": "sell", "qty": 400, "price": 99.0}])]
        with mock.patch("qdcheck.stream.ledger_scenarios", return_value=bad):
            with self.assertRaises(RuntimeError) as ctx:
                G.ledger_vectors()
        msg = str(ctx.exception)
        self.assertIn("'check': 'premature_liquidation'", msg)          # ledger 的前两条违背都要出现在消息里
        self.assertIn("'check': 'liquidation_not_full'", msg)
        self.assertIn("(1, 'premature_liquidation')", msg)                     # ledger_alt 的前两条也在
        self.assertIn("(1, 'liquidation_not_full')", msg)

    def test_helpers_and_timing(self):
        from unittest import mock
        self.assertTrue(G._rec_close(1.0, 2.0, 0.5))                 # 恰在容差边界上算一致
        self.assertFalse(G._rec_close(1.0, 2.0001, 0.5))
        self.assertTrue(G._rec_close(None, None, 1e-9))
        self.assertFalse(G._rec_close(None, 1.0, 1e-9))
        ticks = iter([3.0, 4.23456])
        with mock.patch.object(G.time, "perf_counter", lambda: next(ticks)):
            self.assertEqual(G.check_all(indicators=["cci"])["seconds"], 1.23)

    def test_main_writes_and_checks(self):
        tmp = tempfile.mkdtemp()
        try:
            with captured() as (out, _err):
                self.assertEqual(G.main(["--out", tmp]), 0)
            with captured():
                self.assertEqual(G.main(["--check", "--out", tmp]), 0)
            first = json.loads(out.getvalue().splitlines()[0])
            self.assertEqual(list(first), sorted(first))
            self.assertEqual(first["total"], 1260)
            path = os.path.join(tmp, "dema.json")
            obj = _read_json(path)
            for v in obj["vectors"][:25]:
                k = next(i for i, x in enumerate(v["output"]) if x is not None) if any(
                    x is not None for x in v["output"]) else None
                if k is None:
                    v["output"].append(1.0)
                else:
                    v["output"][k] += 1.0
            _write_json(path, obj)
            rep = os.path.join(tmp, "summary.json")
            with captured() as (out, _err):
                self.assertEqual(G.main(["--check", "--out", tmp, "--json", rep]), 1)
            lines = out.getvalue().splitlines()
            self.assertEqual(len(lines), 21)                            # 最多打印 20 条不一致 + 1 行汇总
            self.assertTrue(lines[0].startswith("dema|"))
            self.assertIn("mismatches 25", lines[-1])
            with open(rep, encoding="utf-8") as f:
                text = f.read()
            summary = json.loads(text)
            self.assertEqual(text, json.dumps(summary, indent=1, sort_keys=True))
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
