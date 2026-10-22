"""生成/更新黄金文件（格式 v2：JSON，浮点按 repr 写出 —— 最短往返表示，≤ 17 位有效数字，读回逐位相同）。

用法：python3.12 tools/make_golden.py            # 写 tests/golden/golden_v2.json（已存在则报错）
      python3.12 tools/make_golden.py --update   # 覆盖（只有在有意改变引擎语义时才这么做，并在提交信息说明）

比较口径（tests/test_golden.py）：整数字段（成交笔数 / 方向 / 根序号 / 订单号 / 订单顺序）完全相等；
金额类浮点（权益 / 价格 / 数量 / 费用）相对容差 1e-9；度量类浮点（夏普等）相对容差 5e-7。
任何机器用同一 seed 重新生成都应在容差内对上；同一机器两次生成逐字节相同另有判据。
"""
import argparse
import json
import os
import platform
import subprocess
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)

from qbt.broker import BrokerConfig                      # noqa: E402
from qbt.data import GenSpec, generate, generate_universe  # noqa: E402
from qbt.engine import run_backtest                       # noqa: E402
from qbt import metrics                                   # noqa: E402
from strategies import REGISTRY                           # noqa: E402

GOLDEN_PATH = os.path.join(REPO, "tests", "golden", "golden_v2.json")
SPEC = GenSpec(n=3000, seed=20240601, halt_prob=0.003, gap_prob=0.02, jump_prob=0.005)
CFG = dict(max_participation=0.5, maker_fee=0.0002, taker_fee=0.0005, slippage=0.0001, max_leverage=1.0)
METRIC_KEYS = ("total_return", "cagr", "sharpe", "sortino", "max_drawdown", "max_drawdown_bars", "win_rate",
               "profit_factor", "cvar_bar", "var_daily", "n_trades")
HEAD = 200     # 数据黄金只存前 HEAD 根（足够暴露生成器任何字节级改动；全量指纹判据另见 test_data）


def _cols(d, n):
    return {c: list(getattr(d, c))[:n] for c in ("ts", "open", "high", "low", "close", "volume")}


def provenance():
    """黄金文件是在哪台机器 / 哪个提交上生成的（没有时间戳：同机两次快照要逐字节相同）。"""
    try:
        commit = subprocess.run(["git", "-C", REPO, "rev-parse", "--short", "HEAD"], capture_output=True,
                                text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        commit = ""
    return {"platform": platform.platform(), "machine": platform.machine(),
            "python": platform.python_implementation() + " " + platform.python_version(), "git_commit": commit}


def snapshot():
    d = generate(SPEC)
    out = {"format": 2, "command": "python3.12 tools/make_golden.py --update", "spec": SPEC.to_dict(), "config": CFG,
           "generated_on": provenance(),
           "data_head": _cols(d, HEAD),
           "pinned_data": {"gen_n2000_seed123_head": _cols(generate(n=2000, seed=123), 2000),        # 全序列钉住（原 sha256 判据覆盖的范围）
                           "universe_B_n500_seed9_head": _cols(generate_universe(["A", "B"], 500, 9)["B"], 500)},
           "strategies": {}}
    for name, cls in sorted(REGISTRY.items()):
        r = run_backtest(d, cls(), BrokerConfig(**CFG))
        m = metrics.compute(r.equity, r.ts, r.bar_seconds, r.trades)
        out["strategies"][name] = {
            "n_fills": len(r.fills), "n_orders": len(r.orders), "n_trades": len(r.trades),
            "fees": r.fees, "final_equity": r.final_equity, "equity": list(r.equity),
            "fills": [f.to_dict() for f in r.fills],
            "order_sequence": [[o.id, o.created, o.side, o.type, o.status] for o in r.orders],
            "metrics": {k: m[k] for k in METRIC_KEYS},
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
        json.dump(snapshot(), f, indent=0, sort_keys=True)
    print("wrote", GOLDEN_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
