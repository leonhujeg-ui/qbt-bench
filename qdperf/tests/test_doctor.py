"""sqlite_doctor / index_advisor 判据：工作负载校验、只读打开零写入、行数估算、建议在独立副本上复核不再 SCAN、
库文件体检、发现与退出码、第一行格式。"""
import copy
import json
import os
import re
import shutil
import sqlite3
import unittest

from qdperf import common
from qdperf import index_advisor as A
from qdperf import sqlite_doctor as D
from qdperf import sqlplan as P
from qdperf.tests._util import WORKLOAD, captured, tiny_db, tmpdir

FIRST_LINE = re.compile(r"^SQLITE_DOCTOR (OK|FAIL) statements=\d+ full_scan=\d+ temp_btree=\d+ suggestions=(\d+/\d+|-) "
                        r"contention_p99_ms=([\d.]+|-) busy=(\d+|-) wal_max_mb=([\d.]+|-) red=\d+ yellow=\d+$")


def wl():
    return common.read_json(WORKLOAD)


class TestWorkload(unittest.TestCase):
    def test_valid_and_defaults(self):
        w = D.load_workload(WORKLOAD)
        self.assertEqual(len(w["statements"]), 23)
        self.assertEqual(w["connection"], {"synchronous": "NORMAL", "busy_timeout_ms": 5000, "begin": "immediate"})
        self.assertTrue(all(st["param_samples"] == [st["params"]] for st in w["statements"]))
        raw = {"format": D.FORMAT, "statements": [{"id": "a", "process": "p", "sql": "SELECT 1", "per_second": 1}]}
        w = D.load_workload(raw)
        self.assertEqual(w["statements"][0]["params"], [])
        self.assertEqual(w["connection"]["begin"], "immediate")
        self.assertNotIn("connection", raw)                       # 不改调用方传进来的 dict

    def test_errors(self):
        good = {"format": D.FORMAT, "statements": [{"id": "a", "process": "p", "sql": "SELECT 1", "per_second": 1}]}
        bad_cases = [
            ({"format": "x"}, "format"), ({"format": D.FORMAT, "statements": []}, "non-empty"),
            ({"format": D.FORMAT, "statements": [1]}, "object"),
            ({"format": D.FORMAT, "statements": [{"id": "", "process": "p", "sql": "x", "per_second": 1}]}, ".id"),
            ({"format": D.FORMAT, "statements": [{"id": "a", "process": "p", "sql": 3, "per_second": 1}]}, ".sql"),
            ({"format": D.FORMAT, "statements": [good["statements"][0]] * 2}, "duplicate"),
            ({"format": D.FORMAT, "statements": [{"id": "a", "process": "p", "sql": "x", "per_second": -1}]}, "per_second"),
            ({"format": D.FORMAT, "statements": [{"id": "a", "process": "p", "sql": "x", "per_second": True}]},
             "per_second"),
            ({"format": D.FORMAT, "statements": [{"id": "a", "process": "p", "sql": "x", "per_second": float("inf")}]},
             "per_second"),
            ({"format": D.FORMAT, "statements": [{"id": "a", "process": "p", "sql": "x", "per_second": float("nan")}]},
             "per_second"),
            ({"format": D.FORMAT, "statements": [{"id": "a", "process": "p", "sql": "x", "per_second": 1, "params": 3}]},
             "params"),
            ({"format": D.FORMAT, "statements": [{"id": "a", "process": "p", "sql": "x", "per_second": 1,
                                                  "param_samples": []}]}, "param_samples"),
            ({"format": D.FORMAT, "statements": [{"id": "a", "process": "p", "sql": "x", "per_second": 1,
                                                  "param_samples": [3]}]}, "param_samples"),
            ({"format": D.FORMAT, "statements": [{"id": "a", "process": "p", "sql": "x", "per_second": 1, "txn": 1}]},
             "txn"),
            (dict(good, connection={"begin": "exclusive"}), "begin"), ([1], "format")]
        for raw, word in bad_cases:
            with self.assertRaises(D.DoctorError) as cm:
                D.load_workload(raw)
            self.assertIn(word, str(cm.exception), raw)
        with self.assertRaises(D.DoctorError):
            D.load_workload(os.path.join(tmpdir(self), "missing.json"))
        p = os.path.join(tmpdir(self), "bad.json")
        with open(p, "w") as f:
            f.write("{nope")
        with self.assertRaises(D.DoctorError):
            D.load_workload(p)


class TestReadOnly(unittest.TestCase):
    def test_open_readonly_no_side_files(self):
        db = tiny_db(self)
        before = D.file_state(db)
        conn, how = D.open_readonly(db)
        self.assertEqual(how, {"wal_header": True, "immutable": True})
        self.assertGreater(conn.execute("SELECT count(*) FROM trades").fetchone()[0], 0)
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE x(a)")
        conn.close()
        self.assertEqual(D.file_state(db), before)
        self.assertEqual(before["siblings"], ["tiny.db"])

    def test_live_wal_opened_ro_and_rollback_db(self):
        d = tmpdir(self)
        p = os.path.join(d, "live.db")
        w = sqlite3.connect(p)
        w.execute("PRAGMA journal_mode=WAL")
        w.execute("CREATE TABLE t(a)")
        w.commit()
        conn, how = D.open_readonly(p)                    # 写者还开着：-wal 存在 → mode=ro（不 immutable）
        self.assertEqual(how, {"wal_header": True, "immutable": False})
        conn.close()
        w.close()
        q = os.path.join(d, "rb.db")
        sqlite3.connect(q).execute("CREATE TABLE t(a)").connection.commit()
        conn, how = D.open_readonly(q)
        self.assertEqual(how, {"wal_header": False, "immutable": False})
        conn.close()

    def test_bad_inputs(self):
        d = tmpdir(self)
        with self.assertRaises(D.DoctorError):
            D.open_readonly(os.path.join(d, "nope.db"))
        p = os.path.join(d, "text.db")
        with open(p, "w") as f:
            f.write("not sqlite" * 20)
        with self.assertRaises(D.DoctorError):
            D.open_readonly(p)
        p2 = os.path.join(d, "short.db")
        with open(p2, "wb") as f:
            f.write(b"SQLite format 3\x00")
        with self.assertRaises(D.DoctorError):
            D.open_readonly(p2)


class TestEstimates(unittest.TestCase):
    def test_node_rows(self):
        counts = {"t": 1000, "u": 50}
        stats = {"ia": [1000, 100, 10], "ib": [1000, 4]}
        n = P.parse_eqp([(1, 0, 0, d) for d in (
            "SCAN t", "SEARCH t USING INDEX ia (a=?)", "SEARCH t USING INDEX ia (a=? AND b=?)",
            "SEARCH t USING INDEX ia (a=? AND b>?)", "SEARCH t USING INDEX ia (a>? AND a<?)",
            "SEARCH t USING INTEGER PRIMARY KEY (rowid=?)", "SEARCH t USING INTEGER PRIMARY KEY (rowid>?)",
            "SEARCH t USING INTEGER PRIMARY KEY (rowid>? AND rowid<?)", "SEARCH t USING INDEX nostat (x=?)",
            "SEARCH t USING INDEX nostat (x>?)", "SEARCH u USING AUTOMATIC COVERING INDEX (k=?)",
            "USE TEMP B-TREE FOR ORDER BY", "SEARCH t USING INDEX ib (a=? AND b=? AND c=?)",
            "SEARCH t USING INDEX ia (a=? AND b=? AND c>?)")])
        got = [D.node_rows(x, counts, stats) for x in n]
        self.assertEqual(got, [1000.0, 100.0, 10.0, 25.0, 1000 / 64, 1.0, 250.0, 1000 / 64, 10.0, 250.0, 10.0, 0.0,
                               4.0, 2.5])

    def test_statement_rows_nested(self):
        counts = {"t": 1000, "u": 50}
        nodes = P.parse_eqp([(3, 0, 0, "SCAN t"), (5, 0, 0, "SEARCH u USING INTEGER PRIMARY KEY (rowid=?)"),
                             (9, 0, 0, "USE TEMP B-TREE FOR ORDER BY")])
        self.assertEqual(D.statement_rows(nodes, counts, {}), 1000 + 1000 * 1)
        auto = P.parse_eqp([(3, 0, 0, "SCAN t"), (5, 0, 0, "SEARCH u USING AUTOMATIC COVERING INDEX (k=?)")])
        self.assertEqual(D.statement_rows(auto, counts, {}), 1000 + 1000 * 10 + 50)
        two = P.parse_eqp([(3, 0, 0, "SCAN t"), (7, 1, 0, "SCAN u")])
        self.assertEqual(D.statement_rows(two, counts, {}), 1050)
        empty_scan = P.parse_eqp([(3, 0, 0, "SCAN t"), (5, 0, 0, "SCAN u")])
        self.assertEqual(D.statement_rows(empty_scan, {"t": 0, "u": 9}, {}), 0)
        self.assertEqual(D.top_by_cost([{"id": "b", "cost": 5}, {"id": "a", "cost": 5}, {"id": "c", "cost": 9}], 2),
                         ["c", "a"])


class TestDiagnoseStatic(unittest.TestCase):
    """小合成库上的计划 / 建议 / 库文件体检（不跑多进程实验）。"""

    @classmethod
    def setUpClass(cls):
        cls.holder = unittest.TestCase()
        import tempfile
        cls.dir = tempfile.mkdtemp(prefix="qdperf-doc-")
        from qdperf import synth_db
        cls.db = os.path.join(cls.dir, "s.db")
        synth_db.generate(cls.db, seed=5, scale=0.004)
        cls.before = D.file_state(cls.db)
        cls.rep = D.diagnose(cls.db, WORKLOAD, sections=("plans", "indexes", "file"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_untouched_and_header(self):
        rep = self.rep
        self.assertTrue(rep["db"]["untouched"])
        self.assertEqual(rep["db"]["sha256_before"], rep["db"]["sha256_after"])
        self.assertEqual(D.file_state(self.db), self.before)
        self.assertEqual(rep["db"]["siblings_after"], ["s.db"])
        self.assertEqual(rep["format"], D.REPORT_FORMAT)
        self.assertRegex(D.first_line(rep), FIRST_LINE)
        self.assertFalse(rep["ok"])

    def test_verdicts_and_ranking(self):
        by = {s["id"]: s for s in self.rep["statements"]}
        want = {"e_open_orders": "full_scan", "h_trades_by_user": "full_scan", "h_orders_by_user": "temp_btree",
                "h_user": "rowid", "e_insert_order": "none", "h_orders_join_bots": "index",
                "h_email_search": "full_scan", "h_setting": "full_scan"}
        for sid, v in want.items():
            self.assertEqual(by[sid]["verdict"], v, sid)
        top = self.rep["top"]
        costs = [by[s]["cost"] for s in top]
        self.assertEqual(costs, sorted(costs, reverse=True))
        self.assertEqual(len(top), 20)
        self.assertEqual(top[0], "e_open_orders")
        counts = {t: v["rows"] for t, v in self.rep["tables"].items()}
        self.assertEqual(by["e_open_orders"]["rows_est"], counts["orders"])
        self.assertEqual(by["e_open_orders"]["cost"], counts["orders"] * 50)
        self.assertEqual(by["h_user"]["rows_est"], 1.0)
        self.assertGreater(by["e_open_orders"]["measured"]["vm_kilo_steps"], 0)
        self.assertEqual(by["h_user"]["measured"]["rows_returned"], 1)
        self.assertFalse(by["e_open_orders"]["measured"]["timed_out"])

    def test_suggestions_verified_independently(self):
        """判据：把输出的 SQL 在原库的另一份拷贝上执行，每条建议对应（以及它实际服务）的语句都不再 SCAN。"""
        idx = self.rep["indexes"]
        self.assertGreaterEqual(len(idx["suggestions"]), 8)
        self.assertEqual(idx["sql"][-1], "ANALYZE;")
        names = {s["name"] for s in idx["suggestions"]}
        self.assertIn("qdperf_orders_bot_id_status", names)
        self.assertIn("qdperf_trades_user_id_ts", names)
        self.assertIn("qdperf_orders_user_id_status_created_at", names)
        dst = os.path.join(tmpdir(self), "patched.db")
        shutil.copyfile(self.db, dst)
        conn = sqlite3.connect(dst)
        conn.executescript("\n".join(idx["sql"]))
        w = D.load_workload(WORKLOAD)
        by = {s["id"]: s for s in w["statements"]}
        cols = {t: [r[1] for r in conn.execute(f"PRAGMA table_info({t})")] for t in self.rep["tables"]}
        for sug in idx["suggestions"]:
            for sid in set(sug["statements"]) | set(sug["serves"]):
                nodes = A.resolve_aliases(D.explain(conn, by[sid]["sql"], by[sid]["param_samples"][0]),
                                          P.parse_statement(by[sid]["sql"], cols))
                self.assertNotIn(sug["table"], {n["table"] for n in nodes if n["op"] == "scan"}, (sid, sug["name"]))
                if sug["kind"] == "sort":
                    self.assertEqual(P.classify(nodes)["temp_btree"], [], sid)
            self.assertTrue(sug["verified"])
            self.assertTrue(sug["serves"])
            self.assertGreater(sug["size_bytes"], 0)
            self.assertLess(abs(sug["size_est_bytes"] - sug["size_bytes"]), 0.5 * sug["size_bytes"] + 8192)
        conn.close()

    def test_unsolvable_and_write_amp(self):
        idx = self.rep["indexes"]
        un = {u["statement"]: u for u in idx["unsolvable"]}
        self.assertIn("h_email_search", un)
        self.assertIn("LIKE", un["h_email_search"]["reasons"][0])
        amp = idx["write_amplification"]
        self.assertGreater(amp["btree_writes_per_second_after"], amp["btree_writes_per_second_before"])
        rows = {r["id"]: r for r in amp["statements"]}
        self.assertEqual(rows["e_insert_order"]["btrees_before"], 2)             # 表 + idx_orders_user
        self.assertEqual(rows["e_update_order"]["btrees_before"], 1)             # SET 的列都不在已有索引里
        self.assertGreater(rows["e_update_order"]["btrees_after"], 1)            # 新索引含 status
        self.assertEqual(rows["e_touch_bot"]["btrees_after"], 2)                 # bots(status, updated_at)
        self.assertEqual(set(idx["write_us_before"]), set(idx["write_us_after"]))
        self.assertEqual(len(idx["write_us_before"]), 9)
        final = idx["verdict_after_all"]
        self.assertEqual([k for k, v in final.items() if v == "full_scan"], ["h_email_search"])

    def test_file_checkup(self):
        fc = self.rep["file"]
        self.assertEqual(fc["no_primary_key"], ["settings"])
        fks = {(x["table"], x["column"]) for x in fc["fk_without_index"]}
        self.assertEqual(fks, {("bots", "user_id"), ("events", "bot_id"), ("events", "user_id"), ("orders", "bot_id"),
                               ("trades", "user_id"), ("trades", "bot_id")})
        self.assertTrue(fc["analyzed"])
        self.assertEqual([s["object"] for s in fc["stats_stale"]], ["idx_orders_user"])
        self.assertIn("idx_trades_order", fc["stats_missing"])
        self.assertEqual(fc["journal_mode_header"], "wal")
        self.assertTrue(fc["opened_immutable"])
        self.assertGreater(fc["pragmas"]["freelist_count"], 0)
        codes = {f["code"] for f in self.rep["findings"]}
        self.assertTrue({"HOT_FULL_SCAN", "NO_PRIMARY_KEY", "FK_WITHOUT_INDEX", "STATS_STALE", "STATS_MISSING"} <= codes)
        text = D.format_text(self.rep)
        for word in ("体检表", "索引建议", "库文件体检", "发现", "CREATE INDEX qdperf_orders_bot_id_status"):
            self.assertIn(word, text)


class TestFindingsAndCli(unittest.TestCase):
    def base_rep(self):
        return {"statements": [{"id": "a", "verdict": "full_scan", "cost": 20000.0, "per_second": 2, "rows_est": 10000,
                                "scan_tables": ["t"], "flags": ["full_scan"], "temp_btree": []},
                               {"id": "b", "verdict": "full_scan", "cost": 50.0, "per_second": 1, "rows_est": 50,
                                "scan_tables": ["u"], "flags": ["full_scan"], "temp_btree": []},
                               {"id": "c", "verdict": "temp_btree", "cost": 20000.0, "per_second": 1, "rows_est": 2e4,
                                "scan_tables": [], "flags": ["temp_btree"], "temp_btree": ["ORDER BY"]}],
                "db": {"untouched": True}}

    def test_levels(self):
        f = D.build_findings(self.base_rep(), D.DEFAULTS)
        self.assertEqual([(x["level"], x["code"]) for x in f],
                         [("red", "HOT_FULL_SCAN"), ("yellow", "FULL_SCAN"), ("yellow", "TEMP_BTREE")])
        rep = self.base_rep()
        rep["statements"] = rep["statements"][1:]
        rep["db"]["untouched"] = False
        rep["contention"] = {"scenarios": [
            {"name": "g", "gate": True, "ok": False, "overall": {"wait_ms": {"p99": 300.0}, "busy": 2},
             "root_cause": ["x"]},
            {"name": "d", "gate": False, "ok": False, "overall": {"wait_ms": {"p99": None}, "busy": 9}},
            {"name": "ok", "gate": True, "ok": True, "overall": {"wait_ms": {"p99": 1.0}, "busy": 0}}]}
        wal = {"config": {"long_reader": True, "autocheckpoint": 100, "duration": 6}, "max_live_frames": 101,
               "max_bytes": 5e6, "resets": 0}
        rep["wal"] = {"scenarios": [wal, dict(wal, max_live_frames=100), dict(wal, resets=1),
                                    dict(wal, config=dict(wal["config"], long_reader=False), max_live_frames=900)]}
        rep["indexes"] = {"rejected": [{"name": "i", "reject_reason": "r"}]}
        rep["file"] = {"fragmentation": 0.05, "pragmas": {"freelist_count": 5}, "no_primary_key": [],
                       "fk_without_index": [], "analyzed": False, "stats_stale": [], "stats_missing": []}
        f = D.build_findings(rep, D.DEFAULTS)
        got = [(x["level"], x["code"]) for x in f]
        self.assertEqual(got, [("yellow", "FULL_SCAN"), ("yellow", "TEMP_BTREE"), ("info", "SUGGESTION_REJECTED"),
                               ("red", "CONTENTION"), ("yellow", "CONTENTION_DEMO"), ("yellow", "WAL_GROWTH"),
                               ("yellow", "FRAGMENTED"), ("yellow", "NEVER_ANALYZED"), ("red", "ORIGINAL_MODIFIED")])
        rep["file"]["fragmentation"] = 0.0499
        self.assertNotIn("FRAGMENTED", {x["code"] for x in D.build_findings(rep, D.DEFAULTS)})

    def test_first_line_values(self):
        rep = self.base_rep()
        rep["findings"] = D.build_findings(rep, D.DEFAULTS)
        rep["ok"] = False
        self.assertEqual(D.first_line(rep), "SQLITE_DOCTOR FAIL statements=3 full_scan=2 temp_btree=1 suggestions=- "
                                            "contention_p99_ms=- busy=- wal_max_mb=- red=1 yellow=2")
        rep["indexes"] = {"suggestions": [1, 2], "rejected": [3]}
        rep["contention"] = {"scenarios": [{"gate": True, "overall": {"wait_ms": {"p99": 1.234}, "busy": 1}},
                                           {"gate": True, "overall": {"wait_ms": {"p99": None}, "busy": 2}},
                                           {"gate": False, "overall": {"wait_ms": {"p99": 999.0}, "busy": 7}}]}
        rep["wal"] = {"scenarios": [{"max_bytes": 2.5e6}, {"max_bytes": 1e6}]}
        self.assertIn("suggestions=2/3 contention_p99_ms=1.23 busy=3 wal_max_mb=2.5 ", D.first_line(rep))

    def test_main_exit_codes(self):
        db = tiny_db(self)
        d = tmpdir(self)
        js, sql = os.path.join(d, "r.json"), os.path.join(d, "i.sql")
        with captured() as (o, _e):
            rc = D.main([db, "--workload", WORKLOAD, "--sections", "plans,indexes", "--json", js, "--emit-sql", sql])
        self.assertEqual(rc, 1)
        self.assertRegex(o.getvalue().splitlines()[0], FIRST_LINE)
        rep = common.read_json(js)
        with open(sql) as f:
            lines = f.read().splitlines()
        self.assertEqual(lines, rep["indexes"]["sql"])
        conn = sqlite3.connect(db)                       # 照建议做完之后再体检：没有红色发现 → 退出码 0
        conn.executescript("\n".join(lines))
        conn.close()
        with captured() as (o, _e):
            rc = D.main([db, "--workload", WORKLOAD, "--sections", "plans,indexes"])
        self.assertEqual(rc, 0, o.getvalue()[:2000])
        self.assertTrue(o.getvalue().startswith("SQLITE_DOCTOR OK "))
        with captured() as (o, _e):
            rc = D.main([os.path.join(d, "missing.db"), "--workload", WORKLOAD])
        self.assertEqual(rc, 2)
        self.assertTrue(o.getvalue().startswith("SQLITE_DOCTOR ERROR"))
        with captured() as (o, _e):
            rc = D.main([db, "--workload", WORKLOAD, "--sections", "plans,nope"])
        self.assertEqual(rc, 2)
        bad = os.path.join(d, "bad.json")
        with open(bad, "w") as f:
            json.dump({"format": "nope"}, f)
        with captured() as (o, _e):
            self.assertEqual(D.main([db, "--workload", bad]), 2)

    def test_writer_mix(self):
        w = D.load_workload(WORKLOAD)
        cols = {"orders": ["id", "bot_id", "user_id", "symbol", "side", "type", "price", "qty", "filled", "status",
                           "created_at", "updated_at"],
                "trades": ["id", "order_id", "bot_id", "user_id", "symbol", "side", "price", "qty", "fee", "ts"],
                "bots": ["id", "user_id", "status", "updated_at"], "events": ["id", "ts", "notified"]}
        mix = D.writer_mix(w, cols)
        self.assertEqual(list(mix), ["http", "engine", "notifier"])
        self.assertEqual([t["name"] for t in mix["engine"]], ["tick"])
        self.assertEqual(len(mix["engine"][0]["statements"]), 4)
        self.assertEqual(mix["engine"][0]["weight"], 20)
        self.assertEqual(mix["engine"][0]["read_table"], "orders")
        self.assertEqual([t["name"] for t in mix["notifier"]], ["n_mark_sent", "n_insert_event", "n_purge_events"])
        wr = D.workload_writers(mix)
        self.assertEqual([(x["name"], x["rate"]) for x in wr], [("http", 2.5), ("engine", 20), ("notifier", 10.01)])
        u = D.uniform_writers(mix, 5, 50.0)
        self.assertEqual([x["name"] for x in u], ["http", "engine", "notifier", "http#2", "engine#2"])
        self.assertTrue(all(x["rate"] == 50.0 for x in u))
        self.assertIs(u[3]["txns"], mix["http"])
        w2 = copy.deepcopy(w)
        for st in w2["statements"]:
            st["per_second"] = 0
        self.assertEqual(D.writer_mix(w2, cols), {})


if __name__ == "__main__":
    unittest.main()
