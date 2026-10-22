"""向量文件的契约校验器（纯标准库，自己写的小方言，不是完整 JSON Schema）。

方言（``vectors/schema.json``）：顶层是命名定义的字典；节点支持
``type``（string / integer / number / boolean / null / array / object，或这些的列表；integer 不接受 bool，number 接受 int/float
但不接受 bool、NaN、inf）、``const``、``enum``、``pattern``（整串 re.fullmatch）、``minimum`` / ``exclusiveMinimum``、
``minItems``、``items``、``required``、``properties``、``additionalProperties``（false 或节点）、``propertyNames``、``$ref``（指向顶层定义）。

除结构外还检查向量文件的语义（schema 表达不了的）：output 长度等于输入长度、OHLC 顺序、文件头 count / variants / lines
与内容一致、数值文本是 ``%.17g``（<= 17 位有效数字且可精确往返）、账本向量的 expected 列长与 records 顺序。

    python3.12 -m qdcheck.vector_schema            # 校验 qdcheck/vectors/ 下全部文件，退出码 0 = 全部合法
"""

import json
import math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
VECTOR_DIR = os.path.join(HERE, "vectors")
SCHEMA_PATH = os.path.join(VECTOR_DIR, "schema.json")
_FLOAT_TOKEN = re.compile(r"(?<![\w.\"])-?\d+\.\d*(?:[eE][-+]?\d+)?|(?<![\w.\"])-?\d+[eE][-+]?\d+")
_TYPES = {"string": lambda v: isinstance(v, str), "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
          "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v),
          "boolean": lambda v: isinstance(v, bool), "null": lambda v: v is None,
          "array": lambda v: isinstance(v, list), "object": lambda v: isinstance(v, dict)}


class SchemaError(ValueError):
    """向量文件不符合契约。"""


def load_schema(path=SCHEMA_PATH):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _check(node, value, where, defs, errors, limit):
    """迭代校验；errors 收集 (路径, 消息)，最多 limit 条。"""
    todo = [(node, value, where)]
    while todo and len(errors) < limit:
        node, v, where = todo.pop()
        while "$ref" in node:
            node = defs[node["$ref"]]
        if "const" in node and v != node["const"]:
            errors.append((where, f"expected const {node['const']!r}"))
            continue
        if "enum" in node and not any(v == c and type(v) is type(c) for c in node["enum"]):
            errors.append((where, f"not one of {node['enum']!r}"[:120]))
            continue
        if "type" in node:
            types = node["type"] if isinstance(node["type"], list) else [node["type"]]
            if not any(_TYPES[t](v) for t in types):
                errors.append((where, f"expected type {'/'.join(types)}, got {type(v).__name__}"))
                continue
        if isinstance(v, str) and "pattern" in node and not re.fullmatch(node["pattern"], v):
            errors.append((where, f"does not match {node['pattern']!r}"))
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            if "minimum" in node and v < node["minimum"]:
                errors.append((where, f"< minimum {node['minimum']}"))
            if "exclusiveMinimum" in node and v <= node["exclusiveMinimum"]:
                errors.append((where, f"<= exclusiveMinimum {node['exclusiveMinimum']}"))
        if isinstance(v, list):
            if len(v) < node.get("minItems", 0):
                errors.append((where, f"fewer than {node['minItems']} items"))
            if "items" in node:
                todo.extend((node["items"], x, f"{where}[{i}]") for i, x in enumerate(v))
        if isinstance(v, dict):
            for k in node.get("required", ()):
                if k not in v:
                    errors.append((where, f"missing key {k!r}"))
            props = node.get("properties", {})
            extra = node.get("additionalProperties", True)
            for k, x in v.items():
                if "propertyNames" in node:
                    todo.append((node["propertyNames"], k, f"{where}.<key {k!r}>"))
                if k in props:
                    todo.append((props[k], x, f"{where}.{k}"))
                elif extra is False:
                    errors.append((where, f"unexpected key {k!r}"))
                elif isinstance(extra, dict):
                    todo.append((extra, x, f"{where}.{k}"))
    return errors


def validate(obj, definition, schema=None, limit=20):
    """按 schema.json 里的命名定义校验 obj，返回 [(路径, 消息)]（空 = 合法）。"""
    schema = schema or load_schema()
    return _check(schema[definition], obj, "$", schema, [], limit)


# ---- schema 表达不了的语义 -------------------------------------------------------------------
def _ohlcv_problems(d, where):
    out = []
    n = len(d["close"])
    if any(len(d[k]) != n for k in ("open", "high", "low", "volume")):
        out.append((where, "ohlcv columns have different lengths"))
        return out
    for i in range(n):
        o, h, lo, c = d["open"][i], d["high"][i], d["low"][i], d["close"][i]
        if not (lo <= min(o, c) <= max(o, c) <= h) or lo <= 0:
            out.append((f"{where}[{i}]", "low <= min(open, close) <= max(open, close) <= high and low > 0 violated"))
            break
    return out


def indicator_file_problems(obj):
    probs = validate(obj, "indicator_file")
    if probs:
        return probs
    if obj["count"] != len(obj["vectors"]):
        probs.append(("$.count", f"{obj['count']} != len(vectors) {len(obj['vectors'])}"))
    ids = set()
    for i, v in enumerate(obj["vectors"]):
        where = f"$.vectors[{i}]"
        if v["indicator"] != obj["indicator"]:
            probs.append((where + ".indicator", "differs from file header"))
        if v["variant"] not in obj["variants"]:
            probs.append((where + ".variant", "not listed in file header"))
        if v["line"] not in obj["lines"]:
            probs.append((where + ".line", "not listed in file header"))
        if len(v["output"]) != len(v["input"]["close"]):
            probs.append((where + ".output", f"length {len(v['output'])} != input length {len(v['input']['close'])}"))
        if v["id"] in ids:
            probs.append((where + ".id", "duplicate id"))
        ids.add(v["id"])
        probs.extend(_ohlcv_problems(v["input"], where + ".input"))
        if len(probs) >= 20:
            break
    return probs


def ledger_file_problems(obj):
    probs = validate(obj, "ledger_file")
    if probs:
        return probs
    n = len(obj["closes"])
    for col, vals in obj["expected"].items():
        if len(vals) != n:
            probs.append((f"$.expected.{col}", f"length {len(vals)} != len(closes) {n}"))
    last = -1
    for i, r in enumerate(obj["records"]):
        where = f"$.records[{i}]"
        if r["bar"] < last:
            probs.append((where + ".bar", "records must be sorted by bar"))
        if r["bar"] >= n:
            probs.append((where + ".bar", f"bar {r['bar']} >= len(closes) {n}"))
        last = r["bar"]
        if r["kind"] in ("trade", "liquidation") and not (r["side"] in ("buy", "sell") and r["qty"] > 0
                                                          and isinstance(r["price"], (int, float)) and r["price"] > 0):
            probs.append((where, f"{r['kind']} needs side buy|sell, qty > 0, price > 0"))
        if r["kind"] in ("funding", "settlement") and not (r["side"] == "none" and r["qty"] == 0):
            probs.append((where, f"{r['kind']} needs side none and qty 0"))
        if r["kind"] == "settlement" and not (isinstance(r["price"], (int, float)) and r["price"] > 0):
            probs.append((where, "settlement needs price > 0"))
    return probs[:20]


def fmt_float(x):
    """与 gen_vectors.fmt_float 相同的写法：%.17g，整数值补 .0。"""
    t = format(x, ".17g")
    return t + ".0" if all(ch in "-0123456789" for ch in t) else t


def number_text_problems(text, limit=5):
    """文件文本里每个浮点字面量都必须恰是 %.17g 写法（<= 17 位有效数字、可精确往返）。"""
    out = []
    for m in _FLOAT_TOKEN.finditer(text):
        tok = m.group(0)
        if fmt_float(float(tok)) != tok:
            out.append((f"offset {m.start()}", f"{tok!r} is not the %.17g spelling {fmt_float(float(tok))!r}"))
            if len(out) >= limit:
                break
    return out


def check_directory(vec_dir=VECTOR_DIR):
    """校验目录下全部文件，返回 {"files": {文件: 问题列表}, "ok": bool, "checked": n}。"""
    schema = load_schema(os.path.join(vec_dir, "schema.json"))
    report = {"files": {}, "ok": True, "checked": 0}
    for name in sorted(os.listdir(vec_dir)):
        if not name.endswith(".json") or name == "schema.json":
            continue
        path = os.path.join(vec_dir, name)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        obj = json.loads(text)
        if name == "manifest.json":
            probs = validate(obj, "manifest", schema)
        elif name.startswith("ledger_"):
            probs = ledger_file_problems(obj) + number_text_problems(text)
        else:
            probs = indicator_file_problems(obj) + number_text_problems(text)
        report["files"][name] = probs
        report["checked"] += 1
        report["ok"] = report["ok"] and not probs
    return report


def main(argv=None):
    vec_dir = argv[0] if argv else VECTOR_DIR
    rep = check_directory(vec_dir)
    for name, probs in rep["files"].items():
        print(f"{name:45s} {'OK' if not probs else 'INVALID'}")
        for where, msg in probs[:5]:
            print(f"    {where}: {msg}")
    print(f"checked {rep['checked']} files, {'all valid' if rep['ok'] else 'INVALID'}")
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
