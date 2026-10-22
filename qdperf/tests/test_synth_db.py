"""synth_db 判据：同 seed 同结果、表 / 行数 / 外键一致、时间单调、故意留下的“病”都在、CLI 退出码。"""
import os
import sqlite3
import unittest

from qdperf import synth_db as S
from qdperf.tests._util import TINY_SCALE, captured, tmpdir


class TestRows(unittest.TestCase):
    def test_rows_for(self):
        full = S.rows_for(1.0)
        self.assertEqual(full, S.FULL_ROWS)
        self.assertEqual(full, {"users": 10_000, "bots": 50_000, "orders": 2_000_000, "trades": 5_000_000,
                                "settings": 300, "events": 1_000_000})
        small = S.rows_for(0.01)
        self.assertEqual(small, {"users": 100, "bots": 500, "orders": 20_000, "trades": 50_000, "settings": 300,
                                 "events": 10_000})
        self.assertEqual(S.rows_for(1e-9)["users"], 1)
        self.assertEqual(S.rows_for(0.00026)["users"], 3)               # 2.6 → round → 3（不是截断）
        for bad in (0, -0.1, 1.0001):
            with self.assertRaises(ValueError):
                S.rows_for(bad)

    def test_pick_and_cum(self):
        w = S._cum(("a", 0.5), ("b", 0.25), ("c", 0.25))
        self.assertEqual(w, (("a", 0.5), ("b", 0.75), ("c", 1.0)))
        self.assertEqual([S._pick(r, w) for r in (0.0, 0.49, 0.5, 0.74, 0.75, 0.999)], ["a", "a", "b", "b", "c", "c"])
        self.assertEqual(S._pick(1.0, w), "c")
        self.assertAlmostEqual(S.ORDER_STATUS[-1][1], 1.0)
        self.assertAlmostEqual(S.LEVEL[-1][1], 1.0)

    def test_generators_shape(self):
        users = list(S.gen_users(1, 50))
        self.assertEqual([u[0] for u in users], list(range(1, 51)))
        self.assertEqual(len(set(u[1] for u in users)), 50)
        self.assertTrue(all(len(u) == 6 for u in users))
        bu, bs, ob = S.array("i"), S.array("i"), S.array("i")
        bots = list(S.gen_bots(1, 200, 50, bu, bs))
        self.assertEqual(len(bu), 200)
        self.assertTrue(all(1 <= b[1] <= 50 and b[1] == bu[k] for k, b in enumerate(bots)))
        self.assertTrue(all(S.SYMBOLS[bs[k]] == b[4] for k, b in enumerate(bots)))
        orders = list(S.gen_orders(1, 1, 501, 500, bu, bs, ob))
        self.assertEqual(len(ob), 500)
        self.assertTrue(all(o[2] == bu[o[1] - 1] and o[3] == S.SYMBOLS[bs[o[1] - 1]] for o in orders))
        self.assertTrue(all(o[11] >= o[10] for o in orders))
        created = [o[10] for o in orders]
        self.assertTrue(all(b >= a - 180 * S.DAY / 500 for a, b in zip(created, created[1:])))
        for o in orders:
            if o[9] == "filled":
                self.assertEqual(o[8], o[7])
            elif o[9] in ("open", "cancelled"):
                self.assertEqual(o[8], 0.0)
            else:
                self.assertTrue(0 <= o[8] <= o[7])
        trades = list(S.gen_trades(1, 1500, 500, ob, bu, bs))
        self.assertTrue(all(1 <= t[1] <= 500 and t[2] == ob[t[1] - 1] and t[3] == bu[t[2] - 1] for t in trades))
        self.assertTrue(all(t[8] == round(t[6] * t[7] * 0.0004, 8) for t in trades))
        oids = [t[1] for t in trades]
        self.assertTrue(all(b >= a - 3 for a, b in zip(oids, oids[1:])))
        events = list(S.gen_events(1, 1000, 50, 200))
        self.assertTrue(all(e[5] == (0 if e[4] == "alert" and e[0] > 980 else 1) for e in events))
        self.assertEqual(len(list(S.gen_settings(1, 300))), 300)
        self.assertEqual(len({s[0] for s in S.gen_settings(1, 300)}), 300)


class TestGenerate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.dir = tempfile.mkdtemp(prefix="qdperf-synth-")
        cls.a = os.path.join(cls.dir, "a.db")
        cls.rep = S.generate(cls.a, seed=11, scale=TINY_SCALE)

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_same_seed_same_result(self):
        b = os.path.join(tmpdir(self), "b.db")
        S.generate(b, seed=11, scale=TINY_SCALE)
        fa, fb = S.fingerprint(self.a), S.fingerprint(b)
        self.assertEqual(fa, fb)
        c = os.path.join(tmpdir(self), "c.db")
        S.generate(c, seed=12, scale=TINY_SCALE)
        fc = S.fingerprint(c)
        self.assertNotEqual(fa["all"], fc["all"])
        self.assertNotEqual(fa["trades"]["sha256"], fc["trades"]["sha256"])
        self.assertEqual(fa["settings"]["rows"], 300)

    def test_report_and_counts(self):
        rep = self.rep
        n = S.rows_for(TINY_SCALE)
        self.assertEqual(rep["rows"], n)
        self.assertEqual(rep["rows_after_housekeeping"]["events"], n["events"] - int(n["events"] * 0.08))
        fp = S.fingerprint(self.a)
        for t in S.TABLES:
            self.assertEqual(fp[t]["rows"], rep["rows_after_housekeeping"][t], t)
        self.assertEqual(set(rep["phases_seconds"]), {"users", "bots", "settings", "orders_1", "indexes_analyze",
                                                      "orders_2", "trades", "events", "housekeeping"})
        self.assertEqual(rep["size_bytes"], os.path.getsize(self.a))
        self.assertEqual((rep["seed"], rep["scale"]), (11, TINY_SCALE))

    def test_planted_problems(self):
        conn = sqlite3.connect(f"file:{self.a}?mode=ro&immutable=1", uri=True)
        with open(self.a, "rb") as f:
            head = f.read(20)
        self.assertEqual((head[18], head[19]), (2, 2))                      # WAL
        self.assertFalse(os.path.exists(self.a + "-wal"))
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_schema WHERE type='index'")}
        self.assertEqual(names, {"ux_users_email", "idx_orders_user", "idx_trades_order"})
        self.assertEqual([r[5] for r in conn.execute("PRAGMA table_info(settings)")], [0, 0, 0])
        stat = dict(conn.execute("SELECT idx, stat FROM sqlite_stat1 WHERE idx IS NOT NULL"))
        n = S.rows_for(TINY_SCALE)
        self.assertEqual(int(stat["idx_orders_user"].split()[0]), n["orders"] // 2 + 1 - 1)   # ANALYZE 时只有一半
        self.assertNotIn("idx_trades_order", stat)                                            # 当时 trades 还是空的
        self.assertEqual(conn.execute("SELECT min(id) FROM events").fetchone()[0], int(n["events"] * 0.08) + 1)
        self.assertEqual(conn.execute("SELECT page_size FROM pragma_page_size").fetchone()[0], 4096)
        bad = conn.execute("SELECT count(*) FROM orders o JOIN bots b ON b.id = o.bot_id WHERE o.user_id != b.user_id")
        self.assertEqual(bad.fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT count(*) FROM trades WHERE order_id NOT IN (SELECT id FROM orders)")
                         .fetchone()[0], 0)
        conn.close()

    def test_exists_and_force(self):
        with self.assertRaises(FileExistsError):
            S.generate(self.a, seed=11, scale=TINY_SCALE)
        d = tmpdir(self)
        p = os.path.join(d, "f.db")
        for suffix in ("", "-wal", "-shm"):
            with open(p + suffix, "w") as f:
                f.write("junk")
        S.generate(p, seed=11, scale=TINY_SCALE, force=True)
        self.assertEqual(S.fingerprint(p), S.fingerprint(self.a))
        self.assertFalse(os.path.exists(p + "-shm"))


class TestCli(unittest.TestCase):
    def test_main(self):
        d = tmpdir(self)
        out = os.path.join(d, "x.db")
        js = os.path.join(d, "x.json")
        with captured() as (o, _e):
            rc = S.main(["--out", out, "--scale", str(TINY_SCALE), "--seed", "3", "--fingerprint", "--json", js])
        self.assertEqual(rc, 0)
        first = o.getvalue().splitlines()[0]
        self.assertRegex(first, r"^SYNTH_DB rows=\d+ seconds=[\d.]+ size_mb=[\d.]+ fingerprint=[0-9a-f]{16} path=")
        self.assertIn(f"rows={sum(S.rows_for(TINY_SCALE).values()) - int(S.rows_for(TINY_SCALE)['events'] * 0.08)} ",
                      first)
        self.assertTrue(os.path.exists(js))
        with captured() as (o, _e):
            rc = S.main(["--out", out, "--scale", str(TINY_SCALE)])
        self.assertEqual(rc, 2)
        self.assertTrue(o.getvalue().startswith("SYNTH_DB ERROR FileExistsError"))
        with captured() as (o, _e):
            rc = S.main(["--out", out, "--scale", "2", "--force"])
        self.assertEqual(rc, 2)
        self.assertIn("ValueError", o.getvalue())


if __name__ == "__main__":
    unittest.main()
