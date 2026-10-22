"""多进程 tick 抖动判据（G3）：引擎 / 面板 / 通知三个真进程共用一个 SQLite 库。

    python3.12 -m qdperf.tick_jitter run  [--T 0.05] [--seconds 8] [--q 20] [--wal off] [--busy 5] [--compute inline]
                                          [--mitigation none|replica|chunked|backoff] [--json OUT]
    python3.12 -m qdperf.tick_jitter scan [--quick] [--json OUT]

模型（全部参数可调，缺省值见 DEFAULTS；所有时长都以 T 为单位给出，判据只看形状 / 倍数，不写死毫秒）：
- 引擎：每 T 秒一个 tick = 读（按用户取挂单 + 最新价，走索引）→ 算（纯 Python 循环，校准到 compute_ratio·T；
  inline = 在引擎进程里（GIL 内），subprocess = 发给常驻子进程算完回传）→ 写（BEGIN IMMEDIATE 插订单 + 插成交 +
  更新 bot，COMMIT）。超时的 tick 结束后立刻开始下一个（不补发）。记每个 tick 的开始时刻与三段耗时。
- 面板：以 Q 次/秒开环发起轮询，线程池（panel_threads 个线程，像 ThreadingHTTPServer）里执行一条长查询
  （成交 JOIN 订单按用户汇总，扫描行数校准到耗时 ≈ panel_ratio·T）。
- 通知：泊松到达（notifier_rate 次/秒）的小写事务。
- WAL 开 / 关（关 = rollback journal：读持 SHARED 锁时写者提交要等读完）；busy_timeout；
- 缓解：replica（面板读只读副本，副本由面板进程每 replica_every 秒刷新：主库 → 内存 → 副本文件，只在内存拷贝那一步
  持有主库读锁；双缓冲）、
  chunked（长查询按 id 区间切块，每块 ≈ chunk_ratio·T，块之间释放锁并让出 chunk_pause 秒）、
  backoff（轮询退避：两次轮询的间隔 ≥ backoff_k × 上次查询耗时）、wal（把库切到 WAL：读不再挡写）。
- tick 间隔：相邻 tick 开始时刻之差，最后一个 tick 记 max(T, 它自己的耗时)（引擎卡死在最后一个 tick 里也算进分布）。
判据：tick 实际间隔的 p50 / p99 / 最大、被拖过 2T 的次数；scan 找出“面板轮询饿死引擎”的拐点（p99 > 2T 的最小 Q）。
"""
import argparse
import concurrent.futures
import json
import os
import random
import shutil
import sqlite3
import sys
import threading
import time

from qdperf import common
from qdperf import synth_db

FORMAT = "qdperf.tick_jitter.v1"
DEFAULTS = {"T": 0.05, "seconds": 8.0, "q": 0.0, "wal": True, "busy": 5.0, "compute": "inline", "mitigation": "none",
            "panel_ratio": 2.5, "compute_ratio": 0.3, "panel_threads": 4, "notifier_rate": 2.0, "bg_threads": 0,
            "replica_every": 1.0, "backoff_k": 19.0, "chunk_ratio": 0.2, "chunk_pause": 0.0, "seed": 0}
MITIGATIONS = ("none", "replica", "chunked", "backoff", "wal")
COMPUTE = ("inline", "subprocess")
Q_GRID = (0.0, 5.0, 20.0, 50.0)

ENGINE_READ = ("SELECT id, side, price, qty, filled FROM orders WHERE user_id = 7 ORDER BY id DESC LIMIT 20",
               "SELECT price FROM trades WHERE id = (SELECT max(id) FROM trades)")
ENGINE_WRITE = ("INSERT INTO orders(bot_id, user_id, symbol, side, type, price, qty, filled, status, created_at, updated_at) "
                "VALUES (42, 7, 'BTCUSDT', 'buy', 'limit', 65000.0, 0.01, 0, 'open', ?, ?)",
                "INSERT INTO trades(order_id, bot_id, user_id, symbol, side, price, qty, fee, ts) "
                "VALUES (last_insert_rowid(), 42, 7, 'BTCUSDT', 'buy', 65000.0, 0.01, 0.26, ?)",
                "UPDATE bots SET updated_at = ? WHERE id = 42")
PANEL_QUERY = ("SELECT o.user_id, count(*), sum(t.price * t.qty) FROM trades t JOIN orders o ON o.id = t.order_id "
               "WHERE t.id > ? AND t.id <= ? GROUP BY o.user_id")
NOTIFIER_WRITE = ("INSERT INTO events(user_id, bot_id, kind, level, notified, ts, payload) "
                  "VALUES (7, 42, 'alert', 'warn', 0, ?, '{}')",
                  "UPDATE events SET notified = 1 WHERE id = last_insert_rowid()")


# ---- CPU 段 ----------------------------------------------------------------------------
def burn(iterations):
    """纯 Python 的 CPU 密集段（GIL 内）。"""
    x = 0
    for i in range(iterations):
        x += i & 7
    return x


def calibrate(seconds=0.2):
    """本进程里 burn 每秒能跑多少次迭代。"""
    n, t0 = 20_000, time.perf_counter()
    done = 0
    while time.perf_counter() - t0 < seconds:
        burn(n)
        done += n
    return done / (time.perf_counter() - t0)


def compute_worker_main(conn):
    """subprocess 模式的常驻计算进程：收到秒数 → 按自己的校准换算成迭代数 → 算完回传。收到 None 退出。"""
    common.child_init()
    rate = calibrate()
    conn.send(rate)
    while True:
        seconds = conn.recv()
        if seconds is None:
            break
        conn.send(burn(int(seconds * rate)))
    common.child_finish()


class InlineCompute:
    def __init__(self):
        self.rate = calibrate()

    def run(self, seconds):
        return burn(int(seconds * self.rate))

    def close(self):
        pass


class SubprocessCompute:
    def __init__(self):
        ctx = common.spawn_context()
        self.conn, child = ctx.Pipe()
        self.proc = ctx.Process(target=compute_worker_main, args=(child,), daemon=True)
        self.proc.start()
        self.rate = self.conn.recv()

    def run(self, seconds):
        self.conn.send(seconds)
        return self.conn.recv()

    def close(self):
        self.conn.send(None)
        self.proc.join(10)


def _bg_spin(stop):
    while not stop.is_set():
        burn(10_000)


# ---- 引擎 ------------------------------------------------------------------------------
def engine_main(spec, out_path):
    common.child_init()
    res = run_engine(spec)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(res, f)
    common.child_finish()


def run_engine(spec):
    conn = sqlite3.connect(spec["db"], timeout=spec["busy"], isolation_level=None)
    comp = SubprocessCompute() if spec["compute"] == "subprocess" else InlineCompute()
    stop = threading.Event()
    bg = [threading.Thread(target=_bg_spin, args=(stop,), daemon=True) for _ in range(spec["bg_threads"])]
    for t in bg:
        t.start()
    T, cseconds = spec["T"], spec["compute_ratio"] * spec["T"]
    ticks = []
    common.sleep_until(spec["start_wall"])
    t0 = time.perf_counter()
    nxt = 0.0
    while nxt < spec["seconds"]:
        now = time.perf_counter() - t0
        if now < nxt:
            time.sleep(nxt - now)
        ticks.append(_tick(conn, comp, cseconds, t0) + [nxt])
        nxt = max(nxt + T, time.perf_counter() - t0)
    stop.set()
    comp.close()
    conn.close()
    return {"ticks": ticks, "compute_rate": comp.rate, "fields": ["start_s", "read_s", "compute_s", "write_s",
                                                                  "read_error", "write_error", "planned_s"]}


def _tick(conn, comp, cseconds, t0):
    start = time.perf_counter()
    rerr = werr = None
    try:
        for sql in ENGINE_READ:
            conn.execute(sql).fetchall()
    except sqlite3.OperationalError as exc:
        rerr = getattr(exc, "sqlite_errorname", None) or "SQLITE_ERROR"
    r1 = time.perf_counter()
    comp.run(cseconds)
    c1 = time.perf_counter()
    now = int(time.time())
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(ENGINE_WRITE[0], (now, now))
        conn.execute(ENGINE_WRITE[1], (now,))
        conn.execute(ENGINE_WRITE[2], (now,))
        conn.execute("COMMIT")
    except sqlite3.OperationalError as exc:
        werr = getattr(exc, "sqlite_errorname", None) or "SQLITE_ERROR"
        if conn.in_transaction:
            conn.execute("ROLLBACK")
    w1 = time.perf_counter()
    return [start - t0, r1 - start, c1 - r1, w1 - c1, rerr, werr]


# ---- 面板 ------------------------------------------------------------------------------
class Replica:
    """双缓冲只读副本：刷新线程把主库 backup 到不在用的那一份，再切换“当前”；读者按当前份读。"""

    def __init__(self, main_db, paths, busy):
        self.main, self.paths, self.busy = main_db, paths, busy
        self.cur = 0
        self.readers = [0, 0]
        self.lock = threading.Condition()
        self.refreshes = 0
        for p in paths:
            self._copy(p)

    def _copy(self, dst_path):
        """主库 → 内存（只在这一步持有主库的读锁，纯内存拷贝）→ 副本文件（写盘时已不碰主库）。"""
        src = sqlite3.connect(self.main, timeout=self.busy)
        mem = sqlite3.connect(":memory:")
        try:
            src.backup(mem)
        finally:
            src.close()
        dst = sqlite3.connect(dst_path)
        try:
            mem.backup(dst)
        finally:
            dst.close()
            mem.close()

    def acquire(self):
        with self.lock:
            k = self.cur
            self.readers[k] += 1
            return k

    def release(self, k):
        with self.lock:
            self.readers[k] -= 1
            self.lock.notify_all()

    def refresh(self):
        with self.lock:
            other = 1 - self.cur
            while self.readers[other]:
                self.lock.wait(0.5)
        self._copy(self.paths[other])
        with self.lock:
            self.cur = other
            self.refreshes += 1


def chunk_ranges(lo, hi, rows_per_chunk):
    """(lo, hi] 按 rows_per_chunk 切成连续区间。"""
    step = max(1, int(rows_per_chunk))
    out, a = [], lo
    while a < hi:
        b = min(hi, a + step)
        out.append((a, b))
        a = b
    return out


def merge_rows(acc, rows):
    """分块结果合并：按 user_id 累加 count 与 sum。"""
    for uid, cnt, total in rows:
        c, s = acc.get(uid, (0, 0.0))
        acc[uid] = (c + cnt, s + (total or 0.0))
    return acc


def panel_main(spec, out_path):
    common.child_init()
    res = run_panel(spec)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(res, f)
    common.child_finish()


def _poll(spec, local, replica):
    """一次面板轮询；返回 (耗时秒, 错误名或 None)。"""
    k = replica.acquire() if replica else None
    db = replica.paths[k] if replica else spec["db"]
    conns = local.__dict__.setdefault("conns", {})
    conn = conns.get(db)
    if conn is None:
        conn = conns[db] = sqlite3.connect(db, timeout=spec["busy"], isolation_level=None, check_same_thread=False)
    t0 = time.perf_counter()
    err = None
    lo, hi = spec["range"]
    try:
        if spec["mitigation"] == "chunked":
            acc = {}
            for a, b in chunk_ranges(lo, hi, spec["chunk_rows"]):
                merge_rows(acc, conn.execute(PANEL_QUERY, (a, b)).fetchall())
                if spec["chunk_pause"]:
                    time.sleep(spec["chunk_pause"])                            # 块之间让出（可选；实测见 REPORT §3.2）
        else:
            conn.execute(PANEL_QUERY, (lo, hi)).fetchall()
    except sqlite3.OperationalError as exc:
        err = getattr(exc, "sqlite_errorname", None) or "SQLITE_ERROR"
    finally:
        if replica:
            replica.release(k)
    return time.perf_counter() - t0, err


def run_panel(spec):
    replica = Replica(spec["db"], spec["replica_paths"], spec["busy"]) if spec["mitigation"] == "replica" else None
    local = threading.local()
    durs, errs, dropped = [], {}, 0
    stop = threading.Event()
    refresher = None
    if replica:
        refresher = threading.Thread(target=_refresh_loop, args=(replica, spec["replica_every"], stop), daemon=True)
    common.sleep_until(spec["start_wall"])
    if refresher:
        refresher.start()
    if spec["mitigation"] == "backoff":
        durs, errs = _poll_backoff(spec, local)
    else:
        durs, errs, dropped = _poll_open_loop(spec, local, replica)
    stop.set()
    if refresher:
        refresher.join(10)
    return {"durations": durs, "errors": errs, "dropped": dropped,
            "replica_refreshes": replica.refreshes if replica else 0}


def _refresh_loop(replica, every, stop):
    while not stop.wait(every):
        replica.refresh()


def _record(durs, errs, res):
    d, e = res
    durs.append(d)
    if e:
        errs[e] = errs.get(e, 0) + 1


def _poll_open_loop(spec, local, replica):
    """开环：按 Q 次/秒的时间表提交到线程池；在途超过 2 × 线程数时丢弃（记 dropped），不无限排队。"""
    durs, errs, dropped = [], {}, 0
    inflight = [0]
    lock = threading.Lock()

    def job():
        try:
            res = _poll(spec, local, replica)
            with lock:
                _record(durs, errs, res)
        finally:
            with lock:
                inflight[0] -= 1

    rnd = random.Random(spec["seed"])
    period = 1.0 / spec["q"]
    t0 = time.perf_counter()
    t = rnd.random() * period
    with concurrent.futures.ThreadPoolExecutor(max_workers=spec["panel_threads"]) as pool:
        while t < spec["seconds"]:
            now = time.perf_counter() - t0
            if now < t:
                time.sleep(t - now)
            with lock:
                busy = inflight[0] >= 2 * spec["panel_threads"]
                if not busy:
                    inflight[0] += 1
            if busy:
                dropped += 1
            else:
                pool.submit(job)
            t += period
    return durs, errs, dropped


def _poll_backoff(spec, local):
    """轮询退避：单线程，下一次轮询至少等 max(1/Q, backoff_k × 上次耗时)。"""
    durs, errs = [], {}
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < spec["seconds"]:
        res = _poll(spec, local, None)
        _record(durs, errs, res)
        time.sleep(max(1.0 / spec["q"], spec["backoff_k"] * res[0]))
    return durs, errs


# ---- 通知 ------------------------------------------------------------------------------
def notifier_main(spec, out_path):
    common.child_init()
    conn = sqlite3.connect(spec["db"], timeout=spec["busy"], isolation_level=None)
    common.sleep_until(spec["start_wall"])
    waits, errs = [], {}
    from qdperf import sqlite_load
    t0 = time.perf_counter()
    for t_plan in sqlite_load.schedule(spec["notifier_rate"], spec["seconds"], spec["seed"] + 7, poisson=True):
        now = time.perf_counter() - t0
        if now < t_plan:
            time.sleep(t_plan - now)
        a = time.perf_counter()
        try:
            conn.execute("BEGIN IMMEDIATE")
            waits.append(time.perf_counter() - a)
            conn.execute(NOTIFIER_WRITE[0], (int(time.time()),))
            conn.execute(NOTIFIER_WRITE[1])
            conn.execute("COMMIT")
        except sqlite3.OperationalError as exc:
            name = getattr(exc, "sqlite_errorname", None) or "SQLITE_ERROR"
            errs[name] = errs.get(name, 0) + 1
            if conn.in_transaction:
                conn.execute("ROLLBACK")
    conn.close()
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"waits": waits, "errors": errs}, f)
    common.child_finish()


# ---- 编排 ------------------------------------------------------------------------------
def prepare(db=None, scale=0.05, seed=synth_db.DEFAULT_SEED):
    """tempfile 里准备母库：给了 db 就只读拷一份（backup API），否则生成合成库。返回 (工作目录, 母库路径)。"""
    work = common.scratch_dir("qdperf-tick-")
    master = os.path.join(work, "master.db")
    if db:
        from qdperf import sqlite_doctor
        src, _how = sqlite_doctor.open_readonly(db)
        try:
            sqlite_doctor.make_copy(src, master)
        finally:
            src.close()
    else:
        synth_db.generate(master, seed=seed, scale=scale)
    return work, master


def calibrate_panel(master, target_s, probe_rows=20_000):
    """把面板长查询的扫描行数调到单线程耗时 ≈ target_s（耗时与行数成正比，先测 probe_rows 行再按比例放大，再复测）。"""
    conn = sqlite3.connect(master)
    hi = conn.execute("SELECT max(id) FROM trades").fetchone()[0]

    def timed(rows):
        best = None
        for _ in range(3):
            t0 = time.perf_counter()
            conn.execute(PANEL_QUERY, (hi - rows, hi)).fetchall()
            dt = time.perf_counter() - t0
            best = dt if best is None else min(best, dt)
        return best

    probe = min(probe_rows, hi - 1)
    per_row = timed(probe) / probe
    rows = max(100, min(hi - 1, int(target_s / per_row)))
    seconds = timed(rows)
    conn.close()
    return {"rows": rows, "range": [hi - rows, hi], "seconds": seconds, "per_row_us": per_row * 1e6,
            "target_seconds": target_s, "capped": rows == hi - 1}


def run_once(master, cfg, work, cal):
    """一个配置：拷母库 → 设日志模式 → 起引擎 / 面板 / 通知三个进程 → 汇总。"""
    cfg = dict(DEFAULTS, **cfg)
    db = os.path.join(work, "run.db")
    for suffix in ("-wal", "-shm", "-journal"):
        if os.path.exists(db + suffix):
            os.remove(db + suffix)
    shutil.copyfile(master, db)
    c = sqlite3.connect(db)
    c.execute(f"PRAGMA journal_mode={'WAL' if cfg['wal'] or cfg['mitigation'] == 'wal' else 'DELETE'}")
    c.close()
    start = time.time() + 2.5
    base = {"db": db, "seconds": cfg["seconds"], "busy": cfg["busy"], "start_wall": start, "seed": cfg["seed"]}
    outs = {k: os.path.join(work, f"{k}.json") for k in ("engine", "panel", "notifier")}
    for p in outs.values():
        if os.path.exists(p):
            os.remove(p)
    procs = [common.start_process(engine_main, dict(base, **{k: cfg[k] for k in ("T", "compute", "compute_ratio",
                                                                               "bg_threads")}), outs["engine"],
                                  name="engine", daemon=cfg["compute"] != "subprocess")]
    if cfg["q"] > 0:
        chunk_rows = max(1, int(cal["rows"] * cfg["chunk_ratio"] * cfg["T"] / max(cal["seconds"], 1e-9)))
        pspec = dict(base, q=cfg["q"], panel_threads=cfg["panel_threads"], mitigation=cfg["mitigation"],
                     range=cal["range"], chunk_rows=chunk_rows, chunk_pause=cfg["chunk_pause"],
                     replica_every=cfg["replica_every"],
                     backoff_k=cfg["backoff_k"],
                     replica_paths=[os.path.join(work, "replica_a.db"), os.path.join(work, "replica_b.db")])
        procs.append(common.start_process(panel_main, pspec, outs["panel"], name="panel"))
    if cfg["notifier_rate"] > 0:
        procs.append(common.start_process(notifier_main, dict(base, notifier_rate=cfg["notifier_rate"]),
                                          outs["notifier"], name="notifier"))
    bad = common.join_all(procs, timeout=cfg["seconds"] + 90)
    got = {k: common.read_json(p) if os.path.exists(p) else None for k, p in outs.items()}
    return summarize(cfg, got["engine"], got["panel"], got["notifier"], bad)


def summarize(cfg, eng, pan, notif, crashed=()):
    T = cfg["T"]
    ticks = (eng or {}).get("ticks", [])
    iv = tick_intervals(ticks, T)
    s = common.summarize(iv)
    res = {"config": {k: cfg[k] for k in DEFAULTS}, "ticks": len(ticks), "crashed": list(crashed),
           "expected_ticks": int(cfg["seconds"] / T), "missed_ticks": max(0, int(cfg["seconds"] / T) - len(ticks)),
           "interval_ms": common.summarize(iv, 1000.0),
           "interval_T": {k: (None if s[k] is None else s[k] / T) for k in ("p50", "p99", "max")},
           "over_2T": sum(1 for x in iv if x > 2 * T),
           "segments_ms": {name: common.summarize([t[k] for t in ticks], 1000.0)
                           for k, name in ((1, "read"), (2, "compute"), (3, "write"))},
           "tick_errors": {"read": _count([t[4] for t in ticks]), "write": _count([t[5] for t in ticks])},
           "wake_late_ms": common.summarize(wake_lateness(ticks), 1000.0),
           "compute_rate": (eng or {}).get("compute_rate")}
    if pan is not None:
        res["panel"] = {"polls": len(pan["durations"]), "achieved_q": len(pan["durations"]) / cfg["seconds"],
                        "query_ms": common.summarize(pan["durations"], 1000.0), "errors": pan["errors"],
                        "dropped": pan["dropped"], "replica_refreshes": pan["replica_refreshes"]}
    if notif is not None:
        res["notifier"] = {"writes": len(notif["waits"]), "wait_ms": common.summarize(notif["waits"], 1000.0),
                           "errors": notif["errors"]}
    p99 = res["interval_T"]["p99"]
    res["starved"] = None if p99 is None else p99 > 2.0
    return res


def tick_intervals(ticks, T):
    """相邻 tick 开始时刻之差；最后一个 tick 记 max(T, 它自己的耗时)——引擎卡在最后一个 tick 里（例如等写锁几秒）
    时，这段卡顿也必须进分布，否则 tick 少到只剩一两个时 p99 会被误算成正常。"""
    starts = [t[0] for t in ticks]
    iv = [b - a for a, b in zip(starts, starts[1:])]
    if ticks:
        iv.append(max(T, ticks[-1][1] + ticks[-1][2] + ticks[-1][3]))
    return iv


def wake_lateness(ticks):
    """引擎按时睡下（上一个 tick 在计划时刻之前就结束了）之后，实际开始比计划晚了多久：这部分是操作系统 / 宿主机
    没及时调度，与库锁、算段无关。tick 记录第 7 列为计划时刻。"""
    out = []
    for prev, cur in zip(ticks, ticks[1:]):
        if len(cur) > 6 and prev[0] + prev[1] + prev[2] + prev[3] <= cur[6]:
            out.append(max(0.0, cur[0] - cur[6]))
    return out


def _count(xs):
    out = {}
    for x in xs:
        if x:
            out[x] = out.get(x, 0) + 1
    return out


def combo(cfg):
    return (cfg["wal"], cfg["busy"], cfg["compute"])


def find_knees(results):
    """每个 (WAL, busy, compute) 组合里 p99 > 2T 的最小 Q（拐点）及其前一个仍正常的 Q。"""
    groups = {}
    for r in results:
        if r["config"]["mitigation"] == "none" and r["config"]["bg_threads"] == 0:
            groups.setdefault(combo(r["config"]), []).append(r)
    out = []
    for key in sorted(groups, key=lambda k: (k[0], -k[1], k[2])):
        rs = sorted(groups[key], key=lambda r: r["config"]["q"])
        knee = next((r for r in rs if r["starved"]), None)
        ok_before = [r["config"]["q"] for r in rs if knee and r["config"]["q"] < knee["config"]["q"]]
        out.append({"wal": key[0], "busy": key[1], "compute": key[2],
                    "knee_q": knee["config"]["q"] if knee else None,
                    "knee_p99_T": knee["interval_T"]["p99"] if knee else None,
                    "last_ok_q": max(ok_before) if ok_before else None,
                    "p99_T_by_q": {f"{r['config']['q']:g}": r["interval_T"]["p99"] for r in rs}})
    return out


def scan(quick=False, base=None, db=None, scale=0.05, mitigations=("replica", "chunked", "backoff", "wal"), log=None):
    """参数扫描 + 拐点 + 缓解实测 + GIL 对照。返回可 JSON 的 dict。"""
    base = dict(DEFAULTS, **(base or {}))
    work, master = prepare(db, scale)
    try:
        cal = calibrate_panel(master, base["panel_ratio"] * base["T"])
        grid = scan_grid(quick)
        results = [_logged(log, run_once(master, dict(base, **g), work, cal)) for g in grid]
        knees = find_knees(results)
        target = pick_knee(knees)
        mit = []
        if target is not None:
            kcfg = dict(base, wal=target["wal"], busy=target["busy"], compute=target["compute"], q=target["knee_q"])
            mit = [_logged(log, run_once(master, dict(kcfg, mitigation=m), work, cal)) for m in mitigations]
        gil = [_logged(log, run_once(master, dict(base, q=0.0, wal=True, compute=c, bg_threads=b), work, cal))
               for b in (0, 1) for c in COMPUTE]
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return {"format": FORMAT, "machine": common.machine(), "base": base, "panel_calibration": cal, "results": results,
            "knees": knees, "knee": target, "mitigations": mit, "gil": gil, "quick": quick}


def scan_grid(quick):
    if quick:
        g = [{"wal": w, "busy": 5.0, "compute": "inline", "q": q} for w in (True, False) for q in (0.0, 5.0)]
        return g + [{"wal": False, "busy": 0.5, "compute": "subprocess", "q": q} for q in (0.0, 5.0)]
    return [{"wal": w, "busy": b, "compute": c, "q": q} for w in (True, False) for b in (5.0, 0.5)
            for c in COMPUTE for q in Q_GRID]


def pick_knee(knees):
    """缓解实验用的拐点：拐点 Q 最小的组合（同 Q 时按 WAL 关、busy 大、inline 优先）。"""
    found = [k for k in knees if k["knee_q"] is not None]
    if not found:
        return None
    return min(found, key=lambda k: (k["knee_q"], k["wal"], -k["busy"], k["compute"] != "inline"))


def _logged(log, res):
    if log:
        log(row_text(res))
    return res


# ---- 文本 ------------------------------------------------------------------------------
def _t(x):
    return "-" if x is None else f"{x:.2f}T"


def row_text(r):
    c = r["config"]
    seg = r["segments_ms"]
    return (f"WAL {'开' if c['wal'] else '关'} busy={c['busy']:g}s {c['compute']:10s} Q={c['q']:>4g} "
            f"{c['mitigation']:8s} bg={c['bg_threads']} → p50 {_t(r['interval_T']['p50'])} p99 {_t(r['interval_T']['p99'])} "
            f"max {_t(r['interval_T']['max'])} >2T {r['over_2T']}/{r['ticks']} 缺 {r.get('missed_ticks', 0)}  读/算/写 p99 "
            f"{seg['read']['p99'] or 0:.1f}/{seg['compute']['p99'] or 0:.1f}/{seg['write']['p99'] or 0:.1f} ms"
            f"  唤醒迟到 max {r.get('wake_late_ms', {}).get('max') or 0:.0f} ms"
            + (f"  面板 {r['panel']['achieved_q']:.1f}/s 查询 p50 {r['panel']['query_ms']['p50'] or 0:.0f} ms"
               if "panel" in r else ""))


def knee_table(scan_res):
    qs = sorted({r["config"]["q"] for r in scan_res["results"]})
    rows = []
    for k in scan_res["knees"]:
        cells = []
        for q in qs:
            v = k["p99_T_by_q"].get(f"{q:g}")
            mark = " ←拐点" if k["knee_q"] == q else ""
            cells.append("-" if v is None else f"{v:.2f}T{mark}")
        rows.append([f"WAL {'开' if k['wal'] else '关'}", f"busy {k['busy']:g}s", k["compute"]] + cells)
    return common.text_table(["日志", "busy_timeout", "算段"] + [f"Q={q:g}" for q in qs], rows)


def first_line_run(r):
    return (f"TICK_JITTER {'STARVED' if r['starved'] else 'OK'} T={r['config']['T']:g} ticks={r['ticks']} "
            f"p50={_t(r['interval_T']['p50'])} p99={_t(r['interval_T']['p99'])} max={_t(r['interval_T']['max'])} "
            f"over2T={r['over_2T']} read_p99_ms={r['segments_ms']['read']['p99'] or 0:.2f} "
            f"compute_p99_ms={r['segments_ms']['compute']['p99'] or 0:.2f} "
            f"write_p99_ms={r['segments_ms']['write']['p99'] or 0:.2f}")


def first_line_scan(s):
    k = s["knee"]
    knee = "none" if k is None else (f"wal={'on' if k['wal'] else 'off'},busy={k['busy']:g},compute={k['compute']}:"
                                     f"Q={k['knee_q']:g}")
    q0 = max((r["interval_T"]["p99"] or 0.0) for r in s["results"] + s.get("gil", [])
             if r["config"]["q"] == 0 and r["config"]["mitigation"] == "none" and r["config"]["bg_threads"] == 0)
    mit = ",".join(f"{m['config']['mitigation']}:{_t(m['interval_T']['p99'])}" for m in s["mitigations"]) or "-"
    return f"TICK_SCAN knee={knee} q0_p99_max={q0:.2f}T mitigations={mit}"


def format_scan(s):
    cal = s["panel_calibration"]
    out = [first_line_scan(s),
           f"T={s['base']['T']:g}s，面板长查询扫描 {cal['rows']:,} 行，单线程 {cal['seconds'] * 1000:.0f} ms"
           f"（目标 {s['base']['panel_ratio']:g}T）；算段 {s['base']['compute_ratio']:g}T；每个配置 {s['base']['seconds']:g} s",
           "", "== tick 间隔 p99（单位 T；> 2T = 被拖过两个周期）==", knee_table(s), "", "== 全部配置 =="]
    out += [row_text(r) for r in s["results"]]
    if s["mitigations"]:
        k = s["knee"]
        out += ["", f"== 缓解（拐点配置：WAL {'开' if k['wal'] else '关'}，busy {k['busy']:g}s，{k['compute']}，"
                    f"Q={k['knee_q']:g}；原 p99 {_t(k['knee_p99_T'])}）=="]
        out += [row_text(r) + ("  有效" if r["starved"] is False else "  无效") for r in s["mitigations"]]
    out += ["", "== GIL 对照（Q=0，WAL 开；bg=引擎进程里另有 1 个纯 Python 忙线程）=="] + [row_text(r) for r in s["gil"]]
    return "\n".join(out)


def _cfg_from_args(a):
    return {"T": a.T, "seconds": a.seconds, "q": a.q, "wal": a.wal == "on", "busy": a.busy, "compute": a.compute,
            "mitigation": a.mitigation, "panel_ratio": a.panel_ratio, "compute_ratio": a.compute_ratio,
            "panel_threads": a.panel_threads, "notifier_rate": a.notifier_rate, "bg_threads": a.bg_threads}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3.12 -m qdperf.tick_jitter", description="多进程 tick 抖动判据")
    ap.add_argument("cmd", choices=["run", "scan"])
    ap.add_argument("--T", type=float, default=DEFAULTS["T"])
    ap.add_argument("--seconds", type=float, default=DEFAULTS["seconds"])
    ap.add_argument("--q", type=float, default=20.0)
    ap.add_argument("--wal", choices=["on", "off"], default="on")
    ap.add_argument("--busy", type=float, default=DEFAULTS["busy"])
    ap.add_argument("--compute", choices=COMPUTE, default="inline")
    ap.add_argument("--mitigation", choices=MITIGATIONS, default="none")
    ap.add_argument("--panel-ratio", type=float, default=DEFAULTS["panel_ratio"])
    ap.add_argument("--compute-ratio", type=float, default=DEFAULTS["compute_ratio"])
    ap.add_argument("--panel-threads", type=int, default=DEFAULTS["panel_threads"])
    ap.add_argument("--notifier-rate", type=float, default=DEFAULTS["notifier_rate"])
    ap.add_argument("--bg-threads", type=int, default=0)
    ap.add_argument("--db", default=None, help="用你的库（只读拷到 tempfile）；缺省生成合成库")
    ap.add_argument("--scale", type=float, default=0.05)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)
    if a.T <= 0 or a.seconds <= 0 or a.q < 0:
        print("TICK_JITTER ERROR T / seconds must be > 0 and q >= 0")
        return 2
    if a.cmd == "scan":
        res = scan(a.quick, _cfg_from_args(a), a.db, a.scale, log=lambda line: print(line, file=sys.stderr, flush=True))
        print(format_scan(res))
        rc = 1 if any(r["starved"] for r in res["results"]) else 0
    else:
        work, master = prepare(a.db, a.scale)
        try:
            cfg = _cfg_from_args(a)
            cal = calibrate_panel(master, cfg["panel_ratio"] * cfg["T"])
            res = run_once(master, cfg, work, cal)
            res["panel_calibration"] = cal
        finally:
            shutil.rmtree(work, ignore_errors=True)
        print(first_line_run(res))
        print(row_text(res))
        rc = 1 if res["starved"] or res["crashed"] else 0
    if a.json:
        common.write_json(a.json, res)
    return rc


if __name__ == "__main__":
    sys.exit(main())
