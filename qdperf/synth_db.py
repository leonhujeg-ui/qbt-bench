"""合成交易库：像一个真实量化交易系统的 SQLite 库，同 seed 同结果。

    python3.12 -m qdperf.synth_db --out /tmp/x.db                 # 全量：users 1 万、bots 5 万、orders 200 万、trades 500 万、
                                                                  # settings 300、events 100 万（缺省 seed 20261022）
    python3.12 -m qdperf.synth_db --out /tmp/x.db --scale 0.01    # 按比例缩小（settings 固定 300）
    python3.12 -m qdperf.synth_db --out /tmp/x.db --fingerprint   # 生成后读回整库算逻辑指纹

确定性：每张表一个 random.Random(f"{seed}:{表名}")（字符串种子走 sha512，与 PYTHONHASHSEED 无关），只用 random() 与
四则运算 / round()（不用 libm），插入顺序固定；逻辑指纹 = 每张表按 rowid 顺序逐行 repr 的 sha256。

故意留下的“病”（体检器应当查出来）：
- bots.user_id / orders.bot_id / trades.user_id / trades.bot_id / events.* 外键列没有索引；
- settings 没有主键；
- ANALYZE 在 orders 只插了一半时跑过一次，之后再没跑 → sqlite_stat1 过期（trades / events 的统计缺失）；
- 清理过最老的 8% events 但没 VACUUM → freelist 有空闲页（碎片）；
- 日志模式 WAL（与被测系统一致）。
"""
import argparse
import hashlib
import os
import random
import sqlite3
import sys
import time
from array import array

from qdperf import common

DEFAULT_SEED = 20261022
FULL_ROWS = {"users": 10_000, "bots": 50_000, "orders": 2_000_000, "trades": 5_000_000, "settings": 300,
             "events": 1_000_000}
TABLES = ("users", "bots", "settings", "orders", "trades", "events")
T0 = 1_700_000_000                    # 2023-11-14 22:13:20 UTC
DAY = 86_400
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "DOTUSDT",
           "LINKUSDT", "MATICUSDT", "LTCUSDT", "TRXUSDT", "ATOMUSDT", "ETCUSDT", "XLMUSDT", "NEARUSDT", "APTUSDT",
           "ARBUSDT", "OPUSDT")
BASE_PRICE = (65000.0, 3400.0, 150.0, 580.0, 0.6, 0.45, 0.15, 35.0, 7.0, 15.0, 0.7, 80.0, 0.12, 9.0, 27.0, 0.11,
              5.0, 9.0, 1.1, 2.4)
STRATEGIES = ("grid", "ma_cross", "dca", "breakout", "mean_revert", "trend_stop")
EVENT_KINDS = ("login", "order", "fill", "alert", "risk", "system")

SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL, name TEXT, tier TEXT, status TEXT,
                    created_at INTEGER);
CREATE TABLE bots (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), name TEXT, strategy TEXT,
                   symbol TEXT, status TEXT, created_at INTEGER, updated_at INTEGER, config TEXT);
CREATE TABLE settings (key TEXT NOT NULL, value TEXT, updated_at INTEGER);
CREATE TABLE orders (id INTEGER PRIMARY KEY, bot_id INTEGER NOT NULL REFERENCES bots(id),
                     user_id INTEGER NOT NULL REFERENCES users(id), symbol TEXT, side TEXT, type TEXT, price REAL,
                     qty REAL, filled REAL, status TEXT, created_at INTEGER, updated_at INTEGER);
CREATE TABLE trades (id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id),
                     bot_id INTEGER NOT NULL REFERENCES bots(id), user_id INTEGER NOT NULL REFERENCES users(id),
                     symbol TEXT, side TEXT, price REAL, qty REAL, fee REAL, ts INTEGER);
CREATE TABLE events (id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id), bot_id INTEGER REFERENCES bots(id),
                     kind TEXT, level TEXT, notified INTEGER, ts INTEGER, payload TEXT);
"""
INDEXES = ("CREATE UNIQUE INDEX ux_users_email ON users(email)",
           "CREATE INDEX idx_orders_user ON orders(user_id)",
           "CREATE INDEX idx_trades_order ON trades(order_id)")


def rows_for(scale):
    """各表行数：settings 固定 300，其余按比例（至少 1 行）。"""
    if not 0 < scale <= 1:
        raise ValueError("scale must be in (0, 1]")
    return {t: n if t == "settings" else max(1, int(round(n * scale))) for t, n in FULL_ROWS.items()}


def _pick(r, weighted):
    """weighted = ((值, 累计权重), ...)，累计权重最后一项为 1.0。"""
    for value, cum in weighted:
        if r < cum:
            return value
    return weighted[-1][0]


def _cum(*pairs):
    total, out = 0.0, []
    for value, w in pairs:
        total += w
        out.append((value, total))
    return tuple(out)


TIER = _cum(("free", 0.70), ("pro", 0.25), ("vip", 0.05))
USER_STATUS = _cum(("active", 0.92), ("suspended", 0.05), ("closed", 0.03))
BOT_STATUS = _cum(("running", 0.35), ("paused", 0.40), ("stopped", 0.25))
ORDER_STATUS = _cum(("filled", 0.70), ("cancelled", 0.22), ("open", 0.05), ("partial", 0.03))
LEVEL = _cum(("info", 0.80), ("warn", 0.15), ("alert", 0.05))


# ---- 各表行生成器（每个都只依赖自己的 Random 与前面表的定长数组） --------------------------------
def gen_users(seed, n):
    rnd = random.Random(f"{seed}:users").random
    for i in range(1, n + 1):
        created = T0 + (i * 365 * DAY) // n + int(rnd() * 3600)
        yield (i, f"u{i:07d}@example.test", f"user{i}", _pick(rnd(), TIER), _pick(rnd(), USER_STATUS), created)


def gen_bots(seed, n, n_users, bot_user, bot_symbol):
    """bot 的用户分布偏斜（r² 让小 id 用户有更多 bot）；bot_user / bot_symbol 顺便填好供后面表使用。"""
    rnd = random.Random(f"{seed}:bots").random
    for i in range(1, n + 1):
        user = 1 + int(n_users * rnd() * rnd())
        sym = int(rnd() * len(SYMBOLS))
        bot_user.append(user)
        bot_symbol.append(sym)
        created = T0 + (i * 300 * DAY) // n
        updated = created + int(rnd() * 60 * DAY)
        cfg = f'{{"size":{round(0.01 + rnd(), 3)},"grid":{1 + int(rnd() * 50)},"stop":{round(rnd() * 0.1, 4)}}}'
        yield (i, user, f"bot{i}", STRATEGIES[int(rnd() * len(STRATEGIES))], SYMBOLS[sym], _pick(rnd(), BOT_STATUS),
               created, updated, cfg)


def gen_settings(seed, n):
    rnd = random.Random(f"{seed}:settings").random
    groups = ("risk", "fee", "notify", "ui", "engine", "api")
    for i in range(n):
        yield (f"cfg.{groups[i % len(groups)]}.k{i:03d}", str(round(rnd() * 1000, 3)), T0 + int(rnd() * 365 * DAY))


def gen_orders(seed, lo, hi, n, bot_user, bot_symbol, order_bot):
    """id 在 [lo, hi)；时间随 id 单调（180 天铺满 n 条）；order_bot 依次追加（供 trades 使用）。"""
    rnd = random.Random(f"{seed}:orders:{lo}").random
    n_bots = len(bot_user)
    step = 180 * DAY / n
    for i in range(lo, hi):
        bot = 1 + int(n_bots * rnd() ** 1.5)
        order_bot.append(bot)
        sym = bot_symbol[bot - 1]
        price = round(BASE_PRICE[sym] * (0.8 + 0.4 * rnd()), 6)
        qty = round(0.001 + 2.0 * rnd(), 4)
        status = _pick(rnd(), ORDER_STATUS)
        filled = qty if status == "filled" else (round(qty * rnd(), 4) if status == "partial" else 0.0)
        created = T0 + 120 * DAY + int(i * step) + int(rnd() * step)
        yield (i, bot, bot_user[bot - 1], SYMBOLS[sym], "buy" if rnd() < 0.5 else "sell",
               "limit" if rnd() < 0.8 else "market", price, qty, filled, status, created, created + int(rnd() * 3600))


def gen_trades(seed, n, n_orders, order_bot, bot_user, bot_symbol):
    """trades 按时间顺序引用 orders（order_id 随 id 单调 + 小抖动）。"""
    rnd = random.Random(f"{seed}:trades").random
    step = 180 * DAY / n
    for i in range(1, n + 1):
        oid = min(n_orders, 1 + (i * n_orders) // n + int(rnd() * 3))
        bot = order_bot[oid - 1]
        sym = bot_symbol[bot - 1]
        price = round(BASE_PRICE[sym] * (0.8 + 0.4 * rnd()), 6)
        qty = round(0.001 + rnd(), 4)
        yield (i, oid, bot, bot_user[bot - 1], SYMBOLS[sym], "buy" if rnd() < 0.5 else "sell", price, qty,
               round(price * qty * 0.0004, 8), T0 + 120 * DAY + int(i * step) + int(rnd() * 600))


def gen_events(seed, n, n_users, n_bots):
    rnd = random.Random(f"{seed}:events").random
    step = 200 * DAY / n
    recent = int(n * 0.98)
    for i in range(1, n + 1):
        level = _pick(rnd(), LEVEL)
        kind = EVENT_KINDS[int(rnd() * len(EVENT_KINDS))]
        notified = 0 if (level == "alert" and i > recent) else 1
        payload = f'{{"msg":"{kind} #{i}","v":{round(rnd() * 100, 3)},"src":"svc{int(rnd() * 8)}"}}'
        yield (i, 1 + int(n_users * rnd() * rnd()), 1 + int(n_bots * rnd()), kind, level, notified,
               T0 + 100 * DAY + int(i * step) + int(rnd() * step), payload)


# ---- 生成 ------------------------------------------------------------------------------
def _insert(conn, table, rows, ncols, batch=50_000):
    sql = f"INSERT INTO {table} VALUES ({','.join('?' * ncols)})"
    buf = []
    for r in rows:
        buf.append(r)
        if len(buf) >= batch:
            conn.executemany(sql, buf)
            buf.clear()
    if buf:
        conn.executemany(sql, buf)


def generate(path, seed=DEFAULT_SEED, scale=1.0, force=False):
    """生成到 path（已存在且 force=False 时抛 FileExistsError），返回生成报告 dict。"""
    if os.path.exists(path) and not force:
        raise FileExistsError(path)
    for suffix in ("", "-wal", "-shm", "-journal"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    n = rows_for(scale)
    t_all = time.perf_counter()
    phases = {}
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("PRAGMA page_size=4096")
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.executescript(SCHEMA)
        bot_user, bot_symbol, order_bot = array("i"), array("i"), array("i")
        half = n["orders"] // 2 + 1
        steps = [("users", lambda: _insert(conn, "users", gen_users(seed, n["users"]), 6)),
                 ("bots", lambda: _insert(conn, "bots", gen_bots(seed, n["bots"], n["users"], bot_user, bot_symbol), 9)),
                 ("settings", lambda: _insert(conn, "settings", gen_settings(seed, n["settings"]), 3)),
                 ("orders_1", lambda: _insert(conn, "orders", gen_orders(seed, 1, half, n["orders"], bot_user,
                                                                          bot_symbol, order_bot), 12)),
                 ("indexes_analyze", lambda: [conn.execute(sql) for sql in INDEXES + ("ANALYZE",)]),
                 ("orders_2", lambda: _insert(conn, "orders", gen_orders(seed, half, n["orders"] + 1, n["orders"],
                                                                          bot_user, bot_symbol, order_bot), 12)),
                 ("trades", lambda: _insert(conn, "trades", gen_trades(seed, n["trades"], n["orders"], order_bot,
                                                                        bot_user, bot_symbol), 10)),
                 ("events", lambda: _insert(conn, "events", gen_events(seed, n["events"], n["users"], n["bots"]), 8)),
                 ("housekeeping", lambda: conn.execute("DELETE FROM events WHERE id <= ?", (int(n["events"] * 0.08),)))]
        for name, fn in steps:
            t0 = time.perf_counter()
            conn.execute("BEGIN")
            fn()
            conn.execute("COMMIT")
            phases[name] = round(time.perf_counter() - t0, 3)
        conn.execute("PRAGMA journal_mode=WAL")
    finally:
        conn.close()
    return {"path": path, "seed": seed, "scale": scale, "rows": n,
            "rows_after_housekeeping": dict(n, events=n["events"] - int(n["events"] * 0.08)),
            "seconds": round(time.perf_counter() - t_all, 2), "phases_seconds": phases,
            "size_bytes": os.path.getsize(path), "sqlite_version": sqlite3.sqlite_version}


def fingerprint(path):
    """逻辑指纹：每张表按 rowid 顺序逐行 repr 的 sha256（只读打开）。返回 {表: {rows, sha256}, "all": sha256}。"""
    conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    out, whole = {}, hashlib.sha256()
    try:
        for t in TABLES:
            h, k = hashlib.sha256(), 0
            for row in conn.execute(f"SELECT * FROM {t} ORDER BY rowid"):
                h.update(repr(row).encode())
                h.update(b"\n")
                k += 1
            out[t] = {"rows": k, "sha256": h.hexdigest()}
            whole.update(f"{t}:{out[t]['sha256']}\n".encode())
    finally:
        conn.close()
    out["all"] = whole.hexdigest()
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3.12 -m qdperf.synth_db", description="生成合成交易库（同 seed 同结果）")
    ap.add_argument("--out", default=None, help="输出库文件（缺省 tempfile 目录下 qdperf-synth-<seed>-<scale>.db）")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--force", action="store_true", help="输出文件已存在时覆盖")
    ap.add_argument("--fingerprint", action="store_true", help="生成后读回整库算逻辑指纹")
    ap.add_argument("--json", default=None, help="把生成报告写成 JSON")
    args = ap.parse_args(argv)
    out = args.out or os.path.join(common.scratch_dir(), f"qdperf-synth-{args.seed}-{args.scale:g}.db")
    try:
        rep = generate(out, args.seed, args.scale, args.force)
    except (FileExistsError, ValueError) as exc:
        print(f"SYNTH_DB ERROR {type(exc).__name__}: {exc}")
        return 2
    if args.fingerprint:
        rep["fingerprint"] = fingerprint(out)
    fp = rep.get("fingerprint", {}).get("all", "-")[:16]
    print(f"SYNTH_DB rows={sum(rep['rows_after_housekeeping'].values())} seconds={rep['seconds']} "
          f"size_mb={rep['size_bytes'] / 1e6:.1f} fingerprint={fp} path={out}")
    print(common.text_table(["表", "行数"], [(t, rep["rows_after_housekeeping"][t]) for t in TABLES]))
    if args.json:
        common.write_json(args.json, rep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
