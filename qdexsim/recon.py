"""对账：客户端本地订单表（CSV / JSON，字段可映射）vs 服务端订单 / 成交（仿真器 /__admin/state 快照，或真实交易所导出）。

    python3.12 -m qdexsim.recon --local orders.csv --remote state.json [--map map.json] [--out report.json]
    python3.12 -m qdexsim.recon --selfcheck            # 合成数据：50 条已知差异全部找出、零误报，否则退出码 1

三类差异与建议动作：
    missing_local  服务端有、本地无（漏记）                 → 补记
    phantom        本地有、服务端无（幻单）                 → 本地状态未终局 → 标失败；本地已记成交 → 人工看
    mismatch       两边都有但 qty / price / filled / status 不一致 → status 落后（本地 NEW 服务端 FILLED/CANCELED）→ 补记；其余 → 人工看

字段映射（--map，JSON）：{"fields": {"id": "order_id", "cid": "client_oid", "symbol": "sym", "side": "side", "qty": "amount",
"price": "px", "filled": "dealt", "status": "state", "ts": "created_at"}, "status_map": {"open": "NEW", "done": "FILLED", ...},
"side_map": {"b": "BUY", "s": "SELL"}, "group_by": "cid", "ts_offset_ms": null}
- 部分成交拆单：本地把一张单拆成多行（同一 cid / 同一 group_by 键）时先聚合（qty / filled 求和、price 成交量加权），再比；
- 时区差：本地时间戳若整体偏移（如写的是 UTC+8 的本地时间），自动按匹配对的中位差识别为常数偏移（≥ 30 分钟且是 15 分钟整数倍）
  并校正，不算差异；校正后仍差 > ts_tol_ms 的单条才记 mismatch(ts)；ts_offset_ms 给定时不自动识别。
匹配键：先 cid，再服务端订单号。数值容差 1e-9。
"""
import argparse
import csv
import json
import os
import random
import statistics
import sys

from qdexsim import common

CANON_FIELDS = ("id", "cid", "symbol", "side", "qty", "price", "filled", "status", "ts")
OPEN = ("NEW", "PARTIALLY_FILLED")
FINAL = ("FILLED", "CANCELED", "REJECTED")
TOL = 1e-9
DEFAULT_TS_TOL_MS = 5000


# ---- 读入与规范化 -----------------------------------------------------------------------
def load_table(path):
    if path.endswith(".csv"):
        with open(path, encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))
    data = common.read_json(path)
    if isinstance(data, dict):
        data = data.get("orders", data.get("rows", []))
    return list(data)


def normalize(rows, mapping=None):
    """按映射把任意字段名的行翻成规范字段；缺的字段为 None；数值转 float，状态 / 方向大写并经映射表。"""
    mapping = mapping or {}
    fields = dict({k: k for k in CANON_FIELDS}, **mapping.get("fields", {}))
    smap = {k.upper(): v for k, v in mapping.get("status_map", {}).items()}
    side_map = {k.upper(): v for k, v in mapping.get("side_map", {}).items()}
    out = []
    for r in rows:
        n = {k: r.get(fields[k]) for k in CANON_FIELDS}
        for k in ("qty", "price", "filled"):
            n[k] = None if n[k] in (None, "") else float(n[k])
        n["ts"] = None if n["ts"] in (None, "") else int(float(n["ts"]))
        n["id"] = None if n["id"] in (None, "") else str(n["id"])
        n["cid"] = None if n["cid"] in (None, "") else str(n["cid"])
        if n["status"] is not None:
            s = str(n["status"]).upper()
            n["status"] = smap.get(s, s)
        if n["side"] is not None:
            s = str(n["side"]).upper()
            n["side"] = side_map.get(s, s)
        out.append(n)
    return out


def aggregate(rows, group_by="cid"):
    """同一 group_by 键的多行（部分成交拆单）聚合成一张单。键为空的行各自成单。"""
    groups, out = {}, []
    for r in rows:
        key = r.get(group_by)
        if key is None:
            out.append(dict(r, parts=1))
            continue
        groups.setdefault(key, []).append(r)
    for key, parts in groups.items():
        if len(parts) == 1:
            out.append(dict(parts[0], parts=1))
            continue
        qty = sum(p["qty"] or 0.0 for p in parts)
        filled = sum(p["filled"] or 0.0 for p in parts)
        w = sum((p["price"] or 0.0) * (p["qty"] or 0.0) for p in parts)
        first = dict(parts[0])
        first.update(qty=qty, filled=filled, price=w / qty if qty else first["price"], parts=len(parts),
                     status=_combine_status([p["status"] for p in parts]), ts=min(p["ts"] for p in parts if p["ts"] is not None) if any(p["ts"] is not None for p in parts) else None,
                     id=first["id"] or next((p["id"] for p in parts if p["id"]), None))
        out.append(first)
    return out


def _combine_status(statuses):
    s = set(statuses)
    if s <= {"FILLED"}:
        return "FILLED"
    if "NEW" in s or "PARTIALLY_FILLED" in s:
        return "PARTIALLY_FILLED" if "FILLED" in s else statuses[0]
    return statuses[-1]


# ---- 对账 -------------------------------------------------------------------------------
def match(local, remote):
    """→ (配对 [(l, r)], 只有本地 [l], 只有服务端 [r])；先 cid 再订单号。"""
    by_cid = {r["cid"]: r for r in remote if r["cid"]}
    by_id = {r["id"]: r for r in remote if r["id"]}
    pairs, only_local, used = [], [], set()
    for l in local:
        r = by_cid.get(l["cid"]) if l["cid"] else None
        if r is None and l["id"]:
            r = by_id.get(l["id"])
        if r is None or id(r) in used:
            only_local.append(l)
        else:
            used.add(id(r))
            pairs.append((l, r))
    only_remote = [r for r in remote if id(r) not in used]
    return pairs, only_local, only_remote


def detect_ts_offset(pairs, min_ms=1800000, step_ms=900000):
    """匹配对中位时间差 ≥ 30 分钟且接近 15 分钟整数倍 → 认定是时区 / 常数偏移，返回它；否则 0。"""
    diffs = [l["ts"] - r["ts"] for l, r in pairs if l["ts"] is not None and r["ts"] is not None]
    if len(diffs) < 3:
        return 0
    med = statistics.median(diffs)
    if abs(med) < min_ms:
        return 0
    rounded = round(med / step_ms) * step_ms
    return int(rounded) if abs(med - rounded) <= DEFAULT_TS_TOL_MS else 0


def compare_pair(l, r, ts_offset_ms, ts_tol_ms):
    fields = []
    for k in ("qty", "price", "filled"):
        if l[k] is not None and r[k] is not None and abs(l[k] - r[k]) > TOL:
            fields.append(k)
    if l["status"] is not None and r["status"] is not None and l["status"] != r["status"]:
        fields.append("status")
    if l["ts"] is not None and r["ts"] is not None and abs(l["ts"] - ts_offset_ms - r["ts"]) > ts_tol_ms:
        fields.append("ts")
    return fields


def action_for(kind, fields, l, r):
    if kind == "missing_local":
        return "补记"
    if kind == "phantom":
        return "标失败" if (l["status"] is None or l["status"] in OPEN) else "人工看"
    if fields == ["status"] and l["status"] in OPEN and r["status"] in FINAL:
        return "补记"
    if fields == ["status", "filled"] or fields == ["filled", "status"]:
        if l["status"] in OPEN and r["status"] in FINAL + ("PARTIALLY_FILLED",):
            return "补记"
    return "人工看"


def reconcile(local_rows, remote_rows, mapping=None, remote_mapping=None, ts_tol_ms=DEFAULT_TS_TOL_MS):
    mapping = mapping or {}
    local = aggregate(normalize(local_rows, mapping), mapping.get("group_by", "cid"))
    remote = normalize(remote_rows, remote_mapping or {})
    pairs, only_local, only_remote = match(local, remote)
    offset = mapping.get("ts_offset_ms")
    if offset is None:
        offset = detect_ts_offset(pairs)
    diffs = []
    for r in only_remote:
        diffs.append({"kind": "missing_local", "key": r["cid"] or r["id"], "fields": [], "local": None, "remote": r,
                      "action": action_for("missing_local", [], None, r)})
    for l in only_local:
        diffs.append({"kind": "phantom", "key": l["cid"] or l["id"], "fields": [], "local": l, "remote": None,
                      "action": action_for("phantom", [], l, None)})
    for l, r in pairs:
        fields = compare_pair(l, r, offset, ts_tol_ms)
        if fields:
            diffs.append({"kind": "mismatch", "key": l["cid"] or l["id"], "fields": fields, "local": l, "remote": r,
                          "action": action_for("mismatch", fields, l, r)})
    diffs.sort(key=lambda d: (d["kind"], str(d["key"])))
    counts = {k: sum(1 for d in diffs if d["kind"] == k) for k in ("missing_local", "phantom", "mismatch")}
    return {"summary": dict(counts, total=len(diffs), local_orders=len(local), remote_orders=len(remote), matched=len(pairs),
                            ts_offset_ms=offset), "diffs": diffs}


def render(report):
    s = report["summary"]
    lines = [f"recon: local {s['local_orders']} remote {s['remote_orders']} matched {s['matched']} ts_offset {s['ts_offset_ms']} ms",
             f"missing_local {s['missing_local']}  phantom {s['phantom']}  mismatch {s['mismatch']}  total {s['total']}",
             f"{'kind':14s} {'key':20s} {'fields':22s} action"]
    for d in report["diffs"]:
        lines.append(f"{d['kind']:14s} {str(d['key'])[:20]:20s} {','.join(d['fields'])[:22]:22s} {d['action']}")
    return "\n".join(lines)


# ---- 合成数据：50 条已知差异 --------------------------------------------------------------
def _synth_remote(rng, n, base_ts):
    remote = []
    for i in range(n):
        status = rng.choice(["NEW", "PARTIALLY_FILLED", "FILLED", "CANCELED"])
        qty = round(rng.randint(1, 50) * 0.01, 2)
        filled = {"NEW": 0.0, "CANCELED": 0.0, "FILLED": qty, "PARTIALLY_FILLED": round(qty / 2, 3)}[status]
        remote.append({"id": str(1000 + i), "cid": f"c{i:04d}", "symbol": "BTCUSDT", "side": rng.choice(["BUY", "SELL"]), "qty": qty,
                       "price": 60000 + rng.randint(-500, 500) * 0.1, "filled": filled, "status": status, "created": base_ts + i * 60000})
    return remote


def _local_row(r, tz_offset_ms, **over):
    d = {"order_id": r["id"], "client_oid": r["cid"], "sym": r["symbol"], "dir": "b" if r["side"] == "BUY" else "s",
         "amount": r["qty"], "px": r["price"], "dealt": r["filled"],
         "state": {"NEW": "open", "PARTIALLY_FILLED": "partial", "FILLED": "done", "CANCELED": "cancelled"}[r["status"]],
         "created_at": r["created"] + tz_offset_ms}
    d.update(over)
    return d


def _plant_mismatch(k, r, tz, local, planted):
    if k < 5:
        local.append(_local_row(r, tz, amount=round(r["qty"] + 0.01, 2)))
        planted[r["cid"]] = ("mismatch", ["qty"])
    elif k < 10:
        local.append(_local_row(r, tz, px=round(r["price"] + 0.5, 1)))
        planted[r["cid"]] = ("mismatch", ["price"])
    elif k < 17:
        local.append(_local_row(r, tz, state="done" if r["status"] != "FILLED" else "open"))
        planted[r["cid"]] = ("mismatch", ["status"])
    else:
        local.append(_local_row(r, tz, state="open", dealt=0.0 if r["filled"] else 0.5))
        planted[r["cid"]] = ("mismatch", ["filled", "status"])


def synth(seed=7, n=200, tz_offset_ms=8 * 3600 * 1000):
    """→ (local_rows（本地字段名、时间戳偏移 +8h、含拆单）, remote_snapshot, planted{key: (kind, fields)})。
    50 条差异：15 漏记 + 15 幻单 + 20 不一致（qty 5 / price 5 / status 7 / filled+status 3），另有 10 张拆单对账应无差异。"""
    rng = random.Random(seed)
    remote = _synth_remote(rng, n, 1_700_000_000_000)
    local, planted = [], {}
    idx = list(range(n))
    rng.shuffle(idx)
    missing, phantom_src, mism, split = idx[:15], idx[15:30], idx[30:50], idx[50:60]
    for i in split:                                   # 拆单样本：必须是已成交且可拆
        remote[i].update(status="FILLED", qty=max(remote[i]["qty"], 0.02))
        remote[i]["filled"] = remote[i]["qty"]
    for i in mism[17:]:                               # filled+status 样本：服务端不能是 NEW（否则只差 filled）
        if remote[i]["status"] == "NEW":
            remote[i].update(status="PARTIALLY_FILLED", filled=round(remote[i]["qty"] / 2, 3))
    for i, r in enumerate(remote):
        if i in missing:
            planted[r["cid"]] = ("missing_local", [])
        elif i in split:
            half = round(r["qty"] / 2, 3)
            local.append(_local_row(r, tz_offset_ms, amount=half, dealt=half))
            local.append(_local_row(r, tz_offset_ms, amount=round(r["qty"] - half, 3), dealt=round(r["qty"] - half, 3), order_id=""))
        elif i in mism:
            _plant_mismatch(mism.index(i), r, tz_offset_ms, local, planted)
        else:
            local.append(_local_row(r, tz_offset_ms))
    for j, i in enumerate(phantom_src):
        r = dict(remote[i], id=str(5000 + j), cid=f"p{j:03d}", status=rng.choice(["NEW", "FILLED"]))
        local.append(_local_row(r, tz_offset_ms))
        planted[r["cid"]] = ("phantom", [])
    rng.shuffle(local)
    return local, {"orders": remote, "fills": []}, planted


SYNTH_MAP = {"fields": {"id": "order_id", "cid": "client_oid", "symbol": "sym", "side": "dir", "qty": "amount", "price": "px",
                        "filled": "dealt", "status": "state", "ts": "created_at"},
             "status_map": {"open": "NEW", "partial": "PARTIALLY_FILLED", "done": "FILLED", "cancelled": "CANCELED"},
             "side_map": {"b": "BUY", "s": "SELL"}, "group_by": "cid"}
REMOTE_MAP = {"fields": {"ts": "created"}}


def selfcheck(seed=7):
    """合成数据上：找出全部 50 条、零误报、三类各自数量对、字段集合对、时区偏移识别对。"""
    local, remote, planted = synth(seed)
    rep = reconcile(local, remote["orders"], SYNTH_MAP, REMOTE_MAP)
    found = {d["key"]: (d["kind"], sorted(d["fields"])) for d in rep["diffs"]}
    want = {k: (kind, sorted(f)) for k, (kind, f) in planted.items()}
    missed = {k: v for k, v in want.items() if found.get(k) != v}
    false_pos = {k: v for k, v in found.items() if k not in want}
    ok = not missed and not false_pos and rep["summary"]["ts_offset_ms"] == 8 * 3600 * 1000 and len(want) == 50
    return {"ok": ok, "planted": len(want), "found": len(found), "missed": missed, "false_positives": false_pos,
            "ts_offset_ms": rep["summary"]["ts_offset_ms"], "summary": rep["summary"], "seed": seed}


def main(argv=None):
    ap = argparse.ArgumentParser(description="qdexsim 对账")
    ap.add_argument("--local")
    ap.add_argument("--remote")
    ap.add_argument("--map", dest="map_path")
    ap.add_argument("--remote-map", dest="remote_map_path")
    ap.add_argument("--ts-tol-ms", type=int, default=DEFAULT_TS_TOL_MS)
    ap.add_argument("--out")
    ap.add_argument("--selfcheck", action="store_true")
    args = ap.parse_args(argv)
    if args.selfcheck:
        res = selfcheck()
        print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
        return 0 if res["ok"] else 1
    if not args.local or not args.remote:
        ap.error("--local and --remote required (or --selfcheck)")
    mapping = common.read_json(args.map_path) if args.map_path else {}
    rmap = common.read_json(args.remote_map_path) if args.remote_map_path else {}
    rep = reconcile(load_table(args.local), load_table(args.remote), mapping, rmap, args.ts_tol_ms)
    print(render(rep))
    if args.out:
        common.write_json(args.out, rep)
    return 0 if rep["summary"]["total"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
