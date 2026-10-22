"""生成 / 复核指标测试向量（qdcheck/vectors/*.json）。

    python3.12 -m qdcheck.gen_vectors                 # 重新生成并写入 qdcheck/vectors/
    python3.12 -m qdcheck.gen_vectors --check         # 在内存里重新生成，和已提交的向量逐点比（1e-9），不写文件
    python3.12 -m qdcheck.gen_vectors --check --json qdcheck/results/vectors_check.json

向量 = 合成 K 线（``SERIES``，固定 ``SEED``）× 每个指标 × 每个变体 × 每条输出线，一条一个 JSON 对象::

    {"id", "indicator", "variant", "line", "series", "params", "input": {"open","high","low","close","volume"},
     "output": [...]}

数值写成 ``%.17g``（17 位有效数字，可精确往返；整数值补 ".0"），预热期 / 未定义写 null。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time

from qdcheck import compare, synth
from qdcheck import indicators_ref as ref

SEED = 20261022
FORMAT = "qdcheck.indicator-vectors.v1"
VECTOR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vectors")
_COLS = ("open", "high", "low", "close", "volume")


def _const(n, price=100.0, volume=1000.0):
    return {k: [volume if k == "volume" else price] * n for k in _COLS}


def _mono(n, start, step):
    close = [start + step * t for t in range(n)]
    opens = [start - step] + close[:-1]
    up = step > 0
    return {"open": opens[:n], "close": close,
            "high": [(c if up else o) + 0.25 for o, c in zip(opens, close)],
            "low": [(o if up else c) - 0.25 for o, c in zip(opens, close)],
            "volume": [1000.0 + 10.0 * t for t in range(n)]}


def series_specs():
    """[(name, 描述, 生成参数)]：生成参数可 JSON 序列化，写进 manifest 方便别处复现。"""
    quiet = {"gap_prob": 0.0, "jump_prob": 0.0}
    return [
        ("const", "常数序列 OHLC=100, volume=1000", {"kind": "const", "n": 80}),
        ("mono_up", "严格单调上升 close=100+0.5t", {"kind": "mono", "n": 80, "start": 100.0, "step": 0.5}),
        ("mono_down", "严格单调下降 close=140-0.5t", {"kind": "mono", "n": 80, "start": 140.0, "step": -0.5}),
        ("randwalk", "随机游走（无事件）", {"kind": "synth", "n": 150, "seed": SEED + 1,
                                     "kw": dict(quiet, weights={"random": 1})}),
        ("trend", "趋势段交替", {"kind": "synth", "n": 150, "seed": SEED + 2,
                            "kw": dict(quiet, weights={"trend_up": 1, "trend_down": 1}, seg_min=25, seg_max=50)}),
        ("jumps", "带跳空缺口 >=3% 与单根 ±8% 跳跃", {"kind": "synth", "n": 150, "seed": SEED + 3,
                                            "kw": {"weights": {"random": 1}, "gap_prob": 0.03, "jump_prob": 0.03,
                                                   "events": {"30": "gap_up", "60": "jump_down",
                                                              "90": "gap_down", "120": "jump_up"}}}),
        ("longflat", "带 70 根精确横盘（量>0）+ 20 根停牌（量=0）", {
            "kind": "synth", "n": 170, "seed": SEED + 4,
            "kw": dict(quiet, weights={"random": 1}, flat_noise=0.0,
                       segments=[[40, 70, "flat"], [125, 20, "halt"]])}),
        ("short1", "极短 n=1", {"kind": "synth", "n": 1, "seed": SEED + 5, "kw": {}}),
        ("short2", "极短 n=2", {"kind": "synth", "n": 2, "seed": SEED + 5, "kw": {}}),
        ("short5", "极短 n=5", {"kind": "synth", "n": 5, "seed": SEED + 5, "kw": {}}),
    ]


def build_series(spec):
    if spec["kind"] == "const":
        return _const(spec["n"])
    if spec["kind"] == "mono":
        return _mono(spec["n"], spec["start"], spec["step"])
    kw = dict(spec["kw"])
    if "events" in kw:
        kw["events"] = {int(k): v for k, v in kw["events"].items()}
    return synth.ohlcv(synth.generate(spec["n"], spec["seed"], **kw))


def all_series():
    return [(name, build_series(spec)) for name, _desc, spec in series_specs()]


# 除默认参数外再加一组参数的指标（检查别人实现里周期参数是否真的传到了每一层）
EXTRA_PARAMS = {"dema": [{"period": 14}], "tema": [{"period": 14}], "trix": [{"period": 15}]}


def param_sets(indicator):
    """[(tag, 覆盖参数)]：tag 为空串表示默认参数，否则形如 "period=14"，会拼进向量 id。"""
    out = [("", {})]
    for extra in EXTRA_PARAMS.get(indicator, []):
        out.append((",".join(f"{k}={extra[k]}" for k in sorted(extra)), extra))
    return out


def make_vectors(indicator, series=None):
    series = series if series is not None else all_series()
    out = []
    for variant in ref.VARIANTS[indicator]:
        for tag, override in param_sets(indicator):
            _v, _k, params = ref.resolve(indicator, variant, override)
            for name, data in series:
                lines = ref.compute(indicator, data, variant, params)
                for line in ref.LINES[indicator]:
                    vid = f"{indicator}|{variant}|{line}|{name}" + (f"|{tag}" if tag else "")
                    out.append({"id": vid, "indicator": indicator, "variant": variant, "line": line,
                                "series": name, "params": params, "input": data, "output": lines[line]})
    return out


# ---- 17 位有效数字的 JSON 写出 ----------------------------------------------------
def fmt_float(x):
    if not math.isfinite(x):
        raise ValueError(f"non-finite value {x!r} cannot be written to a vector")
    s = format(x, ".17g")
    if all(ch in "-0123456789" for ch in s):
        s += ".0"
    return s


def dumps(obj):
    if obj is None:
        return "null"
    if isinstance(obj, bool):
        return "true" if obj else "false"
    if isinstance(obj, int):
        return str(obj)
    if isinstance(obj, float):
        return fmt_float(obj)
    if isinstance(obj, str):
        return json.dumps(obj, ensure_ascii=False)
    if isinstance(obj, (list, tuple)):
        return "[" + ",".join(dumps(v) for v in obj) + "]"
    if isinstance(obj, dict):
        return "{" + ",".join(json.dumps(str(k), ensure_ascii=False) + ":" + dumps(v) for k, v in obj.items()) + "}"
    raise TypeError(f"cannot serialise {type(obj).__name__}")


def file_payload(indicator, vectors):
    head = {"format": FORMAT, "indicator": indicator, "generator": "python3.12 -m qdcheck.gen_vectors",
            "seed": SEED, "variants": list(ref.VARIANTS[indicator]), "lines": list(ref.LINES[indicator]),
            "count": len(vectors)}
    body = ",\n".join(dumps(v) for v in vectors)
    return dumps(head)[:-1] + ',"vectors":[\n' + body + "\n]}\n"


def manifest():
    return {"format": FORMAT, "seed": SEED, "generator": "python3.12 -m qdcheck.gen_vectors",
            "series": [{"name": n, "description": d, "spec": s} for n, d, s in series_specs()],
            "indicators": {ind: {"file": f"{ind}.json", "variants": list(ref.VARIANTS[ind]),
                                 "lines": list(ref.LINES[ind]), "default_params": ref.DEFAULT_PARAMS[ind],
                                 "extra_params": EXTRA_PARAMS.get(ind, []),
                                 "count": len(ref.VARIANTS[ind]) * len(ref.LINES[ind]) * len(series_specs())
                                 * len(param_sets(ind))}
                           for ind in ref.INDICATORS},
            "tolerance": {"rel_tol": compare.REL_TOL, "abs_tol": compare.ABS_TOL}}


def write_all(out_dir=VECTOR_DIR):
    os.makedirs(out_dir, exist_ok=True)
    series = all_series()
    counts = {}
    for ind in ref.INDICATORS:
        vecs = make_vectors(ind, series)
        counts[ind] = len(vecs)
        with open(os.path.join(out_dir, f"{ind}.json"), "w", encoding="utf-8") as f:
            f.write(file_payload(ind, vecs))
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        f.write(json.dumps(manifest(), indent=1, ensure_ascii=False) + "\n")
    return counts


def _cmp_inputs(a, b, rel_tol):
    for col in _COLS:
        res = compare.compare_series(a[col], b.get(col), rel_tol, compare.ABS_TOL, "none")
        if not res["ok"]:
            return f"input.{col}: {compare.format_result(res)}"
    return None


def check_all(vec_dir=VECTOR_DIR, rel_tol=compare.REL_TOL, indicators=None):
    """内存里重新生成，与磁盘上的向量逐条逐点比较。返回可写成 JSON 的汇总。"""
    t0 = time.perf_counter()
    series = all_series()
    summary = {"vec_dir": os.path.relpath(vec_dir), "rel_tol": rel_tol, "abs_tol": compare.ABS_TOL,
               "files": {}, "vectors": 0, "values": 0, "mismatches": [], "max_rel_err": 0.0,
               "max_abs_err": 0.0, "max_tol_ratio": 0.0}
    for ind in indicators or ref.INDICATORS:
        fresh = {v["id"]: v for v in make_vectors(ind, series)}
        on_disk = {v["id"]: v for v in compare.load_vectors(os.path.join(vec_dir, f"{ind}.json"))}
        problems = [f"missing on disk: {k}" for k in sorted(set(fresh) - set(on_disk))]
        problems += [f"not regenerated: {k}" for k in sorted(set(on_disk) - set(fresh))]
        for vid in sorted(set(fresh) & set(on_disk)):
            new, old = fresh[vid], on_disk[vid]
            if (new["params"], new["indicator"], new["variant"], new["line"]) != \
                    (old["params"], old["indicator"], old["variant"], old["line"]):
                problems.append(f"{vid}: metadata differs")
                continue
            bad_in = _cmp_inputs(new["input"], old["input"], rel_tol)
            res = compare.compare_series(old["output"], new["output"], rel_tol, compare.ABS_TOL, "none")
            if bad_in or not res["ok"]:
                problems.append(f"{vid}: {bad_in or compare.format_result(res)}")
            summary["values"] += res["checked"]
            for key in ("max_rel_err", "max_abs_err", "max_tol_ratio"):
                summary[key] = max(summary[key], res[key])
        summary["files"][ind] = {"vectors": len(on_disk), "problems": len(problems)}
        summary["vectors"] += len(on_disk)
        summary["mismatches"].extend(problems)
    summary["ok"] = not summary["mismatches"]
    summary["seconds"] = round(time.perf_counter() - t0, 2)
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(description="生成 / 复核 qdcheck 指标测试向量")
    ap.add_argument("--out", default=VECTOR_DIR)
    ap.add_argument("--check", action="store_true", help="只复核，不写文件")
    ap.add_argument("--json", default=None, help="--check 时把汇总写到这里")
    args = ap.parse_args(argv)
    if not args.check:
        counts = write_all(args.out)
        print(json.dumps({"written": counts, "total": sum(counts.values()), "seed": SEED}, sort_keys=True))
        return 0
    summary = check_all(args.out)
    for line in summary["mismatches"][:20]:
        print(line)
    print(f"vectors {summary['vectors']}  values {summary['values']}  mismatches {len(summary['mismatches'])}  "
          f"max_rel_err {summary['max_rel_err']:.3e}  ({summary['seconds']}s)")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=1, sort_keys=True)
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
