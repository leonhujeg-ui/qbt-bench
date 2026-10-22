"""sqlplan 纯函数判据：EXPLAIN QUERY PLAN 判定、SQL 解析、候选列顺序、记录字节（与 SQLite dbstat 实测逐字节对账）。"""
import os
import sqlite3
import unittest

from qdperf import sqlplan as P
from qdperf.tests._util import tmpdir

SCHEMA = {"users": ["id", "email", "name", "tier", "status", "created_at"],
          "bots": ["id", "user_id", "name", "strategy", "symbol", "status", "created_at", "updated_at", "config"],
          "orders": ["id", "bot_id", "user_id", "symbol", "side", "type", "price", "qty", "filled", "status",
                     "created_at", "updated_at"],
          "trades": ["id", "order_id", "bot_id", "user_id", "symbol", "side", "price", "qty", "fee", "ts"],
          "events": ["id", "user_id", "bot_id", "kind", "level", "notified", "ts", "payload"],
          "settings": ["key", "value", "updated_at"]}


def eqp(*details):
    return P.parse_eqp([(k + 2, 0, 0, d) for k, d in enumerate(details)])


class TestEqp(unittest.TestCase):
    def test_parse_details(self):
        n = eqp("SCAN t", "SCAN t USING COVERING INDEX ia", "SEARCH t USING INDEX ia (a=? AND b>?)",
                "SEARCH t USING INTEGER PRIMARY KEY (rowid=?)", "USE TEMP B-TREE FOR ORDER BY",
                "SEARCH s USING AUTOMATIC COVERING INDEX (k=?)", "SCAN CONSTANT ROW", "SCAN TABLE t AS x",
                "SEARCH TABLE t AS y USING COVERING INDEX ib (b=?)", "CO-ROUTINE sub", "SEARCH t USING PRIMARY KEY (k=?)")
        self.assertEqual([x["op"] for x in n], ["scan", "scan", "search", "search", "temp_btree", "search", "other",
                                                "scan", "search", "other", "search"])
        self.assertEqual((n[0]["table"], n[0]["index"], n[0]["covering"]), ("t", None, False))
        self.assertEqual((n[1]["index"], n[1]["covering"]), ("ia", True))
        self.assertEqual((n[2]["index"], n[2]["constraints"], n[2]["covering"], n[2]["rowid"], n[2]["automatic"]),
                         ("ia", "a=? AND b>?", False, False, False))
        self.assertTrue(n[3]["rowid"])
        self.assertEqual(n[3]["constraints"], "rowid=?")
        self.assertEqual(n[4]["purpose"], "ORDER BY")
        self.assertTrue(n[5]["automatic"] and n[5]["covering"])
        self.assertEqual((n[7]["table"], n[7]["alias"]), ("t", "x"))
        self.assertEqual((n[8]["table"], n[8]["alias"], n[8]["index"]), ("t", "y", "ib"))
        self.assertTrue(n[10]["rowid"])
        self.assertEqual([x["id"] for x in n[:3]], [2, 3, 4])
        self.assertEqual(n[0]["detail"], "SCAN t")

    def test_classify_priority(self):
        cases = [(("SCAN t", "USE TEMP B-TREE FOR ORDER BY"), "full_scan"),
                 (("SEARCH t USING INDEX ia (a=?)", "USE TEMP B-TREE FOR ORDER BY"), "temp_btree"),
                 (("SEARCH s USING AUTOMATIC COVERING INDEX (k=?)", "SEARCH t USING INDEX ia (a=?)"), "automatic_index"),
                 (("SEARCH t USING INDEX ia (a=?)", "SEARCH u USING COVERING INDEX ib (b=?)"), "index"),
                 (("SEARCH u USING COVERING INDEX ib (b=?)", "SEARCH t USING INTEGER PRIMARY KEY (rowid=?)"),
                  "covering_index"),
                 (("SEARCH t USING INTEGER PRIMARY KEY (rowid=?)",), "rowid"),
                 ((), "none"), (("SCAN CONSTANT ROW",), "none")]
        for details, want in cases:
            self.assertEqual(P.classify(eqp(*details))["verdict"], want, details)
        c = P.classify(eqp("SCAN t", "SCAN u", "SCAN t", "USE TEMP B-TREE FOR GROUP BY", "USE TEMP B-TREE FOR DISTINCT"))
        self.assertEqual(c["scan_tables"], ["t", "u"])
        self.assertEqual(c["temp_btree"], ["GROUP BY", "DISTINCT"])
        self.assertEqual(c["flags"], ["full_scan", "temp_btree"])
        self.assertEqual(P.VERDICTS[0], "full_scan")
        self.assertEqual(P.VERDICTS[-1], "none")

    def test_against_real_sqlite(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, a INT, b INT, c TEXT)")
        conn.execute("CREATE INDEX ia ON t(a)")
        cases = {"SELECT * FROM t WHERE b=?": "full_scan", "SELECT * FROM t WHERE a=? ORDER BY b": "temp_btree",
                 "SELECT a FROM t WHERE a=?": "covering_index", "SELECT * FROM t WHERE id=?": "rowid",
                 "SELECT * FROM t WHERE a=?": "index"}
        for sql, want in cases.items():
            rows = conn.execute("EXPLAIN QUERY PLAN " + sql, (1,)).fetchall()
            self.assertEqual(P.classify(P.parse_eqp(rows))["verdict"], want, sql)

    def test_search_constraint_counts(self):
        self.assertEqual(P.search_constraint_counts("a=? AND b=? AND c>?"), (2, 1))
        self.assertEqual(P.search_constraint_counts("b>? AND b<?"), (0, 2))
        self.assertEqual(P.search_constraint_counts("rowid=?"), (1, 0))
        self.assertEqual(P.search_constraint_counts("x>=? AND y<=?"), (0, 2))
        self.assertEqual(P.search_constraint_counts(""), (0, 0))
        self.assertEqual(P.search_constraint_counts("ANY(symbol) AND ts>?"), (0, 1))


class TestTokenize(unittest.TestCase):
    def test_kinds(self):
        toks = P.tokenize("SELECT \"a b\", `c`, [d], 'it''s', 1.5e3, .5, ?1, :name, @x, $y FROM t WHERE x<=1 AND y<>2 "
                          "OR z!=3 AND w||v >= 4;")
        kinds = [k for k, _t in toks]
        self.assertEqual(toks[0], ("kw", "SELECT"))
        self.assertIn(("id", "a b"), toks)
        self.assertIn(("id", "c"), toks)
        self.assertIn(("id", "d"), toks)
        self.assertIn(("str", "'it''s'"), toks)
        self.assertIn(("num", "1.5e3"), toks)
        self.assertIn(("num", ".5"), toks)
        for p in ("?1", ":name", "@x", "$y"):
            self.assertIn(("param", p), toks)
        for op in ("<=", "<>", "!=", "||", ">=", ";"):
            self.assertIn(("op", op), toks)
        self.assertEqual(kinds.count("kw"), 6)
        self.assertEqual(P.tokenize("select from")[1], ("kw", "FROM"))
        self.assertEqual(P.tokenize("  "), [])
        with self.assertRaises(ValueError):
            P.tokenize("SELECT # FROM t")


class TestParse(unittest.TestCase):
    def terms(self, sql):
        p = P.parse_statement(sql, SCHEMA)
        return [(t["table"], t["column"], t["kind"]) for t in p["terms"]], p

    def test_select_eq_order(self):
        terms, p = self.terms("SELECT * FROM orders WHERE user_id = ? AND status = ? ORDER BY created_at DESC LIMIT 50")
        self.assertEqual(terms, [("orders", "user_id", "eq"), ("orders", "status", "eq")])
        self.assertEqual(p["order_by"], [("orders", "created_at", True)])
        self.assertTrue(p["select_star"])
        self.assertEqual(p["kind"], "select")
        self.assertEqual(p["tables"], [{"table": "orders", "alias": "orders"}])

    def test_range_between_in_is(self):
        terms, p = self.terms("SELECT kind, count(*) FROM events WHERE ts BETWEEN ? AND ? AND level IN ('a','b') "
                              "AND notified IS NULL AND bot_id > 3 AND ? < user_id AND 5 = kind GROUP BY kind")
        self.assertEqual(terms, [("events", "ts", "range"), ("events", "level", "eq"), ("events", "notified", "eq"),
                                 ("events", "bot_id", "range"), ("events", "user_id", "range"),
                                 ("events", "kind", "eq")])
        self.assertEqual(p["group_by"], [("events", "kind", False)])
        self.assertFalse(p["select_star"])
        self.assertEqual(p["unusable"], [])                                     # BETWEEN 里的 AND 不切开
        self.assertEqual(p["terms"][0]["text"], "ts BETWEEN ? AND ?")

    def test_unusable_terms(self):
        terms, p = self.terms("SELECT * FROM users WHERE email LIKE ? AND (name = ? OR tier = ?) AND date(created_at) = ? "
                              "AND status NOT IN ('x') AND id IS NOT NULL AND tier COLLATE NOCASE = ? AND name = tier || ?")
        self.assertEqual(terms, [])
        reasons = [u["reason"] for u in p["unusable"]]
        self.assertEqual(len(reasons), 7)
        self.assertIn("LIKE", reasons[0])
        self.assertIn("OR", reasons[1])
        self.assertIn("函数", reasons[2])
        self.assertIn("NOT", reasons[3])
        self.assertIn("IS NOT", reasons[4])
        self.assertIn("COLLATE", reasons[5])
        self.assertIn("右侧", reasons[6])

    def test_top_level_or_and_bare_terms(self):
        terms, p = self.terms("SELECT * FROM orders WHERE user_id = ? OR status = ?")       # 不带括号的顶层 OR
        self.assertEqual((terms, [u["reason"] for u in p["unusable"]]), ([], ["顶层 OR（单个索引无法覆盖）"]))
        terms, p = self.terms("SELECT * FROM orders WHERE user_id IN (SELECT id FROM users WHERE name = 'a' OR tier = 'b')")
        self.assertEqual((terms, p["unusable"]), ([("orders", "user_id", "eq")], []))   # 子查询里的 OR 不是顶层
        terms, p = self.terms("SELECT * FROM orders WHERE status AND 1 AND user_id = ?")   # 裸列 / 字面量：不可用，不崩
        self.assertEqual((terms, [u["text"] for u in p["unusable"]]), ([("orders", "user_id", "eq")], ["status", "1"]))

    def test_join_unaliased_subquery_on(self):
        """JOIN 一个不带别名的子查询再 ON（合法 SQL）：子查询记不可用，ON 条件照常切出来；在线程里跑，防止解析卡死拖住整套测试。"""
        import threading
        box = {}
        sql = "SELECT * FROM orders JOIN (SELECT id AS bid FROM bots) ON bid = orders.bot_id WHERE orders.user_id = ?"
        t = threading.Thread(target=lambda: box.update(p=P.parse_statement(sql, SCHEMA)), daemon=True)
        t.start()
        t.join(10)
        self.assertIn("p", box, "parse_statement did not return")
        p = box["p"]
        self.assertEqual([(x["table"], x["column"], x["kind"]) for x in p["terms"]], [("orders", "user_id", "eq")])
        self.assertEqual([u["text"] for u in p["unusable"]], ["FROM (subquery)", "bid = orders . bot_id"])

    def test_parenthesized_term_and_negative_number(self):
        terms, _p = self.terms("SELECT id FROM trades WHERE (user_id = ?) AND price > -5 AND qty = NULL")
        self.assertEqual(terms, [("trades", "user_id", "eq"), ("trades", "price", "range"), ("trades", "qty", "eq")])

    def test_join_aliases(self):
        terms, p = self.terms("SELECT o.id, b.name FROM orders o JOIN bots AS b ON b.id = o.bot_id "
                              "WHERE o.user_id = ? AND o.created_at >= ? ORDER BY b.name")
        self.assertEqual(terms, [("bots", "id", "join"), ("orders", "bot_id", "join"), ("orders", "user_id", "eq"),
                                 ("orders", "created_at", "range")])
        self.assertEqual(p["tables"], [{"table": "orders", "alias": "o"}, {"table": "bots", "alias": "b"}])
        self.assertEqual(p["order_by"], [("bots", "name", False)])

    def test_comma_join_and_ambiguous_columns(self):
        terms, p = self.terms("SELECT * FROM orders, bots WHERE status = ? AND symbol = 'x' AND orders.side = ?")
        self.assertEqual(terms, [("orders", "side", "eq")])           # status / symbol 两表都有 → 不猜
        self.assertEqual(len(p["unusable"]), 2)
        terms, _p = self.terms("SELECT * FROM orders o LEFT JOIN trades t ON t.order_id = o.id WHERE t.fee > 1")
        self.assertEqual(terms, [("trades", "order_id", "join"), ("orders", "id", "join"), ("trades", "fee", "range")])
        _t, p = self.terms("SELECT * FROM orders INNER JOIN bots ON bots.id = orders.bot_id CROSS JOIN users")
        self.assertEqual([t["table"] for t in p["tables"]], ["orders", "bots", "users"])

    def test_update_delete_insert(self):
        terms, p = self.terms("UPDATE orders SET status = ?, filled = coalesce(?, 0), updated_at = ? WHERE id = ?")
        self.assertEqual(p["kind"], "update")
        self.assertEqual(p["set_columns"], ["status", "filled", "updated_at"])
        self.assertEqual(terms, [("orders", "id", "eq")])
        _t, p = self.terms("UPDATE OR REPLACE bots AS b SET status = 'x' WHERE b.user_id = ?")
        self.assertEqual((p["tables"][0]["alias"], p["terms"][0]["column"]), ("b", "user_id"))
        terms, p = self.terms("DELETE FROM events WHERE ts < ?")
        self.assertEqual((p["kind"], terms), ("delete", [("events", "ts", "range")]))
        _t, p = self.terms("DELETE FROM events")
        self.assertEqual(p["terms"], [])
        _t, p = self.terms("INSERT INTO trades(order_id, ts) VALUES (?, ?)")
        self.assertEqual((p["kind"], p["table"], p["insert_columns"]), ("insert", "trades", ["order_id", "ts"]))
        _t, p = self.terms("REPLACE INTO settings VALUES (?,?,?)")
        self.assertEqual((p["kind"], p["table"], p["insert_columns"]), ("insert", "settings", []))
        _t, p = self.terms("INSERT OR IGNORE INTO users(email) VALUES (?)")
        self.assertEqual(p["table"], "users")

    def test_other_and_cte_and_subquery(self):
        self.assertEqual(P.parse_statement("PRAGMA page_size", SCHEMA)["kind"], "other")
        self.assertEqual(P.parse_statement("", SCHEMA)["kind"], "other")
        p = P.parse_statement("WITH x AS (SELECT 1) SELECT * FROM x", SCHEMA)
        self.assertEqual((p["kind"], p["unusable"][0]["reason"]), ("select", "CTE 不解析"))
        p = P.parse_statement("SELECT * FROM (SELECT * FROM orders) q WHERE q.id = ?", SCHEMA)
        self.assertIn("子查询", p["unusable"][0]["reason"])

    def test_order_by_expression_and_asc(self):
        _t, p = self.terms("SELECT * FROM trades WHERE symbol = ? ORDER BY ts ASC, price * qty DESC, t.x")
        self.assertEqual(p["order_by"][0], ("trades", "ts", False))
        self.assertEqual(p["order_by"][1], (None, "price * qty", True))
        self.assertEqual(p["order_by"][2], (None, "t . x", False))

    def test_has_star(self):
        self.assertTrue(P.parse_statement("SELECT * FROM t", SCHEMA)["select_star"])
        self.assertTrue(P.parse_statement("SELECT o.* FROM orders o", SCHEMA)["select_star"])
        self.assertTrue(P.parse_statement("SELECT DISTINCT * FROM orders", SCHEMA)["select_star"])
        self.assertTrue(P.parse_statement("SELECT id, * FROM orders", SCHEMA)["select_star"])
        self.assertFalse(P.parse_statement("SELECT count(*), price * qty FROM orders", SCHEMA)["select_star"])


class TestCandidates(unittest.TestCase):
    def cand(self, sql, table):
        return P.candidate_columns(P.parse_statement(sql, SCHEMA), table)

    def test_order_eq_range_sort(self):
        cols, why = self.cand("SELECT * FROM trades WHERE ts >= ? AND user_id = ? AND fee > ? AND symbol IN (?, ?) "
                              "ORDER BY ts DESC", "trades")
        self.assertEqual(cols, ["user_id", "symbol", "ts"])
        self.assertEqual(why, {"eq": ["user_id", "symbol"], "range": ["ts"], "order": []})
        cols, why = self.cand("SELECT * FROM orders WHERE user_id = ? AND status = ? ORDER BY created_at DESC", "orders")
        self.assertEqual((cols, why["order"]), (["user_id", "status", "created_at"], ["created_at"]))
        cols, _w = self.cand("SELECT * FROM orders WHERE user_id = ? AND user_id = ? ORDER BY user_id", "orders")
        self.assertEqual(cols, ["user_id"])

    def test_group_by_and_mixed_direction(self):
        self.assertEqual(self.cand("SELECT symbol, count(*) FROM trades WHERE ts >= ? GROUP BY symbol", "trades")[0],
                         ["ts", "symbol"])
        self.assertEqual(self.cand("SELECT * FROM trades WHERE user_id = ? ORDER BY ts DESC, id ASC", "trades")[0],
                         ["user_id"])
        self.assertEqual(self.cand("SELECT * FROM trades WHERE user_id = ? ORDER BY ts, id", "trades")[0],
                         ["user_id", "ts", "id"])
        self.assertEqual(self.cand("SELECT * FROM trades WHERE user_id = ? GROUP BY symbol ORDER BY ts", "trades")[0],
                         ["user_id", "ts"])

    def test_order_on_other_table_not_appended(self):
        cols, _w = self.cand("SELECT * FROM orders o JOIN bots b ON b.id = o.bot_id WHERE o.status = ? ORDER BY b.name",
                             "orders")
        self.assertEqual(cols, ["status"])

    def test_join_fallback_only_without_where_terms(self):
        sql = "SELECT * FROM orders o JOIN trades t ON t.order_id = o.id WHERE o.user_id = ?"
        self.assertEqual(self.cand(sql, "trades")[0], ["order_id"])
        self.assertEqual(self.cand(sql, "orders")[0], ["user_id"])
        self.assertEqual(self.cand("SELECT * FROM users WHERE email LIKE ?", "users")[0], [])

    def test_referenced_and_covers(self):
        sql = "SELECT symbol, count(*), sum(price * qty) FROM trades WHERE ts >= ? GROUP BY symbol"
        self.assertEqual(P.referenced_columns(sql, SCHEMA, "trades"), ["symbol", "price", "qty", "ts"])
        self.assertEqual(P.referenced_columns("SELECT * FROM bots WHERE id = 1", SCHEMA, "bots"), SCHEMA["bots"])
        self.assertEqual(P.referenced_columns("SELECT o.id, b.name FROM orders o JOIN bots b ON b.id = o.bot_id",
                                              SCHEMA, "bots"), ["name", "id"])
        self.assertTrue(P.covers(["a", "b", "c"], ["a", "b"]))
        self.assertTrue(P.covers(["a"], ["a"]))
        self.assertFalse(P.covers(["a"], ["a", "b"]))
        self.assertFalse(P.covers(["b", "a"], ["a", "b"]))


class TestRecordBytes(unittest.TestCase):
    def test_value_and_varint_boundaries(self):
        self.assertEqual([P.varint_len(v) for v in (0, 127, 128, 16383, 16384, 2 ** 56, 2 ** 63)], [1, 1, 2, 2, 3, 9, 9])
        ints = {0: 0, 1: 0, 2: 1, 127: 1, -128: 1, 128: 2, -129: 2, 32767: 2, 32768: 3, 2 ** 23 - 1: 3, 2 ** 23: 4,
                -2 ** 31: 4, 2 ** 31: 6, 2 ** 47 - 1: 6, 2 ** 47: 8, -2 ** 63: 8}
        for v, n in ints.items():
            self.assertEqual(P.value_bytes(v), n, v)
        self.assertEqual([P.value_bytes(v) for v in (None, True, False, 1.5, "abc", "中", b"xy")], [0, 0, 0, 8, 3, 3, 2])
        self.assertEqual([P.serial_type_len(v) for v in (1, "a" * 57, "a" * 58, b"x" * 58, b"x" * 57, None)],
                         [1, 1, 2, 2, 1, 1])

    def test_matches_sqlite_payload(self):
        """Σ record_bytes == dbstat 统计的表 / 索引负载字节（真 SQLite 对账，逐字节相等）。"""
        path = os.path.join(tmpdir(self), "r.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE t(a, b, c)")
        rows = [(i * 37 - 500, f"s{i}" * (i % 7), (i % 5) * 0.5 if i % 3 else None) for i in range(300)]
        rows += [(2 ** 40, "中文" * 30, b"\x00" * 70), (1, "", True), (0, None, -(2 ** 33))]
        conn.executemany("INSERT INTO t VALUES (?,?,?)", rows)
        conn.execute("CREATE INDEX ix ON t(b, a)")
        conn.commit()
        payload = dict(conn.execute("SELECT name, sum(payload) FROM dbstat GROUP BY name"))
        stored = conn.execute("SELECT a, b, c FROM t ORDER BY rowid").fetchall()
        self.assertEqual(sum(P.record_bytes(r) for r in stored), payload["t"])
        idx = conn.execute("SELECT b, a, rowid FROM t").fetchall()
        self.assertEqual(sum(P.record_bytes(r) for r in idx), payload["ix"])
        self.assertEqual(P.index_entry_bytes(("ab", 5), 300), P.record_bytes(("ab", 5, 300)) + 1 + 2)
        self.assertEqual(P.index_entry_bytes(("x" * 200,), 1), P.record_bytes(("x" * 200, 1)) + 2 + 2)
        for n in (126, 127):                       # 头长 varint 把自己也算进去：127 列时头长 129，varint 要 2 字节
            conn.execute(f"CREATE TABLE w{n}({', '.join(f'c{i}' for i in range(n))})")
            conn.execute(f"INSERT INTO w{n} VALUES ({', '.join('?' * n)})", [None] * n)
            conn.execute(f"INSERT INTO w{n} VALUES ({', '.join('?' * n)})", list(range(n)))
        conn.commit()
        payload = dict(conn.execute("SELECT name, sum(payload) FROM dbstat GROUP BY name"))
        for n in (126, 127):
            self.assertEqual(sum(P.record_bytes(r) for r in conn.execute(f"SELECT * FROM w{n}")), payload[f"w{n}"], n)
        conn.close()


if __name__ == "__main__":
    unittest.main()
