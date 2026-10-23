"""REPORT.md 与 results/*.json 一致（报告里的每个数字都由 build_report 从结果文件生成）；README 第一屏的形状。"""
import os
import re
import unittest

from qdperf.tests._util import PKG, UNDER_COVERAGE, tmpdir

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


class TestTriage(unittest.TestCase):
    """mutants/triage.md 由 mutants/triage.py 从结果文件 + mutants/triage_reasons.py 生成；判定表不许重号、不许有用不上的条目。"""

    def _need_history(self):
        import subprocess
        from qdperf.mutants import triage as TG
        for c in TG.COMMITS:
            if subprocess.run(["git", "-C", TG.REPO, "cat-file", "-e", f"{c}^{{commit}}"], capture_output=True).returncode:
                self.skipTest(f"git 历史里没有 {c}（浅克隆，或不在仓库里）")
        return TG

    def test_triage_matches_results(self):
        import contextlib
        import io
        TG = self._need_history()
        text, _counts = TG.Triage().render()
        with open(TG.OUT, encoding="utf-8") as f:
            self.assertEqual(f.read(), text, "triage.md 与结果文件 / triage_reasons.py 不一致：运行 python3.12 qdperf/mutants/triage.py")
        bad = os.path.join(tmpdir(self), "triage.md")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("stale\n")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(TG.main(["--check"]), 0)
            self.assertEqual(TG.main(["--check", "--out", bad]), 1)
        self.assertIn("triage.md differs", out.getvalue())

    def test_triage_rejects_duplicate_and_unused_verdicts(self):
        from unittest import mock
        TG = self._need_history()
        dup = next(iter(TG.TR.ROUND1))
        with mock.patch.dict(TG.TR.ROUND3, {dup: (TG.EQ, "x")}):
            with self.assertRaisesRegex(TG.TriageError, "同时出现在"):
                TG.tables()
        with mock.patch.dict(TG.TR.RESAMPLE3, {"fake_backend:1:const:0": (TG.EQ, "x")}):
            with self.assertRaisesRegex(TG.TriageError, "没在 triage.md 出现"):
                TG.Triage().render()


class TestSelftestParsing(unittest.TestCase):
    def test_constraints_read_from_verbose_log(self):
        """-v 日志里有 docstring 的测试多占一行；三条约束测试都要认出来，失败要认成失败。"""
        from qdperf import selftest
        log = ("test_stdlib_only (qdperf.tests.test_constraints.TestDependencies.test_stdlib_only)\n"
               "grep ^import|^from 与 AST 两种方式：只许标准库。 ... ok\n"
               "test_static (qdperf.tests.test_constraints.TestNoNetwork.test_static) ... ok\n"
               "test_allowed_path_rule (qdperf.tests.test_constraints.TestRuntimeAudit.test_allowed_path_rule) ... ok\n"
               "test_writes_and_connects (qdperf.tests.test_constraints.TestRuntimeAudit.test_writes_and_connects)\n"
               "装审计钩子跑一遍四个工具： ... FAIL\n")
        row = selftest.step_constraints("default", None, {"unittest_log": log})
        self.assertEqual(row["detail"], {"test_stdlib_only": "ok", "test_static": "ok", "test_writes_and_connects": "FAIL"})
        self.assertFalse(row["ok"])
        row = selftest.step_constraints("default", None, {"unittest_log": log.replace("FAIL", "ok")})
        self.assertTrue(row["ok"])
        row = selftest.step_constraints("default", None, {"unittest_log": ""})
        self.assertEqual(set(row["detail"].values()), {"missing"})

    def test_unittest_step_flags_leftover_tmp_dirs(self):
        """测试跑完 tempfile 下多出 qdperf-* 目录 → unittest 这步算失败并列出目录名。"""
        import shutil
        import subprocess
        import tempfile
        from unittest import mock
        from qdperf import selftest
        done = subprocess.CompletedProcess([], 0, "", "Ran 3 tests in 0.1s\n\nOK\n")
        made = []

        def leaky_run(*a, **kw):
            made.append(tempfile.mkdtemp(prefix="qdperf-leaktest-"))
            return done
        with mock.patch.object(selftest.subprocess, "run", side_effect=leaky_run):
            row = selftest.step_unittest("default", None, {})
        shutil.rmtree(made[0])
        self.assertFalse(row["ok"])
        self.assertIn(os.path.basename(made[0]), row["detail"]["leaked_tmp_dirs"])
        self.assertIn("tmp dirs left", row["summary"])
        with mock.patch.object(selftest.subprocess, "run", return_value=done):
            row = selftest.step_unittest("default", None, {})
        self.assertFalse(any(n.startswith("qdperf-leaktest-") for n in row["detail"]["leaked_tmp_dirs"]))
        self.assertEqual(row["ok"], not row["detail"]["leaked_tmp_dirs"])     # 别的进程同时建目录时也不误判方向


if __name__ == "__main__":
    unittest.main()
