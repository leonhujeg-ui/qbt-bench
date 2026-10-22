"""模糊测试：随机 / 畸形输入打 engine、broker、indicators、metrics。

规则：QBTError 及其子类 = 受控拒绝；成功返回 = ok；其他任何异常 = 崩溃（记录类型、回溯与复现 seed）。
另外在 broker 目标里每根 bar 校验会计恒等式，违反也算崩溃（InvariantError）。

用法::

    python fuzz/fuzz.py --iterations 100000 --seed 0 --workers 4 --out fuzz/results.json
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import multiprocessing
import os
import random
import sys
import time
import traceback

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from qbt.broker import Broker, BrokerConfig, ORDER_TYPES          # noqa: E402
from qbt.data import OHLCV, generate                               # noqa: E402
from qbt.engine import run_backtest                                # noqa: E402
from qbt.errors import QBTError                                    # noqa: E402
from qbt import indicators as I                                    # noqa: E402
from qbt import metrics as M                                       # noqa: E402
from strategies import REGISTRY                                    # noqa: E402

WEIRD_NUMBERS = [math.nan, math.inf, -math.inf, 0.0, -1.0, -1e-9, 1e308, -1e308, 1e-308, True, False, None, "1", "x",
                 [], 10 ** 30]


class InvariantError(AssertionError):
    pass


# ---- 输入生成 ------------------------------------------------------------------
def weird(r, p=0.15):
    return r.choice(WEIRD_NUMBERS) if r.random() < p else None


def random_ohlcv(r):
    """从一段合法数据出发，按概率注入各类畸形。"""
    n = r.choice([0, 1, 2, 3, 5, 10, 30])
    d = generate(n=n, seed=r.randrange(1 << 30), halt_prob=r.choice([0.0, 0.1]), gap_prob=0.2, jump_prob=0.1)
    cols = {"ts": d.ts, "open": d.open, "high": d.high, "low": d.low, "close": d.close, "volume": d.volume}
    for name, col in cols.items():
        if col and r.random() < 0.35:
            idx = r.randrange(len(col))
            w = weird(r, 0.7)
            col[idx] = w if w is not None else col[idx] * r.choice([-1, 0, 1e6, 1e-6])
    if d.ts and r.random() < 0.15:
        r.shuffle(d.ts)
    if r.random() < 0.1 and d.close:
        d.close.pop()
    if r.random() < 0.05:
        d.symbol = r.choice(["", 5, None, "OK"])
    d._validated = False
    return d


def random_params(r, cls):
    params = {}
    for k, vals in cls.PARAM_SPACE.items():
        if r.random() < 0.9:
            params[k] = r.choice(vals)
            if r.random() < 0.1:
                w = weird(r, 1.0)
                params[k] = w if w is not None else params[k]
    if r.random() < 0.05:
        params["bogus"] = 1
    return params


def random_config(r):
    kw = {}
    fields = {"cash": [1.0, 100.0, 1e5, 1e12], "maker_fee": [0.0, 0.001, 0.5], "taker_fee": [0.0, 0.002, 0.9],
              "slippage": [0.0, 0.01, 0.5], "impact": [0.0, 0.1, 5.0], "max_participation": [0.01, 0.1, 1.0],
              "max_leverage": [0.1, 1.0, 10.0, 1000.0], "maintenance_margin": [0.0, 0.1, 0.9],
              "liquidation_fee": [0.0, 0.05], "funding_rate": [0.0, 0.01, -0.01, 0.5], "funding_interval": [0, 1, 3],
              "allow_short": [True, False]}
    for k, vals in fields.items():
        if r.random() < 0.6:
            kw[k] = r.choice(vals)
            w = weird(r, 0.08)
            if w is not None:
                kw[k] = w
    try:
        return BrokerConfig(**kw)
    except TypeError:
        return BrokerConfig()


# ---- 目标 ---------------------------------------------------------------------
def fuzz_engine(r):
    cls = r.choice(list(REGISTRY.values()))
    if r.random() < 0.5:                      # 一半：合法数据 + 合法参数 + 极端但合法的配置 => 必须成功跑完
        d = generate(n=r.choice([1, 2, 5, 30, 120]), seed=r.randrange(1 << 30), halt_prob=r.choice([0.0, 0.2]),
                     gap_prob=0.2, jump_prob=0.1, sigma=r.choice([0.0, 0.4, 5.0]))
        params = {k: r.choice(v) for k, v in cls.PARAM_SPACE.items()}
        cfg = random_config(r)
        try:
            cfg.check()
        except QBTError:
            cfg = BrokerConfig(max_leverage=r.choice([0.5, 1.0, 50.0]), max_participation=r.choice([0.01, 1.0]))
        res = run_backtest(d, cls(**params), cfg, strict=r.random() < 0.3)
        if len(res.equity) != len(d):
            raise InvariantError("equity curve length mismatch")
        return
    d = random_ohlcv(r)
    strat = cls(**random_params(r, cls))
    run_backtest(d, strat, random_config(r), strict=r.random() < 0.3)


def _check_broker_invariants(b, i):
    marked = b.cash + sum(p.qty * b.data[s].close[i] for s, p in b.positions.items())
    if abs(marked - b.equity) > 1e-6 * max(1.0, abs(b.equity)):
        raise InvariantError(f"equity {b.equity} != marked {marked}")
    if abs((b.assets() - b.liabilities()) - b.equity) > 1e-6 * max(1.0, abs(b.equity)):
        raise InvariantError("assets - liabilities != equity")
    fees = sum(f.fee for f in b.fills)
    if abs(fees - b.fees_paid) > 1e-6 * max(1.0, abs(fees)):
        raise InvariantError("fees mismatch")


def fuzz_broker(r):
    n = r.choice([1, 2, 5, 20])
    d = generate(n=n, seed=r.randrange(1 << 30), halt_prob=r.choice([0.0, 0.3]))
    cfg = random_config(r)
    b = Broker({d.symbol: d}, cfg)
    for i in range(n):
        b.on_bar(i)
        _check_broker_invariants(b, i)
        for _ in range(r.randint(0, 4)):
            side = r.choice([1, -1, 0, 2, None])
            qty = r.choice([1, 0.5, 1e6, 1e-15]) if r.random() < 0.7 else weird(r, 1.0)
            otype = r.choice(ORDER_TYPES + ("bogus",))
            price = d.close[i] * r.uniform(0.5, 1.5) if r.random() < 0.8 else weird(r, 1.0)
            display = r.choice([None, 1, 0.1]) if r.random() < 0.8 else weird(r, 1.0)
            sym = d.symbol if r.random() < 0.9 else r.choice(["ZZ", 3, None])
            try:
                o = b.submit(sym, side, qty, otype, price=price, display=display,
                             expires=r.choice([None, i, i + 1, -5]), reduce_only=r.random() < 0.3)
            except QBTError:
                continue
            if r.random() < 0.2:
                b.cancel(o)
        if r.random() < 0.05:
            b.cancel_all()


def fuzz_indicators(r):
    n = r.choice([0, 1, 2, 5, 30])
    x = [r.uniform(-100, 100) for _ in range(n)]
    for _ in range(r.randint(0, 3)):
        if x:
            w = weird(r, 1.0)
            x[r.randrange(n)] = w if isinstance(w, (int, float)) and not isinstance(w, bool) else x[0]
    hi = [v + abs(r.gauss(0, 1)) for v in x]
    lo = [v - abs(r.gauss(0, 1)) for v in x]
    vol = [abs(r.gauss(0, 100)) for _ in x]
    win = r.choice([1, 2, 3, 14, 50]) if r.random() < 0.8 else weird(r, 1.0)
    name = r.choice(list(I.REGISTRY))
    fn = I.REGISTRY[name]
    if r.random() < 0.3:                      # 非窗口参数（alpha/k/mult/af/ddof/fast/slow）也打畸形值
        kw = {"ema": {"alpha": r.choice([0.1, 1.0, 1.5, -0.2])}, "bollinger": {"k": r.choice([0.0, 2.0, -1.0])},
              "keltner": {"k": r.choice([1.0, -2.0]), "atr_n": r.choice([1, 0])}, "supertrend": {"mult": r.choice([3.0, -1.0, 0.0])},
              "parabolic_sar": {"af0": r.choice([0.02, 0.5, -1.0]), "af_max": r.choice([0.2, 0.01])},
              "rolling_std": {"ddof": r.choice([0, 1, -1, 2.5])}, "kama": {"fast": r.choice([2, 0, -1]), "slow": r.choice([30, 1])}}
        if name in kw:
            for k2, v2 in kw[name].items():
                w = weird(r, 0.3)
                kw[name][k2] = w if w is not None else v2
            base = {"ema": (x, 5), "bollinger": (x, 5), "keltner": (hi, lo, x, 5), "supertrend": (hi, lo, x, 5),
                    "parabolic_sar": (hi, lo), "rolling_std": (x, 5), "kama": (x, 5)}[name]
            fn(*base, **kw[name])
            return
    args = {"sma": (x, win), "ema": (x, win), "wma": (x, win), "hma": (x, win), "dema": (x, win), "tema": (x, win),
            "kama": (x, win), "rsi": (x, win), "macd": (x,), "roc": (x, win), "momentum": (x, win), "trix": (x, win),
            "rolling_max": (x, win), "rolling_min": (x, win), "donchian": (hi, lo, win),
            "stochastic": (hi, lo, x), "williams_r": (hi, lo, x), "aroon": (hi, lo, win),
            "true_range": (hi, lo, x), "atr": (hi, lo, x, win), "rolling_std": (x, win), "bollinger": (x, win),
            "zscore": (x, win), "keltner": (hi, lo, x), "adx": (hi, lo, x, win), "cci": (hi, lo, x, win),
            "obv": (x, vol), "vwap": (hi, lo, x, vol, win), "mfi": (hi, lo, x, vol, win), "cmf": (hi, lo, x, vol, win),
            "ichimoku": (hi, lo, x), "parabolic_sar": (hi, lo), "supertrend": (hi, lo, x, win),
            "rolling_drawdown": (x, win), "rolling_max_drawdown": (x, win), "linreg_slope": (x, win)}[name]
    fn(*args)


def fuzz_metrics(r):
    n = r.choice([0, 1, 2, 5, 40])
    eq = [100.0 * (1 + r.uniform(-0.5, 0.5)) for _ in range(n)]
    for _ in range(r.randint(0, 2)):
        if eq:
            w = weird(r, 1.0)
            eq[r.randrange(n)] = w if isinstance(w, (int, float)) and not isinstance(w, bool) else 0.0
    ts = [1_600_000_000 + i * r.choice([60, 3600, 86400]) for i in range(n)]
    trades = [{"pnl": r.uniform(-10, 10), "bars": r.randint(0, 5)} for _ in range(r.randint(0, 5))]
    M.compute(eq, ts, r.choice([60, 3600, 86400]), trades, alpha=r.choice([0.01, 0.05, 0.5]))


TARGETS = {"engine": fuzz_engine, "broker": fuzz_broker, "indicators": fuzz_indicators, "metrics": fuzz_metrics}


# ---- 驱动 ---------------------------------------------------------------------
def run_range(args):
    seed, start, stop = args
    counts = collections.Counter()
    crashes = []
    names = sorted(TARGETS)
    for i in range(start, stop):
        r = random.Random(f"{seed}:{i}")
        target = names[i % len(names)]
        try:
            TARGETS[target](r)
            counts[f"{target}:ok"] += 1
        except QBTError as e:
            counts[f"{target}:{type(e).__name__}"] += 1
        except BaseException as e:                       # noqa: BLE001 —— 任何其他异常都算崩溃
            counts[f"{target}:CRASH:{type(e).__name__}"] += 1
            if len(crashes) < 20:
                crashes.append({"target": target, "iteration": i, "seed": seed, "error": repr(e)[:300],
                                "traceback": traceback.format_exc()[-1500:]})
    return counts, crashes


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--iterations", type=int, default=100_000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--workers", type=int, default=max(1, os.cpu_count() or 1))
    ap.add_argument("--out", default=os.path.join(REPO, "fuzz", "results.json"))
    args = ap.parse_args(argv)
    chunk = max(1, args.iterations // (args.workers * 8))
    ranges = [(args.seed, s, min(s + chunk, args.iterations)) for s in range(0, args.iterations, chunk)]
    t0 = time.perf_counter()
    if args.workers > 1:
        with multiprocessing.get_context("fork").Pool(args.workers) as pool:
            parts = pool.map(run_range, ranges)
    else:
        parts = [run_range(rg) for rg in ranges]
    counts, crashes = collections.Counter(), []
    for c, cr in parts:
        counts.update(c)
        crashes.extend(cr)
    n_crash = sum(v for k, v in counts.items() if ":CRASH:" in k)
    summary = {"iterations": args.iterations, "seed": args.seed, "elapsed": round(time.perf_counter() - t0, 2),
               "crashes": n_crash, "distribution": dict(sorted(counts.items())), "crash_samples": crashes[:20]}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=1, ensure_ascii=False)
    for k, v in sorted(counts.items()):
        print(f"{k:40s} {v}")
    print(f"iterations={args.iterations} crashes={n_crash} elapsed={summary['elapsed']}s -> {args.out}")
    return 1 if n_crash else 0


if __name__ == "__main__":
    sys.exit(main())
