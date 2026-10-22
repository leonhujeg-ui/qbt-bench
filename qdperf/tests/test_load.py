"""sqlite_load 判据：时间表 / 抽样 / 汇总 / 饿死结论 / 根因的纯逻辑，加上真进程的小实验（写争用、延迟事务 BUSY_SNAPSHOT、
WAL 长读不重置），以及 sqlite_doctor 跑 contention + wal 两节的端到端。"""
import os
import sqlite3
import struct
import unittest

from qdperf import sqlite_doctor as D
from qdperf import sqlite_load as L
from qdperf.tests._util import TIMING, WORKLOAD, captured, scale, tiny_db, tmpdir

INS = ["INSERT INTO events(user_id, bot_id, kind, level, notified, ts, payload) VALUES (?,?,?,?,?,?,?)",
       [7, 42, "login", "info", 1, 1720000000, "{}"]]
UPD = ["UPDATE bots SET updated_at = ? WHERE id = ?", [1720000000, 42]]


def writers(n, rate):
    return [{"name": f"w{k}", "rate": rate, "txns": [{"name": "t", "weight": 1, "statements": [INS, UPD],
                                                      "read_table": "events"}]} for k in range(n)]


class TestPureLogic(unittest.TestCase):
    def test_schedule(self):
        s = L.schedule(10, 1.0, seed=3)
        self.assertEqual(len(s), 10)
        self.assertTrue(0 <= s[0] < 0.1)
        self.assertTrue(all(abs((b - a) - 0.1) < 1e-12 for a, b in zip(s, s[1:])))
        self.assertTrue(s[-1] < 1.0)
        self.assertEqual(s, L.schedule(10, 1.0, seed=3))
        self.assertNotEqual(s, L.schedule(10, 1.0, seed=4))
        self.assertEqual(L.schedule(0, 5, seed=1), [])
        self.assertEqual(L.schedule(-1, 5, seed=1), [])
        p = L.schedule(100, 20.0, seed=1, poisson=True)
        self.assertTrue(1800 < len(p) < 2200)
        self.assertEqual(p, sorted(p))
        self.assertNotEqual(len({round(b - a, 9) for a, b in zip(p, p[1:])}), 1)

    def test_pick_txn(self):
        t = [{"name": "a", "weight": 1}, {"name": "b", "weight": 3}]
        self.assertEqual([L.pick_txn(t, r)["name"] for r in (0.0, 0.249, 0.25, 0.99)], ["a", "a", "b", "b"])
        self.assertEqual(L.pick_txn(t, 1.0)["name"], "b")

    def test_summarize_writer(self):
        recs = [[0.0, 0.001, 0.002, 0.003, None, "t"], [0.1, 0.0, 0.004, 0.001, None, "t"],
                [0.2, 0.0, 0.5, 0.0, "SQLITE_BUSY", "t"], [0.3, 0.0, 0.0, 0.0, "SQLITE_BUSY_SNAPSHOT", "t"],
                [0.4, 0.0, 0.0, 0.0, "SQLITE_IOERR", "t"]]
        s = L.summarize_writer({"name": "x", "records": recs, "duration": 2.0, "planned": 6})
        self.assertEqual((s["attempts"], s["committed"], s["planned"]), (5, 2, 6))
        self.assertEqual(s["errors"], {"SQLITE_BUSY": 1, "SQLITE_BUSY_SNAPSHOT": 1, "SQLITE_IOERR": 1})
        self.assertEqual(s["busy"], 2)
        self.assertAlmostEqual(s["achieved_rate"], 1.0)
        self.assertAlmostEqual(s["wait_ms"]["max"], 4.0)
        self.assertAlmostEqual(s["hold_ms"]["p50"], 2.0)
        self.assertAlmostEqual(s["lock_share"], 0.002)
        self.assertIsNone(s["crash"])
        c = L.summarize_writer({"name": "y", "error": "boom"})
        self.assertEqual((c["attempts"], c["crash"], c["wait_ms"]["p99"]), (0, "boom", None))

    def summary(self, name, p99, busy=0, share=0.01, hold=1.0, committed=10):
        return {"name": name, "committed": committed, "busy": busy, "lock_share": share, "errors": {},
                "wait_ms": {"p99": p99}, "hold_ms": {"p99": hold}}

    def test_starvation_verdict(self):
        v = L.starvation_verdict([self.summary("a", 1.0, share=0.5), self.summary("b", 3.0, share=0.1)], 200)
        self.assertEqual((v["starver"], v["victim"], v["starved"]), ("a", "b", False))
        self.assertIn("无饿死", v["text"])
        v = L.starvation_verdict([self.summary("a", 1.0, share=0.5), self.summary("b", 300.0, share=0.1)], 200)
        self.assertTrue(v["starved"])
        self.assertIn("b 被饿", v["text"])
        v = L.starvation_verdict([self.summary("a", 1.0, share=0.5), self.summary("b", 2.0, busy=1)], 200)
        self.assertEqual((v["victim"], v["starved"]), ("b", True))
        v = L.starvation_verdict([self.summary("a", None, committed=0)], 200)
        self.assertEqual((v["starver"], v["starved"]), (None, True))
        self.assertFalse(L.starvation_verdict([], 200)["starved"])
        v = L.starvation_verdict([self.summary("a", 200.0)], 200)
        self.assertFalse(v["starved"])

    def test_root_cause(self):
        cfg = {"busy_timeout": 5.0, "budget_ms": 200.0}
        w = self.summary("a", 1.0)
        self.assertIn("退避粒度", L.root_cause([w], cfg)[0])
        snap = dict(w, errors={"SQLITE_BUSY_SNAPSHOT": 3})
        self.assertIn("BEGIN IMMEDIATE", L.root_cause([snap], cfg)[0])
        busy = dict(w, errors={"SQLITE_BUSY": 1})
        self.assertIn("busy_timeout=5.0", L.root_cause([busy], cfg)[0])
        self.assertIn("持锁过长", L.root_cause([self.summary("a", 1.0, hold=51.0)], cfg)[0])
        self.assertEqual(len(L.root_cause([self.summary("a", 1.0, hold=50.0)], cfg)), 1)
        self.assertIn("利用率", L.root_cause([self.summary("a", 1.0, share=0.3), self.summary("b", 1, share=0.21)],
                                              cfg)[0])
        self.assertIn("退避", L.root_cause([self.summary("a", 1.0, share=0.5)], cfg)[0])

    def test_wal_index_and_resets(self):
        d = tmpdir(self)
        shm = os.path.join(d, "x.db-shm")
        head = bytearray(136)
        struct.pack_into("=I", head, 16, 1234)
        struct.pack_into("=I", head, 96, 1000)
        with open(shm, "wb") as f:
            f.write(head)
        self.assertEqual(L.read_wal_index(shm), (1234, 1000))
        with open(shm, "wb") as f:
            f.write(b"\x00" * 99)
        self.assertEqual(L.read_wal_index(shm), (0, 0))
        self.assertEqual(L.read_wal_index(os.path.join(d, "missing")), (0, 0))
        self.assertEqual(L.count_resets([0, 10, 20, 9, 30, 15, 16, 2]), 2)
        self.assertEqual(L.count_resets([0, 5, 10, 20]), 0)
        self.assertEqual(L.count_resets([]), 0)

    def test_real_shm_matches_wal_frames(self):
        d = tmpdir(self)
        p = os.path.join(d, "w.db")
        c = sqlite3.connect(p, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA wal_autocheckpoint=0")
        c.execute("CREATE TABLE t(x)")
        for i in range(30):
            c.execute("INSERT INTO t VALUES (?)", ("y" * 500,))
        mx, back = L.read_wal_index(p + "-shm")
        page = c.execute("PRAGMA page_size").fetchone()[0]
        self.assertEqual(os.path.getsize(p + "-wal"), 32 + mx * (24 + page))
        self.assertEqual(back, 0)
        busy, log, ckpt = c.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        self.assertEqual((busy, log, ckpt), (0, mx, mx))
        self.assertEqual(L.read_wal_index(p + "-shm"), (mx, mx))
        c.close()


class TestRealProcesses(unittest.TestCase):
    def test_contention_immediate_ok(self):
        db = tiny_db(self)
        r = L.contention(db, writers(3, 40.0), duration=scale(1.5, 1.0), busy_timeout=5.0, seed=1)
        self.assertTrue(r["ok"], r["overall"])
        self.assertEqual(r["overall"]["busy"], 0)
        self.assertEqual(r["overall"]["crashed"], [])
        self.assertEqual([w["name"] for w in r["writers"]], ["w0", "w1", "w2"])
        for w in r["writers"]:
            self.assertEqual(w["committed"], w["planned"])
            self.assertGreater(w["committed"], 0)
        self.assertEqual(r["overall"]["committed"], sum(w["committed"] for w in r["writers"]))
        self.assertNotIn("root_cause", r)
        self.assertFalse(r["starvation"]["starved"])
        conn = sqlite3.connect(db)
        n = conn.execute("SELECT count(*) FROM events WHERE ts = 1720000000").fetchone()[0]
        conn.close()
        self.assertEqual(n, r["overall"]["committed"])

    def test_deferred_read_then_write_snapshot(self):
        db = tiny_db(self)
        r = L.contention(db, writers(3, 60.0), duration=scale(2.0, 1.5), busy_timeout=5.0, begin="deferred",
                         think_ms=5.0, seed=2)
        errs = {}
        for w in r["writers"]:
            for k, v in w["errors"].items():
                errs[k] = errs.get(k, 0) + v
        self.assertGreater(errs.get("SQLITE_BUSY_SNAPSHOT", 0), 0, r["overall"])
        self.assertFalse(r["ok"])
        self.assertTrue(any("BUSY_SNAPSHOT" in c for c in r["root_cause"]))
        self.assertEqual(r["config"]["think_ms"], 5.0)

    def test_failed_statements_and_crashed_writer(self):
        d = tmpdir(self)
        p = os.path.join(d, "empty.db")
        sqlite3.connect(p).close()
        r = L.contention(p, writers(1, 20.0), duration=0.5, seed=3)          # 表不存在：每笔事务失败、逐笔记录
        self.assertFalse(r["ok"])
        self.assertEqual(r["overall"]["crashed"], [])
        self.assertEqual(r["writers"][0]["committed"], 0)
        self.assertEqual(r["writers"][0]["errors"], {"SQLITE_ERROR": r["writers"][0]["attempts"]})
        r = L.contention(os.path.join(d, "no", "such", "dir.db"), writers(1, 20.0), duration=0.5, seed=3)
        self.assertEqual(r["overall"]["crashed"], ["w0"])                    # 连不上库：写者进程把异常带回来
        self.assertIn("OperationalError", r["writers"][0]["crash"])
        self.assertFalse(r["ok"])

    def test_wal_growth_shapes(self):
        db = tiny_db(self)
        free = L.wal_growth(db, autocheckpoint=100, long_reader=False, duration=scale(2.5, 2.0), rate=200,
                            rows_per_txn=20, hold=1.0)
        held = L.wal_growth(db, autocheckpoint=100, long_reader=True, duration=scale(2.5, 2.0), rate=200,
                            rows_per_txn=20, hold=1.0)
        # 无长读：检查点完成后 WAL 从头重写。每次提交至少 1 帧，WAL 若从不重置，有效帧会 ≥ 提交数；实际被截在 ~100，
        # 所以必然重置过（这个推断不依赖采样恰好落在重置前后；采样数到的 resets 在机器满载、采样变稀时会漏数）
        self.assertGreater(free["commit_ms"]["n"], 2 * free["max_live_frames"])
        if TIMING:
            self.assertGreaterEqual(free["resets"], 1)
        self.assertLessEqual(free["max_live_frames"], 100 + 60)
        self.assertEqual(held["resets"], 0)
        self.assertGreater(held["max_live_frames"], 3 * free["max_live_frames"])
        self.assertGreater(held["read_txns"], 1)
        self.assertEqual(held["crashed"], [])
        self.assertEqual(held["curve_fields"], ["t_s", "wal_file_bytes", "wal_live_frames", "backfilled_frames"])
        live = [c[2] for c in held["curve"]]
        self.assertEqual(held["max_live_frames"], max(live))
        self.assertEqual(held["final_live_frames"], live[-1])
        self.assertGreater(free["commit_ms"]["n"], 300)
        conn = sqlite3.connect(db)
        self.assertGreater(conn.execute(f"SELECT count(*) FROM {L.PROBE}").fetchone()[0], 0)
        conn.close()


class TestDoctorLoadSections(unittest.TestCase):
    def test_contention_and_wal_sections(self):
        db = tiny_db(self)
        rep = D.diagnose(db, WORKLOAD, sections=("plans", "contention", "wal"), duration=1.0, wal_duration=1.5,
                         wal_checkpoints=(100,), deferred_demo=True, rate=40.0)
        self.assertTrue(rep["db"]["untouched"])
        names = [s["name"] for s in rep["contention"]["scenarios"]]
        self.assertEqual(names, ["workload", "3x40/s", "3x40/s deferred+think5ms"])
        self.assertEqual([s["gate"] for s in rep["contention"]["scenarios"]], [True, True, False])
        crit = rep["contention"]["scenarios"][1]
        self.assertEqual([w["name"] for w in crit["writers"]], ["http", "engine", "notifier"])
        self.assertTrue(crit["ok"])
        self.assertLessEqual(crit["overall"]["wait_ms"]["p99"], 200.0)
        self.assertEqual(rep["contention"]["busy_timeout"], 5.0)
        self.assertEqual(len(rep["wal"]["scenarios"]), 2)
        self.assertNotIn("indexes", rep)
        text = D.format_text(rep)
        for word in ("写争用", "WAL 检查点", "结论：", "谁" if False else "饿"):
            self.assertIn(word, text)
        with captured() as (o, _e):
            rc = D.main([db, "--workload", WORKLOAD, "--sections", "contention", "--duration", "0.8",
                         "--busy-timeout", "2", "--writers", "2", "--rate", "30"])
        self.assertIn(rc, (0, 1))
        self.assertIn("contention_p99_ms=", o.getvalue().splitlines()[0])
        self.assertIn("busy_timeout=2 s", o.getvalue())


if __name__ == "__main__":
    unittest.main()
