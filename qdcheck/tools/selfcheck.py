"""自检脚本：跑满规模的判据并把真实数字写进 qdcheck/results/*.json（REPORT.md 引用这些文件）。

    python3.12 -m qdcheck.tools.selfcheck g1        # -> qdcheck/results/g1_selfcheck.json
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
RESULTS = os.path.join(REPO, "qdcheck", "results")

from qdcheck import compare as C            # noqa: E402
from qdcheck import gen_vectors as G        # noqa: E402
from qdcheck import indicators_alt as A     # noqa: E402
from qdcheck import indicators_ref as R     # noqa: E402
from qdcheck import props as P              # noqa: E402
from qdcheck import synth                   # noqa: E402


def machine():
    return {"python": sys.version.split()[0], "implementation": platform.python_implementation(),
            "platform": platform.platform(), "machine": platform.machine(), "cpu_count": os.cpu_count()}


def _count_fail(store, key, ok, detail):
    store[key]["checks"] += 1
    if not ok:
        store[key]["failures"].append(detail)


def g1():
    t0 = time.perf_counter()
    series = dict(G.all_series())
    out = {k: {"checks": 0, "failures": []} for k in
           ("causality", "chikou_displacement", "length", "warmup_formula", "constant_closed_form",
            "cross_check_vectors", "cross_check_extra")}
    for ind, variant in R.all_variants():
        fn = (lambda d, ind=ind, variant=variant: R.compute(ind, d, variant))
        for name in ("randwalk", "trend", "jumps", "longflat"):
            n = len(series[name]["close"])
            res = P.check_causality(fn, series[name], [5, 10, n // 3, n // 2, 2 * n // 3, n - 5, n - 1],
                                    seed=len(name), exempt=P.chikou_exempt(ind, variant))
            out["causality"]["checks"] += res.get("checked", 0)
            if not res["ok"]:
                out["causality"]["failures"].append([ind, variant, name, res])
            if P.chikou_exempt(ind, variant):
                c = series[name]["close"]
                off = {"shift=forward": 26, "shift=forward_tv": 25}[variant]
                ok = fn(series[name])["chikou"] == [c[t + off] if t + off < n else None for t in range(n)]
                _count_fail(out, "chikou_displacement", ok, [variant, name])
        base = synth.ohlcv(synth.generate(60, 11))
        res = P.check_length(fn, lambda k: {c: v[:k] for c, v in base.items()}, list(range(0, 61)))
        _count_fail(out, "length", res["ok"], [ind, variant, res])
        for name in ("randwalk", "trend"):
            for _tag, override in G.param_sets(ind):
                got = R.compute(ind, series[name], variant, override)
                want = P.expected_warmup(ind, variant, override)
                for line, vals in got.items():
                    _count_fail(out, "warmup_formula", P.leading_nulls(vals) == want[line], [ind, variant, name, line])
        for c, n in ((100.0, 80), (37.25, 120), (0.015625, 100)):
            d = {"open": [c] * n, "high": [c] * n, "low": [c] * n, "close": [c] * n, "volume": [7.0] * n}
            want = P.constant_expectation(ind, variant, None, n, c)
            for impl in (R, A):
                got = impl.compute(ind, d, variant)
                for line in R.LINES[ind]:
                    res = C.compare_series(want[line], got[line], 1e-9, 1e-9, "none")
                    _count_fail(out, "constant_closed_form", res["ok"], [impl.__name__, ind, variant, line, c])
    worst = {"cross_check_vectors": 0.0, "cross_check_extra": 0.0}
    extra = [synth.ohlcv(synth.generate(220, 900 + s, gap_prob=0.05, jump_prob=0.03, seg_min=15, seg_max=60,
                                        flat_noise=0.0 if s % 2 else 0.1)) for s in range(8)]
    for key, datasets in (("cross_check_vectors", list(series.values())), ("cross_check_extra", extra)):
        for d in datasets:
            for ind, variant in R.all_variants():
                for _tag, override in G.param_sets(ind):
                    res = P.check_against(R.compute(ind, d, variant, override), A.compute(ind, d, variant, override))
                    _count_fail(out, key, res["ok"], [ind, variant, override, res])
                    worst[key] = max(worst[key], res.get("max_tol_ratio", 0.0))
    for key, val in worst.items():
        out[key]["max_tol_ratio"] = val
    out["vectors_check"] = G.check_all()
    out["vectors_check"].pop("mismatches_detail", None)
    summary = {k: {"checks": v["checks"], "failures": len(v["failures"])} for k, v in out.items()
               if isinstance(v, dict) and "checks" in v}
    return {"command": "python3.12 -m qdcheck.tools.selfcheck g1", "machine": machine(),
            "seconds": round(time.perf_counter() - t0, 1), "summary": summary,
            "vectors": {k: out["vectors_check"][k] for k in ("ok", "vectors", "values", "max_rel_err",
                                                             "max_abs_err", "max_tol_ratio")},
            "cross_check_max_tol_ratio": worst, "details": out}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("goal", choices=["g1"])
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    result = {"g1": g1}[args.goal]()
    path = args.out or os.path.join(RESULTS, f"{args.goal}_selfcheck.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1, sort_keys=True, ensure_ascii=False)
    print(json.dumps(result["summary"], indent=1, sort_keys=True))
    print(f"wrote {os.path.relpath(path, REPO)} in {result['seconds']}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
