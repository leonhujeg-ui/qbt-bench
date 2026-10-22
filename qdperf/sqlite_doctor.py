"""SQLite 体检器（G1）：库文件 + 工作负载 JSON → 体检表。

    python3.12 -m qdperf.sqlite_doctor DB --workload WL.json [--sections plans,indexes,contention,wal,file]
        [--duration 4] [--writers 3 --rate 50] [--busy-timeout 5] [--budget-ms 200] [--deferred-demo]
        [--json OUT] [--emit-sql OUT.sql] [--quick]

stdout 第一行：``SQLITE_DOCTOR <OK|FAIL> statements=<n> full_scan=<n> temp_btree=<n> suggestions=<已验证>/<候选>
contention_p99_ms=<x|-> busy=<n|-> wal_max_mb=<x|-> red=<n> yellow=<n>``；其后是人读的体检表。
退出码：0 无红色发现；1 有红色发现（热点全表扫描 / 写争用超预算或 SQLITE_BUSY / 建议未通过验证 / 原库被改动）；2 输入错误。

零写入原库：原库只读打开（WAL 库且没有 -wal 文件时用 immutable=1，避免在原库旁边生成 -shm / -wal），用 backup API
拷到 tempfile 里的两个副本：副本 A 做计划 / 统计 / 索引验证，副本 B 做写争用与 WAL 实验。结束时比较原库 sha256、
大小、mtime 与同目录旁路文件清单，任何变化记红色发现。
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
import urllib.parse

from qdperf import common
from qdperf import index_advisor as advisor
from qdperf import sqlite_load as load
from qdperf import sqlplan as P

FORMAT = "qdperf.workload.v1"
REPORT_FORMAT = "qdperf.sqlite_doctor.v1"
SECTIONS = ("plans", "indexes", "contention", "wal", "file")
WRITE_KINDS = ("insert", "update", "delete")


class DoctorError(ValueError):
    """输入不合法（库文件 / 工作负载 / 参数）；CLI 退出码 2。"""


# ---- 工作负载 ----------------------------------------------------------------------------
def load_workload(src):
    """src = 路径或 dict → 校验后的工作负载 dict（缺省字段补齐）。不合法抛 DoctorError。"""
    if isinstance(src, (str, os.PathLike)):
        try:
            wl = common.read_json(src)
        except (OSError, json.JSONDecodeError) as exc:
            raise DoctorError(f"workload unreadable: {exc}") from exc
    else:
        wl = json.loads(json.dumps(src))
    if not isinstance(wl, dict) or wl.get("format") != FORMAT:
        raise DoctorError(f"workload.format must be {FORMAT!r}")
    stmts = wl.get("statements")
    if not isinstance(stmts, list) or not stmts:
        raise DoctorError("workload.statements must be a non-empty list")
    seen = set()
    for k, st in enumerate(stmts):
        _check_statement(k, st, seen)
    conn = wl.setdefault("connection", {})
    conn.setdefault("synchronous", "NORMAL")
    conn.setdefault("busy_timeout_ms", 5000)
    conn.setdefault("begin", "immediate")
    if conn["begin"] not in ("immediate", "deferred"):
        raise DoctorError("connection.begin must be immediate|deferred")
    return wl


def _check_statement(k, st, seen):
    if not isinstance(st, dict):
        raise DoctorError(f"statements[{k}] must be an object")
    for key, typ in (("id", str), ("process", str), ("sql", str)):
        if not isinstance(st.get(key), typ) or not st[key]:
            raise DoctorError(f"statements[{k}].{key} must be a non-empty string")
    if st["id"] in seen:
        raise DoctorError(f"duplicate statement id {st['id']!r}")
    seen.add(st["id"])
    rate = st.get("per_second")
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not rate >= 0 or rate == float("inf"):
        raise DoctorError(f"statements[{k}].per_second must be a finite number >= 0")
    st.setdefault("params", [])
    if not isinstance(st["params"], (list, dict)):
        raise DoctorError(f"statements[{k}].params must be a list or object")
    samples = st.setdefault("param_samples", [st["params"]])
    if not isinstance(samples, list) or not samples or not all(isinstance(p, (list, dict)) for p in samples):
        raise DoctorError(f"statements[{k}].param_samples must be a non-empty list of lists/objects")
    if "txn" in st and not isinstance(st["txn"], str):
        raise DoctorError(f"statements[{k}].txn must be a string")


# ---- 只读打开 / 副本 ----------------------------------------------------------------------
def check_db_file(path):
    """存在且是 SQLite 库文件 → 返回 100 字节文件头；否则抛 DoctorError（只读文件，不碰 SQLite）。"""
    if not os.path.isfile(path):
        raise DoctorError(f"database not found: {path}")
    with open(path, "rb") as f:
        head = f.read(100)
    if len(head) < 100 or not head.startswith(b"SQLite format 3\x00"):
        raise DoctorError(f"not a SQLite database: {path}")
    return head


def open_readonly(path):
    """只读打开原库：不存在 / 不是 SQLite 抛 DoctorError；WAL 库且无 -wal 文件 → immutable=1（不生成旁路文件）。"""
    wal = check_db_file(path)[18] == 2
    immutable = wal and not os.path.exists(path + "-wal")
    uri = "file:" + urllib.parse.quote(os.path.abspath(path)) + "?mode=ro" + ("&immutable=1" if immutable else "")
    conn = sqlite3.connect(uri, uri=True)
    return conn, {"wal_header": wal, "immutable": immutable}


def file_state(path):
    """原库的“指纹”：sha256 / 大小 / mtime_ns / 同目录下以库名开头的文件清单。"""
    d, base = os.path.split(os.path.abspath(path))
    st = os.stat(path)
    return {"sha256": common.sha256_file(path), "size": st.st_size, "mtime_ns": st.st_mtime_ns,
            "siblings": sorted(n for n in os.listdir(d) if n.startswith(base))}


def make_copy(src_conn, dst_path):
    dst = sqlite3.connect(dst_path)
    try:
        src_conn.backup(dst)
    finally:
        dst.close()


# ---- 结构 / 计数 / 统计 -------------------------------------------------------------------
def schema_info(conn):
    tables = {}
    for (name,) in conn.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                                "ORDER BY name"):
        info = conn.execute(f'PRAGMA table_info("{name}")').fetchall()
        pk = [r[1] for r in sorted(info, key=lambda r: r[5]) if r[5] > 0]
        int_pk = len(pk) == 1 and any(r[1] == pk[0] and r[2].upper() == "INTEGER" for r in info)
        fks = [{"column": r[3], "ref_table": r[2], "ref_column": r[4]}
               for r in conn.execute(f'PRAGMA foreign_key_list("{name}")')]
        idx = {}
        for r in conn.execute(f'PRAGMA index_list("{name}")'):
            cols = [c[2] for c in conn.execute(f'PRAGMA index_info("{r[1]}")')]
            idx[r[1]] = {"columns": cols, "unique": bool(r[2]), "origin": r[3], "partial": bool(r[4])}
        tables[name] = {"columns": [r[1] for r in info], "pk": pk, "integer_pk": int_pk, "foreign_keys": fks,
                        "indexes": idx}
    return tables


def columns_map(schema):
    return {t: v["columns"] for t, v in schema.items()}


def table_counts(conn, schema):
    return {t: conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in schema}


def read_stat1(conn):
    """{索引名或表名: [整数...]}；没有 sqlite_stat1 返回 {}。"""
    has = conn.execute("SELECT 1 FROM sqlite_schema WHERE name='sqlite_stat1'").fetchone()
    if not has:
        return {}
    out = {}
    for tbl, idx, stat in conn.execute("SELECT tbl, idx, stat FROM sqlite_stat1"):
        nums = [int(x) for x in (stat or "").split() if x.isdigit()]
        out[idx or tbl] = nums
    return out


def stat1_rows(conn):
    if not conn.execute("SELECT 1 FROM sqlite_schema WHERE name='sqlite_stat1'").fetchone():
        return None
    return conn.execute("SELECT tbl, idx, stat FROM sqlite_stat1").fetchall()


def restore_stat1(conn, rows):
    """把 sqlite_stat1 恢复成 rows（None = 原来没有这张表），并让规划器重新加载统计。"""
    if rows is None:
        conn.execute("DROP TABLE IF EXISTS sqlite_stat1")
    else:
        conn.execute("DELETE FROM sqlite_stat1")
        conn.executemany("INSERT INTO sqlite_stat1(tbl, idx, stat) VALUES (?,?,?)", rows)
    conn.execute("ANALYZE sqlite_schema")


# ---- 计划 / 行数估算 / 实测 ---------------------------------------------------------------
def explain(conn, sql, params):
    return P.parse_eqp(conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall())


NO_STAT_EQ_ROWS = 10          # 没有统计时 SQLite 假定等值命中约 10 行（where.c 的 truthProb / 缺省估计口径）


def node_rows(node, counts, stats):
    """单个计划节点每次执行（每次探查）触达的行数估算。"""
    n = counts.get(node.get("table"), 0)
    if node["op"] == "scan":
        return float(n)
    if node["op"] != "search":
        return 0.0
    eq, rng = P.search_constraint_counts(node["constraints"])
    if node["rowid"]:
        return 1.0 if eq else n / (4.0 if rng == 1 else 64.0)
    if node["automatic"]:
        return float(NO_STAT_EQ_ROWS)
    st = stats.get(node["index"])
    if st:
        base = float(st[min(eq, len(st) - 1)])          # stat1: [表行数, 前 1 列每键平均行数, 前 2 列 ..., ...]
    else:
        base = float(NO_STAT_EQ_ROWS) if eq else float(n)
    if rng:
        base /= 4.0 if rng == 1 else 64.0
    return max(1.0, base)


def statement_rows(nodes, counts, stats):
    """同一父节点下的循环按嵌套连接相乘：Σ_k Π_{j≤k} r_j；自动索引另加一次建索引的整表扫描。"""
    groups = {}
    for nd in nodes:
        if nd["op"] in ("scan", "search"):
            groups.setdefault(nd["parent"], []).append(nd)
    total = 0.0
    for loop in groups.values():
        prod = 1.0
        for nd in loop:
            prod *= max(node_rows(nd, counts, stats), 1.0 if nd["op"] == "search" else 0.0)
            total += prod
            if nd["op"] == "search" and nd["automatic"]:
                total += counts.get(nd["table"], 0)
    return total


def measure(conn, st, kind, timeout_s, repeat=3):
    """在副本上真跑一遍（写语句在事务里执行后回滚）：最好耗时、VM 指令（千条）、返回行数。超时中断记 timed_out。"""
    steps = [0]
    deadline = [0.0]

    def progress():
        steps[0] += 1
        return 1 if time.perf_counter() > deadline[0] else 0

    conn.set_progress_handler(progress, 1000)
    best, rows, timed_out = None, 0, False
    try:
        for _ in range(repeat):
            steps[0] = 0
            deadline[0] = time.perf_counter() + timeout_s
            t0 = time.perf_counter()
            try:
                rows = _run_once(conn, st, kind)
            except sqlite3.OperationalError as exc:
                if "interrupt" not in str(exc):
                    raise
                timed_out = True
            dt = time.perf_counter() - t0
            best = dt if best is None else min(best, dt)
            if timed_out or dt > 1.0:
                break
    finally:
        conn.set_progress_handler(None, 0)
        if conn.in_transaction:
            conn.execute("ROLLBACK")
    return {"ms_best": round(best * 1000.0, 3), "vm_kilo_steps": steps[0], "rows_returned": rows,
            "timed_out": timed_out}


def _run_once(conn, st, kind):
    if kind in WRITE_KINDS:
        conn.execute("BEGIN")
        try:
            cur = conn.execute(st["sql"], st["param_samples"][0])
            return cur.rowcount
        finally:
            conn.execute("ROLLBACK")
    return len(conn.execute(st["sql"], st["param_samples"][0]).fetchall())


def analyze_statements(conn, wl, schema, counts, stats, measure_timeout=5.0):
    """副本 A（原统计状态）上逐条：计划、判定、估算行数、频次×行数、实测。"""
    cols = columns_map(schema)
    out = []
    for st in wl["statements"]:
        parsed = P.parse_statement(st["sql"], cols)
        nodes = advisor.resolve_aliases(explain(conn, st["sql"], st["param_samples"][0]), parsed)
        cls = P.classify(nodes)
        rows = statement_rows(nodes, counts, stats)
        rec = {"id": st["id"], "process": st["process"], "sql": st["sql"], "per_second": st["per_second"],
               "kind": parsed["kind"], "plan": [n["detail"] for n in nodes], **cls,
               "rows_est": round(rows, 1), "cost": round(rows * st["per_second"], 1)}
        if measure_timeout:
            rec["measured"] = measure(conn, st, parsed["kind"], measure_timeout)
            rec["db_ms_per_second"] = round(rec["measured"]["ms_best"] * st["per_second"], 3)
        out.append(rec)
    return out


def top_by_cost(statements, n=20):
    return [s["id"] for s in sorted(statements, key=lambda s: (-s["cost"], s["id"]))[:n]]


def harvest_stats(conn, wl, cols_map):
    """副本上临时 ANALYZE：取新鲜统计（行数估算用）与 ANALYZE 之后的计划判定，然后把 sqlite_stat1 恢复原状。"""
    saved = stat1_rows(conn)
    t0 = time.perf_counter()
    conn.execute("ANALYZE")
    seconds = round(time.perf_counter() - t0, 3)
    fresh = read_stat1(conn)
    after = {}
    for st in wl["statements"]:
        parsed = P.parse_statement(st["sql"], cols_map)
        nodes = advisor.resolve_aliases(explain(conn, st["sql"], st["param_samples"][0]), parsed)
        after[st["id"]] = P.classify(nodes)["verdict"]
    restore_stat1(conn, saved)
    return fresh, after, seconds


def write_costs(conn, wl, cols_map, n=200, repeat=3):
    """每条写语句在事务里连跑 n 次后回滚，best-of-repeat 的单次耗时（微秒）。"""
    out = {}
    for st in wl["statements"]:
        if P.parse_statement(st["sql"], cols_map)["kind"] not in WRITE_KINDS:
            continue
        samples = st["param_samples"]
        best = None
        for _ in range(repeat):
            conn.execute("BEGIN")
            t0 = time.perf_counter()
            for i in range(n):
                conn.execute(st["sql"], samples[i % len(samples)])
            dt = time.perf_counter() - t0
            conn.execute("ROLLBACK")
            best = dt if best is None else min(best, dt)
        out[st["id"]] = round(best / n * 1e6, 2)
    return out


# ---- 库文件体检 ------------------------------------------------------------------------------
def _fk_indexed(info, col):
    if info["integer_pk"] and info["pk"] == [col]:
        return True
    return any(ix["columns"][:1] == [col] for ix in info["indexes"].values())


def file_checkup(ro, schema, counts, copy_conn, how, stale_ratio=0.10):
    prag = {k: ro.execute(f"PRAGMA {k}").fetchone()[0]
            for k in ("page_size", "page_count", "freelist_count", "auto_vacuum", "encoding", "user_version")}
    frag = prag["freelist_count"] / prag["page_count"] if prag["page_count"] else 0.0
    stat = read_stat1(ro)
    stale, missing = [], []
    for t, info in sorted(schema.items()):
        n = counts[t]
        for name in sorted(info["indexes"]) or [t]:
            st = stat.get(name)
            if st is None:
                if n and stat:
                    missing.append(name)
            elif st and abs(st[0] - n) > stale_ratio * max(n, 1):
                stale.append({"object": name, "table": t, "stat_rows": st[0], "actual_rows": n,
                              "drift": round((n - st[0]) / max(st[0], 1), 3)})
    sizes = []
    try:
        sizes = [list(r) for r in copy_conn.execute(
            "SELECT name, sum(pgsize) AS b FROM dbstat GROUP BY name ORDER BY b DESC, name LIMIT 12")]
    except sqlite3.OperationalError:
        pass
    return {"pragmas": prag, "journal_mode_header": "wal" if how["wal_header"] else "rollback",
            "opened_immutable": how["immutable"], "fragmentation": round(frag, 4),
            "no_primary_key": [t for t, v in sorted(schema.items()) if not v["pk"]],
            "fk_without_index": [{"table": t, "column": fk["column"], "ref_table": fk["ref_table"]}
                                 for t, v in sorted(schema.items()) for fk in v["foreign_keys"]
                                 if not _fk_indexed(v, fk["column"])],
            "analyzed": bool(stat), "stat1_entries": len(stat), "stats_stale": stale, "stats_missing": missing,
            "largest_objects": sizes}


# ---- 写争用 / WAL 场景 -----------------------------------------------------------------------
def writer_mix(wl, cols_map):
    """{进程: [事务类型]}：同进程同 txn 名的写语句合成一个事务（速率取组内最大），否则一条语句一个事务。"""
    groups, order = {}, []
    for st in wl["statements"]:
        parsed = P.parse_statement(st["sql"], cols_map)
        if parsed["kind"] not in WRITE_KINDS or st["per_second"] <= 0:
            continue
        key = (st["process"], st.get("txn") or st["id"])
        if key not in groups:
            groups[key] = {"name": key[1], "weight": 0.0, "statements": [], "read_table": parsed["tables"][0]["table"]}
            order.append(key)
        g = groups[key]
        g["weight"] = max(g["weight"], st["per_second"])
        g["statements"].append([st["sql"], st["param_samples"][0]])
    mix = {}
    for key in order:
        mix.setdefault(key[0], []).append(groups[key])
    return mix


def workload_writers(mix):
    return [{"name": p, "rate": round(sum(t["weight"] for t in txns), 6), "txns": txns} for p, txns in mix.items()]


def uniform_writers(mix, n, rate):
    """n 个写者各 rate 次/秒，依次借用各写进程的事务组合（超过进程数时名字加 #k）。"""
    procs = list(mix)
    out = []
    for k in range(n):
        p = procs[k % len(procs)]
        out.append({"name": p if k < len(procs) else f"{p}#{k // len(procs) + 1}", "rate": rate, "txns": mix[p]})
    return out


def run_load(ro, wl, cols_map, work, opts):
    """副本 B 上跑写争用（工作负载速率 + N×w 判据 + 可选延迟事务演示）与 WAL 实验。"""
    copy_b = os.path.join(work, "copy_b.db")
    make_copy(ro, copy_b)
    c = sqlite3.connect(copy_b, isolation_level=None)
    c.execute("PRAGMA journal_mode=WAL")
    c.close()
    out = {}
    mix = writer_mix(wl, cols_map)
    conn_cfg = wl["connection"]
    busy = opts["busy_timeout"] if opts["busy_timeout"] is not None else conn_cfg["busy_timeout_ms"] / 1000.0
    if "contention" in opts["sections"] and mix:
        common_kw = {"duration": opts["duration"], "busy_timeout": busy, "synchronous": conn_cfg["synchronous"],
                     "budget_ms": opts["budget_ms"]}
        scen = [{"name": "workload", "gate": True,
                 **load.contention(copy_b, workload_writers(mix), begin=conn_cfg["begin"], seed=1, **common_kw)},
                {"name": f"{opts['writers']}x{opts['rate']:g}/s", "gate": True,
                 **load.contention(copy_b, uniform_writers(mix, opts["writers"], opts["rate"]), begin="immediate",
                                   seed=2, **common_kw)}]
        if opts["deferred_demo"]:
            scen.append({"name": f"{opts['writers']}x{opts['rate']:g}/s deferred+think5ms", "gate": False,
                         **load.contention(copy_b, uniform_writers(mix, opts["writers"], opts["rate"]),
                                           begin="deferred", think_ms=5.0, seed=3, **common_kw)})
        out["contention"] = {"busy_timeout": busy, "scenarios": scen}
    if "wal" in opts["sections"]:
        out["wal"] = {"scenarios": [load.wal_growth(copy_b, autocheckpoint=ac, long_reader=lr,
                                                    duration=opts["wal_duration"], synchronous=conn_cfg["synchronous"])
                                    for ac in opts["wal_checkpoints"] for lr in (False, True)]}
    return out


# ---- 编排 ------------------------------------------------------------------------------
DEFAULTS = {"sections": SECTIONS, "duration": 4.0, "wal_duration": 6.0, "writers": 3, "rate": 50.0,
            "busy_timeout": None, "budget_ms": 200.0, "deferred_demo": False, "measure_timeout": 5.0,
            "scan_cost_red": 10_000.0, "wal_checkpoints": (200, 1000, 4000), "top": 20}


def diagnose(db_path, workload, **kw):
    """体检主入口（Python 接口）。kw 见 DEFAULTS。返回报告 dict（字段表见 CONTRACT.md §1.3）。"""
    opts = dict(DEFAULTS, **kw)
    unknown = set(opts["sections"]) - set(SECTIONS)
    if unknown:
        raise DoctorError(f"unknown sections {sorted(unknown)}")
    wl = load_workload(workload)
    check_db_file(db_path)
    before = file_state(db_path)
    ro, how = open_readonly(db_path)
    work = common.scratch_dir("qdperf-doctor-")
    t0 = time.perf_counter()
    rep = {"format": REPORT_FORMAT, "machine": common.machine(), "workload_name": wl.get("name", ""),
           "db": {"path": os.path.abspath(db_path), "size_bytes": before["size"], "sha256_before": before["sha256"]},
           "options": {k: (list(v) if isinstance(v, tuple) else v) for k, v in opts.items()}}
    try:
        rep.update(_static_part(ro, how, wl, work, opts))
        if {"contention", "wal"} & set(opts["sections"]):
            rep.update(run_load(ro, wl, columns_map(rep.pop("_schema")), work, opts))
        else:
            rep.pop("_schema")
    finally:
        ro.close()
        shutil.rmtree(work, ignore_errors=True)
    after = file_state(db_path)
    rep["db"].update(sha256_after=after["sha256"], untouched=before == after,
                     siblings_before=before["siblings"], siblings_after=after["siblings"])
    rep["seconds"] = round(time.perf_counter() - t0, 2)
    rep["findings"] = build_findings(rep, opts)
    rep["ok"] = not any(f["level"] == "red" for f in rep["findings"])
    return rep


def _static_part(ro, how, wl, work, opts):
    schema = schema_info(ro)
    cols = columns_map(schema)
    copy_a = os.path.join(work, "copy_a.db")
    make_copy(ro, copy_a)
    a = sqlite3.connect(copy_a, isolation_level=None)
    try:
        counts = table_counts(a, schema)
        fresh, after_analyze, analyze_s = harvest_stats(a, wl, cols)
        stmts = analyze_statements(a, wl, schema, counts, fresh, opts["measure_timeout"])
        for st in stmts:
            st["verdict_after_analyze"] = after_analyze[st["id"]]
        out = {"_schema": schema, "tables": {t: {"rows": counts[t], "indexes": v["indexes"]} for t, v in schema.items()},
               "statements": stmts, "top": top_by_cost(stmts, opts["top"]), "analyze_seconds": analyze_s,
               "sqlite_version": sqlite3.sqlite_version}
        if "indexes" in opts["sections"]:
            out["indexes"] = _index_part(a, wl, schema, cols, counts, stmts)
        if "file" in opts["sections"]:
            out["file"] = file_checkup(ro, schema, counts, a, how)
    finally:
        a.close()
    return out


def _index_part(a, wl, schema, cols, counts, stmts):
    by_id = {s["id"]: s for s in wl["statements"]}
    write_before = write_costs(a, wl, cols)
    sugs, unsolvable, already = advisor.propose(stmts, schema, cols)
    rowid_cols = {t: v["pk"][0] for t, v in schema.items() if v["integer_pk"]}
    verified, rejected, analyze_s = advisor.verify(a, sugs, by_id, cols, counts, explain, rowid_cols)
    write_after = write_costs(a, wl, cols)
    final, uses = {}, {}
    for st in wl["statements"]:
        parsed = P.parse_statement(st["sql"], cols)
        nodes = advisor.resolve_aliases(explain(a, st["sql"], st["param_samples"][0]), parsed)
        final[st["id"]] = P.classify(nodes)["verdict"]
        uses[st["id"]] = sorted({n["index"] for n in nodes if n.get("index")})
    for sug in verified:                                 # 最终状态下实际用到这个索引的全部语句（含别的建议“顺带”修好的）
        sug["serves"] = [sid for sid in by_id if sug["name"] in uses[sid]]
    return {"suggestions": verified, "rejected": rejected, "unsolvable": unsolvable, "already_indexed": already,
            "write_amplification": advisor.write_amplification(wl, schema, verified, cols),
            "write_us_before": write_before, "write_us_after": write_after, "analyze_seconds": analyze_s,
            "verdict_after_all": final, "sql": [s["sql"] for s in verified] + (["ANALYZE;"] if verified else [])}


# ---- 发现 / 判定 -----------------------------------------------------------------------------
def build_findings(rep, opts):
    f = []

    def add(level, code, msg):
        f.append({"level": level, "code": code, "message": msg})

    for st in rep["statements"]:
        if st["verdict"] == "full_scan":
            hot = st["cost"] >= opts["scan_cost_red"]
            add("red" if hot else "yellow", "HOT_FULL_SCAN" if hot else "FULL_SCAN",
                f"{st['id']}: 全表扫描 {', '.join(st['scan_tables'])}，{st['per_second']:g} 次/秒 × "
                f"{st['rows_est']:,.0f} 行 = {st['cost']:,.0f} 行/秒")
        elif st["verdict"] == "temp_btree" and st["cost"] >= opts["scan_cost_red"]:
            add("yellow", "TEMP_BTREE", f"{st['id']}: 临时 B-tree（{'; '.join(st['temp_btree'])}）")
    idx = rep.get("indexes", {})
    for sug in idx.get("rejected", []):
        add("info", "SUGGESTION_REJECTED", f"{sug['name']}: {sug['reject_reason']}")
    for scen in rep.get("contention", {}).get("scenarios", []):
        if not scen["ok"]:
            add("red" if scen["gate"] else "yellow", "CONTENTION" if scen["gate"] else "CONTENTION_DEMO",
                f"{scen['name']}: p99 等待 {_f(scen['overall']['wait_ms']['p99'])} ms、SQLITE_BUSY {scen['overall']['busy']}；"
                f"根因：{'；'.join(scen.get('root_cause', []))}")
    for w in rep.get("wal", {}).get("scenarios", []):
        if w["config"]["long_reader"] and w["resets"] == 0 and w["max_live_frames"] > w["config"]["autocheckpoint"]:
            add("yellow", "WAL_GROWTH", f"长读事务下 WAL {w['config']['duration']:g} s 内一次也没能重置，涨到 "
                                        f"{w['max_bytes'] / 1e6:.1f} MB（autocheckpoint={w['config']['autocheckpoint']} 页挡不住）")
    _file_findings(rep.get("file"), add)
    if not rep["db"]["untouched"]:
        add("red", "ORIGINAL_MODIFIED", "原库文件或其旁路文件在体检前后不一致")
    return f


def _file_findings(fc, add):
    if not fc:
        return
    if fc["fragmentation"] >= 0.05:
        add("yellow", "FRAGMENTED", f"碎片率 {fc['fragmentation'] * 100:.1f}%（freelist {fc['pragmas']['freelist_count']} "
                                    f"页）→ VACUUM 或 auto_vacuum=INCREMENTAL")
    for t in fc["no_primary_key"]:
        add("yellow", "NO_PRIMARY_KEY", f"表 {t} 没有声明主键")
    for fk in fc["fk_without_index"]:
        add("yellow", "FK_WITHOUT_INDEX", f"{fk['table']}.{fk['column']} → {fk['ref_table']} 外键列没有索引")
    if not fc["analyzed"]:
        add("yellow", "NEVER_ANALYZED", "没有 sqlite_stat1：从没跑过 ANALYZE")
    for s in fc["stats_stale"]:
        add("yellow", "STATS_STALE", f"{s['object']}: 统计 {s['stat_rows']:,} 行 vs 实际 {s['actual_rows']:,} 行")
    if fc["stats_missing"]:
        add("yellow", "STATS_MISSING", f"{len(fc['stats_missing'])} 个表 / 索引没有统计：{', '.join(fc['stats_missing'])}")


def first_line(rep):
    sts = rep["statements"]
    idx = rep.get("indexes")
    gate = [c for c in rep.get("contention", {}).get("scenarios", []) if c["gate"]]
    wal = rep.get("wal", {}).get("scenarios", [])
    p99 = "-" if not gate else f"{max((c['overall']['wait_ms']['p99'] or 0.0) for c in gate):.2f}"
    busy = "-" if not gate else str(sum(c["overall"]["busy"] for c in gate))
    wal_mb = "-" if not wal else f"{max(w['max_bytes'] for w in wal) / 1e6:.1f}"
    sug = "-" if not idx else f"{len(idx['suggestions'])}/{len(idx['suggestions']) + len(idx['rejected'])}"
    red = sum(f["level"] == "red" for f in rep["findings"])
    yellow = sum(f["level"] == "yellow" for f in rep["findings"])
    return (f"SQLITE_DOCTOR {'OK' if rep['ok'] else 'FAIL'} statements={len(sts)} "
            f"full_scan={sum(s['verdict'] == 'full_scan' for s in sts)} "
            f"temp_btree={sum('temp_btree' in s['flags'] for s in sts)} suggestions={sug} "
            f"contention_p99_ms={p99} busy={busy} wal_max_mb={wal_mb} red={red} yellow={yellow}")


# ---- 文本体检表 ------------------------------------------------------------------------------
def _f(x, spec=".2f"):
    return "-" if x is None else format(x, spec)


def format_text(rep):
    out = [first_line(rep), f"库 {rep['db']['path']}  {rep['db']['size_bytes'] / 1e6:.1f} MB  SQLite {rep['sqlite_version']}"
           f"  原库未改动: {'是' if rep['db']['untouched'] else '否'}  用时 {rep['seconds']} s"]
    by = {s["id"]: s for s in rep["statements"]}
    rows = [(k + 1, sid, by[sid]["process"], f"{by[sid]['per_second']:g}", by[sid]["verdict"], f"{by[sid]['rows_est']:,.0f}",
             f"{by[sid]['cost']:,.0f}", _f(by[sid].get("measured", {}).get("ms_best"), ".3f"),
             _f(by[sid].get("db_ms_per_second"), ".1f"), by[sid]["verdict_after_analyze"])
            for k, sid in enumerate(rep["top"])]
    load_ms = sum(s.get("db_ms_per_second", 0.0) for s in rep["statements"])
    out += ["", f"== 体检表：按 频次×行数 排序（前 {len(rows)} / 共 {len(by)} 条）；全部语句合计占库 {load_ms:,.1f} ms/秒"
                f"（> 1000 即单连接跟不上工作负载）==",
            common.text_table(["#", "语句", "进程", "次/秒", "判定", "行/次(估)", "行/秒", "实测ms", "占库ms/秒", "ANALYZE后"],
                              rows)]
    out += ["", "== 计划 =="] + [f"{s['id']}: {' | '.join(s['plan'])}" for s in rep["statements"]]
    if "indexes" in rep:
        out += _index_text(rep["indexes"], by)
    if "contention" in rep:
        out += _contention_text(rep["contention"])
    if "wal" in rep:
        out += _wal_text(rep["wal"])
    if "file" in rep:
        out += _file_text(rep["file"])
    out += ["", "== 发现 =="] + [f"[{f['level']}] {f['code']}: {f['message']}" for f in rep["findings"]]
    return "\n".join(out)


def _index_text(idx, by):
    amp = idx["write_amplification"]
    rows = [(k + 1, s["name"], ",".join(s["serves"]), s["kind"],
             f"{sum(c['ok'] for c in s['check_analyzed'].values())}/{len(s['check_analyzed'])}"
             f"（ANALYZE前 {sum(c['ok'] for c in s['check_as_is'].values())}/{len(s['check_as_is'])}）",
             f"{s['size_est_bytes'] / 1e6:.2f}", f"{s['size_bytes'] / 1e6:.2f}", s["build_seconds"],
             amp["per_suggestion_extra_btree_writes_per_second"][s["name"]]) for k, s in enumerate(idx["suggestions"])]
    out = ["", f"== 索引建议（{len(idx['suggestions'])} 条，已在临时副本上执行下面的 SQL 并用 EXPLAIN QUERY PLAN 验证："
               f"验证列 = 建索引 + ANALYZE 后对应语句不再扫描的条数；括号里是只建索引、还没 ANALYZE 时的情况）==",
           common.text_table(["#", "索引", "语句", "类型", "验证", "估算MB", "实测MB", "构建s", "+B-tree写/秒"], rows)]
    out += [f"  {sql}" for sql in idx["sql"]]
    for s in idx["rejected"]:
        out.append(f"  未采用：{s['name']}（语句 {','.join(s['statements'])}）：{s['reject_reason']}")
    if idx["unsolvable"]:
        out += ["", "== 索引解决不了的语句 =="] + [f"{u['statement']} ({u['table']}): {'; '.join(u['reasons'])}"
                                              for u in idx["unsolvable"]]
    out += ["", f"== 写放大：B-tree 写入 {amp['btree_writes_per_second_before']:g} → "
                f"{amp['btree_writes_per_second_after']:g} 次/秒（{common.fmt_pct(amp['relative_increase'])}）；"
                f"写语句实测单次耗时 µs（建议前 → 建议后）=="]
    out += [f"{sid}: {us} → {idx['write_us_after'].get(sid)}" for sid, us in idx["write_us_before"].items()]
    return out


def _contention_text(con):
    rows, notes = [], []
    for sc in con["scenarios"]:
        for w in sc["writers"]:
            rows.append((sc["name"], w["name"], f"{w['achieved_rate']:.1f}", w["committed"], _f(w["wait_ms"]["p50"], ".3f"),
                         _f(w["wait_ms"]["p99"], ".3f"), _f(w["wait_ms"]["max"], ".3f"), w["busy"],
                         _f(w["hold_ms"]["p99"], ".3f")))
        notes.append(f"{sc['name']}: {'通过' if sc['ok'] else '不通过'}；{sc['starvation']['text']}")
        notes += [f"    根因：{c}" for c in sc.get("root_cause", [])]
    return ["", f"== 写争用（真进程 + WAL，busy_timeout={con['busy_timeout']:g} s）==",
            common.text_table(["场景", "写者", "实际次/秒", "提交", "等待p50ms", "等待p99ms", "最大ms", "BUSY", "持锁p99ms"],
                              rows)] + notes


def _wal_text(wal):
    rows = [(w["config"]["autocheckpoint"], "有" if w["config"]["long_reader"] else "无", f"{w['max_bytes'] / 1e6:.1f}",
             w["max_live_frames"], w["resets"], _f(w["commit_ms"]["p99"], ".2f"),
             common.sparkline([c[2] for c in w["curve"]], 40)) for w in wal["scenarios"]]
    return ["", "== WAL 检查点：WAL 有效帧数随时间（每 0.1 s 采样；写进程 200 次/秒 × 20 行；长读 = 面板两条连接交替持有读事务）==",
            common.text_table(["autocheckpoint", "长读事务", "WAL文件峰值MB", "有效帧峰值", "重置次数", "提交p99ms",
                               "有效帧曲线"], rows), wal_conclusion(wal)]


def wal_conclusion(wal):
    """由实测数据给一句结论（不是公式）：无长读时峰值被 autocheckpoint 截住；有长读时 WAL 不重置、一路涨。"""
    sc = wal["scenarios"]
    free = [w for w in sc if not w["config"]["long_reader"]]
    held = [w for w in sc if w["config"]["long_reader"]]
    capped = all(w["max_live_frames"] <= 1.1 * w["config"]["autocheckpoint"] + 50 for w in free)
    stuck = all(w["resets"] == 0 for w in held)
    caps = ", ".join(f"{w['max_live_frames']}/{w['config']['autocheckpoint']}" for w in free)
    peaks = ", ".join(f"{w['max_bytes'] / 1e6:.1f}" for w in held)
    return (f"结论：无长读时 WAL 有效帧峰值{'都被' if capped else '没有都被'} autocheckpoint 截住（峰值/阈值 {caps}）；"
            f"有长读时{'全部' if stuck else '并非全部'}场景 WAL 一次也没重置，峰值 {peaks} MB，与 autocheckpoint 取值无关"
            f" → 面板读走副本 / 缩短读事务，而不是调 autocheckpoint")


def _file_text(fc):
    p = fc["pragmas"]
    out = ["", "== 库文件体检 ==",
           f"页大小 {p['page_size']}，页数 {p['page_count']:,}，空闲页 {p['freelist_count']:,}（碎片率 "
           f"{fc['fragmentation'] * 100:.2f}%），auto_vacuum={p['auto_vacuum']}，日志模式（文件头）{fc['journal_mode_header']}",
           f"没主键的表：{', '.join(fc['no_primary_key']) or '无'}",
           f"没索引的外键列：{', '.join(x['table'] + '.' + x['column'] for x in fc['fk_without_index']) or '无'}",
           f"ANALYZE 过：{'是' if fc['analyzed'] else '否'}（sqlite_stat1 {fc['stat1_entries']} 条）；过期 "
           f"{len(fc['stats_stale'])} 条，缺失 {len(fc['stats_missing'])} 条"]
    out += [f"  过期：{s['object']} 统计 {s['stat_rows']:,} 行 / 实际 {s['actual_rows']:,} 行" for s in fc["stats_stale"]]
    if fc["largest_objects"]:
        out.append("最大的对象：" + "，".join(f"{n} {b / 1e6:.1f} MB" for n, b in fc["largest_objects"][:6]))
    return out


# ---- CLI ------------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3.12 -m qdperf.sqlite_doctor", description="SQLite 体检器")
    ap.add_argument("db")
    ap.add_argument("--workload", required=True)
    ap.add_argument("--sections", default=",".join(SECTIONS))
    ap.add_argument("--duration", type=float, default=DEFAULTS["duration"], help="每个写争用场景的秒数")
    ap.add_argument("--wal-duration", type=float, default=DEFAULTS["wal_duration"])
    ap.add_argument("--writers", type=int, default=DEFAULTS["writers"])
    ap.add_argument("--rate", type=float, default=DEFAULTS["rate"], help="判据场景里每个写者的次/秒")
    ap.add_argument("--busy-timeout", type=float, default=None, help="秒；缺省取工作负载 connection.busy_timeout_ms")
    ap.add_argument("--budget-ms", type=float, default=DEFAULTS["budget_ms"])
    ap.add_argument("--scan-cost-red", type=float, default=DEFAULTS["scan_cost_red"])
    ap.add_argument("--deferred-demo", action="store_true", help="另跑一个 BEGIN DEFERRED 读后写的根因演示场景")
    ap.add_argument("--quick", action="store_true", help="缩短实验：写争用 2 s、WAL 3 s、只测 autocheckpoint 1000")
    ap.add_argument("--json", default=None)
    ap.add_argument("--emit-sql", default=None, help="把通过验证的 CREATE INDEX 写到这个文件")
    args = ap.parse_args(argv)
    kw = {"sections": tuple(s for s in args.sections.split(",") if s), "duration": args.duration,
          "wal_duration": args.wal_duration, "writers": args.writers, "rate": args.rate,
          "busy_timeout": args.busy_timeout, "budget_ms": args.budget_ms, "scan_cost_red": args.scan_cost_red,
          "deferred_demo": args.deferred_demo}
    if args.quick:
        kw.update(duration=2.0, wal_duration=3.0, wal_checkpoints=(1000,))
    try:
        rep = diagnose(args.db, args.workload, **kw)
    except DoctorError as exc:
        print(f"SQLITE_DOCTOR ERROR {exc}")
        return 2
    print(format_text(rep))
    if args.json:
        common.write_json(args.json, rep)
    if args.emit_sql and "indexes" in rep:
        with open(args.emit_sql, "w", encoding="utf-8") as f:
            f.write("\n".join(rep["indexes"]["sql"]) + "\n")
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
