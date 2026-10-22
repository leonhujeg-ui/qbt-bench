"""schema 驱动的入参模糊器：固定 seed 生成乱喂序列，跑 ``validate(dict)``，把结果分成
通过 / 受控错误 / 裸异常，裸异常自动缩小到最小复现并保留栈顶三行，输出 JSON 报告。

schema（可 JSON 序列化）::

    {"type": "object", "fields": {
        "period": {"type": "int", "min": 1, "max": 500},                       # required 缺省 True
        "fee":    {"type": "float", "min": 0.0, "max": 0.01, "required": False},
        "side":   {"type": "enum", "choices": ["long", "short"]},
        "name":   {"type": "str", "min_len": 1, "max_len": 16, "charset": "ascii_printable"},
        "tags":   {"type": "list", "items": {"type": "str"}, "min_len": 0, "max_len": 4},
        "risk":   {"type": "object", "fields": {...}, "extra": "forbid"}}}

类型：int（不含 bool）、float（int 或 float，不含 bool，必须有限）、bool、str、enum、list、object。
``schema_errors(schema, obj)`` 是这套语义的参考判定（键名区分大小写，object 默认不许多键）。

用法::

    from qdcheck import fuzz
    report = fuzz.run(my_validate, schema, n=20000, seed=0, is_controlled=MyValidationError)   # -> Report（dict 子类）
    report.ok; report["outcomes"]; report.to_json("fuzz_report.json")        # 字段清单见 Report.FIELDS / CONTRACT.md
    obj = fuzz.decode(report["bare"][0]["minimal_input"])      # 精确还原最小复现输入
    case = fuzz.make_case(schema, seed=0, index=123)           # 单独重放第 123 个用例

乱喂类别（CATEGORIES）：类型乱放（str/list/dict/None/bool 冒充数字、顶层非 dict）、NaN、±inf、-0.0、
超大数（含 10**400 这类转 float 会溢出的整数）、负数、边界 ±1、空串 / 空容器、缺键、多键（含非 str 键）、
键名大小写 / 首尾空格、嵌套乱（含 50~3000 层深链）、超长字符串（1e6）、Unicode 怪字符、数字字符串。
"""

import argparse
import json
import math
import os
import random
import re
import sys
import threading
import time
import traceback

CATEGORIES = ("valid", "type_wrong", "nan", "inf", "neg_zero", "huge", "negative", "boundary", "empty",
              "missing_key", "extra_key", "key_case", "nested_garbage", "long_string", "unicode", "numeric_string")
TYPES = ("int", "float", "bool", "str", "enum", "list", "object")
LONG_LEN = 1_000_000
UNICODE_SAMPLES = ("‮evil", "\x00", "\ud800", "\U0001d518\U0001d52b\U0001d526", "a​b", "é",
                   "\U0001f4a5", "１２", "٣", "﻿BOM", "Ⅻ", "İ", "ß", "\r\n\t",
                   "\\u0000", "中文", "العربية")
NUMERIC_STRINGS = ("10", "1e3", "-0", "nan", "inf", " 7 ", "0x1A", "1_000", "١٢", "３", "3.0", "+5",
                   "1e400", "-1e-400", "0.1.2")
LONG_UNITS = ("A", "ab", "9", "é", "{", "(", " ", "\U0001f600")


# ---- schema --------------------------------------------------------------------------
def check_schema(schema, path="$"):
    """schema 本身的格式校验（错误抛 ValueError）。迭代实现。"""
    todo = [(schema, path)]
    while todo:
        node, where = todo.pop()
        if not isinstance(node, dict) or node.get("type") not in TYPES:
            raise ValueError(f"{where}: schema node needs a 'type' in {TYPES}")
        t = node["type"]
        if t == "object":
            fields = node.get("fields")
            if not isinstance(fields, dict) or not all(isinstance(k, str) for k in fields):
                raise ValueError(f"{where}: object needs 'fields' (dict with str keys)")
            todo.extend((sub, f"{where}.{k}") for k, sub in fields.items())
        elif t == "list":
            todo.append((node.get("items"), f"{where}[]"))
        elif t == "enum" and not (isinstance(node.get("choices"), list) and node["choices"]):
            raise ValueError(f"{where}: enum needs a non-empty 'choices' list")
        if t in ("int", "float") and node.get("min", -math.inf) > node.get("max", math.inf):
            raise ValueError(f"{where}: min > max")
    return schema


def _required(node):
    return node.get("required", True)


def _num_ok(node, v):
    return node.get("min", -math.inf) <= v <= node.get("max", math.inf)


def _str_ok(node, v):
    if not node.get("min_len", 0) <= len(v) <= node.get("max_len", 256):
        return False
    return node.get("charset") != "ascii_printable" or (v.isascii() and v.isprintable())


def _leaf_error(node, v):
    t = node["type"]
    if t == "bool":
        return None if isinstance(v, bool) else "expected bool"
    if t == "int":
        if not isinstance(v, int) or isinstance(v, bool):
            return "expected int"
        return None if _num_ok(node, v) else "int out of range"
    if t == "float":
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            return "expected number"
        if isinstance(v, float) and not math.isfinite(v):
            return "number must be finite"
        return None if _num_ok(node, v) else "number out of range"
    if t == "str":
        if not isinstance(v, str):
            return "expected str"
        return None if _str_ok(node, v) else "str length/charset"
    return None if any(v == c and type(v) is type(c) for c in node["choices"]) else "not one of choices"   # enum


def schema_errors(schema, obj, limit=1):
    """参考判定：返回 [(path, message)]，最多 limit 条；空列表 = 合法。迭代实现，不随输入深度递归。"""
    errors, todo = [], [(schema, obj, "$")]
    while todo and len(errors) < limit:
        node, v, where = todo.pop()
        t = node["type"]
        if t == "object":
            if not isinstance(v, dict):
                errors.append((where, "expected object"))
                continue
            fields = node["fields"]
            for k in v:
                if (not isinstance(k, str) or k not in fields) and node.get("extra", "forbid") == "forbid":
                    errors.append((where, f"unexpected key {k!r}"[:80]))
            for k, sub in fields.items():
                if k in v:
                    todo.append((sub, v[k], f"{where}.{k}"))
                elif _required(sub):
                    errors.append((where, f"missing key {k!r}"))
        elif t == "list":
            if not isinstance(v, list):
                errors.append((where, "expected list"))
            elif not node.get("min_len", 0) <= len(v) <= node.get("max_len", 64):
                errors.append((where, "list length"))
            else:
                todo.extend((node["items"], item, f"{where}[{i}]") for i, item in enumerate(v))
        else:
            msg = _leaf_error(node, v)
            if msg:
                errors.append((where, msg))
    return errors[:limit]


# ---- 合法实例 --------------------------------------------------------------------------
def _rand_str(rnd, lo, hi):
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(rnd.choice(letters) for _ in range(rnd.randint(lo, max(lo, hi))))


def valid_instance(node, rnd):
    """按 schema 生成一个合法实例（schema 深度有限，这里可以递归）。"""
    t = node["type"]
    if t == "int":
        lo, hi = node.get("min", -1000), node.get("max", 1000)
        return rnd.choice((lo, hi, rnd.randint(lo, hi)))
    if t == "float":
        lo, hi = node.get("min", -1e6), node.get("max", 1e6)
        return rnd.choice((float(lo), float(hi), lo + (hi - lo) * rnd.random()))
    if t == "bool":
        return rnd.random() < 0.5
    if t == "str":
        return _rand_str(rnd, max(1, node.get("min_len", 1)), min(node.get("max_len", 12), 12))
    if t == "enum":
        return rnd.choice(node["choices"])
    if t == "list":
        lo, hi = node.get("min_len", 0), min(node.get("max_len", 4), 4)
        return [valid_instance(node["items"], rnd) for _ in range(rnd.randint(lo, max(lo, hi)))]
    out = {}
    for k, sub in node["fields"].items():
        if _required(sub) or rnd.random() < 0.6:
            out[k] = valid_instance(sub, rnd)
    return out


# ---- 路径工具（迭代） ---------------------------------------------------------------------
def _schema_at(schema, path):
    node = schema
    for step in path:
        if node is None:
            return None
        if node["type"] == "object" and isinstance(step, str):
            node = node["fields"].get(step)
        elif node["type"] == "list" and isinstance(step, int):
            node = node["items"]
        else:
            return None
    return node


def value_paths(obj, max_depth=12):
    """所有 (路径) —— dict 的每个键、list 的每个元素，深度 <= max_depth。根路径 () 不含在内。"""
    out, todo = [], [((), obj)]
    while todo:
        path, v = todo.pop()
        if len(path) >= max_depth:
            continue
        if isinstance(v, dict):
            for k in list(v):
                out.append(path + (k,))
                todo.append((path + (k,), v[k]))
        elif isinstance(v, list):
            for i, item in enumerate(v[:16]):
                out.append(path + (i,))
                todo.append((path + (i,), item))
    out.sort(key=lambda p: [repr(s) for s in p])            # 与哈希种子无关的确定顺序
    return out


def get_at(obj, path):
    for step in path:
        obj = obj[step]
    return obj


def set_at(obj, path, value):
    if not path:
        return value
    get_at(obj, path[:-1])[path[-1]] = value
    return obj


def clone(obj):
    """迭代深拷贝（copy.deepcopy 在 3000 层深链上会 RecursionError）。"""
    root = [None]
    todo = [(obj, root, 0)]
    while todo:
        v, parent, key = todo.pop()
        if isinstance(v, dict):
            new = dict.fromkeys(v)                   # 先按原顺序占位，保持键序
            parent[key] = new
            todo.extend((sub, new, k) for k, sub in v.items())
        elif isinstance(v, list):
            new = [None] * len(v)
            parent[key] = new
            todo.extend((sub, new, i) for i, sub in enumerate(v))
        else:
            parent[key] = v
    return root[0]


def deep_chain(kind, depth, leaf, key="k"):
    v = leaf
    for _ in range(depth):
        v = [v] if kind == "list" else {key: v}
    return v


# ---- 乱喂 ----------------------------------------------------------------------------
class Case:
    """一个用例：index（重放用）、categories（施加的乱喂类别）、paths（每个类别乱在哪个 schema 路径上）、input。"""
    __slots__ = ("index", "categories", "paths", "input")

    def __init__(self, index, categories, value, paths=()):
        self.index, self.categories, self.input, self.paths = index, categories, value, tuple(paths)


def path_str(path):
    """路径元组 -> "$.risk.window" / "$.symbols[0]" 这种 schema 路径（list 下标统一写成 []）。"""
    return "$" + "".join(f".{p}" if isinstance(p, str) else "[]" for p in path)


def schema_path_for(schema, path):
    """实际对象里的路径 -> 最近的 schema 路径：沿 schema 能走多深算多深（乱出来的结构 / 改名的键算在包含它的那个字段上）。"""
    node, out = schema, ()
    for step in path:
        if node["type"] == "object" and isinstance(step, str) and step in node["fields"]:
            node, out = node["fields"][step], out + (step,)
        elif node["type"] == "list" and isinstance(step, int):
            node, out = node["items"], out + (0,)
        else:
            break
    return path_str(out)


def schema_paths(schema):
    """schema 里全部字段路径（含嵌套；list 的元素记为 [...]）；"$" 代表顶层对象本身。"""
    out, todo = ["$"], [(schema, ())]
    while todo:
        node, path = todo.pop()
        if node["type"] == "object":
            for k, sub in node["fields"].items():
                out.append(path_str(path + (k,)))
                todo.append((sub, path + (k,)))
        elif node["type"] == "list":
            out.append(path_str(path + (0,)))
            todo.append((node["items"], path + (0,)))
    return sorted(set(out))


def _pick_path(rnd, obj, schema, want_types):
    paths = value_paths(obj)
    if not paths:
        return None
    good = [p for p in paths if (_schema_at(schema, p) or {}).get("type") in want_types]
    pool = good if good and rnd.random() < 0.8 else paths
    return rnd.choice(pool)


def _wrong_type_value(rnd, node):
    t = (node or {}).get("type")
    pool = ["abc", "12", [1, 2], {"a": 1}, None, True, False, 3.7, 7, b"bytes", ()]
    keep = {"int": (int,), "float": (int, float), "bool": (bool,), "str": (str,), "list": (list,),
            "object": (dict,)}.get(t, ())
    pool = [v for v in pool if not (isinstance(v, keep) and not (isinstance(v, bool) and t in ("int", "float")))]
    return rnd.choice(pool)


def _boundary_value(rnd, node):
    t = (node or {}).get("type")
    if t in ("int", "float"):
        lo, hi = node.get("min", 0), node.get("max", 100)
        if t == "int":
            return rnd.choice((lo - 1, hi + 1, lo, hi, 0))
        eps = 1e-9 * max(1.0, abs(lo), abs(hi))
        return rnd.choice((lo - eps, hi + eps, math.nextafter(float(hi), math.inf), float(lo), 0.0))
    if t == "str":
        return "x" * rnd.choice((node.get("max_len", 256) + 1, max(0, node.get("min_len", 1) - 1)))
    if t == "list":
        return [None] * (node.get("max_len", 64) + 1)
    if t == "enum":
        return str(node["choices"][0]).upper() + rnd.choice(("", " "))
    return rnd.choice((0, -1, 1))


def _garbage(rnd):
    if rnd.random() < 0.25:
        kind = rnd.choice(("list", "dict"))
        return deep_chain(kind, rnd.choice((50, 200, 1200, 3000)), rnd.choice((0, "x", None)))
    v = rnd.choice((1, "s", None, 2.5))
    for _ in range(rnd.randint(2, 6)):
        v = rnd.choice(([v, v], {"x": v, "y": [v]}, [{"z": v}], {"": v}))
    return v


def _value_for(cat, rnd, node):
    if cat == "type_wrong":
        if node and node.get("type") in ("int", "float") and rnd.random() < 0.4:
            return rnd.choice((True, False))                      # bool 冒充数字
        return _wrong_type_value(rnd, node)
    if cat == "nan":
        return float("nan")
    if cat == "inf":
        return rnd.choice((math.inf, -math.inf))
    if cat == "neg_zero":
        return -0.0
    if cat == "huge":
        return rnd.choice((1e308, -1e308, 1.7976931348623157e308, 10 ** 30, -(10 ** 30), 2 ** 63, 2 ** 64 + 1,
                           10 ** 400, 5e-324))
    if cat == "negative":
        return rnd.choice((-1, -0.5, -(10 ** 6), -1e-300, -float((node or {}).get("max", 1) or 1)))
    if cat == "boundary":
        return _boundary_value(rnd, node)
    if cat == "empty":
        return rnd.choice(("", [], {}, "   "))
    if cat == "long_string":
        unit = rnd.choice(LONG_UNITS)
        return unit * (LONG_LEN // len(unit))
    if cat == "unicode":
        return rnd.choice(UNICODE_SAMPLES)
    if cat == "numeric_string":
        return rnd.choice(NUMERIC_STRINGS)
    return _garbage(rnd)                                          # nested_garbage


_PREFERRED = {"nan": ("float", "int"), "inf": ("float", "int"), "neg_zero": ("float", "int"),
              "huge": ("float", "int"), "negative": ("float", "int"), "boundary": ("float", "int", "str", "list", "enum"),
              "long_string": ("str", "enum"), "unicode": ("str", "enum"), "numeric_string": ("int", "float"),
              "empty": ("str", "list", "object"), "nested_garbage": ("object", "list"), "type_wrong": TYPES}


def _mutate(cat, rnd, obj, schema):
    """对 obj 施加一次 cat 类乱喂，返回 (新的顶层对象, 乱在哪个 schema 路径上；None = 没施加)。"""
    if cat == "type_wrong" and rnd.random() < 0.05:
        return rnd.choice((None, [], "config", 42, [obj])), "$"      # 顶层就不是 dict
    if not isinstance(obj, dict):
        return obj, None
    if cat in ("missing_key", "key_case"):
        keys = [p for p in value_paths(obj) if isinstance(p[-1], str) and isinstance(get_at(obj, p[:-1]), dict)]
        if not keys:
            return obj, None
        path = rnd.choice(keys)
        parent, key = get_at(obj, path[:-1]), path[-1]
        val = parent.pop(key)
        if cat == "key_case":
            new = rnd.choice((key.upper(), key.title(), key.swapcase(), " " + key, key + " "))
            parent[new if new != key else key + "_"] = val
            if rnd.random() < 0.3:
                parent[key] = val                                 # 同时存在大小写两个版本
        return obj, schema_path_for(schema, path)
    if cat == "extra_key":
        objs = [()] + [p for p in value_paths(obj) if isinstance(get_at(obj, p), dict)]
        where = rnd.choice(objs)
        target = get_at(obj, where)
        key = rnd.choice(("unexpected", "__class__", "", "x" * 300, 1, None, (1, 2)))
        target[key] = rnd.choice((1, "v", None, [1], {"a": 1}))
        return obj, schema_path_for(schema, where)
    path = _pick_path(rnd, obj, schema, _PREFERRED[cat])
    if path is None:
        key = rnd.choice(sorted(schema["fields"]) or ["k"])
        obj[key] = _value_for(cat, rnd, None)
        return obj, schema_path_for(schema, (key,))
    return set_at(obj, path, _value_for(cat, rnd, _schema_at(schema, path))), schema_path_for(schema, path)


def make_case(schema, seed, index):
    """第 index 个用例（只依赖 schema / seed / index，可单独重放）。"""
    rnd = random.Random(f"qdcheck-fuzz:{seed}:{index}")
    obj = valid_instance(schema, rnd)
    if rnd.random() < 0.05:
        return Case(index, ("valid",), obj)
    k = rnd.choice((1, 1, 1, 1, 1, 1, 2, 2, 2, 3))
    applied, paths = [], []
    for cat in (rnd.choice(CATEGORIES[1:]) for _ in range(k)):
        obj, where = _mutate(cat, rnd, obj, schema)
        if where is not None:
            applied.append(cat)
            paths.append(where)
    return Case(index, tuple(applied) or ("valid",), obj, paths)


def iter_cases(schema, seed, n):
    for i in range(n):
        yield make_case(schema, seed, i)


# ---- 编码（报告用，JSON 安全且可精确还原） ---------------------------------------------------
def _period(s):
    for p in range(1, 17):
        if len(s) >= 4 * p and s[:p] * (len(s) // p) + s[:len(s) % p] == s:
            return p
    return 0


def _chain_info(v):
    """v 是否为长度 >= 32 的同形链（[[[..]]] 或 {"k": {"k": ..}}），是则返回 (kind, key, depth, leaf)。"""
    kind = "list" if isinstance(v, list) else "dict"
    key = next(iter(v)) if kind == "dict" and len(v) == 1 else None
    depth, cur = 0, v
    while ((kind == "list" and isinstance(cur, list) and len(cur) == 1) or
           (kind == "dict" and isinstance(cur, dict) and len(cur) == 1 and key in cur and isinstance(key, str))):
        cur = cur[0] if kind == "list" else cur[key]
        depth += 1
    return (kind, key, depth, cur) if depth >= 32 else None


def _enc_scalar(v):
    if isinstance(v, float):
        if math.isnan(v):
            return {"$float": "nan"}
        if math.isinf(v):
            return {"$float": "inf" if v > 0 else "-inf"}
        if v == 0 and math.copysign(1.0, v) < 0:
            return {"$float": "-0.0"}
        return v
    if isinstance(v, bytes):
        return {"$bytes": v.hex()}
    if isinstance(v, tuple):
        return {"$tuple": [_enc_scalar(x) for x in v]}
    if isinstance(v, str) and len(v) > 64 and _period(v):
        p = _period(v)
        return {"$repeat": v[:p], "times": len(v) // p, "tail": v[:len(v) % p]}
    if isinstance(v, int) and not isinstance(v, bool) and abs(v) > 2 ** 53:
        return {"$int": str(v)}
    return v


def encode(obj):
    """Python 值 -> JSON 安全结构（标签：$float/$bytes/$tuple/$repeat/$int/$dict/$chain）。迭代实现。"""
    root = [None]
    todo = [(obj, root, 0)]
    while todo:
        v, parent, slot = todo.pop()
        chain = _chain_info(v) if isinstance(v, (list, dict)) else None
        if chain:
            kind, key, depth, leaf = chain
            node = {"$chain": kind, "key": key, "depth": depth, "leaf": None}
            parent[slot] = node
            todo.append((leaf, node, "leaf"))
        elif isinstance(v, list):
            new = [None] * len(v)
            parent[slot] = new
            todo.extend((x, new, i) for i, x in enumerate(v))
        elif isinstance(v, dict) and all(isinstance(k, str) and not k.startswith("$") for k in v):
            new = dict.fromkeys(v)
            parent[slot] = new
            todo.extend((x, new, k) for k, x in v.items())
        elif isinstance(v, dict):
            pairs = [[None, None] for _ in v]
            parent[slot] = {"$dict": pairs}
            for pair, (k, x) in zip(pairs, v.items()):
                pair[0] = _enc_scalar(k)
                todo.append((x, pair, 1))
        else:
            parent[slot] = _enc_scalar(v)
    return root[0]


def _dec_tag(v):
    if "$float" in v:
        return float(v["$float"])
    if "$bytes" in v:
        return bytes.fromhex(v["$bytes"])
    if "$tuple" in v:
        return tuple(_dec_tag(x) if isinstance(x, dict) else x for x in v["$tuple"])
    if "$repeat" in v:
        return v["$repeat"] * v["times"] + v["tail"]
    if "$int" in v:
        return int(v["$int"])
    raise ValueError(f"unknown tag in {v!r}"[:100])


def _pop(results, n):
    vals = results[len(results) - n:]
    del results[len(results) - n:]
    return vals


def decode(obj):
    """encode 的逆。迭代后序遍历：子节点先还原，再组装父节点。"""
    stack, results = [(obj, False)], []
    while stack:
        node, done = stack.pop()
        if isinstance(node, list):
            if done:
                results.append(_pop(results, len(node)))
            else:
                stack.append((node, True))
                stack.extend((child, False) for child in reversed(node))
        elif isinstance(node, dict) and "$chain" in node:
            if done:
                results.append(deep_chain(node["$chain"], node["depth"], results.pop(), node["key"]))
            else:
                stack.extend(((node, True), (node["leaf"], False)))
        elif isinstance(node, dict) and "$dict" in node:
            if done:
                vals = _pop(results, len(node["$dict"]))
                results.append({(_dec_tag(k) if isinstance(k, dict) else k): v
                                for (k, _x), v in zip(node["$dict"], vals)})
            else:
                stack.append((node, True))
                stack.extend((pair[1], False) for pair in reversed(node["$dict"]))
        elif isinstance(node, dict) and any(k.startswith("$") for k in node):
            results.append(_dec_tag(node))
        elif isinstance(node, dict):
            if done:
                results.append(dict(zip(node, _pop(results, len(node)))))
            else:
                stack.append((node, True))
                stack.extend((child, False) for child in reversed(list(node.values())))
        else:
            results.append(node)
    return results[0]


def same_value(a, b):
    """结构相等（迭代；NaN 等于 NaN，-0.0 与 0.0 区分，类型必须一致，dict 不看键序）。"""
    todo = [(a, b)]
    while todo:
        x, y = todo.pop()
        if type(x) is not type(y):
            return False
        if isinstance(x, float):
            if not (x == y or (math.isnan(x) and math.isnan(y))) or math.copysign(1, x) != math.copysign(1, y):
                return False
        elif isinstance(x, (list, tuple)):
            if len(x) != len(y):
                return False
            todo.extend(zip(x, y))
        elif isinstance(x, dict):
            if len(x) != len(y) or any(k not in y for k in x):
                return False
            todo.extend((x[k], y[k]) for k in x)
        elif x != y:
            return False
    return True


def size(obj):
    """输入“长度”：encode 前的紧凑文本长度（字符数，迭代估算，与 json.dumps 紧凑格式一致到量级）。"""
    total, todo = 0, [obj]
    while todo:
        v = todo.pop()
        if isinstance(v, dict):
            total += 2 + max(0, len(v) - 1)
            for k, x in v.items():
                total += (len(k) + 3) if isinstance(k, str) else len(repr(k)) + 1
                todo.append(x)
        elif isinstance(v, list):
            total += 2 + max(0, len(v) - 1)
            todo.extend(v)
        elif isinstance(v, str):
            total += len(v) + 2
        else:
            total += len(repr(v)) if not isinstance(v, int) or abs(v) < 10 ** 30 else len(str(v))
    return total


# ---- 运行器 ----------------------------------------------------------------------------
def _is_controlled(controlled, exc):
    if controlled is None:
        return False
    if isinstance(controlled, type) or isinstance(controlled, tuple):
        return isinstance(exc, controlled)
    return bool(controlled(exc))


def _frames(exc, repo_root):
    """栈顶三行 + 签名键。去掉运行器 call() 自己那一帧后只取最后 3 帧（RecursionError 有上千帧，limit=-3 只保留末尾）。"""
    tb = exc.__traceback__
    tb = tb.tb_next if tb is not None else None
    last = traceback.extract_tb(tb, limit=-3) if tb is not None else []
    out = []
    for fr in last:
        fn = os.path.relpath(fr.filename, repo_root) if fr.filename.startswith(repo_root) else fr.filename
        out.append(f"{fn}:{fr.lineno} in {fr.name}: {(fr.line or '').strip()[:120]}")
    shape = message_shape(exc)
    if not last:
        return out, (type(exc).__name__, "", "", 0, shape)
    fr = last[-1]
    lineno = 0 if isinstance(exc, RecursionError) else fr.lineno
    return out, (type(exc).__name__, fr.filename, fr.name, lineno, shape)


_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"|\d+")


def message_shape(exc):
    """异常消息的“形状”：引号内容与数字换成 …，同一行不同原因的异常分开归组，同一原因不同取值合并。"""
    return _QUOTED.sub("\u2026", str(exc))[:80]


def call(validate, obj, controlled):
    """返回 (outcome, exc)；outcome ∈ pass / controlled / bare。KeyboardInterrupt 照常抛出。"""
    try:
        validate(obj)
    except KeyboardInterrupt:
        raise
    except BaseException as exc:                     # noqa: BLE001 —— 模糊器就是要接住一切
        return ("controlled" if _is_controlled(controlled, exc) else "bare"), exc
    return "pass", None


def call_isolated(validate, obj, controlled):
    """在新线程里调用：每次尝试的栈深度都相同（否则 RecursionError 的复现阈值会随调用路径漂移）。"""
    box = []
    worker = threading.Thread(target=lambda: box.append(call(validate, obj, controlled)), daemon=True)
    worker.start()
    worker.join()
    return box[0] if box else ("bare", RuntimeError("validate killed its thread"))


class _Shrinker:
    """在“仍以同一签名裸异常”约束下缩小输入。每次尝试都传一份迭代深拷贝，validate 改动输入也不影响。"""

    def __init__(self, validate, controlled, signature, repo_root, budget):
        self.validate, self.controlled, self.signature = validate, controlled, signature
        self.repo_root, self.budget, self.calls = repo_root, budget, 0

    def reproduces(self, obj):
        if self.calls >= self.budget:
            return False
        self.calls += 1
        outcome, exc = call_isolated(self.validate, clone(obj), self.controlled)
        return outcome == "bare" and _frames(exc, self.repo_root)[1] == self.signature

    def _try(self, obj, path, value):
        cand = clone(obj)
        cand = set_at(cand, path, value)
        return cand if self.reproduces(cand) else None

    def _margin(self, hi, full):
        """RecursionError 的阈值随调用方栈深度变化：二分出的临界长度 / 深度再留 max(64, 10%) 余量。"""
        if self.signature[0] != "RecursionError":
            return hi
        return min(full, hi + max(64, hi // 10))

    def _shrink_str(self, obj, path, s):
        lo, hi = 0, len(s)                           # 二分最短仍复现的前缀长度
        while lo < hi and self.calls < self.budget:
            mid = (lo + hi) // 2
            if self._try(obj, path, s[:mid]) is not None:
                hi = mid
            else:
                lo = mid + 1
        hi = self._margin(hi, len(s))
        return set_at(clone(obj), path, s[:hi]) if hi < len(s) else obj

    def _shrink_chain(self, obj, path, v):
        info = _chain_info(v)
        kind, key, depth, leaf = info
        lo, hi = 0, depth
        while lo < hi and self.calls < self.budget:
            mid = (lo + hi) // 2
            if self._try(obj, path, deep_chain(kind, mid, leaf, key)) is not None:
                hi = mid
            else:
                lo = mid + 1
        hi = self._margin(hi, depth)
        return set_at(clone(obj), path, deep_chain(kind, hi, leaf, key)) if hi < depth else obj

    def _simpler(self, v):
        """v 的候选简化值（按优先顺序、去重；与 v 本身相同的候选去掉，否则“换成自己”会被当成一次变化）。"""
        if isinstance(v, bool) or v is None:
            out = []
        elif isinstance(v, (int, float)):
            out = [0, 1, -1]
            if isinstance(v, float) and math.isfinite(v) and v == int(v):
                out.append(int(v))
        elif isinstance(v, dict):
            out = [{}] + [x for x in v.values() if isinstance(x, (dict, list))][:2]
        elif isinstance(v, list):
            half = len(v) // 2
            drops = [v[:i] + v[i + 1:] for i in range(min(len(v), 16))]
            out = [[], v[:1], v[:half], v[half:]] + drops + [x for x in v if isinstance(x, (dict, list))][:2]
        else:
            out = []
        uniq = []
        for x in out:
            if not same_value(x, v) and not any(same_value(x, u) for u in uniq):
                uniq.append(x)
        return uniq

    def _pass(self, obj):
        changed = False
        if not isinstance(obj, (dict, list)):
            return obj, False
        for path in sorted(value_paths(obj, 6), key=len):
            try:
                parent = get_at(obj, path[:-1])
                v = get_at(obj, path)
            except (KeyError, IndexError, TypeError):
                continue                             # 路径已被前面的缩小删掉
            if isinstance(parent, dict):
                cand = clone(obj)
                del get_at(cand, path[:-1])[path[-1]]
                if self.reproduces(cand):
                    obj, changed = cand, True
                    continue
            if isinstance(v, str) and len(v) > 1:
                new = self._shrink_str(obj, path, v)
                changed |= new is not obj
                obj = new
            elif isinstance(v, (list, dict)) and _chain_info(v):
                new = self._shrink_chain(obj, path, v)
                changed |= new is not obj
                obj = new
            else:
                for simple in self._simpler(v):
                    cand = self._try(obj, path, simple)
                    if cand is not None:
                        obj, changed = cand, True
                        break
        return obj, changed

    def shrink(self, obj):
        """make_case 从不把顶层换成深链，所以只需在各条路径上缩小。"""
        for _ in range(6):
            obj, changed = self._pass(obj)
            if not changed or self.calls >= self.budget:
                break
        return obj


def _qualname(fn):
    return f"{getattr(fn, '__module__', '?')}:{getattr(fn, '__qualname__', repr(fn))}"


class Report(dict):
    """``run`` 的返回值：普通 dict（字段见 ``FIELDS``，每个字段的含义见 CONTRACT.md）+ ``to_json()``。"""

    FIELDS = ("tool", "format", "validate", "seed", "n", "elapsed_s", "per_case_us", "outcomes", "bare_by_type",
              "categories", "field_coverage", "fields_under_4_kinds", "schema_valid_cases", "accepted_invalid",
              "rejected_valid", "bare_unrecorded", "bare")

    @property
    def ok(self):
        """没有任何裸异常。"""
        return self["outcomes"]["bare"] == 0

    def to_json(self, path=None):
        """ASCII、键排序、缩进 1 的 JSON 文本（NaN 等特殊值已在 encode 时打标签，所以是严格 JSON）；给 path 则同时写文件。"""
        text = json.dumps(self, indent=1, sort_keys=True, ensure_ascii=True, allow_nan=False)
        if path is not None:
            with open(path, "w", encoding="ascii") as f:
                f.write(text)
        return text


def run(validate, schema, n=20000, seed=0, is_controlled=None, shrink=True, shrink_budget=3000, max_signatures=50,
        repo_root=None, check_accepted=True, controlled=None):
    """跑 n 个用例并返回 Report（见模块文档与 CONTRACT.md）。``is_controlled``：受控异常的类 / 元组 / 判定函数；
    ``controlled`` 是同一参数的旧名。"""
    if controlled is not None:
        is_controlled = controlled
    controlled = is_controlled
    check_schema(schema)
    repo_root = repo_root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    outcomes = {"pass": 0, "controlled": 0, "bare": 0}
    cats = {c: {"cases": 0, "pass": 0, "controlled": 0, "bare": 0} for c in CATEGORIES}
    field_cov = {p: {} for p in schema_paths(schema)}
    bare_types, groups = {}, {}
    accepted_invalid, rejected_valid, valid_cases, unrecorded = [], [], 0, 0
    n_accepted_invalid = n_rejected_valid = 0
    t0 = time.perf_counter()
    for i in range(n):
        case = make_case(schema, seed, i)
        is_valid = not schema_errors(schema, case.input) if check_accepted else None
        outcome, exc = call(validate, case.input, controlled)
        outcomes[outcome] += 1
        for c in set(case.categories):
            cats[c]["cases"] += 1
            cats[c][outcome] += 1
        for c, where in zip(case.categories, case.paths):
            per = field_cov.setdefault(where, {})
            per[c] = per.get(c, 0) + 1
        valid_cases += bool(is_valid)
        if outcome == "pass" and is_valid is False:          # 误收：validate 放过了 schema 判为非法的输入
            n_accepted_invalid += 1
            if len(accepted_invalid) < 20:
                accepted_invalid.append(i)
        if outcome == "controlled" and is_valid:             # 误拒：validate 拒绝了 schema 判为合法的输入
            n_rejected_valid += 1
            if len(rejected_valid) < 20:
                rejected_valid.append(i)
        if outcome == "bare":
            bare_types[type(exc).__name__] = bare_types.get(type(exc).__name__, 0) + 1
            top3, key = _frames(exc, repo_root)
            g = groups.get(key)
            if g is None and len(groups) < max_signatures:
                groups[key] = {"signature": " | ".join(str(x) for x in key), "exc_type": type(exc).__name__,
                               "message": str(exc)[:200], "count": 1, "first_case": i,
                               "categories": list(case.categories), "top3": top3, "_key": key}
            elif g is not None:
                g["count"] += 1
            else:
                unrecorded += 1
    elapsed = time.perf_counter() - t0
    bare = []
    for g in groups.values():
        original = make_case(schema, seed, g["first_case"]).input
        g["original_size"] = size(original)
        if shrink:
            sh = _Shrinker(validate, controlled, g["_key"], repo_root, shrink_budget)
            minimal = sh.shrink(clone(original))
            g["shrink_calls"] = sh.calls
        else:
            minimal, g["shrink_calls"] = original, 0
        g["minimal_size"] = size(minimal)
        g["ratio"] = g["minimal_size"] / g["original_size"] if g["original_size"] else 1.0
        g["minimal_input"] = encode(minimal)
        g["minimal_preview"] = repr(minimal)[:200] if not _deep(minimal) else "<deep chain, see minimal_input>"
        del g["_key"]
        bare.append(g)
    bare.sort(key=lambda g: (-g["count"], g["first_case"]))
    field_cov = {k: dict(sorted(v.items())) for k, v in sorted(field_cov.items())}
    return Report({"tool": "qdcheck.fuzz", "format": 2, "validate": _qualname(validate), "seed": seed, "n": n,
            "elapsed_s": round(elapsed, 3), "per_case_us": round(1e6 * elapsed / n, 1) if n else 0.0,
            "outcomes": outcomes, "bare_by_type": dict(sorted(bare_types.items())), "categories": cats,
            "field_coverage": field_cov,
            "fields_under_4_kinds": [p for p in schema_paths(schema) if p != "$" and len(field_cov.get(p, {})) < 4],
            "schema_valid_cases": valid_cases if check_accepted else None,
            "accepted_invalid": {"count": n_accepted_invalid, "first_cases": accepted_invalid},
            "rejected_valid": {"count": n_rejected_valid, "first_cases": rejected_valid},
            "bare_unrecorded": unrecorded, "bare": bare})


def _deep(v):
    depth, todo = 0, [(v, 0)]
    while todo:
        x, d = todo.pop()
        depth = max(depth, d)
        if depth > 40:
            return True
        if isinstance(x, dict):
            todo.extend((y, d + 1) for y in x.values())
        elif isinstance(x, list):
            todo.extend((y, d + 1) for y in x[:8])
    return False


def write_report(report, path):
    """= Report.to_json(path)（兼容旧调用）。"""
    Report(report).to_json(path)


def _load_callable(spec):
    """``模块:对象.属性`` -> 对象（只用内建 __import__，不引入 importlib）。"""
    mod, _, name = spec.partition(":")
    __import__(mod)
    obj = sys.modules[mod]
    for part in name.split("."):
        obj = getattr(obj, part)
    return obj


def build_parser():
    ap = argparse.ArgumentParser(description="schema 驱动的入参模糊器")
    ap.add_argument("validate", help="模块:函数，例如 qdcheck.examples:validate_good")
    ap.add_argument("--schema", required=True, help="schema JSON 文件，或 模块:变量")
    ap.add_argument("--controlled", default=None, help="受控异常类（模块:类），缺省 = 所有异常都算裸异常")
    ap.add_argument("--n", type=int, default=20000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--no-shrink", action="store_true")
    ap.add_argument("--out", default=None)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    if os.path.exists(args.schema):
        with open(args.schema, encoding="utf-8") as f:
            schema = json.load(f)
    else:
        schema = _load_callable(args.schema)
    controlled = _load_callable(args.controlled) if args.controlled else None
    rep = run(_load_callable(args.validate), schema, args.n, args.seed, is_controlled=controlled, shrink=not args.no_shrink)
    print(json.dumps({k: rep[k] for k in ("validate", "n", "elapsed_s", "outcomes", "bare_by_type")}, sort_keys=True))
    for g in rep["bare"][:10]:
        print(f"  {g['exc_type']} x{g['count']} first case {g['first_case']} min {g['minimal_size']}/"
              f"{g['original_size']} chars: {g['minimal_preview'][:100]}")
        for line in g["top3"]:
            print(f"      {line}")
    if args.out:
        write_report(rep, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
