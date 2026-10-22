"""index_advisor 判据：别名回填、候选合并 / 命名 / 已有索引、抽样与大小估算（对记录格式手算）、没有 dbstat 时的大小、
覆盖索引变体（单测 + 端到端：单值列上 ANALYZE 后普通索引被弃用、覆盖索引被采用）、写放大的精确值。"""
import math
import os
import sqlite3
import unittest
from unittest import mock

from qdperf import index_advisor as A
from qdperf import sqlite_doctor as D
from qdperf import sqlplan as P
from qdperf.tests._util import tmpdir

SCHEMA_COLS = {"t": ["id", "a", "b", "pad"], "u": ["id", "t_id", "c"]}


def stmt(sid, sql, rate=1.0, params=None):
    return {"id": sid, "process": "p", "sql": sql, "per_second": rate, "params": params or [],
            "param_samples": [params or []]}


class TestPure(unittest.TestCase):
    def test_resolve_aliases(self):
        parsed = P.parse_statement("SELECT * FROM t x JOIN u y ON y.t_id = x.id", SCHEMA_COLS)
        nodes = P.parse_eqp([(2, 0, 0, "SCAN x"), (4, 0, 0, "SEARCH y USING INDEX iu (t_id=?)"),
                             (6, 0, 0, "USE TEMP B-TREE FOR ORDER BY")])
        out = A.resolve_aliases(nodes, parsed)
        self.assertIs(out, nodes)
        self.assertEqual([(n.get("table"), n.get("alias")) for n in out], [("t", "x"), ("u", "y"), (None, None)])
        plain = A.resolve_aliases(P.parse_eqp([(2, 0, 0, "SCAN t")]), P.parse_statement("SELECT * FROM t", SCHEMA_COLS))
        self.assertEqual((plain[0]["table"], plain[0]["alias"]), ("t", "t"))

    def test_merge_and_names(self):
        cands = [{"table": "t", "columns": ["a"], "kind": "sort", "statements": ["s1"], "why": {"s1": {"eq": ["a"]}}},
                 {"table": "t", "columns": ["a", "b"], "kind": "scan", "statements": ["s2"], "why": {"s2": {"eq": ["a", "b"]}}},
                 {"table": "t", "columns": ["a", "b"], "kind": "sort", "statements": ["s2", "s3"], "why": {"s3": {}}},
                 {"table": "u", "columns": ["a"], "kind": "sort", "statements": ["s4"], "why": {"s4": {}}}]
        out = A._merge(cands)
        self.assertEqual([(o["table"], o["columns"], o["kind"]) for o in out],
                         [("t", ["a", "b"], "scan"), ("u", ["a"], "sort")])
        self.assertEqual(out[0]["statements"], ["s2", "s3", "s1"])
        self.assertEqual(set(out[0]["why"]), {"s1", "s2", "s3"})
        self.assertEqual([o["name"] for o in out], ["qdperf_t_a_b", "qdperf_u_a"])
        self.assertEqual(cands[1]["statements"], ["s2"])                        # 不改入参
        self.assertEqual(A.index_name("orders", ["bot_id", "status"]), "qdperf_orders_bot_id_status")

    def test_propose_already_and_unsolvable(self):
        schema = {"t": {"columns": SCHEMA_COLS["t"], "indexes": {"ix_a_b": {"columns": ["a", "b"]}}},
                  "u": {"columns": SCHEMA_COLS["u"], "indexes": {}}}
        recs = [{"id": "q1", "kind": "select", "sql": "SELECT * FROM t WHERE a = ?", "scan_tables": ["t"], "temp_btree": []},
                {"id": "q2", "kind": "select", "sql": "SELECT * FROM u WHERE c = ? ORDER BY id", "scan_tables": ["u"],
                 "temp_btree": ["ORDER BY"]},
                {"id": "q3", "kind": "select", "sql": "SELECT * FROM u WHERE c LIKE ?", "scan_tables": ["u"], "temp_btree": []},
                {"id": "q4", "kind": "insert", "sql": "INSERT INTO u(c) VALUES (?)", "scan_tables": [], "temp_btree": []},
                {"id": "q5", "kind": "select", "sql": "SELECT * FROM t WHERE id = ?", "scan_tables": [], "temp_btree": []},
                {"id": "q6", "kind": "select", "sql": "SELECT * FROM t x JOIN u ON u.t_id = x.id ORDER BY u.c, x.a",
                 "scan_tables": [], "temp_btree": ["ORDER BY"]}]
        sugs, unsolvable, already = A.propose(recs, schema, SCHEMA_COLS)
        self.assertEqual(already, [{"statement": "q1", "table": "t", "columns": ["a"], "index": "ix_a_b"}])
        self.assertEqual([(s["name"], s["statements"], s["kind"]) for s in sugs], [("qdperf_u_c_id", ["q2"], "scan")])
        self.assertEqual([(u["statement"], u["table"]) for u in unsolvable], [("q3", "u"), ("q6", "t"), ("q6", "u")])
        self.assertIn("LIKE", unsolvable[0]["reasons"][0])
        self.assertIn("排序列不全在本表", unsolvable[1]["reasons"][0])               # ORDER BY 跨两张表

    def test_sample_rowids(self):
        d = tmpdir(self)
        c = sqlite3.connect(os.path.join(d, "s.db"))
        c.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, a)")
        self.assertEqual(A.sample_rowids(c, "t", 5), [])
        c.executemany("INSERT INTO t(id, a) VALUES (?, 1)", [(i,) for i in range(10, 110)])
        ids = A.sample_rowids(c, "t", 5)
        self.assertEqual(ids, [10, 34, 59, 84, 109])
        self.assertEqual(A.sample_rowids(c, "t", 100), list(range(10, 110)))
        self.assertEqual(A.sample_rowids(c, "t", 200), list(range(10, 110)))
        ids = A.sample_rowids(c, "t", 7)
        self.assertEqual((len(ids), ids[0], ids[-1]), (7, 10, 109))
        self.assertEqual(ids, sorted(ids))
        c.close()

    def test_estimate_size_matches_hand_computation(self):
        d = tmpdir(self)
        c = sqlite3.connect(os.path.join(d, "e.db"))
        c.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, a, b)")
        rows = [(1, "x" * 300, 5), (2, "y", 70000), (3, "zz", -1)]
        c.executemany("INSERT INTO t VALUES (?,?,?)", rows)
        sizes = [P.index_entry_bytes((a, b), rid) for rid, a, b in rows]
        avg = sum(sizes) / 3
        want = math.ceil(3_000_000 * avg / ((4096 - 8) * A.FILL)) * 4096
        self.assertEqual(A.estimate_size(c, "t", ["a", "b"], 3_000_000, 4096), want)
        self.assertEqual(A.estimate_size(c, "t", ["a", "b"], 0, 4096), 4096)
        c.execute("DELETE FROM t")
        self.assertEqual(A.estimate_size(c, "t", ["a"], 10, 4096), 4096)
        c.close()

    def test_covering_variant(self):
        sug = {"table": "t", "columns": ["a"], "statements": ["s"], "name": "qdperf_t_a", "kind": "scan"}
        by = {"s": stmt("s", "SELECT id, b, a FROM t WHERE a = ?"), "s2": stmt("s2", "SELECT a FROM t WHERE a = ?"),
              "s3": stmt("s3", "SELECT b, pad FROM t WHERE a = ? AND b > 0")}
        cov = A._covering_variant(sug, by, SCHEMA_COLS, {"t": "id"})
        self.assertEqual((cov["columns"], cov["covering"], cov["plain_columns"]), (["a", "b"], True, ["a"]))
        self.assertIsNone(A._covering_variant(dict(sug, statements=["s2"]), by, SCHEMA_COLS, {"t": "id"}))
        two = A._covering_variant(dict(sug, statements=["s", "s3"]), by, SCHEMA_COLS, {})
        self.assertEqual(two["columns"], ["a", "id", "b", "pad"])
        self.assertEqual(sug["columns"], ["a"])

    def test_write_amplification_exact(self):
        wl = {"statements": [stmt("ins", "INSERT INTO t(a, b) VALUES (?, ?)", 10.0),
                             stmt("upd_b", "UPDATE t SET b = ? WHERE id = ?", 4.0),
                             stmt("upd_pad", "UPDATE t SET pad = ? WHERE id = ?", 2.0),
                             stmt("del", "DELETE FROM u WHERE id = ?", 1.0),
                             stmt("sel", "SELECT * FROM t", 99.0)]}
        schema = {"t": {"indexes": {"ix_a": {"columns": ["a"]}}}, "u": {"indexes": {}}}
        sugs = [{"name": "qdperf_t_b", "table": "t", "columns": ["b"]}, {"name": "qdperf_u_c", "table": "u", "columns": ["c"]}]
        amp = A.write_amplification(wl, schema, sugs, SCHEMA_COLS)
        rows = {r["id"]: (r["btrees_before"], r["btrees_after"]) for r in amp["statements"]}
        self.assertEqual(rows, {"ins": (2, 3), "upd_b": (1, 2), "upd_pad": (1, 1), "del": (1, 2)})
        self.assertEqual(amp["btree_writes_per_second_before"], 2 * 10 + 4 + 2 + 1)
        self.assertEqual(amp["btree_writes_per_second_after"], 3 * 10 + 2 * 4 + 2 + 2)
        self.assertAlmostEqual(amp["relative_increase"], round((42 - 27) / 27, 4))
        self.assertEqual(amp["per_suggestion_extra_btree_writes_per_second"], {"qdperf_t_b": 14.0, "qdperf_u_c": 1.0})
        self.assertEqual(A.write_amplification({"statements": [wl["statements"][-1]]}, schema, sugs, SCHEMA_COLS)
                         ["relative_increase"], 0.0)
        self.assertTrue(A.affected("insert", [], ["x"]))
        self.assertTrue(A.affected("delete", [], ["x"]))
        self.assertTrue(A.affected("update", ["x", "y"], ["y"]))
        self.assertFalse(A.affected("update", ["x"], ["y"]))
        self.assertFalse(A.affected("select", ["x"], ["x"]))


class TestVerify(unittest.TestCase):
    def build(self, rows=20000):
        path = os.path.join(tmpdir(self), "v.db")
        c = sqlite3.connect(path, isolation_level=None)
        c.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, a INT, b INT, pad TEXT)")
        c.execute("CREATE TABLE u(id INTEGER PRIMARY KEY, t_id INT, c INT)")
        c.execute("BEGIN")
        c.executemany("INSERT INTO t(a, b, pad) VALUES (?,?,?)", [(0, i, "x" * 20) for i in range(rows)])
        c.executemany("INSERT INTO u(t_id, c) VALUES (?,?)", [(i, i % 50) for i in range(rows)])
        c.execute("COMMIT")
        return path, c

    def test_covering_fallback_end_to_end(self):
        """a 列只有一个值：普通索引 (a) ANALYZE 后被弃用（仍 SCAN）→ 改试覆盖索引 (a, b) → 采用。"""
        _path, c = self.build()
        by = {"q": stmt("q", "SELECT b FROM t WHERE a = ?", 50.0, [0]),
              "r": stmt("r", "SELECT id FROM u WHERE c = ?", 5.0, [7])}
        sugs = [{"table": "t", "columns": ["a"], "kind": "scan", "statements": ["q"], "why": {}, "name": "qdperf_t_a"},
                {"table": "u", "columns": ["c"], "kind": "scan", "statements": ["r"], "why": {}, "name": "qdperf_u_c"}]
        counts = {"t": 20000, "u": 20000}
        verified, rejected, secs = A.verify(c, sugs, by, SCHEMA_COLS, counts, D.explain, {"t": "id", "u": "id"})
        names = [s["name"] for s in verified]
        self.assertEqual(sorted(names), ["qdperf_t_a_b", "qdperf_u_c"])
        cov = next(s for s in verified if s["name"] == "qdperf_t_a_b")
        self.assertTrue(cov["covering"] and cov["verified"])
        self.assertEqual(cov["plain_columns"], ["a"])
        self.assertTrue(cov["check_analyzed"]["q"]["ok"] and cov["check_analyzed"]["q"]["uses_index"])
        self.assertIn("qdperf_t_a_b", cov["check_as_is"]["q"]["plan"][0])      # 变体自己的“建后、ANALYZE 前”计划（不是从普通索引继承的）
        self.assertIn("COVERING", cov["check_analyzed"]["q"]["plan"][0])
        self.assertEqual([(r["name"], r["reject_reason"][:12]) for r in rejected], [("qdperf_t_a", "ANALYZE 后仍扫描")])
        self.assertGreaterEqual(secs, 0.0)
        idx = {r[1] for r in c.execute("SELECT type, name FROM sqlite_schema WHERE type = 'index'")}
        self.assertEqual(idx, {"qdperf_t_a_b", "qdperf_u_c"})                     # 被弃用的普通索引已删
        self.assertEqual(cov["sql"], "CREATE INDEX qdperf_t_a_b ON t(a, b);")
        self.assertGreater(cov["size_bytes"], 0)
        c.close()

    def test_redundant_and_no_dbstat_size(self):
        _path, c = self.build(rows=3000)
        by = {"q": stmt("q", "SELECT * FROM u WHERE c = ?", 1.0, [3]), "q2": stmt("q2", "SELECT * FROM u WHERE c = ?", 1.0, [4])}
        sugs = [{"table": "u", "columns": ["c"], "kind": "scan", "statements": ["q"], "why": {}, "name": "x"},
                {"table": "u", "columns": ["c", "t_id"], "kind": "scan", "statements": ["q2"], "why": {}, "name": "y"}]
        with mock.patch.object(A, "object_size", return_value=None):
            verified, rejected, _s = A.verify(c, sugs, by, SCHEMA_COLS, {"t": 3000, "u": 3000}, D.explain)
        self.assertEqual(len(verified) + len(rejected), 2)
        self.assertEqual(len(verified), 1)
        self.assertIn("没有任何语句用到它", rejected[0]["reject_reason"])
        self.assertEqual(verified[0]["size_source"], "page_count-freelist delta")
        self.assertGreater(verified[0]["size_bytes"], 0)
        self.assertLess(verified[0]["size_bytes"], 3000 * 40)
        self.assertEqual(verified[0]["size_bytes"] % 4096, 0)
        self.assertIsNone(A.object_size(sqlite3.connect(":memory:"), "nope"))
        c.close()


if __name__ == "__main__":
    unittest.main()
