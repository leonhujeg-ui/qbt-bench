"""真实用例：把本仓 qbt 回测引擎（qdcheck 不修改它）的成交流喂给参考账本，逐根核对 qbt 自己报的权益。

    python3.12 -m qdcheck.tools.qbt_ledger_check [--n 20000] [--json qdcheck/results/g3_qbt_ledger.json]

转换规则（qbt 的口径，见 qbt/broker.py）：同一根内先是撮合成交，再是资金费
（(i+1) % funding_interval == 0 时 pay = qty * close * funding_rate），最后是强平成交（tag == "liquidation"）；
qbt 是全仓（cross），维持保证金 = maintenance_margin * 名义额。
"""

import argparse
import json
import os
import sys
import time

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from qdcheck import ledger      # noqa: E402

# (BrokerConfig 参数, 策略参数覆盖（策略有 size 参数时）, 合成数据参数)
CONFIGS = {
    "default": ({}, {}, {}),
    "levered_funding": ({"max_leverage": 3.0, "maintenance_margin": 0.05, "funding_interval": 60,
                         "funding_rate": 0.0001, "max_participation": 0.5}, {}, {}),
    "liquidation_prone": ({"max_leverage": 8.0, "maintenance_margin": 0.12, "funding_interval": 60,
                           "funding_rate": 0.0002, "max_participation": 0.5}, {"size": 6.0},
                          {"sigma": 1.2, "jump_prob": 0.004}),
}


def to_records(result, data, cfg):
    """qbt Result -> qdcheck 成交流记录。"""
    by_bar = {}
    for f in result.fills:
        by_bar.setdefault(f.bar, []).append(f)
    records, qty = [], 0.0
    for i in range(len(data.close)):
        fills = by_bar.get(i, [])
        for f in (x for x in fills if x.tag != "liquidation"):
            records.append({"bar": i, "kind": "trade", "side": "buy" if f.side > 0 else "sell", "qty": f.qty,
                            "price": f.price, "fee": f.fee, "funding": 0.0})
            qty += f.side * f.qty
        if cfg.funding_interval and (i + 1) % cfg.funding_interval == 0 and qty:
            records.append({"bar": i, "kind": "funding", "side": "none", "qty": 0, "price": None, "fee": 0.0,
                            "funding": qty * data.close[i] * cfg.funding_rate})
        for f in (x for x in fills if x.tag == "liquidation"):
            records.append({"bar": i, "kind": "liquidation", "side": "buy" if f.side > 0 else "sell", "qty": f.qty,
                            "price": f.price, "fee": f.fee, "funding": 0.0})
            qty += f.side * f.qty
    return records


def run(n=20000, seeds=(1, 2, 3)):
    from qbt.broker import BrokerConfig
    from qbt.data import generate
    from qbt.engine import run_backtest
    from strategies import REGISTRY
    out = []
    for cfg_name, (kw, strat_kw, data_kw) in CONFIGS.items():
        cfg = BrokerConfig(**kw)
        for seed in seeds:
            data = generate(n=n, seed=seed, **dict({"halt_prob": 0.002, "gap_prob": 0.01, "jump_prob": 0.002},
                                                   **data_kw))
            for name in sorted(REGISTRY):
                cls = REGISTRY[name]
                params = {k: v for k, v in strat_kw.items() if k in cls.PARAM_SPACE}
                res = run_backtest(data, cls(**params), cfg)
                recs = to_records(res, data, cfg)
                rep = ledger.check_engine(recs, data.close, res.equity, initial_cash=cfg.cash, margin_mode="cross",
                                          leverage=cfg.max_leverage, mmr=cfg.maintenance_margin)
                out.append({"config": cfg_name, "seed": seed, "strategy": name, "params": params, "bars": rep["bars"],
                            "records": len(recs), "fills": len(res.fills), "liquidations": res.liquidations,
                            "ok": rep["ok"], "max_rel_err": rep["max_rel_err"],
                            "first_mismatch": rep["first_mismatch"], "violations": rep["violations"][:3],
                            "n_violations": len(rep["violations"]), "qbt_fees": res.fees, "qbt_funding": res.funding})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20000)
    ap.add_argument("--json", default=None)
    args = ap.parse_args(argv)
    t0 = time.perf_counter()
    rows = run(args.n)
    summary = {"runs": len(rows), "ok": sum(r["ok"] for r in rows), "bars": sum(r["bars"] for r in rows),
               "records": sum(r["records"] for r in rows), "liquidations": sum(r["liquidations"] for r in rows),
               "max_rel_err": max(r["max_rel_err"] for r in rows), "seconds": round(time.perf_counter() - t0, 1)}
    print(json.dumps(summary, sort_keys=True))
    for r in rows:
        if not r["ok"]:
            print(f"  {r['config']} seed {r['seed']} {r['strategy']}: {r['first_mismatch']} {r['violations'][:1]}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"command": f"python3.12 -m qdcheck.tools.qbt_ledger_check --n {args.n}", "summary": summary,
                       "runs": rows}, f, indent=1, sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
