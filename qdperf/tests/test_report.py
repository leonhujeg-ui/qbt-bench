"""REPORT.md 与 results/*.json 一致（报告里的每个数字都由 build_report 从结果文件生成）；README 第一屏的形状。"""
import os
import re
import unittest

from qdperf.tests._util import PKG, UNDER_COVERAGE

RESULTS = os.path.join(PKG, "results")
NEEDED = ("g1_synth_verify.json", "g1_doctor.json", "g2_profile_gate.json", "g2_noise_probe.json", "g3_tick_jitter.json",
          "g4_http_load.json",
          "coverage.json", "mutation.json", "mutation_round3.json", "selftest_summary.json")


class TestReport(unittest.TestCase):
    @unittest.skipIf(UNDER_COVERAGE, "覆盖率运行期间 results/coverage.json 正被改写、REPORT 还没重建；普通运行里检查")
    def test_report_matches_results(self):
        missing = [n for n in NEEDED if not os.path.exists(os.path.join(RESULTS, n))]
        if missing:
            self.skipTest(f"results not generated yet: {missing}")
        from qdperf.tools import build_report
        with open(build_report.TEMPLATE, encoding="utf-8") as f:
            text = build_report.render(f.read())
        with open(build_report.OUT, encoding="utf-8") as f:
            self.assertEqual(f.read(), text, "REPORT.md 与 results/*.json 不一致：运行 python3.12 qdperf/tools/build_report.py")
        self.assertNotIn("{{", text)
        self.assertNotIn("❌", text)

    def test_checks_follow_results(self):
        """达成列由结果算出：把一个结果改坏，对应判定就变 ❌（防止“手写 ✅”）。"""
        from unittest import mock
        from qdperf.tools import build_report as B
        fake = {"g4_http_load.json": {"summary": {"p99_measured_ms": 140.0, "knee_measured": 12,
                                                  "ab_slow_verdict": "OK", "ab_same_verdict": "OK"}},
                "mutation.json": {"summary": {"kill_rate": 84.9, "modules": {"m": {"total": 50}}}, "mutants": []},
                "coverage.json": {"library": {"total_percent": 89.99}, "tests_failed": 0}}
        with mock.patch.object(B, "_load", side_effect=lambda name, cache: fake[name]):
            for name in ("g4_p99", "g4_knee", "g4_ab", "g5_mutation", "g5_coverage"):
                self.assertFalse(B.CHECKS[name]({}), name)
            fake["g4_http_load.json"]["summary"].update(p99_measured_ms=131.9, knee_measured=6, ab_slow_verdict="REGRESSION")
            fake["mutation.json"]["summary"]["kill_rate"] = 85.0
            fake["coverage.json"]["library"]["total_percent"] = 90.0
            for name in ("g4_p99", "g4_knee", "g4_ab", "g5_mutation", "g5_coverage"):
                self.assertTrue(B.CHECKS[name]({}), name)
            self.assertEqual(B.render("{{?g4_p99}}"), "✅")

    def test_readme_first_screen(self):
        with open(os.path.join(PKG, "README.md"), encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertTrue(lines[0].startswith("**接库**") and "sqlite_doctor" in lines[0])
        self.assertTrue(lines[1].startswith("**接代码**") and "profile_gate" in lines[1])
        self.assertTrue(lines[2].startswith("**接服务**") and "http_load ab" in lines[2])
        first_screen = "\n".join(lines[:40])
        self.assertIn("python3.12 -m qdperf.selftest\n", first_screen)
        self.assertNotIn("__SELFTEST_OUTPUT__", first_screen)
        self.assertRegex(first_screen, r"TOTAL\s+[\d.]+s\s+OK\s+8/8 steps passed")
        rows = [ln for ln in lines[:40] if re.match(r"^(unittest|constraints|coverage|g1_sqlite|g2_gate|g3_tick|g4_http|"
                                                     r"perf_qbt)\s", ln)]
        self.assertEqual(len(rows), 8)


if __name__ == "__main__":
    unittest.main()
