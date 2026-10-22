"""common 判据：分位数口径、相对变化 / 噪声、文本表 / 曲线、文件工具、子进程覆盖率与审计钩子、spawn 工具。"""
import json
import math
import os
import subprocess
import sys
import tempfile
import time
import unittest

from qdperf import common as C
from qdperf.tests._util import REPO, tmpdir


def _sleeper(seconds):
    time.sleep(seconds)


def _fail():
    raise SystemExit(3)


class TestStats(unittest.TestCase):
    def test_percentile_type7(self):
        xs = [5, 1, 4, 2, 3]
        self.assertEqual(C.percentile(xs, 0.5), 3)
        self.assertEqual(C.percentile(xs, 0.0), 1)
        self.assertEqual(C.percentile(xs, 1.0), 5)
        self.assertAlmostEqual(C.percentile(xs, 0.25), 2.0)
        self.assertAlmostEqual(C.percentile([1, 2], 0.99), 1.99)
        self.assertAlmostEqual(C.percentile(list(range(101)), 0.99), 99.0)
        self.assertAlmostEqual(C.percentile([10, 20, 30, 40], 0.9), 37.0)
        self.assertIsNone(C.percentile([], 0.5))
        self.assertEqual(C.percentile([7], 0.99), 7)
        self.assertEqual(xs, [5, 1, 4, 2, 3])                       # 不改入参

    def test_summarize(self):
        s = C.summarize([0.001, 0.002, 0.003, 0.004], 1000.0)
        self.assertEqual(s["n"], 4)
        self.assertAlmostEqual(s["min"], 1.0)
        self.assertAlmostEqual(s["max"], 4.0)
        self.assertAlmostEqual(s["mean"], 2.5)
        self.assertAlmostEqual(s["p50"], 2.5)
        self.assertAlmostEqual(s["p95"], 3.85)
        self.assertAlmostEqual(s["p99"], 3.97)
        self.assertEqual(C.summarize([]), {"n": 0, "min": None, "p50": None, "p95": None, "p99": None, "max": None,
                                           "mean": None})

    def test_rel(self):
        self.assertAlmostEqual(C.rel_change(2.0, 2.5), 0.25)
        self.assertAlmostEqual(C.rel_change(2.0, 1.0), -0.5)
        self.assertEqual(C.rel_change(0, 0), 0.0)
        self.assertEqual(C.rel_change(0, 1), math.inf)
        self.assertAlmostEqual(C.rel_spread([1.0, 1.1, 1.05]), 0.1)
        self.assertEqual(C.rel_spread([1.0]), 0.0)
        self.assertEqual(C.rel_spread([]), 0.0)
        self.assertEqual(C.rel_spread([0.0, 1.0]), 0.0)
        self.assertEqual(C.fmt_pct(0.1234), "+12.3%")
        self.assertEqual(C.fmt_pct(-0.05), "-5.0%")
        self.assertEqual(C.fmt_pct(math.inf), "+inf%")


class TestText(unittest.TestCase):
    def test_table(self):
        t = C.text_table(["名", "n"], [("ab", 1), ("中文字", 22)])
        lines = t.splitlines()
        self.assertEqual(lines[0], "名      n")
        self.assertEqual(lines[1], "------  --")
        self.assertEqual(lines[2], "ab      1")
        self.assertEqual(lines[3], "中文字  22")
        self.assertEqual(C._width("a中"), 3)

    def test_sparkline(self):
        self.assertEqual(C.sparkline([]), "")
        self.assertEqual(C.sparkline([0, 0, 0]), "   ")
        self.assertEqual(C.sparkline([0, 4, 8]), " ▄█")
        self.assertEqual(len(C.sparkline(list(range(1000)), 50)), 50)
        self.assertEqual(C.sparkline([1, 8, 1, 1], 2), "█▁")         # 每格取该段最大值：[1, 8] → 8，[1, 1] → 1


class TestFiles(unittest.TestCase):
    def test_json_sha_scratch_loopback(self):
        d = tmpdir(self)
        p = os.path.join(d, "sub", "x.json")
        C.write_json(p, {"b": 1, "a": "中"})
        with open(p, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(text, '{\n "a": "中",\n "b": 1\n}\n')
        self.assertEqual(C.read_json(p), {"a": "中", "b": 1})
        self.assertEqual(C.sha256_file(p, chunk=3), C.sha256_file(p))
        import hashlib
        self.assertEqual(C.sha256_file(p), hashlib.sha256(text.encode()).hexdigest())
        s = C.scratch_dir()
        self.addCleanup(os.rmdir, s)
        self.assertTrue(os.path.realpath(s).startswith(os.path.realpath(tempfile.gettempdir())))
        self.assertTrue(os.path.basename(s).startswith("qdperf-"))
        for url, ok in (("http://127.0.0.1:1/x", True), ("http://localhost", True), ("http://127.8.0.1", True),
                        ("http://[::1]:80/", True), ("http://example.com", False), ("http://10.0.0.1", False),
                        ("http://", False)):
            self.assertEqual(C.is_loopback_url(url), ok, url)

    def test_machine(self):
        m = C.machine()
        for k in ("platform", "python", "cpu_model", "cpu_count", "sqlite_version", "git_commit", "timestamp"):
            self.assertIn(k, m)
        self.assertEqual(m["python"].split(".")[:2], [str(sys.version_info[0]), str(sys.version_info[1])])
        self.assertTrue(C.cpu_model())
        if os.path.exists(os.path.join(REPO, ".git")):
            self.assertRegex(C.git_commit(), r"^[0-9a-f]{4,}$")
        else:                                                          # 突变 / 拷贝出来的目录不是 git 检出
            self.assertEqual(C.git_commit(), "")


class TestAudit(unittest.TestCase):
    def test_is_write_and_events(self):
        self.assertTrue(C._is_write("w", 0))
        self.assertTrue(C._is_write("a", 0))
        self.assertTrue(C._is_write("r+", 0))
        self.assertTrue(C._is_write("xb", 0))
        self.assertFalse(C._is_write("r", 0))
        self.assertFalse(C._is_write("rb", None))
        self.assertTrue(C._is_write(None, os.O_WRONLY))
        self.assertTrue(C._is_write(None, os.O_CREAT))
        self.assertFalse(C._is_write(None, os.O_RDONLY))
        got = []

        def emit(kind, target):
            got.append((kind, target))

        C._audit_event("open", ("/x/y", "w", 0), emit)
        C._audit_event("open", ("/x/z", "r", 0), emit)
        C._audit_event("open", (3, "w", 0), emit)
        C._audit_event("os.remove", ("/x/r", -1), emit)
        C._audit_event("sqlite3.connect", ("/x/db",), emit)
        C._audit_event("sqlite3.connect", (), emit)
        C._audit_event("socket.connect", (None, ("127.0.0.1", 80)), emit)
        C._audit_event("socket.connect", (None, "/run/sock"), emit)
        C._audit_event("os.listdir", ("/x",), emit)
        self.assertEqual(got, [("write", "/x/y"), ("os.remove", "/x/r"), ("sqlite3.connect", "/x/db"),
                               ("socket.connect", ["127.0.0.1", 80]), ("socket.connect", "/run/sock")])

    def test_child_hooks_in_subprocess(self):
        """子进程里装审计钩子 + 覆盖率：写文件 / sqlite / socket 事件进日志；覆盖行写进 QDPERF_COV_DIR。"""
        d = tmpdir(self)
        log, cov = os.path.join(d, "audit.log"), os.path.join(d, "cov")
        os.makedirs(cov)
        code = ("import os, sqlite3, socket, sys; from qdperf import common; common.child_init(); common.child_init();"
                f"open({os.path.join(d, 'w.txt')!r}, 'w').write('x'); sqlite3.connect({os.path.join(d, 'a.db')!r}).close();"
                "s = socket.socket(); s.settimeout(0.2)\n"
                "try:\n s.connect(('127.0.0.1', 9))\nexcept OSError:\n pass\n"
                "common.percentile([1, 2], 0.5)")
        env = dict(os.environ, QDPERF_AUDIT_LOG=log, QDPERF_COV_DIR=cov, PYTHONDONTWRITEBYTECODE="1")
        subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env, check=True, timeout=60)
        with open(log) as f:
            events = [json.loads(line) for line in f]
        kinds = {(e["event"], str(e["target"])) for e in events}
        self.assertIn(("write", os.path.join(d, "w.txt")), kinds)
        self.assertIn(("sqlite3.connect", os.path.join(d, "a.db")), kinds)
        self.assertIn(("socket.connect", str(["127.0.0.1", 9])), kinds)
        dumps = os.listdir(cov)
        self.assertEqual(len(dumps), 1)
        with open(os.path.join(cov, dumps[0])) as f:
            hits = json.load(f)
        common_py = os.path.join(REPO, "qdperf", "common.py")
        self.assertIn(common_py, hits)
        self.assertTrue(hits[common_py])


class TestSpawn(unittest.TestCase):
    def test_join_all_and_sleep_until(self):
        ok = C.start_process(_sleeper, 0.05, name="ok")
        slow = C.start_process(_sleeper, 30, name="slow")
        bad = C.start_process(_fail, name="bad")
        t0 = time.monotonic()
        self.assertEqual(C.join_all([ok, slow, bad], timeout=3.0), ["slow", "bad"])
        self.assertLess(time.monotonic() - t0, 20)
        self.assertFalse(slow.is_alive())
        self.assertEqual(C.spawn_context().get_start_method(), "spawn")
        nd = C.start_process(_sleeper, 0.01, name="nd", daemon=False)
        self.assertFalse(nd.daemon)
        self.assertEqual(C.join_all([nd], timeout=30), [])
        t = time.time() + 0.2
        C.sleep_until(t)
        self.assertGreaterEqual(time.time(), t)
        t0 = time.time()
        C.sleep_until(t0 - 5)
        self.assertLess(time.time() - t0, 0.05)


if __name__ == "__main__":
    unittest.main()
