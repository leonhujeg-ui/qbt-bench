import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout

from qdtext import common as C
from qdtext import wordy as W
from qdtext.tests._util import packs_from, tmpdir, write_packs

CFG = C.load_config(None, W.DEFAULT_CONFIG)
TABLES = W.load_tables()


class TestScore(unittest.TestCase):
    def test_clause_rule(self):
        self.assertEqual(W.score_text("甲，乙，丙", CFG), (2.0, ["clauses=3"]))
        self.assertEqual(W.score_text("甲，乙", CFG), (1.0, ["clauses=2"]))
        self.assertEqual(W.score_text("甲：乙；丙。", CFG), (2.0, ["clauses=3"]))        # 冒号 / 分号也切，末尾句号不产生空分句
        self.assertEqual(W.score_text("甲", CFG), (0.0, []))

    def test_word_rule_weights_and_cap(self):
        s, hits = W.score_text("因为所以如果注意为了", CFG)
        self.assertEqual(s, 3.0)                                                   # 5 个词各 1 分，封顶 3
        self.assertEqual(set(hits), {"word:因为", "word:所以", "word:如果", "word:注意", "word:为了"})
        self.assertEqual(W.score_text("请", CFG), (0.5, ["word:请"]))
        self.assertEqual(W.score_text("立即", CFG), (0.0, []))                       # 「即」不在词表
        self.assertGreaterEqual(len(CFG["explain_words"]), 35)
        self.assertTrue(all(w in (0.5, 1) for w in CFG["explain_words"].values()))
        for word, weight in {"因为": 1, "由于": 1, "所以": 1, "因此": 1, "如果": 1, "注意": 1, "将会": 1, "不代表": 1, "仅供": 1,
                             "说明": 1, "也就是": 1, "而是": 1, "若": 0.5, "请": 0.5, "可能": 0.5, "建议": 0.5, "需要": 0.5}.items():
            self.assertEqual(W.score_text(word, CFG), (weight, [f"word:{word}"]), word)

    def test_parens_and_width(self):
        self.assertEqual(W.score_text("甲（补充内容）", CFG), (1.0, ["parens"]))
        self.assertEqual(W.score_text("甲(x)", CFG)[1], [])                          # 括号里不足 2 字不算
        self.assertEqual(W.score_text("一" * 17, CFG), (1.0, ["width=17>16"]))
        self.assertEqual(W.score_text("一" * 25, CFG), (2.0, ["width=25>24"]))
        self.assertEqual(W.score_text("一" * 16, CFG), (0.0, []))

    def test_examples(self):
        wordy = ["由于网络波动，订单可能延迟成交，请耐心等待", "注意：杠杆越高，爆仓风险越大", "该功能仅供参考，不代表投资建议，请谨慎操作"]
        good = ["请输入密码", "余额不足，请先充值", "网络连接已断开，正在重试…", "此操作不可撤销", "保存并返回", "机器人已暂停（手动）"]
        for t in wordy:
            self.assertGreaterEqual(W.score_text(t, CFG)[0], CFG["threshold"], t)
        for t in good:
            self.assertLess(W.score_text(t, CFG)[0], CFG["threshold"], t)

    def test_find_explanatory_severity_by_type(self):
        packs = packs_from({"a.okBtn": "如果余额不足，请先充值，否则失败", "a.hint": "如果余额不足，请先充值，否则失败", "a.short": "好", "a.n": 5})
        f = W.find_explanatory(packs, CFG)
        self.assertEqual([(x["key"], x["severity"], x["key_type"]) for x in f], [("a.hint", "yellow", "desc"), ("a.okBtn", "red", "button")])
        self.assertEqual(f[0]["score"], 4.5)
        self.assertIn("clauses=3", f[0]["hits"])
        self.assertIn("说明句（4.5 分", f[0]["suggestion"])


class TestDuplicate(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(W.normalize("已暂停", CFG), "暂停")
        self.assertEqual(W.normalize("正在处理中…", CFG), "处理")
        self.assertEqual(W.normalize("请稍候，谢谢。", CFG), "稍候谢谢")
        self.assertEqual(W.normalize("中", CFG), "中")                               # 不会剥成空串
        self.assertEqual(W.normalize("ABC Def", CFG), "abcdef")

    def test_concepts_longest_first(self):
        groups = [["执行异常", "急停"], ["异常"]]
        self.assertEqual(W.concepts("执行异常，已通知", groups), {0})               # 「异常」已被「执行异常」吃掉
        self.assertEqual(W.concepts("运行异常", groups), {1})
        self.assertEqual(W.concepts("正常", groups), set())

    def test_levenshtein_similarity(self):
        self.assertEqual(W.levenshtein("", "abc"), 3)
        self.assertEqual(W.levenshtein("kitten", "sitting"), 3)
        self.assertEqual(W.levenshtein("同一", "同一"), 0)
        self.assertEqual(W.levenshtein("abc", "bc"), 1)
        self.assertEqual(W.levenshtein("bc", "abc"), 1)
        self.assertEqual(W.levenshtein("abc", "abd"), 1)
        self.assertEqual(W.levenshtein("abc", ""), 3)
        self.assertAlmostEqual(W.similarity("下单失败请检查参数", "下单失败请核对参数"), 1 - 2 / 9)
        self.assertEqual(W.similarity("", ""), 1.0)

    def test_antonym_veto(self):
        ant = TABLES["antonyms"]
        self.assertEqual(W.antonym_veto("已买入", "已卖出", ant), "买入/卖出")
        self.assertEqual(W.antonym_veto("已卖出", "已买入", ant), "买入/卖出")
        self.assertIsNone(W.antonym_veto("买入卖出", "买入卖出中", ant))              # 两边都有不算
        self.assertIsNone(W.antonym_veto("运行中", "执行中", ant))

    def test_duplicate_reason_methods(self):
        cases = {("执行异常，已通知客服", "急停后待确认"): "synonym", ("已暂停", "暂停中"): "normalized-equal",
                 ("余额不足", "可用余额不足，请充值"): "containment", ("下单失败，请检查参数", "下单失败，请核对参数"): "edit-distance",
                 ("已买入", "已卖出"): None, ("操作成功", "操作失败"): None, ("运行中", "运行异常"): None, ("已保存", "更新成功"): None,
                 ("策略正在运行", "策略运行中"): "synonym", ("中", "中"): "normalized-equal", ("甲", "甲乙丙"): None}
        for (a, b), want in cases.items():
            self.assertEqual(W.duplicate_reason(a, b, CFG, TABLES), want, (a, b))
            self.assertEqual(W.duplicate_reason(b, a, CFG, TABLES), want, (b, a))

    def test_edit_distance_thresholds(self):
        cfg = dict(CFG, edit_similarity=0.5)
        self.assertEqual(W.duplicate_reason("创建订单", "删除订单", cfg, TABLES), "edit-distance")
        self.assertIsNone(W.duplicate_reason("创建订单", "删除订单", CFG, TABLES))
        cfg = dict(CFG, edit_min_len=10)
        self.assertIsNone(W.duplicate_reason("下单失败，请检查参数", "下单失败，请核对参数", cfg, TABLES))

    def test_resolve_key(self):
        packs = {"a": {"zh": {"k": "甲", "n": 1}}, "b": {"zh": {"k": "乙", "u": "丙"}}}
        self.assertEqual(W.resolve_key("a:k", packs, "zh"), ("a", "k", "甲"))
        self.assertEqual(W.resolve_key("u", packs, "zh"), ("b", "u", "丙"))
        self.assertEqual(W.resolve_key("k", packs, "zh")[0], None)
        self.assertIn("多个命名空间", W.resolve_key("k", packs, "zh")[2])
        self.assertEqual(W.resolve_key("a:zz", packs, "zh"), (None, "a:zz", "键不存在"))
        self.assertEqual(W.resolve_key("a:n", packs, "zh"), (None, "a:n", "键不存在"))
        self.assertEqual(W.resolve_key("zz", packs, "zh"), (None, "zz", "键不存在"))

    def test_find_duplicates(self):
        packs = packs_from({"a": "已暂停", "b": "暂停中", "c": "买入", "d": "卖出"})
        scr = {"s1": ["app:a", "app:b", "c", "d"], "s2": ["app:zz", "app:c"]}
        f = W.find_duplicates(packs, scr, CFG, TABLES)
        dup = [x for x in f if x["rule"] == "same-screen-duplicate"]
        self.assertEqual([(x["screen"], x["key"], x["other_key"], x["method"], x["severity"]) for x in dup],
                         [("s1", "a", "b", "normalized-equal", "red")])
        self.assertIn("建议留「已暂停」", dup[0]["suggestion"])
        miss = [x for x in f if x["rule"] == "screen-key-missing"]
        self.assertEqual([(x["key"], x["severity"], x["screen"]) for x in miss], [("app:zz", "yellow", "s2")])


class TestRunAndCli(unittest.TestCase):
    def test_run_and_main(self):
        d = tmpdir(self)
        loc = write_packs(os.path.join(d, "loc"), packs_from({"a.hint": "由于网络波动，订单可能延迟成交，请耐心等待", "b": "已暂停", "c": "暂停中"}))
        scr = os.path.join(d, "screens.json")
        C.write_json(scr, {"s": ["app:b", "app:c"]})
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(W.main([loc, "--screens", scr, "--out", os.path.join(d, "o")]), 1)
        self.assertTrue(out.getvalue().startswith("QDTEXT WORDY FAIL n=2 red=1 yellow=1\n"))
        doc = C.read_json(os.path.join(d, "o", "wordy.json"))
        self.assertEqual(doc["meta"]["screens"], 1)
        self.assertEqual([f["rule"] for f in doc["findings"]], ["same-screen-duplicate", "explanatory"])
        with open(os.path.join(d, "o", "wordy.md"), encoding="utf-8") as f:
            md = f.read()
        self.assertIn("normalized-equal", md)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(W.main([loc, "--out", os.path.join(d, "o2")]), 0)               # 没有屏配置：只有黄
            self.assertEqual(W.main([loc, "--out", os.path.join(d, "o3"), "--strict"]), 1)
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(W.main([os.path.join(d, "none")]), 2)
        self.assertIn("目录不存在", err.getvalue())

    def test_custom_tables_and_config(self):
        d = tmpdir(self)
        tables = os.path.join(d, "t.json")
        C.write_json(tables, {"synonyms": [["甲", "乙"]], "antonyms": []})
        self.assertEqual(W.load_tables(tables)["synonyms"], [["甲", "乙"]])
        self.assertEqual(W.duplicate_reason("甲状态", "乙状态", CFG, W.load_tables(tables)), "synonym")
        cfg = os.path.join(d, "cfg.json")
        C.write_json(cfg, {"threshold": 0.5})
        loc = write_packs(os.path.join(d, "loc"), packs_from({"a": "请"}))
        with redirect_stdout(io.StringIO()) as out:
            W.main([loc, "--out", os.path.join(d, "o"), "--config", cfg, "--tables", tables])
        self.assertIn("n=1", out.getvalue().splitlines()[0])

    def test_default_tables_shape(self):
        self.assertTrue(all(len(g) >= 2 for g in TABLES["synonyms"]))
        self.assertTrue(all(len(p) == 2 for p in TABLES["antonyms"]))
        members = [m for g in TABLES["synonyms"] for m in g]
        self.assertEqual(len(members), len(set(members)), "同义词组之间成员不应重复")


if __name__ == "__main__":
    unittest.main()
