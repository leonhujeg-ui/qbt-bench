"""tick_jitter 判据：纯逻辑（分块、合并、拐点、汇总、文本）+ 真进程小实验（Q=0 不抖；WAL 关 + 面板长查询饿死引擎；
分块读 / 只读副本缓解；subprocess 算段；CLI 退出码与首行）。"""
import os
import re
import shutil
import sqlite3
import unittest

from qdperf import tick_jitter as J
from qdperf.tests._util import TIMING, captured, scale, tmpdir

RUN_LINE = re.compile(r"^TICK_JITTER (OK|STARVED) T=[\d.]+ ticks=\d+ p50=[\d.]+T p99=[\d.]+T max=[\d.]+T over2T=\d+ "
                      r"read_p99_ms=[\d.]+ compute_p99_ms=[\d.]+ write_p99_ms=[\d.]+$")


def fake_result(wal, busy, compute, q, p99, mitigation="none", bg=0):
    cfg = dict(J.DEFAULTS, wal=wal, busy=busy, compute=compute, q=q, mitigation=mitigation, bg_threads=bg)
    return {"config": cfg, "interval_T": {"p50": 1.0, "p99": p99, "max": p99}, "starved": p99 > 2.0, "over_2T": 0,
            "ticks": 10, "segments_ms": {k: {"p99": 1.0} for k in ("read", "compute", "write")}}


class TestPure(unittest.TestCase):
    def test_burn_and_calibrate(self):
        self.assertEqual(J.burn(0), 0)
        self.assertEqual(J.burn(16), sum(i & 7 for i in range(16)))
        self.assertGreater(J.calibrate(0.02), 1000)

    def test_chunks_and_merge(self):
        self.assertEqual(J.chunk_ranges(0, 10, 4), [(0, 4), (4, 8), (8, 10)])
        self.assertEqual(J.chunk_ranges(5, 5, 4), [])
        self.assertEqual(J.chunk_ranges(0, 3, 0), [(0, 1), (1, 2), (2, 3)])
        self.assertEqual(J.chunk_ranges(0, 7, 7.9), [(0, 7)])
        acc = J.merge_rows({}, [(1, 2, 10.0), (2, 1, None)])
        acc = J.merge_rows(acc, [(1, 3, 5.5)])
        self.assertEqual(acc, {1: (5, 15.5), 2: (1, 0.0)})

    def test_chunked_equals_whole_query(self):
        d = tmpdir(self)
        p = os.path.join(d, "t.db")
        from qdperf import synth_db
        synth_db.generate(p, seed=3, scale=0.001)
        conn = sqlite3.connect(p)
        hi = conn.execute("SELECT max(id) FROM trades").fetchone()[0]
        whole = {r[0]: (r[1], r[2]) for r in conn.execute(J.PANEL_QUERY, (hi - 3000, hi))}
        acc = {}
        for a, b in J.chunk_ranges(hi - 3000, hi, 700):
            J.merge_rows(acc, conn.execute(J.PANEL_QUERY, (a, b)).fetchall())
        self.assertEqual(set(acc), set(whole))
        for k, (c, s) in whole.items():
            self.assertEqual(acc[k][0], c)
            self.assertAlmostEqual(acc[k][1], s, places=6)
        conn.close()

    def test_summarize(self):
        cfg = dict(J.DEFAULTS, T=0.1, seconds=1.0)
        ticks = [[0.0, 0.001, 0.03, 0.002, None, None], [0.1, 0.001, 0.03, 0.002, None, None],
                 [0.35, 0.001, 0.03, 0.2, None, "SQLITE_BUSY"], [0.45, 0.001, 0.03, 0.002, "SQLITE_BUSY", None]]
        r = J.summarize(cfg, {"ticks": ticks, "compute_rate": 5.0}, {"durations": [0.1, 0.3], "errors": {"X": 1},
                                                                     "dropped": 2, "replica_refreshes": 3},
                        {"waits": [0.001], "errors": {}}, ["panel"])
        self.assertEqual(r["ticks"], 4)
        self.assertEqual((r["expected_ticks"], r["missed_ticks"]), (10, 6))
        self.assertEqual(r["over_2T"], 1)
        self.assertEqual(r["interval_ms"]["n"], 4)                        # 3 个间隔 + 最后一个 tick 的 max(T, 耗时)
        self.assertAlmostEqual(r["interval_ms"]["max"], 250.0)
        self.assertAlmostEqual(r["interval_T"]["max"], 2.5)
        self.assertAlmostEqual(r["interval_T"]["p50"], 1.0)
        stuck = [[0.0, 0.001, 0.03, 0.002, None, None], [0.1, 0.001, 0.03, 4.0, None, None]]
        self.assertEqual(J.tick_intervals(stuck, 0.1), [0.1, 4.031])          # 卡在最后一个 tick 里也算
        self.assertEqual(J.tick_intervals(stuck[:1], 0.1), [0.1])
        self.assertEqual(J.tick_intervals([], 0.1), [])
        late = [[0.0, 0.001, 0.01, 0.001, None, None, 0.0], [0.13, 0.001, 0.01, 0.001, None, None, 0.1],
                [0.2, 0.001, 0.01, 0.15, None, None, 0.2], [0.36, 0.001, 0.01, 0.001, None, None, 0.3]]
        self.assertEqual([round(x, 3) for x in J.wake_lateness(late)], [0.03, 0.0])    # 第 4 个 tick 是被上一个拖晚的，不算
        self.assertEqual(J.wake_lateness(stuck), [])                                    # 旧格式（没有计划时刻）
        self.assertAlmostEqual(J.summarize(cfg, {"ticks": late}, None, None)["wake_late_ms"]["max"], 30.0)
        stuck_r = J.summarize(dict(cfg, seconds=4.0), {"ticks": stuck}, None, None)
        self.assertTrue(stuck_r["starved"])
        self.assertEqual(stuck_r["missed_ticks"], 38)
        self.assertTrue(r["starved"])
        self.assertEqual(r["tick_errors"], {"read": {"SQLITE_BUSY": 1}, "write": {"SQLITE_BUSY": 1}})
        self.assertAlmostEqual(r["segments_ms"]["write"]["max"], 200.0)
        self.assertEqual((r["segments_ms"]["read"]["max"], r["segments_ms"]["compute"]["max"]), (1.0, 30.0))   # 各段取各自那一列
        self.assertEqual(r["panel"]["polls"], 2)
        self.assertAlmostEqual(r["panel"]["achieved_q"], 2.0)
        self.assertEqual((r["panel"]["dropped"], r["panel"]["replica_refreshes"]), (2, 3))
        self.assertEqual(r["notifier"]["writes"], 1)
        self.assertEqual(r["crashed"], ["panel"])
        empty = J.summarize(cfg, None, None, None)
        self.assertIsNone(empty["starved"])
        self.assertNotIn("panel", empty)
        self.assertEqual(J._count([None, "a", "a", None, "b"]), {"a": 2, "b": 1})

    def test_knees_and_pick(self):
        res = [fake_result(True, 5.0, "inline", q, p) for q, p in ((0, 1.0), (5, 1.1), (20, 1.3), (50, 1.9))]
        res += [fake_result(False, 5.0, "inline", q, p) for q, p in ((0, 1.0), (5, 1.5), (20, 3.1), (50, 4.0))]
        res += [fake_result(False, 0.5, "subprocess", q, p) for q, p in ((0, 1.0), (5, 2.5))]
        res += [fake_result(False, 5.0, "inline", 5, 1.0, mitigation="replica"), fake_result(True, 5.0, "inline", 0, 3, bg=1)]
        k = J.find_knees(res)
        self.assertEqual([(x["wal"], x["busy"], x["compute"]) for x in k],
                         [(False, 5.0, "inline"), (False, 0.5, "subprocess"), (True, 5.0, "inline")])
        self.assertEqual((k[0]["knee_q"], k[0]["last_ok_q"], k[0]["knee_p99_T"]), (20, 5, 3.1))
        self.assertEqual((k[1]["knee_q"], k[1]["last_ok_q"]), (5, 0))
        self.assertEqual((k[2]["knee_q"], k[2]["last_ok_q"]), (None, None))
        self.assertEqual(k[0]["p99_T_by_q"], {"0": 1.0, "5": 1.5, "20": 3.1, "50": 4.0})
        self.assertEqual(J.pick_knee(k)["compute"], "subprocess")
        self.assertIsNone(J.pick_knee([k[2]]))
        tie = [dict(k[1], knee_q=20), k[0]]
        self.assertEqual(J.pick_knee(tie)["compute"], "inline")
        s = {"results": res, "knees": k, "knee": J.pick_knee(k), "mitigations": [res[-2]], "gil": [res[-1]],
             "panel_calibration": {"rows": 10, "seconds": 0.1}, "base": dict(J.DEFAULTS)}
        table = J.knee_table(s)
        self.assertIn("3.10T ←拐点", table)
        self.assertIn("2.50T ←拐点", table)
        self.assertEqual(J.first_line_scan(s), "TICK_SCAN knee=wal=off,busy=0.5,compute=subprocess:Q=5 q0_p99_max=1.00T "
                                               "mitigations=replica:1.00T")
        text = J.format_scan(s)
        self.assertIn("有效", text)
        self.assertIn("GIL 对照", text)
        s2 = dict(s, knee=None, mitigations=[])
        self.assertIn("knee=none", J.first_line_scan(s2))
        self.assertIn("mitigations=-", J.first_line_scan(s2))
        self.assertRegex(J.first_line_run(dict(res[0], starved=False)), RUN_LINE)
        self.assertEqual(len(J.scan_grid(False)), 2 * 2 * 2 * 4)
        self.assertEqual(J.scan_grid(True), [{"wal": True, "busy": 5.0, "compute": "inline", "q": 0.0},
                                             {"wal": True, "busy": 5.0, "compute": "inline", "q": 5.0},
                                             {"wal": False, "busy": 5.0, "compute": "inline", "q": 0.0},
                                             {"wal": False, "busy": 5.0, "compute": "inline", "q": 5.0},
                                             {"wal": False, "busy": 0.5, "compute": "subprocess", "q": 0.0},
                                             {"wal": False, "busy": 0.5, "compute": "subprocess", "q": 5.0}])
        order = J.find_knees([fake_result(False, 0.5, "inline", 0, 1.0), fake_result(False, 5.0, "subprocess", 0, 1.0)])
        self.assertEqual([(x["busy"], x["compute"]) for x in order], [(5.0, "subprocess"), (0.5, "inline")])  # busy 大的在前
        self.assertIn("单线程 2000 ms", J.format_scan(dict(s, panel_calibration={"rows": 10, "seconds": 2.0})))
        blank = dict(res[0], starved=False, segments_ms={k: {"p99": None} for k in ("read", "compute", "write")},
                     wake_late_ms={"max": None})
        self.assertIn("read_p99_ms=0.00 compute_p99_ms=0.00 write_p99_ms=0.00", J.first_line_run(blank))   # 没有样本记 0
        self.assertIn("读/算/写 p99 0.0/0.0/0.0 ms  唤醒迟到 max 0 ms", J.row_text(blank))


class TestPanelParts(unittest.TestCase):
    def test_replica_refresh_sees_new_rows(self):
        d = tmpdir(self)
        main = os.path.join(d, "m.db")
        c = sqlite3.connect(main)
        c.execute("CREATE TABLE t(a)")
        c.execute("INSERT INTO t VALUES (1)")
        c.commit()
        r = J.Replica(main, [os.path.join(d, "a.db"), os.path.join(d, "b.db")], 5.0)
        c.execute("INSERT INTO t VALUES (2)")
        c.commit()
        c.close()
        r.refresh()                                                          # 刷新的是“不在用的那一份”，然后切过去
        k = r.acquire()
        try:
            rc = sqlite3.connect(r.paths[k])
            n = rc.execute("SELECT count(*) FROM t").fetchone()[0]
            rc.close()
        finally:
            r.release(k)
        self.assertEqual((n, r.refreshes, r.cur), (2, 1, 1))

    def test_open_loop_caps_inflight(self):
        """开环轮询：在途最多 2 × 线程数，其余记为丢弃（不无限排队）。_poll 换成卡住直到放行的假查询。"""
        from unittest import mock
        import threading
        gate = threading.Event()
        threading.Timer(0.6, gate.set).start()
        with mock.patch.object(J, "_poll", side_effect=lambda spec, local, replica: (gate.wait(5) and 0.001, None)):
            durs, errs, dropped = J._poll_open_loop({"q": 200.0, "seconds": 0.3, "panel_threads": 1, "seed": 0}, None, None)
        self.assertEqual((len(durs), errs), (2, {}))
        self.assertGreater(dropped, 20)

    def test_backoff_sleep(self):
        """退避：下一次至少等 max(1/Q, k × 上次耗时)。"""
        from unittest import mock
        slept, durs = [], iter([0.05, 0.01] * 1000)
        with mock.patch.object(J, "_poll", side_effect=lambda spec, local, replica: (next(durs), None)), \
                mock.patch.object(J.time, "sleep", side_effect=slept.append):
            J._poll_backoff({"q": 10.0, "backoff_k": 4.0, "seconds": 0.01}, None)
        self.assertGreater(len(slept), 1)
        self.assertEqual([round(x, 9) for x in slept[:2]], [0.2, 0.1])


class TestScanCleanup(unittest.TestCase):
    def test_scan_cleanup_never_loses_results(self):
        """扫描跑几十分钟：收尾删临时目录失败（例如子进程还占着文件）不能把结果一起丢掉。"""
        from unittest import mock
        work, calls = tmpdir(self), []

        def rmtree(path, ignore_errors=False):
            calls.append((path, ignore_errors))
            if not ignore_errors:
                raise OSError("directory busy")

        with mock.patch.object(J, "prepare", return_value=(work, os.path.join(work, "m.db"))), \
                mock.patch.object(J, "calibrate_panel", return_value={"rows": 1}), \
                mock.patch.object(J, "scan_grid", return_value=[]), \
                mock.patch.object(J, "run_once", side_effect=lambda master, cfg, w, cal: {"config": cfg}), \
                mock.patch.object(J.shutil, "rmtree", side_effect=rmtree):
            res = J.scan(quick=True)
        self.assertEqual(calls, [(work, True)])
        self.assertEqual((res["knee"], res["mitigations"], len(res["gil"])), (None, [], 4))


class TestRealProcesses(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work, cls.master = J.prepare(scale=0.02)
        cls.cal = J.calibrate_panel(cls.master, 3.0 * 0.04)
        cls.starved = cls.run_cfg(cls, q=10.0, wal=False)        # 同一会话里的“饿死”参照（rollback 模式 + 面板长查询）

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.work, ignore_errors=True)

    def run_cfg(self, **kw):
        cfg = dict(T=0.04, seconds=scale(3.0, 2.0), notifier_rate=2.0, panel_ratio=3.0)
        cfg.update(kw)
        return J.run_once(self.master, cfg, self.work, self.cal)

    def test_calibration(self):
        capped = J.calibrate_panel(self.master, 1000.0)                      # 目标远超整表耗时 → 行数封顶为整表
        self.assertTrue(capped["capped"])
        self.assertEqual(capped["rows"], capped["range"][1] - 1)
        self.assertFalse(self.cal["capped"])
        c = self.cal
        self.assertEqual(c["range"][1] - c["range"][0], c["rows"])
        self.assertGreater(c["rows"], 100)
        self.assertGreater(c["per_row_us"], 0)
        if TIMING:
            self.assertLess(abs(c["seconds"] - c["target_seconds"]) / c["target_seconds"], 0.5)

    def test_q0_no_jitter_and_wal_off_starves(self):
        quiet = self.run_cfg(q=0.0, wal=False)
        self.assertGreater(quiet["ticks"], 30)
        self.assertEqual(quiet["crashed"], [])
        self.assertLess(abs(quiet["interval_T"]["p50"] - 1.0), 0.05)
        self.assertLess(quiet["interval_T"]["p99"], 0.5 * self.starved["interval_T"]["p99"])
        if TIMING:
            self.assertFalse(quiet["starved"])
        self.assertGreater(quiet["notifier"]["writes"], 0)
        self.assertEqual(quiet["tick_errors"]["write"], {})                 # 每个 tick 的写事务都提交了
        self.assertLess(quiet["wake_late_ms"]["max"] or 0.0, 10_000.0)      # tick 起点相对引擎起跑时刻（不是绝对时钟）
        starved = self.starved
        self.assertTrue(starved["starved"], starved["interval_T"])
        self.assertGreater(starved["over_2T"], 0)
        self.assertGreater(starved["segments_ms"]["write"]["max"], 2 * 40.0 - 20)
        wal_fix = self.run_cfg(q=10.0, wal=False, mitigation="wal")
        self.assertLess(wal_fix["interval_T"]["p99"], 0.5 * starved["interval_T"]["p99"])   # 形状：短跑 p99≈最大值，不卡绝对 2T
        self.assertLess(wal_fix["segments_ms"]["write"]["max"], starved["segments_ms"]["write"]["max"])
        self.assertGreater(starved["panel"]["polls"], 5)
        chunked = self.run_cfg(q=10.0, wal=False, mitigation="chunked")          # 分块读在 rollback 模式下不一定有效：只验能跑
        self.assertGreater(chunked["panel"]["polls"], 3)
        self.assertEqual(chunked["crashed"], [])
        wal = self.run_cfg(q=10.0, wal=True)
        self.assertLess(wal["interval_T"]["p99"], 0.5 * starved["interval_T"]["p99"])

    def test_replica_backoff_subprocess_bg(self):
        rep = self.run_cfg(q=10.0, wal=False, mitigation="replica", replica_every=0.5)
        self.assertLess(rep["interval_T"]["p99"], 0.5 * self.starved["interval_T"]["p99"])
        self.assertGreater(rep["panel"]["replica_refreshes"], 0)
        bo = self.run_cfg(q=10.0, wal=False, mitigation="backoff", backoff_k=4.0, notifier_rate=0.5)
        self.assertIn("notifier", bo)                                        # 通知速率 ≤ 1 次/秒时也照样起通知进程
        self.assertLess(bo["panel"]["achieved_q"], 10.0)
        sp = self.run_cfg(q=0.0, wal=True, compute="subprocess", bg_threads=1, notifier_rate=0.0)
        self.assertEqual(sp["crashed"], [])
        self.assertNotIn("notifier", sp)
        self.assertNotIn("panel", sp)
        self.assertGreater(sp["segments_ms"]["compute"]["p50"], 0.25 * 40.0)

    def test_cli(self):
        d = tmpdir(self)
        from unittest import mock
        real_rmtree = shutil.rmtree

        def picky(path, ignore_errors=False, *a, **k):                      # 收尾删目录失败不能让 run 丢结果
            if not ignore_errors:
                raise OSError("directory busy")
            return real_rmtree(path, ignore_errors=True)

        with captured() as (o, _e), mock.patch.object(J.shutil, "rmtree", side_effect=picky):
            rc = J.main(["run", "--q", "0", "--seconds", "1.0", "--T", "0.05", "--scale", "0.002",
                         "--json", os.path.join(d, "r.json")])
        self.assertIn(rc, (0, 1))
        self.assertRegex(o.getvalue().splitlines()[0], RUN_LINE)
        self.assertTrue(o.getvalue().splitlines()[1].startswith("WAL "))     # 首行之后是这一配置的明细行（人读）
        self.assertTrue(os.path.exists(os.path.join(d, "r.json")))
        with captured() as (o, _e):
            self.assertEqual(J.main(["run", "--T", "0"]), 2)
        self.assertTrue(o.getvalue().startswith("TICK_JITTER ERROR"))
        src = os.path.join(d, "mine.db")
        shutil.copyfile(self.master, src)
        w, m = J.prepare(db=src)
        try:
            a = sqlite3.connect(f"file:{m}?mode=ro&immutable=1", uri=True)
            b = sqlite3.connect(f"file:{src}?mode=ro&immutable=1", uri=True)
            self.assertEqual(a.execute("SELECT count(*) FROM trades").fetchone(),
                             b.execute("SELECT count(*) FROM trades").fetchone())
            a.close()
            b.close()
        finally:
            shutil.rmtree(w, ignore_errors=True)
        self.assertEqual(sorted(os.listdir(d)), ["mine.db", "r.json"])           # 用户的库旁边没多出任何文件


if __name__ == "__main__":
    unittest.main()
