import io
import json
import os
import unittest
from contextlib import redirect_stdout

from qdtext import common as C
from qdtext import synth
from qdtext.tests._util import tmpdir

VOCAB = synth.load_vocab()


class TestBuild(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.packs, cls.screens, cls.answers = synth.generate(7, plant=True)
        cls.clean, _s, cls.clean_answers = synth.generate(7, plant=False)

    def test_size_and_shape(self):
        self.assertGreaterEqual(self.clean_answers["keys"], 400)
        self.assertEqual(sorted(self.clean), ["account", "bot", "common", "trade", "wallet"])
        for ns in self.clean:
            self.assertEqual(sorted(self.clean[ns]), sorted(C.LANGS))
            for lang in C.LANGS:
                self.assertEqual(set(self.clean[ns][lang]), set(self.clean[ns]["zh"]), (ns, lang))
                self.assertTrue(all(isinstance(v, str) and v.strip() for v in self.clean[ns][lang].values()), (ns, lang))

    def test_six_key_families(self):
        keys = [k for ns in self.clean for k in self.clean[ns]["zh"]]
        fam = {"Btn": 0, "Title/Tab": 0, "Label": 0, "Chip": 0, "Hint/Help/Note/Tip": 0, "body": 0}
        for k in keys:
            last = k.rsplit(".", 1)[-1]
            if last.endswith("Btn"):
                fam["Btn"] += 1
            elif last.endswith(("Title", "Tab")):
                fam["Title/Tab"] += 1
            elif last.endswith("Label"):
                fam["Label"] += 1
            elif last.endswith("Chip"):
                fam["Chip"] += 1
            elif C.classify_key(k) == "desc":
                fam["Hint/Help/Note/Tip"] += 1
            else:
                fam["body"] += 1
        self.assertTrue(all(v >= 12 for v in fam.values()), fam)
        self.assertEqual(fam["Btn"], 144)
        self.assertEqual(fam["Label"], 96)
        self.assertEqual(fam["Chip"], 60)

    def test_templates(self):
        self.assertEqual(self.clean["trade"]["zh"]["order.createBtn"], "创建订单")
        self.assertEqual(self.clean["trade"]["zh-TW"]["order.createBtn"], "建立訂單")
        self.assertEqual(self.clean["trade"]["en"]["order.createBtn"], "Create order")
        self.assertEqual(self.clean["trade"]["ja"]["order.createBtn"], "注文作成")
        self.assertEqual(self.clean["trade"]["fr"]["order.nameLabel"], "Nom de l'ordre")
        self.assertEqual(self.clean["trade"]["de"]["order.nameLabel"], "Order-Name")
        self.assertEqual(self.clean["trade"]["en"]["order.count"], "1 order | {n} orders")
        self.assertEqual(self.clean["trade"]["zh"]["order.deleteConfirm"], "确定要删除这个订单吗？")
        self.assertEqual(self.clean["trade"]["en"]["order.deleteConfirm"], "Delete this order?")
        self.assertEqual(self.clean["bot"]["zh"]["pageTitle"], "机器人")
        self.assertEqual(self.clean["common"]["ko"]["status.runningChip"], "실행 중")
        self.assertEqual(synth.fill("{V} {n}", "en", v="create", n="bot"), "Create bot")
        self.assertEqual(synth.fill("{n}{v}", "ja", v="作成", n="注文"), "注文作成")
        self.assertEqual(synth.fill("{v} {n}", "de", v="laden", n="Order"), "laden Order")       # 只有 en / vi / fr 做首字母大写
        self.assertEqual(synth.fill("{v} {n}", "vi", v="xem", n="ví"), "Xem ví")

    def test_arabic_wrapping(self):
        self.assertEqual(synth.wrap_ltr("رسوم {fee} USDT"), "رسوم {fee} ⁦USDT⁩")
        self.assertEqual(synth.wrap_ltr("لا شيء"), "لا شيء")
        self.assertEqual(synth.wrap_ltr("API-v2 x"), "⁦API-v2⁩ x")
        for ns in self.clean:
            for k, v in self.clean[ns]["ar"].items():
                self.assertEqual(C.placeholders(v), C.placeholders(self.clean[ns]["zh"][k]), (ns, k))

    def test_deterministic(self):
        again = synth.generate(7, plant=True)
        self.assertEqual(json.dumps(again[0], sort_keys=True), json.dumps(self.packs, sort_keys=True))
        self.assertEqual(again[2], self.answers)
        other = synth.generate(8, plant=True)
        self.assertNotEqual(other[2]["lint"], self.answers["lint"])
        self.assertEqual(json.dumps(other[0]["trade"]["zh"]["order.createBtn"]), json.dumps("创建订单"))

    def test_plant_counts(self):
        rules = {}
        for a in self.answers["lint"]:
            if not a.get("secondary"):
                rules[a["rule"]] = rules.get(a["rule"], 0) + 1
        self.assertEqual(rules, {"missing-key": 8, "empty-value": 4, "placeholder-mismatch": 6, "pipe-mismatch": 3, "han-residue": 5,
                                 "fullwidth-punct": 3, "rtl-unwrapped-ltr": 3, "over-budget": 6, "extra-key": 2, "leaf-type": 1})
        self.assertEqual(rules, synth.LINT_PLAN)
        self.assertEqual(len(self.answers["lint"]), 44)
        self.assertTrue(all(a["lang"] not in ("zh", "zh-TW") for a in self.answers["lint"]))
        self.assertEqual(len(self.answers["zh_pair"]["typos"]), 20)
        self.assertEqual(len(self.answers["zh_pair"]["paraphrases"]), 50)
        kinds = {p["kind"] for p in self.answers["zh_pair"]["paraphrases"]}
        self.assertEqual(kinds, {"rewrite", "term-swap"})
        conf = {tuple(p) for p in C.data_file("zh_confusable.json")["pairs"]}
        for t in self.answers["zh_pair"]["typos"]:
            self.assertTrue((t["expected"], t["actual"]) in conf or (t["actual"], t["expected"]) in conf, t)
            self.assertEqual(self.packs[t["namespace"]]["zh-TW"][t["key"]][t["pos"]], t["actual"])
        self.assertEqual(len(self.answers["wordy"]["wordy_keys"]), 30)
        self.assertEqual(len(self.answers["wordy"]["good_keys"]), 30)
        self.assertEqual(len(self.answers["wordy"]["dup_groups"]), 10)
        self.assertEqual(len(self.screens), 18)

    def test_plants_applied(self):
        for a in self.answers["lint"]:
            flat = self.packs[a["namespace"]][a["lang"]]
            if a["rule"] == "missing-key":
                self.assertNotIn(a["key"], flat)
            elif a["rule"] == "empty-value":
                self.assertEqual(flat[a["key"]].strip(), "")
            elif a["rule"] == "han-residue":
                self.assertTrue(any(C.is_han(c) for c in flat[a["key"]]))
            elif a["rule"] == "fullwidth-punct":
                self.assertTrue(flat[a["key"]].endswith("。"))
            elif a["rule"] == "rtl-unwrapped-ltr":
                self.assertTrue(flat[a["key"]].endswith(" BTC"))
            elif a["rule"] == "over-budget":
                self.assertEqual(flat[a["key"]], synth.LONG_BUTTON[a["lang"]])
            elif a["rule"] == "extra-key":
                self.assertTrue(a["key"].endswith("Extra") and a["key"] in flat)
            elif a["rule"] == "leaf-type":
                self.assertEqual(flat[a["key"]], 123)
            elif a["rule"] == "pipe-mismatch":
                self.assertNotIn("|", flat[a["key"]])
            elif a["rule"] == "placeholder-mismatch" and not a.get("secondary"):
                self.assertNotEqual(C.placeholders(flat[a["key"]]), C.placeholders(self.packs[a["namespace"]]["zh"][a["key"]]))

    def test_write_and_main(self):
        d = tmpdir(self)
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(synth.main(["--out", d, "--seed", "3"]), 0)
        self.assertTrue(out.getvalue().startswith("QDTEXT SYNTH OK keys="))
        self.assertEqual(sorted(os.listdir(d)), ["answers.json", "locales", "screens.json"])
        self.assertEqual(len(os.listdir(os.path.join(d, "locales"))), 50)
        ans = C.read_json(os.path.join(d, "answers.json"))
        self.assertEqual((ans["seed"], ans["planted"]), (3, True))
        packs = C.load_locale_dir(os.path.join(d, "locales"))
        self.assertEqual(len(packs), 5)
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(synth.main(["--out", os.path.join(d, "c"), "--clean"]), 0)
        self.assertIn("lint_planted=0 typos=0 paraphrases=0", out.getvalue())
        self.assertEqual(C.read_json(os.path.join(d, "c", "answers.json"))["seed"], 7)      # 默认种子 7
        self.assertEqual(synth.DEFAULT_SEED, 7)


if __name__ == "__main__":
    unittest.main()
