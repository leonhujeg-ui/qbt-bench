"""SQLite 落盘：订单 / 成交 / 持仓 / 账户键值 / 事件，写穿（每次状态变化立刻落库），进程重启后 Engine 从这里恢复。

每行存一个 JSON 文档加几列索引字段；clientOrderId 在 orders.cid 上有 UNIQUE 约束——幂等的最后一道闸在数据库，
不只在内存字典里（突变掉内存里的查重，这里仍会拒绝第二张单）。
"""
import contextlib
import json
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY, cid TEXT UNIQUE, symbol TEXT NOT NULL, status TEXT NOT NULL,
                                   created INTEGER NOT NULL, doc TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS orders_symbol_status ON orders (symbol, status);
CREATE TABLE IF NOT EXISTS fills (id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL, symbol TEXT NOT NULL,
                                  ts INTEGER NOT NULL, doc TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS fills_order ON fills (order_id);
CREATE TABLE IF NOT EXISTS positions (symbol TEXT PRIMARY KEY, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, kind TEXT NOT NULL,
                                   doc TEXT NOT NULL);
"""


class DuplicateClientId(Exception):
    pass


class Store:
    def __init__(self, path=":memory:"):
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)

    def close(self):
        with self.lock:
            self.conn.close()

    @contextlib.contextmanager
    def transaction(self):
        """一组写操作要么全落、要么全不落（撮合一个 tick 产生的多笔成交 + 持仓 + 账户）。"""
        with self.lock:
            self.conn.execute("BEGIN")
            try:
                yield
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    # ---- 写 ----
    def save_order(self, o):
        with self.lock:
            try:
                self.conn.execute("INSERT INTO orders (id, cid, symbol, status, created, doc) VALUES (?,?,?,?,?,?) "
                                  "ON CONFLICT(id) DO UPDATE SET cid=excluded.cid, symbol=excluded.symbol, status=excluded.status, "
                                  "created=excluded.created, doc=excluded.doc",
                                  (o["id"], o["cid"], o["symbol"], o["status"], o["created"], json.dumps(o)))
            except sqlite3.IntegrityError as e:
                raise DuplicateClientId(o["cid"]) from e

    def save_fill(self, f):
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO fills (id, order_id, symbol, ts, doc) VALUES (?,?,?,?,?)",
                              (f["id"], f["order_id"], f["symbol"], f["ts"], json.dumps(f)))

    def save_position(self, p):
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO positions (symbol, doc) VALUES (?,?)", (p["symbol"], json.dumps(p)))

    def set_kv(self, key, value):
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO kv (key, doc) VALUES (?,?)", (key, json.dumps(value)))

    def add_event(self, ts, kind, doc):
        with self.lock:
            cur = self.conn.execute("INSERT INTO events (ts, kind, doc) VALUES (?,?,?)", (ts, kind, json.dumps(doc)))
            return cur.lastrowid

    # ---- 读 ----
    def get_kv(self, key, default=None):
        with self.lock:
            row = self.conn.execute("SELECT doc FROM kv WHERE key=?", (key,)).fetchone()
        return default if row is None else json.loads(row[0])

    def load_orders(self):
        with self.lock:
            rows = self.conn.execute("SELECT doc FROM orders ORDER BY id").fetchall()
        return [json.loads(r[0]) for r in rows]

    def load_fills(self):
        with self.lock:
            rows = self.conn.execute("SELECT doc FROM fills ORDER BY id").fetchall()
        return [json.loads(r[0]) for r in rows]

    def load_positions(self):
        with self.lock:
            rows = self.conn.execute("SELECT doc FROM positions").fetchall()
        return [json.loads(r[0]) for r in rows]

    def events(self, since_seq=0, kind=None, limit=100000):
        with self.lock:
            if kind is None:
                rows = self.conn.execute("SELECT seq, ts, kind, doc FROM events WHERE seq>? ORDER BY seq LIMIT ?",
                                         (since_seq, limit)).fetchall()
            else:
                rows = self.conn.execute("SELECT seq, ts, kind, doc FROM events WHERE seq>? AND kind=? ORDER BY seq LIMIT ?",
                                         (since_seq, kind, limit)).fetchall()
        return [dict(json.loads(r[3]), seq=r[0], ts=r[1], kind=r[2]) for r in rows]

    def counts(self):
        with self.lock:
            return {t: self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    for t in ("orders", "fills", "positions", "events")}
