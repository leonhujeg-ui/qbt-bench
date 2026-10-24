import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout

from qdtext import common as C
from qdtext import locale_lint as L
from qdtext.tests._util import packs_from, tmpdir, write_packs

CFG = C.load_config(None, L.DEFAULT_CONFIG)


def cfg_langs(*langs):
    c = C.load_config(None, L.DEFAULT_CONFIG)
    c["langs"] = list(langs)
    return c


def rules(findings, rule=None):
    return [f for f in findings if rule is None or f["rule"] == rule]


class TestAlignment(unittest.TestCase):
    def test_missing_file_and_key(self):
        packs = packs_from({"a": "甲", "b": "乙"}, en={"a": "A"})
        f = L.lint(packs, cfg_langs("zh", "en", "fr"))
        self.assertEqual([(x["rule"], x["lang"], x["key"]) for x in f], [("missing-file", "fr", "*"), ("missing-key", "en", "b")])
        self.assertIn("2 键全缺", f[0]["suggestion"])
        self.assertTrue(all(x["severity"] == "red" for x in f))

    def test_missing_base_file(self):
        f = L.lint({"app": {"en": {"a": "A"}}}, cfg_langs("zh", "en"))
        self.assertEqual([(x["rule"], x["lang"]) for x in f], [("missing-file", "zh")])

    def test_empty_extra_leaf(self):
        packs = packs_from({"a": "甲", "e": " "}, en={"a": "", "x": "extra", "e": "E", "n": 5})
        f = L.lint(packs, cfg_langs("zh", "en"))
        got = sorted((x["rule"], x["lang"], x["key"], x["severity"]) for x in f)
        self.assertEqual(got, [("empty-value", "en", "a", "red"), ("empty-value", "zh", "e", "red"), ("extra-key", "en", "n", "yellow"),
                               ("extra-key", "en", "x", "yellow"), ("leaf-type", "en", "n", "yellow")])

    def test_placeholder_and_pipe(self):
        packs = packs_from({"p": "{a} 和 {b}", "q": "1 个 | {n} 个", "ok": "{n} 个 | 没有"}, en={"p": "{a} and {c}", "q": "one", "ok": "{n} | none"})
        f = L.lint(packs, cfg_langs("zh", "en"))
        ph = rules(f, "placeholder-mismatch")
        self.assertEqual([(x["key"], x["expected"], x["actual"]) for x in ph], [("p", ["a", "b"], ["a", "c"]), ("q", ["n"], [])])
        self.assertIn("{a}, {b}", ph[0]["suggestion"])
        pp = rules(f, "pipe-mismatch")
        self.assertEqual([(x["key"], x["expected"], x["actual"]) for x in pp], [("q", 2, 1)])
        self.assertEqual(len(f), 3)

    def test_check_pair_direct(self):
        self.assertEqual(L.check_pair("ns", "k", "en", "{n}", "{n}"), [])
        self.assertEqual([x["rule"] for x in L.check_pair("ns", "k", "en", "a|b", "a")], ["pipe-mismatch"])
        self.assertIn("实际无", L.check_pair("ns", "k", "en", "{n}", "n")[0]["suggestion"])


class TestResidue(unittest.TestCase):
    def test_han_residue_by_language(self):
        packs = packs_from({"a": "确认"}, en={"a": "确认 OK"}, ja={"a": "確認"}, ko={"a": "확인 確認"}, zh_TW={"a": "確認"})
        f = rules(L.lint(packs, cfg_langs("zh", "en", "ja", "ko", "zh-TW")), "han-residue")
        self.assertEqual(sorted((x["lang"], x["suggestion"][:7]) for x in f), [("en", "残留汉字 确认"), ("ko", "残留汉字 確認")])
        self.assertTrue(all(x["severity"] == "red" for x in f))

    def test_fullwidth_only_en(self):
        packs = packs_from({"a": "好，的。"}, en={"a": "Loading… ok，done。(x)"}, fr={"a": "oui，"})
        f = rules(L.lint(packs, cfg_langs("zh", "en", "fr")), "fullwidth-punct")
        self.assertEqual([(x["lang"], x["suggestion"]) for x in f], [("en", "全角标点 。 ， 应换成半角")])

    def test_rtl_unwrapped(self):
        self.assertEqual(L.unwrapped_ltr_runs("سعر BTC الآن"), ["BTC"])
        self.assertEqual(L.unwrapped_ltr_runs("سعر ⁦BTC⁩ الآن"), [])
        self.assertEqual(L.unwrapped_ltr_runs("‎USDT‎"), [])
        self.assertEqual(L.unwrapped_ltr_runs("{amount} USDT و API-v2 و x"), ["USDT", "API-v2"])     # 占位符不算，单字母不算
        self.assertEqual(L.unwrapped_ltr_runs("⁦BTC الآن"), ["BTC"])                              # 只开不关
        packs = packs_from({"a": "{n} USDT"}, ar={"a": "{n} USDT"})
        f = rules(L.lint(packs, cfg_langs("zh", "ar")), "rtl-unwrapped-ltr")
        self.assertEqual([(x["severity"], x["lang"]) for x in f], [("yellow", "ar")])
        self.assertIn("只报不改", f[0]["suggestion"])


class TestBudget(unittest.TestCase):
    def test_budget_by_type_and_language(self):
        packs = packs_from({"x.okBtn": "确认订单吗", "x.runChip": "运行中啦啦", "x.nameLabel": "一二三四五六七八九", "x.feeHint": "说" * 25,
                            "x.body": "正" * 100},
                           en={"x.okBtn": "Confirm this order", "x.runChip": "Running", "x.nameLabel": "n" * 24, "x.feeHint": "h" * 61,
                               "x.body": "b" * 200})
        f = rules(L.lint(packs, cfg_langs("zh", "en")), "over-budget")
        got = {(x["lang"], x["key"]): (x["key_type"], x["width"], x["limit"], x["over"]) for x in f}
        self.assertEqual(got[("zh", "x.okBtn")], ("button", 5.0, 4.0, 1.0))
        self.assertEqual(got[("zh", "x.runChip")], ("status", 5.0, 4.0, 1.0))
        self.assertEqual(got[("zh", "x.nameLabel")], ("label", 9.0, 8.0, 1.0))
        self.assertEqual(got[("zh", "x.feeHint")], ("desc", 25.0, 24.0, 1.0))
        self.assertEqual(got[("en", "x.okBtn")], ("button", 8.0, 7.0, 1.0))          # 16 字符（含空格不计）> 14
        self.assertEqual(got[("en", "x.feeHint")], ("desc", 30.5, 30.0, 0.5))
        self.assertNotIn(("en", "x.nameLabel"), got)                                 # 刚好 24 字符不超
        self.assertNotIn(("zh", "x.body"), got)                                      # 正文不设预算
        self.assertTrue(all(x["severity"] == "yellow" for x in f))
        self.assertIn("button 预算 14 字符，现在 16 字符，缩短 2 字符", got and [x["suggestion"] for x in f if x["lang"] == "en" and x["key"] == "x.okBtn"][0])

    def test_budget_config(self):
        cfg = cfg_langs("zh")
        cfg["budgets"] = {"button": [10, 30]}
        cfg["budget_severity"] = "red"
        cfg["key_rules"] = [["button", ["go"]]]
        packs = packs_from({"a.go": "一" * 11, "a.okBtn": "一" * 11, "a.hint": "一" * 99})
        f = L.lint(packs, cfg)
        self.assertEqual([(x["key"], x["severity"]) for x in f], [("a.go", "red")])

    def test_markdown_ranking_sorted_by_over(self):
        packs = packs_from({"a.btn": "一二三四五六", "b.btn": "一二三四五"})
        f = L.lint(packs, cfg_langs("zh"))
        md = L.markdown(f, {"dir": "d", "namespaces": 1, "base_keys": 2, "langs": ["zh"]})
        pos_a, pos_b = md.index("| app | a.btn"), md.index("| app | b.btn")
        self.assertLess(pos_a, pos_b)
        self.assertIn("| over-budget | 2 |", md)
        self.assertIn("## 超预算排行", md)
        self.assertIn("无。", L.markdown([], {"dir": "d", "namespaces": 0, "base_keys": 0, "langs": []}))


class TestRunAndCli(unittest.TestCase):
    def test_run_writes_outputs(self):
        d = tmpdir(self)
        write_packs(os.path.join(d, "loc"), packs_from({"a": "甲"}, en={"a": "A"}))
        line, code, findings = L.run(os.path.join(d, "loc"), os.path.join(d, "out"), cfg_langs("zh", "en"))
        self.assertEqual((line, code, findings), ("QDTEXT LOCALE OK n=0 red=0 yellow=0", 0, []))
        doc = C.read_json(os.path.join(d, "out", "locale_lint.json"))
        self.assertEqual(doc["meta"]["base_keys"], 1)
        self.assertEqual(doc["tool"], "locale_lint")

    def test_main_exit_codes(self):
        d = tmpdir(self)
        loc = write_packs(os.path.join(d, "loc"), packs_from({"a": "甲", "b": "乙"}, en={"a": "A"}))
        cfg = os.path.join(d, "cfg.json")
        C.write_json(cfg, {"langs": ["zh", "en"]})
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(L.main([loc, "--out", os.path.join(d, "o1"), "--config", cfg]), 1)
        self.assertTrue(out.getvalue().startswith("QDTEXT LOCALE FAIL n=1 red=1 yellow=0\n"), out.getvalue())
        C.write_json(cfg, {"langs": ["zh", "en"], "budgets": {"button": [1, 1]}})
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(L.main([loc, "--out", os.path.join(d, "o2"), "--config", cfg, "--strict"]), 1)
        C.write_json(cfg, {"langs": ["zh"]})
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(L.main([loc, "--out", os.path.join(d, "o3"), "--config", cfg]), 0)
        self.assertTrue(out.getvalue().startswith("QDTEXT LOCALE OK n=0"))
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(L.main([os.path.join(d, "nope")]), 2)
        self.assertIn("目录不存在", err.getvalue())
        C.write_json(cfg, {"budgets": [1]})
        with redirect_stderr(err):
            self.assertEqual(L.main([loc, "--config", cfg, "--out", os.path.join(d, "o4")]), 2)
        C.write_json(cfg, {"base_lang": "xx"})
        with redirect_stderr(err):
            self.assertEqual(L.main([loc, "--config", cfg, "--out", os.path.join(d, "o5")]), 2)
        self.assertIn("配置不合法", err.getvalue())

    def test_default_config_consistency(self):
        self.assertEqual(L.DEFAULT_CONFIG["langs"], list(C.LANGS))
        self.assertEqual(L.DEFAULT_CONFIG["base_lang"], "zh")
        self.assertEqual(set(L.DEFAULT_CONFIG["budgets"]), {"button", "status", "label", "desc"})


if __name__ == "__main__":
    unittest.main()
