"""真实用例：拿本仓 qbt/indicators.py（另一份独立实现，qdcheck 不修改它）对 qdcheck 向量跑 compare。

    python3.12 -m qdcheck.tools.qbt_indicators_vs_vectors [--json qdcheck/results/g1_qbt_indicators.json]

映射（qbt 的口径 -> qdcheck 的 variant）：
    qbt.dema / tema(x, n)            -> dema / tema  "ma=ema;seed=first"（qbt 的 EMA 以首值为种子、无预热）
    100 * qbt.trix(x, n)             -> trix "ma=ema;seed=first"（qbt 返回比例，乘 100 对齐 scale=100）
    qbt.cci(h, l, c, n)              -> cci  "src=hlc3;zero_dev=zero"
    qbt.ichimoku(h, l, c, 9, 26, 52, 26) 的前四条线 -> ichimoku "shift=forward"
    qbt.ichimoku 的 chikou（qbt 定义为 close[i] - close[i-26]，与传统迟行线不同）-> 预期不一致，用来展示报错格式
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from qdcheck import compare as C                     # noqa: E402
from qdcheck.gen_vectors import VECTOR_DIR            # noqa: E402


def _qbt_outputs(v):
    from qbt import indicators as Q
    d, p, ind = v["input"], v["params"], v["indicator"]
    if ind == "dema":
        return Q.dema(d["close"], p["period"])
    if ind == "tema":
        return Q.tema(d["close"], p["period"])
    if ind == "trix":
        return [None if x is None else 100.0 * x for x in Q.trix(d["close"], p["period"])]
    if ind == "cci":
        return Q.cci(d["high"], d["low"], d["close"], p["period"])
    lines = dict(zip(("tenkan", "kijun", "senkou_a", "senkou_b", "chikou"),
                     Q.ichimoku(d["high"], d["low"], d["close"], p["tenkan"], p["kijun"], p["senkou_b"],
                                p["displacement"])))
    return lines[v["line"]]


MAPPING = [("dema", "ma=ema;seed=first", None), ("tema", "ma=ema;seed=first", None),
           ("trix", "ma=ema;seed=first", None), ("cci", "src=hlc3;zero_dev=zero", None),
           ("ichimoku", "shift=forward", ("tenkan", "kijun", "senkou_a", "senkou_b")),
           ("ichimoku", "shift=forward", ("chikou",))]


def run():
    report = []
    for ind, variant, lines in MAPPING:
        vecs = [v for v in C.load_vectors(os.path.join(VECTOR_DIR, f"{ind}.json"))
                if v["variant"] == variant and (lines is None or v["line"] in lines)
                and len(v["input"]["close"]) >= 1]
        actual = {C.vector_id(v): _qbt_outputs(v) for v in vecs}
        rep = C.compare_file(vecs, actual)
        report.append({"indicator": ind, "variant": variant, "lines": list(lines) if lines else ["value"],
                       "checked": rep["checked"], "passed": rep["passed"], "failed": rep["failed"],
                       "max_rel_err": rep["max_rel_err"], "max_tol_ratio": rep["max_tol_ratio"],
                       "first_failures": [{"id": f["id"], "message": C.format_result(f)} for f in rep["failures"][:3]]})
    return report


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=None)
    args = ap.parse_args(argv)
    report = run()
    for r in report:
        print(f"{r['indicator']:9s} {r['variant']:24s} {','.join(r['lines']):34s} "
              f"checked {r['checked']:3d} passed {r['passed']:3d} failed {r['failed']:3d}")
        for f in r["first_failures"][:1]:
            print(f"    {f['id']}: {f['message']}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"command": "python3.12 -m qdcheck.tools.qbt_indicators_vs_vectors", "results": report},
                      f, indent=1, sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
