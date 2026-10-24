import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout

from qdtext import common as C
from qdtext import zh_pair as Z
from qdtext.tests._util import packs_from, tmpdir, write_packs

MINI = Z.Tables(
    {"马": "馬", "机": "機", "器": "器", "发": "發髮", "后": "後后", "台": "台臺", "录": "錄", "登": "登", "户": "戶", "用": "用", "买": "買", "卖": "賣",
     "头": "頭", "取": "取", "消": "消", "弃": "棄", "认": "認", "确": "確", "里": "裡", "面": "面麵"},
    {"登录": ["登入", "登錄"], "用户": ["使用者", "用戶"], "确认": ["確認", "確定"], "马丁": ["馬丁"], "面条": ["麵條"]},
    {"pairs": [["裡", "裏"], ["臺", "台"]], "punct": [["“", "「"], ["”", "」"]]},
    {"pairs": [["丁", "工"], ["買", "賣"]]})


class TestTables(unittest.TestCase):
    def test_simplified_only_and_equiv(self):
        self.assertEqual(MINI.simplified_only, {"马", "机", "发", "录", "户", "买", "卖", "头", "弃", "认", "确", "里"})   # 后 / 台 / 面 两边都有
        self.assertTrue(MINI.equivalent("裡", "裏"))
        self.assertTrue(MINI.equivalent("裏", "裡"))
        self.assertTrue(MINI.equivalent("“", "「"))
        self.assertTrue(MINI.equivalent("x", "x"))
        self.assertFalse(MINI.equivalent("裡", "台"))
        self.assertTrue(MINI.is_confusable("工", "丁"))
        self.assertFalse(MINI.is_confusable("工", "王"))
        self.assertEqual(MINI.max_word, 2)

    def test_real_tables_sanity(self):
        T = Z.load_tables()
        self.assertGreaterEqual(len(T.chars), 2000)
        self.assertGreaterEqual(len(T.words), 300)
        self.assertEqual(T.chars["马"][0], "馬")
        self.assertEqual(T.chars["台"][0], "台")
        self.assertIn("臺", T.chars["台"])
        self.assertEqual(T.chars["后"][0], "後")
        self.assertEqual(T.chars["发"][0], "發")
        self.assertIn("髮", T.chars["发"])
        self.assertEqual(Z.convert("马丁机器人", T)[0], "馬丁機器人")
        self.assertEqual(Z.convert("杠杆余额", T)[0], "槓桿餘額")
        self.assertEqual(Z.convert("台湾平台台风柜台", T)[0], "台灣平台颱風櫃檯")
        self.assertEqual(Z.convert("头发发布", T)[0], "頭髮發布")
        identity_default = 0
        for k, v in T.chars.items():
            self.assertEqual(len(k), 1, k)
            if v[0] == k:
                identity_default += 1                                            # 台 → 台臺颱檯 这类：默认保留，带依词替代
                self.assertGreater(len(v), 1, k)
            k.encode("gb2312")                                                   # 键必须是简体编码里的字
            v.encode("big5hkscs")                                                # 候选必须是繁体编码里的字
        self.assertLess(identity_default, 100)
        self.assertEqual(Z.convert("密钥", T)[0], "金鑰")
        for w, alts in list(T.words.items())[:50]:
            self.assertTrue(alts and all(alts), w)

    def test_extra_words_override(self):
        d = tmpdir(self)
        p = os.path.join(d, "w.json")
        C.write_json(p, {"words": {"马丁": ["馬汀"]}})
        T = Z.load_tables(p)
        self.assertEqual(Z.convert("马丁", T)[0], "馬汀")


class TestConvert(unittest.TestCase):
    def test_words_longest_first_and_spans(self):
        exp, spans, alts = Z.convert("用户登录", MINI)
        self.assertEqual(exp, "使用者登入")
        self.assertEqual(spans, [(0, 3, ["使用者", "用戶"]), (3, 5, ["登入", "登錄"])])
        self.assertEqual(len(alts), 5)

    def test_char_candidates(self):
        exp, _spans, alts = Z.convert("发后台x", MINI)
        self.assertEqual(exp, "發後台x")
        self.assertEqual(alts, [{"發", "髮"}, {"後", "后"}, {"台", "臺"}, {"x"}])

    def test_pick_alternatives(self):
        exp, spans, alts = Z.convert("用户登录", MINI)
        e2, a2 = Z.pick_alternatives(exp, spans, alts, "用戶登錄")
        self.assertEqual(e2, "用戶登錄")
        self.assertEqual(a2[0], {"用"})
        e3, _ = Z.pick_alternatives(exp, spans, alts, "使用者登入")
        self.assertEqual(e3, "使用者登入")                                            # 默认已出现就不换


class TestCompare(unittest.TestCase):
    def status(self, src, act):
        return Z.compare(src, act, MINI)

    def test_ok_paths(self):
        self.assertEqual(self.status("马丁机器人", "馬丁機器人")["status"], "ok")
        self.assertEqual(self.status("登录", "登入")["status"], "ok")
        self.assertEqual(self.status("登录", "登錄")["status"], "ok")
        self.assertEqual(self.status("用户", "用戶")["status"], "ok")                  # 不同长度的词级替代
        self.assertEqual(self.status("头发", "頭髮")["status"], "ok")                  # 字级候选
        self.assertEqual(self.status("台", "臺")["status"], "ok")                      # 合法异体
        self.assertEqual(self.status("“确认”", "「確定」")["status"], "ok")             # 标点对应 + 词级替代
        self.assertEqual(self.status("abc", "abc")["status"], "ok")

    def test_typo(self):
        r = self.status("马丁机器人", "馬工機器人")
        self.assertEqual(r["status"], "typo")
        self.assertEqual(r["typos"], [{"pos": 1, "expected": "丁", "actual": "工", "confusable": True}])
        r = self.status("用户马丁机器人", "使用者馬工機器人")                             # 前面换词也不影响定位
        self.assertEqual((r["status"], r["typos"][0]["pos"]), ("typo", 4))
        r = self.status("后台", "後兮")
        self.assertEqual((r["status"], r["typos"][0]["confusable"]), ("typo", False))
        r = self.status("买入", "賣出")                                               # 两处都不同但有形近字 → 错字
        self.assertEqual(r["status"], "typo")
        self.assertEqual([t["actual"] for t in r["typos"]], ["賣", "出"])
        r = self.status("后台买入机器人", "後台賣出機器人")                            # 替换块不在句首：位置要对
        self.assertEqual([(t["pos"], t["expected"], t["actual"]) for t in r["typos"]], [(2, "買", "賣"), (3, "入", "出")])
        r = self.status("后台头发", "後台頭髮")                                        # 字级候选（髮）要查对位置
        self.assertEqual(r["status"], "ok")
        r = self.status("后台买发", "後台賣髮")                                        # 两字替换块：第 1 个形近错字，第 2 个是候选 → 只报一个
        self.assertEqual([(t["pos"], t["expected"], t["actual"]) for t in r["typos"]], [(2, "買", "賣")])

    def test_paraphrase(self):
        r = self.status("取消", "放棄")
        self.assertEqual((r["status"], r["paraphrase"]), ("paraphrase", [{"expected": "取消", "actual": "放棄", "pos": 0}]))
        r = self.status("马丁", "馬丁格爾")
        self.assertEqual((r["status"], r["paraphrase"][0]["actual"]), ("paraphrase", "格爾"))
        r = self.status("马丁机器人", "機器人")
        self.assertEqual(r["status"], "paraphrase")
        r = self.status("马丁机器人取消", "馬工機器人放棄")                              # 形近错字 + 换词 → 错字优先
        self.assertEqual((r["status"], len(r["typos"]), len(r["paraphrase"])), ("typo", 1, 1))
        r = self.status("后台机器人取消", "後兮機器人放棄")                              # 非形近单字差 + 别处换词 → 并入意译
        self.assertEqual((r["status"], r["typos"], [x["actual"] for x in r["paraphrase"]]), ("paraphrase", [], ["兮", "放棄"]))
        r = self.status("后台", "後兮")                                                  # 单独的非形近单字差仍是错字（已知盲区）
        self.assertEqual(r["status"], "typo")

    def test_residue(self):
        r = self.status("马丁", "马丁")
        self.assertEqual((r["status"], r["residue"], r["expected"]), ("residue", ["马"], "馬丁"))
        self.assertEqual(self.status("后台", "后台")["status"], "ok")                  # 「后」两边都有，不是残留

    def test_check_packs(self):
        packs = packs_from({"a": "马丁", "b": "登录", "c": "取消", "d": "马丁", "e": "", "f": "马丁", "g": 3},
                           zh_TW={"a": "馬工", "b": "登入", "c": "放棄", "d": "马丁", "e": "x", "g": "三"})
        f = Z.check_packs(packs, Z.DEFAULT_CONFIG, MINI)
        got = {x["key"]: (x["rule"], x["severity"]) for x in f}
        self.assertEqual(got, {"a": ("suspect-typo", "red"), "c": ("paraphrase", "info"), "d": ("simplified-residue", "red")})
        a = [x for x in f if x["key"] == "a"][0]
        self.assertEqual(a["suggestion"], "丁 ≠ 工（位置 2，形近），疑似错字；期望「馬丁」")
        self.assertEqual(a["expected"], "馬丁")
        self.assertIn("意译，人工看", [x for x in f if x["key"] == "c"][0]["suggestion"])
        self.assertIn("残留简体字 马", [x for x in f if x["key"] == "d"][0]["suggestion"])


class TestRunAndCli(unittest.TestCase):
    def test_run_main(self):
        d = tmpdir(self)
        loc = write_packs(os.path.join(d, "loc"), packs_from({"a": "马丁机器人", "b": "取消"}, zh_TW={"a": "馬工機器人", "b": "放棄"}))
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(Z.main([loc, "--out", os.path.join(d, "o")]), 1)
        self.assertTrue(out.getvalue().startswith("QDTEXT ZHPAIR FAIL n=2 red=1 yellow=0\n"))
        doc = C.read_json(os.path.join(d, "o", "zh_pair.json"))
        self.assertEqual(doc["meta"]["pairs"], 2)
        self.assertEqual([x["rule"] for x in doc["findings"]], ["suspect-typo", "paraphrase"])
        with open(os.path.join(d, "o", "zh_pair.md"), encoding="utf-8") as f:
            self.assertIn("| 建议 | expected |", f.read())
        loc2 = write_packs(os.path.join(d, "loc2"), packs_from({"a": "马丁机器人"}, zh_TW={"a": "馬丁機器人"}))
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(Z.main([loc2, "--out", os.path.join(d, "o2")]), 0)
        self.assertTrue(out.getvalue().startswith("QDTEXT ZHPAIR OK n=0"))
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(Z.main(["--text", "马丁机器人 杠杆"]), 0)
        self.assertEqual(out.getvalue().strip(), "馬丁機器人 槓桿")
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(Z.main([os.path.join(d, "none")]), 2)
            self.assertEqual(Z.main([]), 2)
        self.assertIn("目录不存在", err.getvalue())


if __name__ == "__main__":
    unittest.main()
