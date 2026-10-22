"""SQL 计划与语句的纯函数分析（不碰数据库）：

- parse_eqp / classify：把 ``EXPLAIN QUERY PLAN`` 的行解析成节点，判定 全表扫描 / 临时 B-tree / 用了索引 / 覆盖索引 / 主键；
- tokenize / parse_statement：轻量 SQL 解析（SELECT / UPDATE / DELETE / INSERT / REPLACE）——表与别名、WHERE 与 JOIN ON 的合取项、
  ORDER BY / GROUP BY、UPDATE 的 SET 列、INSERT 的列；
- candidate_columns：候选索引列顺序 = 等值列（出现顺序）→ 第一个范围列 → 排序列（ORDER BY，否则 GROUP BY）；
- record_bytes / index_entry_bytes：按 SQLite 记录格式（serial type）估算一条索引项的字节数。

解析器只覆盖交易系统里常见的写法；看不懂的项（OR、对列套函数、LIKE、子查询）一律记为“不可用于索引”，并带原因返回，
绝不猜。所有结论都要在临时副本上用 EXPLAIN QUERY PLAN 复核（sqlite_doctor 负责）。
"""
import re

# ---- EXPLAIN QUERY PLAN ----------------------------------------------------------------
_RE_SCAN = re.compile(r"^SCAN (?:TABLE )?(\S+)(?: AS (\S+))?(?: USING (COVERING )?INDEX (\S+))?")
_RE_SEARCH = re.compile(r"^SEARCH (?:TABLE )?(\S+)(?: AS (\S+))? USING (AUTOMATIC )?(COVERING )?(?:INDEX (\S+)|"
                        r"(INTEGER PRIMARY KEY|PRIMARY KEY))(?: \((.*)\))?")
_RE_TEMP = re.compile(r"^USE TEMP B-TREE FOR (.+)$")
_PSEUDO = ("CONSTANT ROW", "SUBQUERY", "CTE")


def parse_eqp(rows):
    """rows = cursor.fetchall() of EXPLAIN QUERY PLAN（(id, parent, notused, detail)）→ 节点列表。"""
    nodes = []
    for row in rows:
        node_id, parent, detail = row[0], row[1], row[-1]
        nodes.append(dict(_parse_detail(detail), id=node_id, parent=parent, detail=detail))
    return nodes


def _parse_detail(detail):
    m = _RE_TEMP.match(detail)
    if m:
        return {"op": "temp_btree", "purpose": m.group(1)}
    m = _RE_SCAN.match(detail)
    if m and not detail.startswith(tuple("SCAN " + p for p in _PSEUDO)):
        return {"op": "scan", "table": m.group(1), "alias": m.group(2) or m.group(1),
                "covering": bool(m.group(3)), "index": m.group(4)}
    m = _RE_SEARCH.match(detail)
    if m:
        return {"op": "search", "table": m.group(1), "alias": m.group(2) or m.group(1),
                "automatic": bool(m.group(3)), "covering": bool(m.group(4)), "index": m.group(5),
                "rowid": m.group(6) is not None, "constraints": m.group(7) or ""}
    return {"op": "other"}


VERDICTS = ("full_scan", "temp_btree", "automatic_index", "index", "covering_index", "rowid", "none")


def classify(nodes):
    """判定一条语句：verdict 取最坏（VERDICTS 顺序）；scan_tables = 被全表扫描（含整索引扫描）的表名。"""
    flags = set()
    scan_tables, temp = [], []
    for n in nodes:
        op = n["op"]
        if op == "scan":
            flags.add("full_scan")
            scan_tables.append(n["table"])
        elif op == "temp_btree":
            flags.add("temp_btree")
            temp.append(n["purpose"])
        elif op == "search":
            if n["automatic"]:
                flags.add("automatic_index")
            elif n["rowid"]:
                flags.add("rowid")
            else:
                flags.add("covering_index" if n["covering"] else "index")
    verdict = next((v for v in VERDICTS if v in flags), "none")
    return {"verdict": verdict, "flags": sorted(flags), "scan_tables": sorted(set(scan_tables)),
            "temp_btree": temp}


def search_constraint_counts(constraints):
    """'a=? AND b>? AND b<?' → (等值个数, 范围项个数)。"""
    eq = rng = 0
    for part in constraints.split(" AND "):
        part = part.strip()
        if not part:
            continue
        if re.search(r"(<=|>=|<|>)", part):
            rng += 1
        elif "=" in part:
            eq += 1
    return eq, rng


# ---- 词法 ------------------------------------------------------------------------------
_TOKEN = re.compile(r"""\s*(?:
    (?P<str>'(?:[^']|'')*')|
    (?P<qid>"(?:[^"]|"")*"|`[^`]*`|\[[^\]]*\])|
    (?P<num>\d+(?:\.\d*)?(?:[eE][-+]?\d+)?|\.\d+)|
    (?P<param>\?\d*|[:@$][A-Za-z_][A-Za-z0-9_]*)|
    (?P<id>[A-Za-z_][A-Za-z0-9_$]*)|
    (?P<op><=|>=|==|!=|<>|\|\||[-+*/%<>=(),.;~&|])
)""", re.VERBOSE)


def tokenize(sql):
    """→ [(kind, text)]，kind ∈ str/qid/num/param/id/kw/op；关键字转大写为 kw。无法识别的字符抛 ValueError。"""
    out, pos, sql = [], 0, sql.strip()
    while pos < len(sql):
        m = _TOKEN.match(sql, pos)
        if not m or m.end() == pos:
            raise ValueError(f"cannot tokenize at {pos}: {sql[pos:pos + 20]!r}")
        pos = m.end()
        kind = m.lastgroup
        text = m.group(kind)
        if kind == "id" and text.upper() in KEYWORDS:
            out.append(("kw", text.upper()))
        elif kind == "qid":
            out.append(("id", text[1:-1]))
        else:
            out.append((kind, text))
    return out


KEYWORDS = frozenset("""SELECT FROM WHERE AND OR NOT IN IS NULL LIKE GLOB BETWEEN ORDER BY GROUP HAVING LIMIT OFFSET
    ASC DESC JOIN LEFT RIGHT FULL INNER OUTER CROSS NATURAL ON USING AS UPDATE SET DELETE INSERT INTO VALUES
    REPLACE WITH DISTINCT ALL UNION EXCEPT INTERSECT CASE WHEN THEN ELSE END EXISTS RETURNING WINDOW COLLATE
    ESCAPE DEFAULT INDEXED""".split())
_CLAUSE_END = {"WHERE", "GROUP", "ORDER", "LIMIT", "HAVING", "WINDOW", "RETURNING", "UNION", "EXCEPT", "INTERSECT"}
_JOIN_WORDS = {"JOIN", "LEFT", "RIGHT", "FULL", "INNER", "OUTER", "CROSS", "NATURAL"}


# ---- 语句结构 ----------------------------------------------------------------------------
def parse_statement(sql, schema):
    """schema = {表: [列...]}。返回 dict：
    kind（select/insert/update/delete/other）、tables（[{table, alias}]）、terms（[{table, column, kind(eq/range), text}]）、
    unusable（[{text, reason}]）、order_by / group_by（[(table, column, desc)]）、set_columns / insert_columns、select_star。
    """
    toks = tokenize(sql)
    kind = _kind(toks)
    out = {"kind": kind, "tables": [], "terms": [], "unusable": [], "order_by": [], "group_by": [],
           "set_columns": [], "insert_columns": [], "select_star": False, "table": None}
    if kind == "other":
        return out
    if kind == "insert":
        _parse_insert(toks, out)
        return out
    if kind == "update":
        _parse_update(toks, out)
    elif kind == "delete":
        _parse_delete(toks, out)
    else:
        _parse_select(toks, out)
    resolver = _Resolver(out["tables"], schema)
    for cond in out.pop("_conds", []):
        _collect_terms(cond, resolver, out)
    out["order_by"] = _resolve_list(out.pop("_order", []), resolver)
    out["group_by"] = _resolve_list(out.pop("_group", []), resolver)
    out["set_columns"] = out.pop("_set", [])
    return out


def _kind(toks):
    if not toks:
        return "other"
    head = toks[0][1].upper()
    if head in ("SELECT", "WITH"):
        return "select"
    if head in ("INSERT", "REPLACE"):
        return "insert"
    return {"UPDATE": "update", "DELETE": "delete"}.get(head, "other")


def _split_top(toks, words):
    """在括号深度 0 处按关键字 words 切开 → [(关键字或 None, 片段)]。"""
    parts, cur, depth, key = [], [], 0, None
    for t in toks:
        if t == ("op", "("):
            depth += 1
        elif t == ("op", ")"):
            depth -= 1
        if depth == 0 and t[0] == "kw" and t[1] in words:
            parts.append((key, cur))
            key, cur = t[1], []
            continue
        cur.append(t)
    parts.append((key, cur))
    return parts


def _clauses(toks):
    """顶层子句：{"HEAD": [...], "WHERE": [...], "GROUP": [...], "ORDER": [...], ...}（同名只取第一个）。"""
    out = {}
    for key, part in _split_top(toks, _CLAUSE_END):
        out.setdefault(key or "HEAD", part)
    return out


def _parse_select(toks, out):
    if toks[0][1].upper() == "WITH":
        out["unusable"].append({"text": "WITH ...", "reason": "CTE 不解析"})
        return
    cl = _clauses(toks)
    head = _split_top(cl.get("HEAD", []), {"FROM"})
    out["select_star"] = _has_star(head[0][1])
    from_part = next((p for k, p in head if k == "FROM"), [])
    out["_conds"] = _parse_from(from_part, out)
    if "WHERE" in cl:
        out["_conds"].append(cl["WHERE"])
    out["_group"] = _strip_by(cl.get("GROUP", []))
    out["_order"] = _strip_by(cl.get("ORDER", []))


def _has_star(cols):
    """结果列里有没有 * 或 t.*（count(*)、a * b 里的星号不算）。"""
    depth, prev = 0, None
    for t in cols:
        if t == ("op", "*") and depth == 0 and prev in (("kw", "SELECT"), ("kw", "DISTINCT"), ("kw", "ALL"),
                                                        ("op", ","), ("op", "."), None):
            return True
        depth += t == ("op", "(")
        depth -= t == ("op", ")")
        prev = t
    return False


def _strip_by(part):
    return part[1:] if part and part[0] == ("kw", "BY") else part


def _parse_from(part, out):
    """FROM 列表：表 [AS] 别名，逗号或 JOIN 连接；返回 ON 条件片段列表。"""
    conds, i = [], 0
    while i < len(part):
        t = part[i]
        if t[0] == "id":
            alias, j = _alias(part, i + 1)
            out["tables"].append({"table": t[1], "alias": alias or t[1]})
            i = j
        elif t == ("kw", "ON"):
            j = i + 1
            depth = 0
            while j < len(part) and not (depth == 0 and (part[j][1] in _JOIN_WORDS or part[j] == ("op", ","))):
                depth += part[j] == ("op", "(")
                depth -= part[j] == ("op", ")")
                j += 1
            conds.append(part[i + 1:j])
            i = j
        elif t == ("op", "("):
            out["unusable"].append({"text": "FROM (subquery)", "reason": "子查询不解析"})
            i = _skip_paren(part, i)
        else:
            i += 1
    return conds


def _alias(part, i):
    if i < len(part) and part[i] == ("kw", "AS"):
        i += 1
    if i < len(part) and part[i][0] == "id":
        return part[i][1], i + 1
    return None, i


def _skip_paren(toks, i):
    depth = 0
    for j in range(i, len(toks)):
        depth += toks[j] == ("op", "(")
        depth -= toks[j] == ("op", ")")
        if depth == 0:
            return j + 1
    return len(toks)


def _parse_update(toks, out):
    i = 1
    while i < len(toks) and toks[i][0] == "kw" and toks[i][1] == "OR":
        i += 2
    alias, _j = _alias(toks, i + 1)
    out["tables"].append({"table": toks[i][1], "alias": alias or toks[i][1]})
    cl = _clauses(toks)
    set_part = _split_top(cl.get("HEAD", []), {"SET"})
    sets = next((p for k, p in set_part if k == "SET"), [])
    out["_set"] = [item[0][1] for item in _split_commas(sets)
                   if len(item) > 1 and item[0][0] == "id" and item[1] == ("op", "=")]
    out["_conds"] = [cl["WHERE"]] if "WHERE" in cl else []


def _split_commas(toks):
    items, cur, depth = [], [], 0
    for t in toks:
        depth += t == ("op", "(")
        depth -= t == ("op", ")")
        if depth == 0 and t == ("op", ","):
            items.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        items.append(cur)
    return items


def _parse_delete(toks, out):
    i = 2 if len(toks) > 1 and toks[1] == ("kw", "FROM") else 1
    alias, _j = _alias(toks, i + 1)
    out["tables"].append({"table": toks[i][1], "alias": alias or toks[i][1]})
    cl = _clauses(toks)
    out["_conds"] = [cl["WHERE"]] if "WHERE" in cl else []


def _parse_insert(toks, out):
    i = 1
    while i < len(toks) and toks[i] != ("kw", "INTO"):
        i += 1
    table = toks[i + 1][1] if i + 1 < len(toks) else None
    out["tables"].append({"table": table, "alias": table})
    out["table"] = table
    if i + 2 < len(toks) and toks[i + 2] == ("op", "("):
        end = _skip_paren(toks, i + 2)
        out["insert_columns"] = [t[1] for t in toks[i + 3:end - 1] if t[0] == "id"]


class _Resolver:
    """列引用 → (表, 列)。限定名按别名解析；非限定名按 schema 在 FROM 的表里找（唯一才算）。"""

    def __init__(self, tables, schema):
        self.by_alias = {t["alias"]: t["table"] for t in tables}
        self.tables = [t["table"] for t in tables]
        self.schema = {k: set(v) for k, v in schema.items()}

    def resolve(self, qual, col):
        if qual is not None:
            table = self.by_alias.get(qual)
            return (table, col) if table and col in self.schema.get(table, ()) else None
        owners = [t for t in self.tables if col in self.schema.get(t, ())]
        return (owners[0], col) if len(set(owners)) == 1 else None


def _colref(toks, i):
    """toks[i:] 开头是列引用（id 或 id.id）→ (qual, col, 下一个位置)；否则 None。"""
    if i < len(toks) and toks[i][0] == "id":
        if i + 2 < len(toks) and toks[i + 1] == ("op", ".") and toks[i + 2][0] == "id":
            return toks[i][1], toks[i + 2][1], i + 3
        if i + 1 < len(toks) and toks[i + 1] == ("op", "("):
            return None
        return None, toks[i][1], i + 1
    return None


def _split_and(toks):
    """顶层 AND 切分（BETWEEN x AND y 里的 AND 不切）。"""
    terms, cur, depth, pending_between = [], [], 0, False
    for t in toks:
        depth += t == ("op", "(")
        depth -= t == ("op", ")")
        if depth == 0 and t == ("kw", "BETWEEN"):
            pending_between = True
        elif depth == 0 and t == ("kw", "AND"):
            if pending_between:
                pending_between = False
            else:
                terms.append(cur)
                cur = []
                continue
        cur.append(t)
    terms.append(cur)
    return [t for t in terms if t]


_EQ_OPS = {"=", "=="}
_RANGE_OPS = {"<", "<=", ">", ">="}
_FLIP = {"<": ">", "<=": ">=", ">": "<", ">=": "<=", "=": "=", "==": "=="}


def _collect_terms(cond, resolver, out):
    for term in _split_and(cond):
        text = " ".join(t[1] for t in term)
        res = _classify_term(term, resolver)
        if isinstance(res, str):
            out["unusable"].append({"text": text, "reason": res})
        else:
            for table, col, kind in res:
                out["terms"].append({"table": table, "column": col, "kind": kind, "text": text})


def _classify_term(term, resolver):
    """→ [(表, 列, eq|range|join)] 或 原因字符串（join = 两张表的列相等，两边各记一项）。"""
    if any(t == ("kw", "OR") for t in _top_level(term)):
        return "顶层 OR（单个索引无法覆盖）"
    if term[0] == ("op", "(") and _skip_paren(term, 0) == len(term):
        return _classify_term(term[1:-1], resolver)
    left = _colref(term, 0)
    if left is not None:
        qual, col, j = left
        ref = resolver.resolve(qual, col)
        rest = term[j:]
        other = _join_ref(rest, resolver)
        if ref is not None and other is not None and other[0] != ref[0]:
            return [ref + ("join",), other + ("join",)]
        if ref is not None and rest:
            kind = _rhs_kind(rest, resolver)
            if kind in ("eq", "range"):
                return [ref + (kind,)]
            if kind is not None and kind not in ("eq", "range"):
                return kind
    flipped = _flip_value_first(term, resolver)
    if flipped is not None:
        return flipped
    return "不是 列 运算符 值 的形式（函数 / 表达式 / 子查询）"


def _join_ref(rest, resolver):
    """rest = '= 另一列' → 该列的 (表, 列)；否则 None。"""
    if not rest or rest[0] not in (("op", "="), ("op", "==")):
        return None
    ref = _colref(rest, 1)
    if ref is None or ref[2] != len(rest):
        return None
    return resolver.resolve(ref[0], ref[1])


def _top_level(term):
    depth = 0
    for t in term:
        depth += t == ("op", "(")
        depth -= t == ("op", ")")
        if depth == 0:
            yield t


def _is_value(toks, resolver):
    """参数 / 字面量 / 另一张表的列（连接条件）都算“值”。"""
    if len(toks) == 1 and toks[0][0] in ("param", "num", "str"):
        return True
    if len(toks) == 1 and toks[0] == ("kw", "NULL"):
        return True
    if len(toks) == 2 and toks[0] == ("op", "-") and toks[1][0] == "num":
        return True
    ref = _colref(toks, 0)
    return ref is not None and ref[2] == len(toks) and resolver.resolve(ref[0], ref[1]) is not None


def _rhs_kind(rest, resolver):
    op = rest[0]
    if op[0] == "op" and op[1] in _EQ_OPS | _RANGE_OPS:
        if not _is_value(rest[1:], resolver):
            return "右侧不是参数 / 字面量 / 列"
        return "eq" if op[1] in _EQ_OPS else "range"
    if op == ("kw", "IS"):
        return "eq" if rest[1:] == [("kw", "NULL")] or _is_value(rest[1:], resolver) else "IS NOT / 表达式"
    if op == ("kw", "IN"):
        return "eq" if len(rest) > 1 and rest[1] == ("op", "(") else "IN 后不是列表"
    if op == ("kw", "BETWEEN"):
        return "range"
    if op == ("kw", "NOT"):
        return "NOT IN / NOT BETWEEN / NOT LIKE 不能用索引定位"
    if op[1] in ("LIKE", "GLOB"):
        return "LIKE / GLOB（缺省大小写不敏感，普通索引用不上）"
    if op == ("kw", "COLLATE"):
        return "COLLATE（索引排序规则需一致）"
    return None


def _flip_value_first(term, resolver):
    """'? = col' / '100 < col' 形式。"""
    for k, t in enumerate(term):
        if t[0] == "op" and t[1] in _EQ_OPS | _RANGE_OPS:
            lhs, rhs = term[:k], term[k + 1:]
            ref = _colref(rhs, 0)
            if ref is not None and ref[2] == len(rhs) and _is_value(lhs, resolver):
                r = resolver.resolve(ref[0], ref[1])
                if r is not None:
                    return [r + ("eq" if t[1] in _EQ_OPS else "range",)]
            return None
    return None


def _resolve_list(part, resolver):
    """ORDER BY / GROUP BY 列表 → [(表, 列, desc)]；含表达式的项记为 (None, 文本, desc)。"""
    items, cur = [], []
    for t in list(part) + [("op", ",")]:
        if t == ("op", ","):
            if cur:
                items.append(cur)
            cur = []
        else:
            cur.append(t)
    out = []
    for item in items:
        desc = item[-1] == ("kw", "DESC")
        if item[-1][0] == "kw" and item[-1][1] in ("ASC", "DESC"):
            item = item[:-1]
        ref = _colref(item, 0)
        if ref is not None and ref[2] == len(item):
            r = resolver.resolve(ref[0], ref[1])
            out.append((r[0], r[1], desc) if r else (None, " ".join(t[1] for t in item), desc))
        else:
            out.append((None, " ".join(t[1] for t in item), desc))
    return out


# ---- 候选索引 ----------------------------------------------------------------------------
def candidate_columns(parsed, table):
    """对 table 给出候选索引列：等值 → 第一个范围 → 排序（ORDER BY 全在本表时；否则 GROUP BY）。
    本表没有 WHERE 等值 / 范围项时，用连接列（join）当等值列。返回 (列列表, 说明 dict)；没有可用列时列列表为空。"""
    eq, rng, join = [], [], []
    for t in parsed["terms"]:
        if t["table"] != table:
            continue
        bucket = {"eq": eq, "range": rng, "join": join}[t["kind"]]
        if t["column"] not in bucket:
            bucket.append(t["column"])
    if not eq and not rng:
        eq = join                                    # 本表只有连接条件：按连接列做被驱动表的等值探查
    cols = list(eq)
    rng = [c for c in rng if c not in cols]
    if rng:
        cols.append(rng[0])
    order = _sort_cols(parsed, table)
    for c in order:
        if c not in cols:
            cols.append(c)
    return cols, {"eq": eq, "range": rng[:1], "order": [c for c in order if c not in eq + rng[:1]]}


def _sort_cols(parsed, table):
    for key in ("order_by", "group_by"):
        items = parsed[key]
        if items and all(t == table for t, _c, _d in items):
            descs = {d for _t, _c, d in items}
            if len(descs) == 1:                       # 混合方向时普通索引只能部分利用，不追加
                return [c for _t, c, _d in items]
            return []
    return []


def referenced_columns(sql, cols_map, table):
    """语句里引用到的 table 的列（出现顺序、去重）；SELECT * 时为该表全部列。用于“覆盖索引”候选。"""
    parsed = parse_statement(sql, cols_map)
    if parsed["select_star"]:
        return list(cols_map.get(table, []))
    resolver = _Resolver(parsed["tables"], cols_map)
    toks = tokenize(sql)
    out, i = [], 0
    while i < len(toks):
        ref = _colref(toks, i)
        if ref is None:
            i += 1
            continue
        r = resolver.resolve(ref[0], ref[1])
        if r is not None and r[0] == table and r[1] not in out:
            out.append(r[1])
        i = ref[2]
    return out


def covers(index_cols, wanted):
    """已有索引列 index_cols 是否以 wanted 为前缀（即已有索引已经能满足这条建议）。"""
    return list(index_cols[:len(wanted)]) == list(wanted)


# ---- 记录字节估算（SQLite 记录格式） --------------------------------------------------------------
def varint_len(v):
    n = 1
    while v >= 0x80 and n < 9:
        v >>= 7
        n += 1
    return n


def value_bytes(v):
    """一个值在记录体里占的字节数（serial type 规则）。"""
    if v is None or v is True or v is False:
        return 0
    if isinstance(v, int):
        if v in (0, 1):
            return 0
        for size, bound in ((1, 1 << 7), (2, 1 << 15), (3, 1 << 23), (4, 1 << 31), (6, 1 << 47)):
            if -bound <= v < bound:
                return size
        return 8
    if isinstance(v, float):
        return 8
    if isinstance(v, str):
        return len(v.encode("utf-8"))
    return len(v)


def serial_type_len(v):
    """serial type 本身的 varint 长度（文本 / blob 的 serial type = 2·len + 12/13）。"""
    if isinstance(v, str):
        return varint_len(2 * len(v.encode("utf-8")) + 13)
    if isinstance(v, (bytes, bytearray)):
        return varint_len(2 * len(v) + 12)
    return 1


def record_bytes(values):
    """一条记录（头 + 体）的字节数。"""
    body = sum(value_bytes(v) for v in values)
    types = sum(serial_type_len(v) for v in values)
    header = types + varint_len(types + 1)
    return header + body


def index_entry_bytes(values, rowid):
    """索引 B-tree 的一个单元：记录（索引列 + rowid）+ 负载长度 varint + 2 字节单元指针。"""
    rec = record_bytes(list(values) + [rowid])
    return rec + varint_len(rec) + 2
