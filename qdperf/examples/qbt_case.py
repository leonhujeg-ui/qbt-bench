"""G2 的真实用例：本仓 qbt 的回测引擎（合成 K 线 × ma_cross，缺省 100 万根）。不改 qbt/ 的任何代码。

    python3.12 -m qdperf.profile_gate baseline qdperf.examples.qbt_case:backtest --args '{"n": 1000000}' --out base.json
    python3.12 -m qdperf.profile_gate run qdperf.examples.qbt_case:backtest --args '{"n": 1000000}' \
        --env QDPERF_QBT_SLOW=0.12 --out slow.json

- 数据按 (n, seed) 在进程内缓存：冷测量含生成数据，热测量不含（与 bench/bench.py 的冷 / 热口径一致：数据已在内存）；
- 每次调用用全新的 IndicatorCache，热路径包含指标计算 + 逐 bar 主循环 + 撮合；
- 环境变量 QDPERF_QBT_SLOW=f > 0 时，回测之后跑一个“无意义循环” meaningless_loop，空转时长 = f × 本次调用已用时长，
  即这次调用恰好慢 f（例如 0.12 = 12%），与进程本身快慢无关（本机不同进程的速度能差 10% 以上，按固定迭代次数放慢
  达不到标称比例）；用来证明回归闸能抓到。调用参数不变，基线与本次两份 JSON 可比。
"""
import os
import sys
import time

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

_DATA = {}
SLOW_ENV = "QDPERF_QBT_SLOW"          # “新版本”的开关：放慢比例（如 0.12）；调用参数不变，基线与本次两份 JSON 可比


def _data(n, seed):
    from qbt.data import GenSpec, generate
    key = (n, seed)
    if key not in _DATA:
        _DATA[key] = generate(GenSpec(n=n, seed=seed))
    return _DATA[key]


def meaningless_loop(seconds):
    """故意放慢用的无意义循环：空转到 seconds 秒过去为止（结果不被使用），返回空转的迭代次数。"""
    end = time.perf_counter() + seconds
    x = n = 0
    while time.perf_counter() < end:
        for i in range(1000):
            x += i
        n += 1000
    return n


def backtest(n=1_000_000, seed=1, strategy="ma_cross"):
    t0 = time.perf_counter()
    from qbt.broker import BrokerConfig
    from qbt.engine import IndicatorCache, run_backtest
    from strategies import REGISTRY
    res = run_backtest(_data(n, seed), REGISTRY[strategy](), BrokerConfig(max_participation=1.0),
                       cache=IndicatorCache())
    slow = float(os.environ.get(SLOW_ENV, "0") or 0)
    if slow > 0:
        meaningless_loop(slow * (time.perf_counter() - t0))
    return {"final_equity": res.final_equity, "fills": len(res.fills)}
