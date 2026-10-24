import subprocess
import sys
import unittest

from qdexsim import judges, scenarios
from qdexsim.scenarios import gen
from qdexsim.tests._util import REPO


class TestScenarioFiles(unittest.TestCase):
    def test_count_and_schema(self):
        all_ = scenarios.load_all()
        self.assertGreaterEqual(len(all_), 30)
        self.assertEqual(len(all_), len(gen.TEMPLATES) * len(gen.DIALECTS))
        names = set()
        for s in all_:
            names.add(s["name"])
            self.assertIn(s["dialect"], gen.DIALECTS)
            self.assertIn(s["template"], gen.TEMPLATES)
            self.assertTrue(set(s["judges"]) <= set(judges.JUDGES), s["name"])
            self.assertEqual(set(s["expect"]), {"naive", "correct"})
            self.assertEqual(s["expect"]["correct"], "green")
            self.assertTrue(all("op" in st for st in s["script"]))
        self.assertEqual(len(names), len(all_))
        seeds = [s["seed"] for s in all_]
        self.assertEqual(len(set(seeds)), len(seeds))                                   # 种子互不相同

    def test_core_templates_expect_naive_red(self):
        for t in ("unknown_timeout", "unknown_drop", "unknown_502", "client_restart"):
            for s in scenarios.load_all(template=t):
                self.assertEqual(s["expect"]["naive"], "red", s["name"])

    def test_loaders(self):
        self.assertEqual(len(scenarios.load_all(dialect="okx")), len(gen.TEMPLATES))
        self.assertEqual(scenarios.load("okx/baseline")["dialect"], "okx")
        with self.assertRaises(KeyError):
            scenarios.load("nope/none")
        self.assertEqual(len(scenarios.paths()), len(scenarios.load_all()))

    def test_generated_matches_disk(self):
        proc = subprocess.run([sys.executable, "qdexsim/scenarios/gen.py", "--check"], cwd=REPO, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("stale 0", proc.stdout)
        self.assertEqual(gen.build("okx", "baseline", 5)["chaos"]["seed"], 5)
        self.assertTrue(gen.render(gen.build("okx", "baseline", 5)).endswith("\n"))


if __name__ == "__main__":
    unittest.main()
