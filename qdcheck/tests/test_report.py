"""REPORT.md 必须由模板 + 入库的结果文件生成（数字不得手改、不得与结果文件脱节）；结果文件之间的关键数字互相一致。"""
import json
import os
import unittest

from qdcheck.tests._util import REPO, UNDER_COVERAGE

RESULTS = os.path.join(REPO, "qdcheck", "results")


def _load(name):
    with open(os.path.join(RESULTS, name), encoding="utf-8") as f:
        return json.load(f)


@unittest.skipIf(UNDER_COVERAGE, "报告引用 coverage.json 本身：覆盖率运行时它还没生成，普通运行里执行")
class TestReport(unittest.TestCase):
    def test_report_is_generated_from_results(self):
        from qdcheck.tools import build_report as B
        with open(B.TEMPLATE, encoding="utf-8") as f:
            rendered = B.render(f.read())
        with open(B.OUT, encoding="utf-8") as f:
            self.assertEqual(f.read(), rendered, "REPORT.md is stale: python3.12 qdcheck/tools/build_report.py")
        self.assertNotIn("{{", rendered)

    def test_result_files_meet_the_targets(self):
        g1, g2, g3, g4 = (_load(f) for f in ("g1_selfcheck.json", "g2_selfcheck.json", "g3_streams.json",
                                             "g4_selfcheck.json"))
        self.assertTrue(all(v["failures"] == 0 for v in g1["summary"].values()))
        self.assertTrue(g1["vectors"]["ok"])
        self.assertTrue(g2["summary"]["coverage_ge_500"] and g2["summary"]["good_validate_le_20s"])
        self.assertTrue(all(v["caught"] and v["ratio_le_30pct"] for v in g2["summary"]["per_validate"].values()))
        self.assertGreaterEqual((g3["n_streams"], g3["n_bars"]), (200, 3000))
        self.assertLessEqual(g3["seconds"], 180.0)
        self.assertEqual((g3["identity_violations"], g3["alt_issues"], g3["row_mismatches"]), (0, 0, 0))
        self.assertEqual(g4["summary"]["as_expected"], g4["summary"]["cases"])
        cov, mut = _load("coverage.json"), _load("mutation.json")
        self.assertGreaterEqual(cov["library"]["total_percent"], 90.0)
        self.assertEqual(cov["tests_failed"], 0)
        self.assertGreaterEqual(mut["summary"]["kill_rate"], 85.0)
        self.assertTrue(all(m["total"] >= 40 for m in mut["summary"]["modules"].values()))
        perf = _load("perf.json")
        by = {r["block"]: r for r in perf["rows"]}
        self.assertLessEqual(by["fuzz_20k"]["wall_seconds"], 20.0)
        self.assertLessEqual(by["streams_200x3000"]["wall_seconds"], 180.0)
        self.assertTrue(all(r["error"] is None for r in perf["rows"]))
        replay = _load("mutation_replay.json")
        self.assertGreaterEqual(replay["replayed"], 100)
        triage = _load("mutation_triage.json")
        survivors = {r["id"] for r in mut["mutants"] if r["status"] == "survived"}
        self.assertTrue(survivors <= set(triage), f"surviving mutants without a verdict: {sorted(survivors - set(triage))}")
        replay_survivors = {r["id"] for r in replay["results"] if r["status"] == "survived"}
        self.assertTrue(replay_survivors <= set(triage), f"replayed survivors without a verdict: {sorted(replay_survivors - set(triage))}")
        self.assertTrue(all(t["verdict"] and t["reason"] for t in triage.values()))


if __name__ == "__main__":
    unittest.main()
