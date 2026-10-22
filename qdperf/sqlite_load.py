"""SQLite 多进程实测（G1 的“写争用模型”与“WAL 检查点”两节；全部是真进程 + 真 SQLite，不是公式）。

contention(db, writers, ...)
    每个写者一个 spawn 进程、一条连接（busy_timeout = 给定秒数），按开环时间表（每 1/rate 秒一次、带随机相位）执行写事务：
    ``BEGIN IMMEDIATE``（或 ``BEGIN`` 延迟事务：先读后写）→ 事务里的写语句 → ``COMMIT``。逐笔记录：
    等待 = 发起到拿到写锁（BEGIN IMMEDIATE 返回；延迟事务记第一条写语句耗时）；持锁 = 拿到锁到 COMMIT 返回；
    迟到 = 实际发起时刻 - 计划时刻；失败按 sqlite_errorname 计数（SQLITE_BUSY / SQLITE_BUSY_SNAPSHOT ...）。
    汇总给出每个进程与总体的 p50 / p99 / 最大等待、SQLITE_BUSY 次数、实际速率，以及“谁在饿死谁”的结论。

wal_growth(db, ...)
    一个写进程按固定速率往副本里自建的探针表写；可选一个“面板”进程用两条连接交替持有长读事务（任意时刻至少一个读事务在开着），
    父进程每 sample 秒 stat 一次 <db>-wal 的文件大小，得到 WAL 大小随时间曲线；对不同 wal_autocheckpoint 取值各跑一遍。

两个实验都只在调用方给的库文件上写（sqlite_doctor 传进来的是 tempfile 里的副本）。
"""
import json
import os
import random
import sqlite3
import struct
import time

from qdperf import common

BUSY_ERRORS = ("SQLITE_BUSY", "SQLITE_BUSY_SNAPSHOT", "SQLITE_BUSY_RECOVERY", "SQLITE_BUSY_TIMEOUT", "SQLITE_LOCKED")


# ---- 写者进程 ----------------------------------------------------------------------------
def writer_main(spec, out_path):
    """spawn 子进程入口：跑 spec 描述的写者，把逐笔记录写到 out_path（JSON）。"""
    common.child_init()
    try:
        res = run_writer(spec)
    except Exception as exc:                       # noqa: BLE001 —— 子进程崩溃也要把原因带回父进程
        res = {"name": spec.get("name"), "error": f"{type(exc).__name__}: {exc}"}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(res, f)
    common.child_finish()


def _connect(db, busy_timeout, synchronous, autocheckpoint=None):
    conn = sqlite3.connect(db, timeout=busy_timeout, isolation_level=None)
    conn.execute(f"PRAGMA synchronous={synchronous}")
    if autocheckpoint is not None:
        conn.execute(f"PRAGMA wal_autocheckpoint={int(autocheckpoint)}")
    return conn


def schedule(rate, duration, seed, poisson=False):
    """开环时间表（相对起跑的秒数）：固定间隔 1/rate + 随机相位，或泊松到达（seed 固定）。"""
    rnd = random.Random(seed)
    if rate <= 0:
        return []
    period = 1.0 / rate
    t = rnd.random() * period
    out = []
    while t < duration:
        out.append(t)
        t += rnd.expovariate(rate) if poisson else period
    return out


def pick_txn(txns, r):
    """按 weight 抽一个事务类型；r ∈ [0, 1)。"""
    total = sum(t["weight"] for t in txns)
    acc = 0.0
    for t in txns:
        acc += t["weight"] / total
        if r < acc:
            return t
    return txns[-1]


def run_writer(spec):
    """spec: name, db, busy_timeout(秒), synchronous, begin(immediate|deferred), rate, duration, start_wall, seed,
    txns=[{name, weight, statements: [[sql, params], ...], read_table}], poisson / autocheckpoint / think（秒，延迟事务读后
    写前的应用耗时）可选。"""
    conn = _connect(spec["db"], spec["busy_timeout"], spec.get("synchronous", "NORMAL"), spec.get("autocheckpoint"))
    rnd = random.Random(spec["seed"] + 1)
    times = schedule(spec["rate"], spec["duration"], spec["seed"], spec.get("poisson", False))
    records = []
    common.sleep_until(spec["start_wall"])
    t0 = time.perf_counter()
    for t_plan in times:
        now = time.perf_counter() - t0
        if now < t_plan:
            time.sleep(t_plan - now)
        txn = pick_txn(spec["txns"], rnd.random())
        records.append(_one_txn(conn, txn, spec["begin"], t0, t_plan, spec.get("think", 0.0)))
    conn.close()
    return {"name": spec["name"], "records": records, "duration": spec["duration"], "planned": len(times)}


def _one_txn(conn, txn, begin, t0, t_plan, think=0.0):
    """一笔写事务 → [计划时刻, 迟到, 等待, 持锁, 错误名或 None, 事务名]（秒）。"""
    t_attempt = time.perf_counter()
    late = t_attempt - t0 - t_plan
    stmts = txn["statements"]
    try:
        if begin == "immediate":
            conn.execute("BEGIN IMMEDIATE")
            t_lock = time.perf_counter()
            for sql, params in stmts:
                conn.execute(sql, params)
        else:
            conn.execute("BEGIN")
            conn.execute(f"SELECT max(rowid) FROM {txn['read_table']}").fetchone()   # 先读：拿到读快照
            if think:
                time.sleep(think)                                                    # 读完在应用里算一会儿再写
            t_w = time.perf_counter()
            conn.execute(*stmts[0])                                                  # 第一条写：此刻升级写锁
            t_lock = time.perf_counter()
            t_attempt = t_w
            for sql, params in stmts[1:]:
                conn.execute(sql, params)
        conn.execute("COMMIT")
        t_done = time.perf_counter()
        return [t_plan, late, t_lock - t_attempt, t_done - t_lock, None, txn["name"]]
    except sqlite3.OperationalError as exc:
        name = getattr(exc, "sqlite_errorname", None) or "SQLITE_ERROR"
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        return [t_plan, late, time.perf_counter() - t_attempt, 0.0, name, txn["name"]]


# ---- 汇总 ------------------------------------------------------------------------------
def summarize_writer(res):
    recs = res.get("records", [])
    ok = [r for r in recs if r[4] is None]
    errors = {}
    for r in recs:
        if r[4] is not None:
            errors[r[4]] = errors.get(r[4], 0) + 1
    waits = [r[2] for r in ok]
    holds = [r[3] for r in ok]
    dur = res.get("duration") or 1.0
    return {"name": res.get("name"), "planned": res.get("planned", 0), "attempts": len(recs), "committed": len(ok),
            "achieved_rate": len(ok) / dur, "wait_ms": common.summarize(waits, 1000.0),
            "hold_ms": common.summarize(holds, 1000.0), "late_ms": common.summarize([r[1] for r in recs], 1000.0),
            "errors": errors, "busy": sum(v for k, v in errors.items() if k in BUSY_ERRORS),
            "lock_share": sum(holds) / dur, "crash": res.get("error")}


def starvation_verdict(writers, budget_ms):
    """“谁在饿死谁”：持锁占比最高者 = 主要持锁者；p99 等待最高者 = 受害者。给出一句结论 + 证据。"""
    live = [w for w in writers if w["committed"]]
    if not live:
        return {"starver": None, "victim": None, "starved": bool(writers), "text": "没有任何写事务提交成功"}
    holder = max(live, key=lambda w: (w["lock_share"], w["name"]))
    victim = max(writers, key=lambda w: ((w["wait_ms"]["p99"] or 0.0) + 1e9 * w["busy"], w["name"]))
    starved = victim["busy"] > 0 or (victim["wait_ms"]["p99"] or 0.0) > budget_ms
    if starved:
        text = (f"{holder['name']} 持锁占比 {holder['lock_share'] * 100:.1f}%（持锁 p99 {holder['hold_ms']['p99']:.2f} ms），"
                f"{victim['name']} 被饿：p99 等待 {victim['wait_ms']['p99'] or 0.0:.1f} ms、SQLITE_BUSY {victim['busy']} 次")
    else:
        text = (f"无饿死：最差的 {victim['name']} p99 等待 {victim['wait_ms']['p99'] or 0.0:.2f} ms ≤ {budget_ms:g} ms，"
                f"SQLITE_BUSY 0；主要持锁者 {holder['name']}（占比 {holder['lock_share'] * 100:.1f}%）")
    return {"starver": holder["name"], "victim": victim["name"], "starved": starved, "text": text}


def root_cause(writers, cfg):
    """p99 超预算或出现 SQLITE_BUSY 时给根因（按证据逐条判断，不猜）。"""
    causes = []
    errs = {}
    for w in writers:
        for k, v in w["errors"].items():
            errs[k] = errs.get(k, 0) + v
    if errs.get("SQLITE_BUSY_SNAPSHOT"):
        causes.append("延迟事务（BEGIN DEFERRED）先读后写：读快照过期时升级写锁直接返回 SQLITE_BUSY_SNAPSHOT，"
                      "busy_timeout 不生效 → 写事务改用 BEGIN IMMEDIATE")
    if errs.get("SQLITE_BUSY"):
        causes.append(f"busy_timeout={cfg['busy_timeout']} s 内拿不到写锁：同时写者 × 持锁时间超过超时 → 缩短事务 / 合并写 / 加大超时")
    hold99 = max((w["hold_ms"]["p99"] or 0.0) for w in writers)
    load = sum(w["lock_share"] for w in writers)
    if hold99 > cfg["budget_ms"] / 4:
        causes.append(f"持锁过长：最大持锁 p99 {hold99:.1f} ms（> 预算的 1/4）→ 事务里有慢语句或检查点")
    if load > 0.5:
        causes.append(f"写锁利用率 {load * 100:.0f}% > 50%：排队论下等待随利用率陡增 → 降低写频次或批量提交")
    if not causes:
        causes.append("busy handler 退避粒度（1,2,5,10,15,20,25,25,25,50,50,100 ms）放大了少量冲突的等待")
    return causes


# ---- 争用实验 ----------------------------------------------------------------------------
def contention(db, writers, duration=5.0, busy_timeout=5.0, begin="immediate", synchronous="NORMAL",
               budget_ms=200.0, seed=0, poisson=False, think_ms=0.0):
    """writers = [{name, rate, txns: [{name, weight, statements, read_table}]}]。返回汇总 dict。"""
    work = common.scratch_dir("qdperf-contention-")
    start = time.time() + 1.5 + 0.2 * len(writers)              # 给 spawn 留出导入时间，全部写者同一时刻起跑
    procs, outs = [], []
    for k, w in enumerate(writers):
        spec = {"name": w["name"], "db": db, "busy_timeout": busy_timeout, "synchronous": synchronous,
                "begin": begin, "rate": w["rate"], "duration": duration, "start_wall": start, "seed": seed * 1000 + k,
                "txns": w["txns"], "poisson": poisson, "think": think_ms / 1000.0}
        out = os.path.join(work, f"w{k}.json")
        outs.append(out)
        procs.append(common.start_process(writer_main, spec, out, name=w["name"]))
    late = common.join_all(procs, timeout=duration + 60)
    raw = [common.read_json(o) if os.path.exists(o) else {"name": w["name"], "error": "no output"}
           for o, w in zip(outs, writers)]
    per = [summarize_writer(r) for r in raw]
    waits = [r[2] for res in raw for r in res.get("records", []) if r[4] is None]
    cfg = {"busy_timeout": busy_timeout, "begin": begin, "synchronous": synchronous, "duration": duration,
           "budget_ms": budget_ms, "writers": len(writers), "rates": [w["rate"] for w in writers], "poisson": poisson,
           "think_ms": think_ms}
    overall = {"wait_ms": common.summarize(waits, 1000.0), "busy": sum(w["busy"] for w in per),
               "committed": sum(w["committed"] for w in per), "attempts": sum(w["attempts"] for w in per),
               "crashed": [w["name"] for w in per if w["crash"]] + late}
    p99 = overall["wait_ms"]["p99"]
    ok = not overall["crashed"] and overall["busy"] == 0 and p99 is not None and p99 <= budget_ms
    out = {"config": cfg, "writers": per, "overall": overall, "ok": ok,
           "starvation": starvation_verdict(per, budget_ms)}
    if not ok:
        out["root_cause"] = root_cause(per, cfg)
    return out


# ---- WAL 增长实验 ------------------------------------------------------------------------
WAL_PAYLOAD = "x" * 200
PROBE = "qdperf_wal_probe"            # WAL 实验在副本里自建的探针表（与业务表无关，任何库都能跑）


def wal_writer_main(spec, out_path):
    common.child_init()
    conn = _connect(spec["db"], 5.0, spec["synchronous"], spec["autocheckpoint"])
    rows = [(WAL_PAYLOAD,)] * spec["rows_per_txn"]
    commits = []
    common.sleep_until(spec["start_wall"])
    t0 = time.perf_counter()
    for t_plan in schedule(spec["rate"], spec["duration"], spec["seed"]):
        now = time.perf_counter() - t0
        if now < t_plan:
            time.sleep(t_plan - now)
        t = time.perf_counter()
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany(f"INSERT INTO {PROBE}(payload) VALUES (?)", rows)
        conn.execute("COMMIT")
        commits.append(time.perf_counter() - t)
    conn.close()
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"commits": commits}, f)
    common.child_finish()


def long_reader_main(spec, out_path):
    """两条连接交替开读事务，每个持有 hold 秒，错开 hold/2：任意时刻至少一个读事务在开着（面板长查询）。"""
    common.child_init()
    conns = [sqlite3.connect(spec["db"], isolation_level=None, timeout=5.0) for _ in range(2)]
    common.sleep_until(spec["start_wall"])
    t0 = time.perf_counter()
    opened = [None, None]
    k = 0
    reads = 0
    while time.perf_counter() - t0 < spec["duration"]:
        c = k % 2
        if opened[c] is not None:
            conns[c].execute("COMMIT")
        conns[c].execute("BEGIN")
        conns[c].execute(f"SELECT count(*) FROM (SELECT id FROM {PROBE} ORDER BY id DESC LIMIT 1000)").fetchone()
        opened[c] = time.perf_counter()
        reads += 1
        k += 1
        time.sleep(spec["hold"] / 2)
    for c in conns:
        if c.in_transaction:
            c.execute("COMMIT")
        c.close()
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"read_txns": reads}, f)
    common.child_finish()


def wal_growth(db, autocheckpoint=1000, long_reader=False, duration=8.0, rate=200.0, rows_per_txn=20,
               hold=2.0, sample=0.1, synchronous="NORMAL", seed=0):
    """返回 {config, curve: [[t, WAL 文件字节, 有效帧, 已回填帧]], max_bytes, final_bytes, max_live_frames, commit_ms, ...}。"""
    wal = db + "-wal"
    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"CREATE TABLE IF NOT EXISTS {PROBE}(id INTEGER PRIMARY KEY, payload TEXT)")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")                # 起点：WAL 清零
    conn.close()
    work = common.scratch_dir("qdperf-wal-")
    start = time.time() + 1.5
    wspec = {"db": db, "synchronous": synchronous, "autocheckpoint": autocheckpoint, "rows_per_txn": rows_per_txn,
             "rate": rate, "duration": duration, "start_wall": start, "seed": seed}
    procs = [common.start_process(wal_writer_main, wspec, os.path.join(work, "w.json"), name="writer")]
    if long_reader:
        rspec = {"db": db, "hold": hold, "duration": duration + 0.5, "start_wall": start - 0.2}
        procs.append(common.start_process(long_reader_main, rspec, os.path.join(work, "r.json"), name="panel"))
    curve = _sample_wal(db, start, duration, sample)
    late = common.join_all(procs, timeout=duration + 60)
    w = common.read_json(os.path.join(work, "w.json")) if os.path.exists(os.path.join(work, "w.json")) else {}
    r = common.read_json(os.path.join(work, "r.json")) if os.path.exists(os.path.join(work, "r.json")) else {}
    sizes = [c[1] for c in curve]
    live = [c[2] for c in curve]
    return {"config": {"autocheckpoint": autocheckpoint, "long_reader": long_reader, "duration": duration,
                       "rate": rate, "rows_per_txn": rows_per_txn, "hold": hold, "synchronous": synchronous},
            "curve_fields": ["t_s", "wal_file_bytes", "wal_live_frames", "backfilled_frames"],
            "curve": curve, "max_bytes": max(sizes) if sizes else 0, "final_bytes": sizes[-1] if sizes else 0,
            "max_live_frames": max(live) if live else 0, "final_live_frames": live[-1] if live else 0,
            "resets": count_resets(live),
            "commit_ms": common.summarize(w.get("commits", []), 1000.0), "read_txns": r.get("read_txns", 0),
            "crashed": late + ([] if w else ["writer"])}


def count_resets(live):
    """WAL 重置次数：有效帧数比上一个采样点少一半以上的次数（检查点完成后写者从 WAL 头部重新开始）。"""
    return sum(1 for a, b in zip(live, live[1:]) if b < a / 2)


def read_wal_index(shm_path):
    """读 <db>-shm 的 wal-index 头（只读，不加锁）：→ (mxFrame, nBackfill)。
    布局（SQLite wal.c）：WalIndexHdr 48 字节 × 2 份，mxFrame 在偏移 16；其后 WalCkptInfo，nBackfill 在偏移 96；本机字节序。"""
    try:
        with open(shm_path, "rb") as f:
            head = f.read(100)
    except OSError:
        return 0, 0
    if len(head) < 100:
        return 0, 0
    return struct.unpack_from("=I", head, 16)[0], struct.unpack_from("=I", head, 96)[0]


def _sample_wal(db, start, duration, every):
    """每 every 秒一行：[t, WAL 文件字节（高水位）, WAL 中有效帧数 mxFrame, 已回填帧数 nBackfill]。"""
    common.sleep_until(start)
    t0 = time.perf_counter()
    curve = []
    while True:
        t = time.perf_counter() - t0
        if t > duration:
            return curve
        size = os.path.getsize(db + "-wal") if os.path.exists(db + "-wal") else 0
        curve.append([round(t, 3), size, *read_wal_index(db + "-shm")])
        time.sleep(every)
