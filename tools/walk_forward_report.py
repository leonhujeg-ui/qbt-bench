"""REPORT.md 第 6 节：6 个策略 x 3 组 seed 合成数据的 walk-forward（样本内 / 样本外）结果表。

用法：python tools/walk_forward_report.py --n 30000 --combos 60 --folds 3 --workers 4 --out out/walk_forward.json
输出 JSON + Markdown 表（stdout）。合成数据，不构成任何投资结论。
"""
import argparse
import json
import os
import sys
import time

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)

from qbt.broker import BrokerConfig                                 # noqa: E402
from qbt.data import GenSpec                                         # noqa: E402
from qbt.optimizer import make_task, random_search, walk_forward     # noqa: E402
from strategies import REGISTRY                                      # noqa: E402

SEEDS = (101, 202, 303)


def run(n, combos, folds, workers, seeds):
    rows = []
    for name in sorted(REGISTRY):
        space = REGISTRY[name].PARAM_SPACE
        for seed in seeds:
            task = make_task(name, GenSpec(n=n, seed=seed, halt_prob=0.002, gap_prob=0.01, jump_prob=0.002).to_dict(),
                             broker=BrokerConfig(max_participation=0.5), objective="sharpe")
            t0 = time.perf_counter()
            wf = walk_forward(task, random_search(space, combos, seed), n_folds=folds, workers=workers)
            rows.append({"strategy": name, "seed": seed, "is_sharpe": wf["is_mean_objective"],
                         "oos_sharpe": wf["oos_mean_objective"],
                         "is_return": sum(f["in_sample"]["total_return"] for f in wf["folds"]) / folds,
                         "oos_return": sum(f["out_of_sample"]["total_return"] for f in wf["folds"]) / folds,
                         "oos_mdd": max(f["out_of_sample"]["max_drawdown"] for f in wf["folds"]),
                         "oos_trades": sum(f["out_of_sample"]["n_trades"] for f in wf["folds"]),
                         "folds": wf["folds"], "seconds": round(time.perf_counter() - t0, 1)})
            print(f"{name:22s} seed={seed} IS sharpe={rows[-1]['is_sharpe']:8.2f} OOS sharpe={rows[-1]['oos_sharpe']:8.2f} "
                  f"({rows[-1]['seconds']}s)", flush=True)
    return rows


def markdown(rows, n, combos, folds):
    out = [f"| 策略 | seed | 样本内夏普(均值) | 样本外夏普(均值) | 样本内收益(均值/折) | 样本外收益(均值/折) | 样本外最大回撤(最差折) | 样本外交易数 |",
           "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        out.append(f"| {r['strategy']} | {r['seed']} | {r['is_sharpe']:.2f} | {r['oos_sharpe']:.2f} | "
                   f"{100 * r['is_return']:.2f}% | {100 * r['oos_return']:.2f}% | {100 * r['oos_mdd']:.2f}% | {r['oos_trades']} |")
    out.append("")
    out.append(f"设置：每组 seed 生成 {n} 根 1 分钟合成 K 线（含停牌/缺口/跳跃），{folds} 折滚动 walk-forward，"
               f"每折前 70% 样本内在 {combos} 组随机参数中按夏普选优，后 30% 样本外评估；手续费 maker 2bp / taker 5bp，滑点 1bp。")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=30_000)
    ap.add_argument("--combos", type=int, default=60)
    ap.add_argument("--folds", type=int, default=3)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default=os.path.join(REPO, "out", "walk_forward.json"))
    args = ap.parse_args(argv)
    rows = run(args.n, args.combos, args.folds, args.workers, SEEDS)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"n": args.n, "combos": args.combos, "folds": args.folds, "rows": rows}, f, indent=1)
    md = markdown(rows, args.n, args.combos, args.folds)
    with open(args.out.replace(".json", ".md"), "w", encoding="utf-8") as f:
        f.write(md + "\n")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
