"""生成/更新黄金文件：固定 seed 数据 + 6 个策略默认参数 + 固定 BrokerConfig 的结果快照。

用法：python tools/make_golden.py            # 写 tests/golden/golden_v1.json（已存在则报错）
      python tools/make_golden.py --update   # 覆盖（只有在有意改变引擎语义时才这么做，并在提交信息说明）
快照里的浮点数用 repr 保存（可逐字节往返），比较时精确相等。
"""
import argparse
import hashlib
import json
import os
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)

from qbt.broker import BrokerConfig                      # noqa: E402
from qbt.data import GenSpec, generate                    # noqa: E402
from qbt.engine import run_backtest                       # noqa: E402
from qbt import metrics                                   # noqa: E402
from strategies import REGISTRY                           # noqa: E402

GOLDEN_PATH = os.path.join(REPO, "tests", "golden", "golden_v1.json")
SPEC = GenSpec(n=3000, seed=20240601, halt_prob=0.003, gap_prob=0.02, jump_prob=0.005)
CFG = dict(max_participation=0.5, maker_fee=0.0002, taker_fee=0.0005, slippage=0.0001, max_leverage=1.0)


def equity_hash(equity):
    return hashlib.sha256(",".join(repr(x) for x in equity).encode("ascii")).hexdigest()


def snapshot():
    d = generate(SPEC)
    out = {"spec": SPEC.to_dict(), "config": CFG, "data_fingerprint": d.fingerprint(), "strategies": {}}
    for name, cls in sorted(REGISTRY.items()):
        r = run_backtest(d, cls(), BrokerConfig(**CFG))
        m = metrics.compute(r.equity, r.ts, r.bar_seconds, r.trades)
        out["strategies"][name] = {
            "equity_sha256": equity_hash(r.equity), "final_equity": repr(r.final_equity),
            "n_fills": len(r.fills), "n_orders": len(r.orders), "n_trades": len(r.trades), "fees": repr(r.fees),
            "first_fills": [{k: (repr(v) if isinstance(v, float) else v) for k, v in f.to_dict().items()} for f in r.fills[:3]],
            "last_fill": ({k: (repr(v) if isinstance(v, float) else v) for k, v in r.fills[-1].to_dict().items()} if r.fills else None),
            "metrics": {k: repr(m[k]) for k in ("total_return", "sharpe", "sortino", "max_drawdown", "win_rate", "cvar_bar")},
        }
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true")
    args = ap.parse_args(argv)
    if os.path.exists(GOLDEN_PATH) and not args.update:
        raise SystemExit(f"{GOLDEN_PATH} exists; pass --update to overwrite")
    os.makedirs(os.path.dirname(GOLDEN_PATH), exist_ok=True)
    with open(GOLDEN_PATH, "w", encoding="utf-8") as f:
        json.dump(snapshot(), f, indent=1, sort_keys=True)
    print("wrote", GOLDEN_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
