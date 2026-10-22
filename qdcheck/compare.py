"""指标输出对拍器：拿别人的输出数组和 qdcheck 的向量 JSON 比，打印第一处不一致。

判定规则（全部满足才算一致）：
1. 长度必须相等；
2. “无值”位置必须一致：期望为 null 的位置，实际必须是 None / NaN（``warmup="either"``，默认；
   ``"none"`` 只接受 None，``"nan"`` 只接受 NaN）；期望有值的位置，实际不得为 None / NaN；
3. 有值位置：实际必须是实数（bool 不算），且 ``|a - e| <= max(rel_tol * max(|a|, |e|), abs_tol)``，
   默认 ``rel_tol = 1e-9``、``abs_tol = 1e-12``（abs_tol 只为数学上恰为 0 的点兜底，例如常数输入的
   TRIX / AO / Force，其浮点结果是 ±1e-15 量级的噪声，纯相对容差在 0 附近没有意义）。

Python 用法::

    from qdcheck import compare
    res = compare.compare_series(expected_list, actual_list)        # -> dict(ok, reason, index, ...)
    rep = compare.compare_file("qdcheck/vectors/dema.json", {vector_id: actual_list, ...})

命令行::

    python3.12 -m qdcheck.compare --vector qdcheck/vectors/dema.json --actual mine.json [--id ID]
           [--rel 1e-9] [--abs 1e-12] [--warmup either|none|nan] [--strict] [--json report.json]
    cat mine.json | python3.12 -m qdcheck.compare --vector one_vector.json --actual -

``mine.json`` 是 ``{vector_id: [..]}``；或者一个裸数组（此时用 --id 指定向量，或向量文件里只有一条）。
退出码：0 = 全部一致（--strict 时还要求没有缺失的向量），1 = 有不一致，2 = 输入不合法。
stdout 第一行固定格式（见 first_line）：``OK n=<k> max_rel=<r>`` 或
``MISMATCH idx=<n> expected=<x> actual=<y> rel=<r> reason=<length|warmup|type|value>``。
"""

import argparse
import json
import math
import sys

REL_TOL = 1e-9
ABS_TOL = 1e-12
WARMUP_MODES = ("either", "none", "nan")


def is_missing(v):
    return v is None or (isinstance(v, float) and math.isnan(v))


def _missing_ok(a, warmup):
    if warmup == "none":
        return a is None
    if warmup == "nan":
        return isinstance(a, float) and math.isnan(a)
    return is_missing(a)


def _is_real(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def rel_err(e, a):
    """|a - e| / max(|a|, |e|)（两者都为 0 时为 0）。"""
    scale = max(abs(a), abs(e))
    return 0.0 if scale == 0 else abs(a - e) / scale


class _Stats:
    """逐点误差统计：max_rel_err 只统计相对容差起作用的点（rel_tol*scale >= abs_tol），
    其余（接近 0 的点）计入 max_abs_err；max_tol_ratio = max(|a-e| / 允许误差)，<= 1 即通过。"""

    def __init__(self):
        self.checked, self.max_rel, self.max_abs, self.max_ratio = 0, 0.0, 0.0, 0.0

    def add(self, e, a, rel_tol, abs_tol):
        scale, diff = max(abs(a), abs(e)), abs(a - e)
        allowed = max(rel_tol * scale, abs_tol)
        self.checked += 1
        if scale > 0 and rel_tol * scale >= abs_tol:
            self.max_rel = max(self.max_rel, diff / scale)
        else:
            self.max_abs = max(self.max_abs, diff)
        if allowed > 0:
            self.max_ratio = max(self.max_ratio, diff / allowed)


def _result(ok, reason=None, index=None, expected=None, actual=None, stats=None, detail=""):
    stats = stats or _Stats()
    return {"ok": ok, "reason": reason, "index": index, "expected": expected, "actual": actual,
            "checked": stats.checked, "max_rel_err": stats.max_rel, "max_abs_err": stats.max_abs,
            "max_tol_ratio": stats.max_ratio, "detail": detail}


def compare_series(expected, actual, rel_tol=REL_TOL, abs_tol=ABS_TOL, warmup="either"):
    """逐点比较，返回第一处不一致（ok=False）或 ok=True。reason ∈ {length, warmup, type, value}。"""
    if warmup not in WARMUP_MODES:
        raise ValueError(f"warmup must be one of {WARMUP_MODES}")
    if not isinstance(actual, (list, tuple)):
        return _result(False, "type", None, None, type(actual).__name__, detail="actual is not a list")
    if len(expected) != len(actual):
        idx = min(len(expected), len(actual))
        return _result(False, "length", idx, len(expected), len(actual),
                       detail=f"length differs: expected {len(expected)}, actual {len(actual)}")
    st = _Stats()
    for i, (e, a) in enumerate(zip(expected, actual)):
        if is_missing(e):
            if not _missing_ok(a, warmup):
                return _result(False, "warmup", i, e, a, st, f"expected no value (warmup/undefined) at {i}, actual {a!r}")
            continue
        if is_missing(a):
            return _result(False, "warmup", i, e, a, st, f"expected {e!r} at {i}, actual has no value")
        if not _is_real(a):
            return _result(False, "type", i, e, a, st, f"non-numeric actual {a!r} at {i}")
        diff = abs(a - e)
        if not math.isfinite(a) or not diff <= max(rel_tol * max(abs(a), abs(e)), abs_tol):
            return _result(False, "value", i, e, a, st,
                           f"value differs at {i}: expected {e!r}, actual {a!r}, rel_err {rel_err(e, a):.3e}")
        st.add(e, a, rel_tol, abs_tol)
    return _result(True, stats=st)


def load_vectors(path_or_obj):
    """读向量文件（或已解析的对象），返回向量列表。接受 {"vectors": [...]}、[...] 或单条向量。"""
    if isinstance(path_or_obj, str):
        with open(path_or_obj, "r", encoding="utf-8") as f:
            obj = json.load(f)
    else:
        obj = path_or_obj
    if isinstance(obj, dict) and "vectors" in obj:
        return list(obj["vectors"])
    if isinstance(obj, dict) and "output" in obj:
        return [obj]
    if isinstance(obj, list):
        return list(obj)
    raise ValueError("unrecognised vector file layout")


def vector_id(v):
    return v.get("id") or f"{v['indicator']}|{v['variant']}|{v.get('line', 'value')}|{v.get('series', '')}"


def compare_vector(vector, actual, rel_tol=REL_TOL, abs_tol=ABS_TOL, warmup="either"):
    res = compare_series(vector["output"], actual, rel_tol, abs_tol, warmup)
    res["id"] = vector_id(vector)
    return res


def compare_file(path_or_obj, actual_map, rel_tol=REL_TOL, abs_tol=ABS_TOL, warmup="either"):
    """对文件里每条向量，若 actual_map 里有同 id 的数组就比较。返回汇总 + 每条失败的第一处不一致。"""
    vectors = load_vectors(path_or_obj)
    known = {vector_id(v) for v in vectors}
    out = {"total": len(vectors), "checked": 0, "passed": 0, "failed": 0, "missing": [], "unknown": [],
           "failures": [], "max_rel_err": 0.0, "max_abs_err": 0.0, "max_tol_ratio": 0.0}
    for v in vectors:
        vid = vector_id(v)
        if vid not in actual_map:
            out["missing"].append(vid)
            continue
        res = compare_vector(v, actual_map[vid], rel_tol, abs_tol, warmup)
        out["checked"] += 1
        for key in ("max_rel_err", "max_abs_err", "max_tol_ratio"):
            out[key] = max(out[key], res[key])
        if res["ok"]:
            out["passed"] += 1
        else:
            out["failed"] += 1
            out["failures"].append(res)
    out["unknown"] = sorted(k for k in actual_map if k not in known)
    return out


def format_result(res):
    if res["ok"]:
        return (f"OK ({res['checked']} values, max rel err {res['max_rel_err']:.3e}, "
                f"max abs err near 0 {res['max_abs_err']:.3e})")
    head = f"MISMATCH [{res['reason']}]"
    if res["index"] is not None:
        head += f" first index {res['index']}: expected {res['expected']!r}, actual {res['actual']!r}"
    return head + (f" -- {res['detail']}" if res["detail"] else "")


def _actual_map(actual, vectors, chosen_id):
    if isinstance(actual, dict):
        return actual
    if not isinstance(actual, list):
        raise ValueError("actual must be a JSON array or an object {vector_id: array}")
    if chosen_id is None:
        if len(vectors) != 1:
            raise ValueError("actual is a bare array: pass --id to choose the vector")
        chosen_id = vector_id(vectors[0])
    return {chosen_id: actual}


def _fmt_num(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "null"
    return repr(v)


def first_line(res, vid=None):
    """stdout 第一行的固定格式。

    一致:   ``OK n=<有值点数> max_rel=<最大相对误差>``
    不一致: ``MISMATCH idx=<下标> expected=<期望> actual=<实际> rel=<相对误差> reason=<length|warmup|type|value>``
    多条向量时末尾附 ``id=<向量 id>``。长度不一致时 idx = 较短的长度，expected/actual = 两边的长度，rel=nan；
    无值位置不一致 / 非数值时 rel=nan。数值按 repr（最短可往返的十进制）打印。"""
    tail = f" id={vid}" if vid is not None else ""
    if res["ok"]:
        return f"OK n={res['checked']} max_rel={res['max_rel_err']:.3e}{tail}"
    e, a = res["expected"], res["actual"]
    if res["reason"] == "value" and isinstance(a, (int, float)) and not isinstance(a, bool) and math.isfinite(a):
        rel = f"{rel_err(e, a):.3e}"
    else:
        rel = "nan"
    return f"MISMATCH idx={res['index']} expected={_fmt_num(e)} actual={_fmt_num(a)} rel={rel} reason={res['reason']}{tail}"


def build_parser():
    ap = argparse.ArgumentParser(description="对拍：别人的指标输出 vs qdcheck 向量",
                                 epilog="退出码：0 一致 / 1 不一致 / 2 输入不合法。stdout 第一行固定格式见 CONTRACT.md。")
    ap.add_argument("--vector", required=True, help="向量 JSON 文件：单条向量、{\"vectors\": [...]} 或数组")
    ap.add_argument("--actual", required=True, help="实际输出 JSON 文件，或 - 读 stdin：裸数组（配合 --id）或 {vector_id: 数组}")
    ap.add_argument("--id", default=None, help="--actual 为裸数组且向量文件里有多条向量时，选哪一条")
    ap.add_argument("--rel", type=float, default=REL_TOL)
    ap.add_argument("--abs", type=float, default=ABS_TOL)
    ap.add_argument("--warmup", choices=WARMUP_MODES, default="either")
    ap.add_argument("--strict", action="store_true", help="向量文件里的每一条都必须给出实际输出")
    ap.add_argument("--json", default=None, help="把汇总写到这个 JSON 文件")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        vectors = load_vectors(args.vector)
        if args.actual == "-":
            actual = json.loads(sys.stdin.read())
        else:
            with open(args.actual, "r", encoding="utf-8") as f:
                actual = json.load(f)
        amap = _actual_map(actual, vectors, args.id)
        if args.id is not None and args.id not in {vector_id(v) for v in vectors}:
            raise ValueError(f"unknown vector id {args.id!r}")
        if not amap:
            raise ValueError("actual is empty")
        for vid, arr in amap.items():
            if not isinstance(arr, list):
                raise ValueError(f"actual[{vid!r}] is not an array")
    except (OSError, ValueError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    rep = compare_file(vectors, amap, args.rel, args.abs, args.warmup)
    results = [compare_vector(v, amap[vector_id(v)], args.rel, args.abs, args.warmup)
               for v in vectors if vector_id(v) in amap]
    if not results:
        print("error: none of the actual ids match a vector", file=sys.stderr)
        return 2
    multi = len(results) > 1
    failures = [r for r in results if not r["ok"]]
    head = failures[0] if failures else results[0]
    print(first_line(head, head["id"] if multi else None))
    for r in (failures[1:] if failures else results[1:]):
        print(first_line(r, r["id"] if multi else None))
    print(f"checked {rep['checked']}/{rep['total']}  passed {rep['passed']}  failed {rep['failed']}  "
          f"missing {len(rep['missing'])}  unknown {len(rep['unknown'])}  max_rel_err {rep['max_rel_err']:.3e}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=1, sort_keys=True)
    bad = rep["failed"] > 0 or (args.strict and rep["missing"])
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
