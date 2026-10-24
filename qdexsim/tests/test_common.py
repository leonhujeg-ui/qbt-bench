import json
import os
import unittest

from qdexsim import common
from qdexsim.tests._util import tmpdir


class TestNumbers(unittest.TestCase):
    def test_fnum(self):
        self.assertEqual(common.fnum(60000.0), "60000")
        self.assertEqual(common.fnum(0.0100000001, 8), "0.01")
        self.assertEqual(common.fnum(-0.0), "0")
        self.assertEqual(common.fnum(1.5, 1), "1.5")
        self.assertEqual(common.fnum(0.0), "0")

    def test_round_step(self):
        self.assertEqual(common.round_step(59999.96, 0.1), 60000.0)
        self.assertEqual(common.round_step(0.0149, 0.001), 0.015)
        self.assertEqual(common.round_step(7.3, 0), 7.3)
        self.assertTrue(common.is_multiple(0.3, 0.1))
        self.assertTrue(common.is_multiple(0.015, 0.001))
        self.assertFalse(common.is_multiple(0.0105, 0.001))
        self.assertTrue(common.is_multiple(123.4, 0))

    def test_digest_and_now(self):
        self.assertEqual(common.digest({"a": 1, "b": [2]}), common.digest({"b": [2], "a": 1}))
        self.assertNotEqual(common.digest({"a": 1}), common.digest({"a": 2}))
        self.assertGreater(common.now_ms(), 1_700_000_000_000)


class TestFiles(unittest.TestCase):
    def test_write_read_json_atomic(self):
        d = tmpdir(self)
        p = os.path.join(d, "sub", "x.json")
        common.write_json(p, {"z": 1, "a": [1, 2]})
        self.assertEqual(common.read_json(p), {"z": 1, "a": [1, 2]})
        self.assertFalse(os.path.exists(p + ".tmp"))
        with open(p, encoding="utf-8") as f:
            self.assertTrue(f.read().startswith('{\n "a"'))          # sort_keys + indent

    def test_machine(self):
        m = common.machine()
        for k in ("platform", "python", "cpu_count", "sqlite_version", "git_commit", "timestamp"):
            self.assertIn(k, m)
        self.assertTrue(m["python"].startswith("3."))


class TestAudit(unittest.TestCase):
    def test_is_write(self):
        self.assertTrue(common._is_write("w", None))
        self.assertTrue(common._is_write("r+", None))
        self.assertFalse(common._is_write("r", None))
        self.assertTrue(common._is_write(None, os.O_WRONLY | os.O_CREAT))
        self.assertFalse(common._is_write(None, os.O_RDONLY))
        self.assertFalse(common._is_write(None, None))

    def test_audit_event_dispatch(self):
        got = []
        emit = lambda k, t: got.append((k, t))        # noqa: E731
        common._audit_event("open", ("/tmp/x.txt", "w", None), emit)
        common._audit_event("open", ("/tmp/x.txt", "r", None), emit)
        common._audit_event("os.remove", ("/tmp/y", -1), emit)
        common._audit_event("os.remove", ("rel.txt", 999999), emit)              # 解析不了的目录 fd
        common._audit_event("sqlite3.connect", ("/tmp/a.db",), emit)
        common._audit_event("socket.connect", (None, ("127.0.0.1", 80)), emit)
        common._audit_event("socket.bind", (None, ("127.0.0.1", 0)), emit)
        common._audit_event("other.event", (1, 2), emit)
        kinds = [k for k, _ in got]
        self.assertEqual(kinds, ["write", "os.remove", "os.remove", "sqlite3.connect", "socket.connect", "socket.bind"])
        self.assertTrue(got[2][1].startswith(common.UNRESOLVED_DIR_FD))
        self.assertEqual(got[4][1], ["127.0.0.1", 80])

    def test_install_audit_writes_jsonl(self):
        d = tmpdir(self)
        log = os.path.join(d, "audit.log")
        # 装在子进程里（钩子装上不可卸载）：
        import subprocess, sys
        code = ("from qdexsim import common; import os, sys; common.install_audit(sys.argv[1]); "
                "open(os.path.join(sys.argv[2], 'p.txt'), 'w').write('x')")
        subprocess.run([sys.executable, "-c", code, log, d], cwd=os.path.join(os.path.dirname(__file__), "..", ".."), check=True)
        with open(log, encoding="utf-8") as f:
            events = [json.loads(line) for line in f]
        self.assertTrue(any(e["event"] == "write" and e["target"].endswith("p.txt") for e in events))

    def test_child_init_idempotent_without_env(self):
        saved = {k: os.environ.pop(k, None) for k in ("QDEXSIM_COV_DIR", "QDEXSIM_AUDIT_LOG")}
        try:
            common.child_init()
            common.child_init()
            self.assertIsNone(common._CHILD["hits"])
        finally:
            os.environ.update({k: v for k, v in saved.items() if v is not None})   # 别把父进程的覆盖率目录弄丢


if __name__ == "__main__":
    unittest.main()
