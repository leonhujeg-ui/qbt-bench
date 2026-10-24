"""判据对拍（tools/selfcheck）本身的测试：G1 / G2 用合成包真跑，G4 用离线快照；G3 真跑（字表齐了才有意义）。"""
import io
import os
import unittest
from contextlib import redirect_stdout

from qdtext import common as C
from qdtext.tests._util import tmpdir
from qdtext.tools import selfcheck as S

SNAP = os.path.join(C.FIXTURES, "snapshots", "site.json")


class TestSelfcheck(unittest.TestCase):
    def test_g1(self):
        s = S.g1(7)["summary"]
        self.assertTrue(S.g1_ok(s), s)
        self.assertEqual((s["recall_pct"], s["false_positives"], s["clean_red"]), (100.0, 0, 0))
        self.assertGreaterEqual(s["keys"], 400)
        self.assertEqual(set(s["per_rule"]), {"missing-key", "empty-value", "placeholder-mismatch", "pipe-mismatch", "han-residue",
                                              "fullwidth-punct", "rtl-unwrapped-ltr", "over-budget", "extra-key", "leaf-type"})

    def test_g2(self):
        s = S.g2(7)["summary"]
        self.assertTrue(S.g2_ok(s), s)
        self.assertEqual((s["wordy_caught"], s["good_flagged"], s["dup_caught"], s["good_screens_flagged"]), (30, [], 10, []))
        self.assertEqual(s["dup_methods"], ["containment", "edit-distance", "synonym"])

    def test_g3(self):
        s = S.g3(7)["summary"]
        self.assertTrue(S.g3_ok(s), s)
        self.assertEqual((s["typos_caught"], s["paraphrases_red"], s["other_red"], s["clean_red"]), (20, [], [], 0))

    def test_g4_offline(self):
        d = tmpdir(self)
        s = S.g4(d, SNAP)["summary"]
        self.assertTrue(S.g4_ok(s), s)
        self.assertEqual((s["mode"], s["findings"], s["normal_page_findings"]), ("offline", 8, 0))
        self.assertTrue(os.path.exists(os.path.join(d, "g4_mobile_layout_offline.json")))

    def test_g4_ok_rule(self):
        self.assertFalse(S.g4_ok({"all_ok": True, "normal_page_findings": 1}))
        self.assertFalse(S.g1_ok({"recall_pct": 99.0, "false_positives": 0, "clean_red": 0, "clean_yellow_non_wordy": 0}))
        self.assertFalse(S.g2_ok({"wordy_recall_pct": 100, "good_false_positive_pct": 11, "dup_recall_pct": 100, "good_screens_flagged": [],
                                  "screen_key_missing": 0}))
        self.assertFalse(S.g3_ok({"typo_recall_pct": 100.0, "paraphrases_red": ["x"], "other_red": [], "clean_red": 0}))

    def test_main_g2(self):
        d = tmpdir(self)
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(S.main(["g2", "--out", d]), 0)
        self.assertTrue(out.getvalue().startswith("g2: OK"))
        self.assertTrue(os.path.exists(os.path.join(d, "g2_wordy.json")))


if __name__ == "__main__":
    unittest.main()
