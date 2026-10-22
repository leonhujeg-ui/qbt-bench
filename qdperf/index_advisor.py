"""索引建议（G1）：对全表扫描 / 临时 B-tree 排序的语句给候选索引，并在临时副本上用 EXPLAIN QUERY PLAN 验证。

propose(statements, schema)
    候选列顺序 = 等值 → 第一个范围 → 排序（sqlplan.candidate_columns）；已有索引以候选列为前缀时不重复建议；
    同一张表上一个候选是另一个的前缀时合并成较长的那个（较短候选的语句一并验证）。看不懂 / 用不上索引的语句单列原因。
verify(conn, suggestions, ...)
    副本上逐个 CREATE INDEX：记构建耗时、估算大小（按 SQLite 记录格式对抽样行算单元字节）与实测大小（dbstat）；
    在“原统计状态”与“重新 ANALYZE 之后”两种状态下各 EXPLAIN 一遍对应语句，两种状态都不再 SCAN 该表（排序类：不再有
    临时 B-tree）才算 verified；验证不过的进 rejected，不出现在建议里。
write_amplification(...)
    写放大模型：每条写语句每秒触达的 B-tree 数（表 1 棵 + 受影响的索引；INSERT / DELETE 影响全部索引，UPDATE 影响含 SET 列的
    索引），加建议前后对比；另由 sqlite_doctor 实测写语句前后的单次耗时。
"""
import math
import time

from qdperf import sqlplan as P

FILL = 0.995       # 估算用的叶页填充率：CREATE INDEX 按键顺序构建，叶页几乎全满（全量合成库上 8 个索引 dbstat 实测校准，
                   # 每次体检都同时给出估算与实测，误差写进报告）；随机插入长出来的索引约 2/3 满，不适用此系数
SAMPLE_ROWS = 2000


def resolve_aliases(nodes, parsed):
    """EXPLAIN 输出里出现的是别名（3.45：'SCAN t1'），换回真实表名，别名留在 alias 字段。"""
    amap = {t["alias"]: t["table"] for t in parsed["tables"]}
    for nd in nodes:
        if "table" in nd:
            nd["alias"] = nd["table"]
            nd["table"] = amap.get(nd["table"], nd["table"])
    return nodes


def index_name(table, cols):
    return "qdperf_" + table + "_" + "_".join(cols)


def propose(statements, schema, cols_map):
    """statements = sqlite_doctor.analyze_statements 的输出。→ (suggestions, unsolvable, already_indexed)。"""
    cands, unsolvable, already = [], [], []
    for rec in statements:
        if rec["kind"] not in ("select", "update", "delete"):
            continue
        scan = list(rec["scan_tables"])
        sort = bool(rec["temp_btree"]) and not scan
        if not scan and not sort:
            continue
        parsed = P.parse_statement(rec["sql"], cols_map)
        targets = scan or sorted({t for t, _c, _d in parsed["order_by"] + parsed["group_by"] if t})
        for table in targets:
            _propose_one(rec, parsed, table, "scan" if scan else "sort", schema, cands, unsolvable, already)
    return _merge(cands), unsolvable, already


def _propose_one(rec, parsed, table, kind, schema, cands, unsolvable, already):
    cols, why = P.candidate_columns(parsed, table)
    if kind == "sort" and not why["order"]:
        unsolvable.append({"statement": rec["id"], "table": table, "kind": kind,
                           "reasons": ["排序列不全在本表 / 方向混合，普通索引消不掉临时 B-tree"]})
        return
    if not cols:
        reasons = [f"{u['text']}: {u['reason']}" for u in parsed["unusable"]] or ["本表没有 WHERE / ORDER BY 可用列"]
        unsolvable.append({"statement": rec["id"], "table": table, "kind": kind, "reasons": reasons})
        return
    for name, ix in sorted(schema.get(table, {}).get("indexes", {}).items()):
        if P.covers(ix["columns"], cols):
            already.append({"statement": rec["id"], "table": table, "columns": cols, "index": name})
            return
    cands.append({"table": table, "columns": cols, "kind": kind, "statements": [rec["id"]],
                  "why": {rec["id"]: why}})


def _merge(cands):
    """同表上：完全相同的合并；一个是另一个前缀的并入较长者（kind 取 scan 优先）。顺序确定。"""
    out = []
    for c in sorted(cands, key=lambda c: (c["table"], -len(c["columns"]), c["columns"])):
        host = next((o for o in out if o["table"] == c["table"] and P.covers(o["columns"], c["columns"])), None)
        if host is None:
            out.append(dict(c, statements=list(c["statements"]), why=dict(c["why"])))
            continue
        for sid in c["statements"]:
            if sid not in host["statements"]:
                host["statements"].append(sid)
        host["why"].update(c["why"])
        if c["kind"] == "scan":
            host["kind"] = "scan"
    for o in out:
        o["name"] = index_name(o["table"], o["columns"])
    return out


# ---- 大小 ------------------------------------------------------------------------------
def sample_rowids(conn, table, k=SAMPLE_ROWS):
    lo, hi = conn.execute(f'SELECT min(rowid), max(rowid) FROM "{table}"').fetchone()
    if lo is None:
        return []
    if hi - lo + 1 <= k:
        return list(range(lo, hi + 1))
    return [lo + (hi - lo) * i // (k - 1) for i in range(k)]


def estimate_size(conn, table, cols, rows, page_size):
    """估算索引字节：行数 × 抽样平均单元字节 / (页可用字节 × FILL) × 页大小（向上取整到页）。"""
    ids = sample_rowids(conn, table)
    collist = ", ".join(f'"{c}"' for c in cols)
    sizes = []
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        q = f'SELECT {collist}, rowid FROM "{table}" WHERE rowid IN ({",".join("?" * len(chunk))})'
        for row in conn.execute(q, chunk):
            sizes.append(P.index_entry_bytes(row[:-1], row[-1]))
    if not sizes or rows == 0:
        return page_size
    avg = sum(sizes) / len(sizes)
    pages = math.ceil(rows * avg / ((page_size - 8) * FILL))
    return int(pages * page_size)


def _quoted(cols):
    return ", ".join(f'"{c}"' for c in cols)


def used_bytes(conn):
    pc = conn.execute("PRAGMA page_count").fetchone()[0]
    fl = conn.execute("PRAGMA freelist_count").fetchone()[0]
    ps = conn.execute("PRAGMA page_size").fetchone()[0]
    return (pc - fl) * ps


def object_size(conn, name):
    """dbstat 实测某个 B-tree 的字节数；没有 dbstat 时返回 None。"""
    try:
        r = conn.execute("SELECT sum(pgsize) FROM dbstat WHERE name = ?", (name,)).fetchone()
    except Exception:                                   # noqa: BLE001 —— 未编译 dbstat 时是 OperationalError
        return None
    return r[0]


# ---- 验证 ------------------------------------------------------------------------------
def check_statement(conn, st, sug, cols_map, explain):
    """建议对这条语句是否生效：该表不再 SCAN（排序类：也不再有临时 B-tree）。"""
    parsed = P.parse_statement(st["sql"], cols_map)
    nodes = resolve_aliases(explain(conn, st["sql"], st["param_samples"][0]), parsed)
    cls = P.classify(nodes)
    scanned = {n["table"] for n in nodes if n["op"] == "scan"}
    ok = sug["table"] not in scanned and (sug["kind"] != "sort" or not cls["temp_btree"])
    used = any(n.get("index") == sug["name"] for n in nodes)
    return {"ok": ok, "uses_index": used, "verdict": cls["verdict"], "plan": [n["detail"] for n in nodes]}


def _create(conn, sug, counts, page_size):
    sug["name"] = index_name(sug["table"], sug["columns"])
    sug["size_est_bytes"] = estimate_size(conn, sug["table"], sug["columns"], counts[sug["table"]], page_size)
    before = used_bytes(conn)
    t0 = time.perf_counter()
    conn.execute(f'CREATE INDEX "{sug["name"]}" ON "{sug["table"]}"({_quoted(sug["columns"])})')
    sug["build_seconds"] = round(time.perf_counter() - t0, 3)
    measured = object_size(conn, sug["name"])
    sug["size_bytes"] = measured if measured is not None else used_bytes(conn) - before
    sug["size_source"] = "dbstat" if measured is not None else "page_count-freelist delta"
    sug["sql"] = f'CREATE INDEX {sug["name"]} ON {sug["table"]}({", ".join(sug["columns"])});'


def _checks(conn, sug, by_id, cols_map, explain):
    return {sid: check_statement(conn, by_id[sid], sug, cols_map, explain) for sid in sug["statements"]}


def _covering_variant(sug, by_id, cols_map, rowid_cols):
    """在候选列后面补上语句引用到的本表其余列（覆盖索引；INTEGER PRIMARY KEY 列本来就在索引里，不补）。
    补不出新列时返回 None。"""
    extra = []
    for sid in sug["statements"]:
        for c in P.referenced_columns(by_id[sid]["sql"], cols_map, sug["table"]):
            if c not in sug["columns"] and c not in extra and c != rowid_cols.get(sug["table"]):
                extra.append(c)
    if not extra:
        return None
    return dict(sug, columns=sug["columns"] + extra, covering=True, plain_columns=sug["columns"])


def verify(conn, suggestions, by_id, cols_map, counts, explain, rowid_cols=None, rounds=4):
    """建全部候选索引（记“建后、ANALYZE 前”的计划）→ ANALYZE → 逐个验证：对应语句不再 SCAN 且该索引真被用到；
    仍扫描的换覆盖索引再验，没被用到的（语句已被别的建议覆盖）删掉；重复直到一轮内没有变化（最多 rounds 轮）。
    判据状态 = “建议的索引 + ANALYZE”（输出的 SQL 末尾带 ANALYZE），即用户照做之后的状态。返回 (verified, rejected, 秒)。"""
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    live, rejected = list(suggestions), []
    for sug in live:
        _create(conn, sug, counts, page_size)
    for sug in live:
        sug["check_as_is"] = _checks(conn, sug, by_id, cols_map, explain)
    analyze_s = 0.0
    for _ in range(rounds):
        t0 = time.perf_counter()
        conn.execute("ANALYZE")
        analyze_s += time.perf_counter() - t0
        if not _verify_round(conn, live, rejected, by_id, cols_map, counts, explain, rowid_cols or {}, page_size):
            break
    for sug in live:
        sug["verified"] = True
        sug["needs_analyze"] = not all(c["ok"] for c in sug["check_as_is"].values())
    return live, rejected, round(analyze_s, 3)


def _verify_round(conn, live, rejected, by_id, cols_map, counts, explain, rowid_cols, page_size):
    """一轮：返回本轮是否改动了索引集合（改动了就要重新 ANALYZE 再验）。"""
    changed = False
    for sug in list(live):
        sug["check_analyzed"] = _checks(conn, sug, by_id, cols_map, explain)
        ok = all(c["ok"] for c in sug["check_analyzed"].values())
        used = any(c["uses_index"] for c in sug["check_analyzed"].values())
        if ok and used:
            continue
        changed = True
        conn.execute(f'DROP INDEX "{sug["name"]}"')
        live.remove(sug)
        cov = None if ok or sug.get("covering") else _covering_variant(sug, by_id, cols_map, rowid_cols)
        if cov is not None:
            _create(conn, cov, counts, page_size)
            cov["check_as_is"] = _checks(conn, cov, by_id, cols_map, explain)
            live.append(cov)
            rejected.append(dict(sug, reject_reason=f"ANALYZE 后仍扫描 → 改试覆盖索引 ({', '.join(cov['columns'])})"))
        else:
            rejected.append(dict(sug, reject_reason="没有任何语句用到它（语句已被别的建议索引覆盖）" if ok
                                 else "ANALYZE 后仍扫描，且没有可补的覆盖列"))
    return changed


# ---- 写放大 ------------------------------------------------------------------------------
def affected(kind, set_cols, index_cols):
    if kind in ("insert", "delete"):
        return True
    return kind == "update" and any(c in index_cols for c in set_cols)


def write_amplification(wl, schema, suggestions, cols_map):
    """→ {statements: [{id, table, rate, btrees_before, btrees_after}], per_second_before/after, per_suggestion}。"""
    rows, per_sug = [], {s["name"]: 0.0 for s in suggestions}
    tot_b = tot_a = 0.0
    for st in wl["statements"]:
        parsed = P.parse_statement(st["sql"], cols_map)
        if parsed["kind"] not in ("insert", "update", "delete") or not parsed["tables"]:
            continue
        table = parsed["tables"][0]["table"]
        existing = schema.get(table, {}).get("indexes", {})
        b = 1 + sum(affected(parsed["kind"], parsed["set_columns"], ix["columns"]) for ix in existing.values())
        extra = [s for s in suggestions if s["table"] == table
                 and affected(parsed["kind"], parsed["set_columns"], s["columns"])]
        for s in extra:
            per_sug[s["name"]] += st["per_second"]
        rows.append({"id": st["id"], "table": table, "rate": st["per_second"], "btrees_before": b,
                     "btrees_after": b + len(extra)})
        tot_b += b * st["per_second"]
        tot_a += (b + len(extra)) * st["per_second"]
    return {"statements": rows, "btree_writes_per_second_before": round(tot_b, 3),
            "btree_writes_per_second_after": round(tot_a, 3),
            "relative_increase": round((tot_a - tot_b) / tot_b, 4) if tot_b else 0.0,
            "per_suggestion_extra_btree_writes_per_second": {k: round(v, 3) for k, v in per_sug.items()}}
