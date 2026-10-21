"""参数优化：网格搜索 + 随机搜索 + walk-forward，进程池并行，结果去重，sqlite 断点续跑。

确定性设计
- 参数组合有规范键 param_key(params)（排序后的 JSON），结果按键排序输出，与完成顺序 / 进程数无关。
- 每个 worker 在初始化时只加载一次数据（按 data_spec 生成或读 CSV），任务只传参数（避免序列化大数组）。
- 同一 worker 内的指标缓存跨参数组共享（例如 SMA(20) 只算一次）。
- 断点：sqlite3 表 results(task, key, result)。重启后已完成的键直接复用，JSON(repr 浮点) 往返逐字节一致。

命令行（供断点恢复判据使用）::

    python -m qbt.optimizer --strategy ma_cross --n 2000 --seed 1 --workers 2 --checkpoint out/ck.db --out out/res.json
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import itertools
import json
import multiprocessing
import os
import random
import sqlite3
import sys
import time

from qbt.broker import BrokerConfig
from qbt.data import GenSpec, OHLCV, generate
from qbt.engine import run_backtest, INDICATOR_CACHE
from qbt.errors import ConfigError
from qbt import metrics as _metrics

RESULT_METRICS = ("total_return", "cagr", "sharpe", "sortino", "max_drawdown", "calmar", "win_rate",
                  "profit_factor", "n_trades", "cvar_daily", "end_equity")


# ---- 参数空间 ------------------------------------------------------------------
def param_key(params):
    return json.dumps(params, sort_keys=True, separators=(",", ":"))


def grid(space):
    """笛卡尔积，按键名排序展开，顺序确定。"""
    if not space:
        raise ConfigError("empty param space")
    keys = sorted(space)
    for k in keys:
        if not space[k]:
            raise ConfigError(f"empty choices for {k}")
    return [dict(zip(keys, vals)) for vals in itertools.product(*(list(space[k]) for k in keys))]


def random_search(space, n, seed):
    """无放回随机抽 n 组（不足则取全部），显式 seed。"""
    full = grid(space)
    rng = random.Random(seed)
    if n >= len(full):
        return sorted(full, key=param_key)
    picked = rng.sample(range(len(full)), n)
    return [full[i] for i in sorted(picked)]


def dedupe(combos):
    seen, out = set(), []
    for c in combos:
        k = param_key(c)
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out


# ---- 任务描述 ------------------------------------------------------------------
def make_task(strategy, data_spec, broker=None, objective="sharpe", symbol=None, slice_=None):
    """data_spec: GenSpec dict 或 {"csv": path, "symbol": ...}；slice_: [a, b) 可选。"""
    task = {"strategy": strategy, "data_spec": dict(data_spec), "broker": (broker or BrokerConfig()).to_dict(),
            "objective": objective, "symbol": symbol, "slice": list(slice_) if slice_ else None}
    task["id"] = hashlib.sha256(json.dumps(task, sort_keys=True).encode()).hexdigest()[:16]
    return task


# ---- worker 侧 ----------------------------------------------------------------
_DATA_CACHE = {}


def load_data(data_spec):
    """按规格加载数据（进程内缓存）。"""
    key = json.dumps(data_spec, sort_keys=True)
    d = _DATA_CACHE.get(key)
    if d is None:
        if "csv" in data_spec:
            d = OHLCV.from_csv(data_spec["csv"], symbol=data_spec.get("symbol", "CSV"),
                               bar_seconds=data_spec.get("bar_seconds")).validate()
        else:
            d = generate(GenSpec.from_dict(data_spec))
        _DATA_CACHE.clear()
        _DATA_CACHE[key] = d
    return d


def _worker_init(data_spec):
    load_data(data_spec)


def evaluate(task, params):
    """单个参数组的回测与评价；返回可 JSON 的 dict。"""
    from strategies import REGISTRY
    d = load_data(task["data_spec"])
    if task["slice"]:
        d = d.slice(task["slice"][0], task["slice"][1])
    cls = REGISTRY[task["strategy"]]
    res = run_backtest(d, cls(symbol=task["symbol"], **params), BrokerConfig(**task["broker"]))
    m = _metrics.compute(res.equity, res.ts, res.bar_seconds, res.trades)
    out = {k: m[k] for k in RESULT_METRICS}
    out["fills"] = len(res.fills)
    raw = m[task["objective"]]
    obj = raw
    if obj is None or m["n_trades"] == 0:
        obj = -1e300                     # 无交易 / 目标未定义（如 Calmar 无回撤）的组合不参与选优
    return {"key": param_key(params), "params": params, "metrics": out, "objective": obj, "raw_objective": raw}


def _evaluate_star(args):
    return evaluate(*args)


# ---- 断点 ---------------------------------------------------------------------
class Checkpoint:
    def __init__(self, path):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.execute("CREATE TABLE IF NOT EXISTS results (task TEXT, key TEXT, result TEXT, PRIMARY KEY (task, key))")
        self.conn.commit()
        self._pending = 0

    def load(self, task_id):
        rows = self.conn.execute("SELECT key, result FROM results WHERE task = ?", (task_id,)).fetchall()
        return {k: json.loads(r) for k, r in rows}

    def save(self, task_id, result, flush_every=20):
        self.conn.execute("INSERT OR REPLACE INTO results (task, key, result) VALUES (?, ?, ?)",
                          (task_id, result["key"], json.dumps(result, sort_keys=True)))
        self._pending += 1
        if self._pending >= flush_every:
            self.flush()

    def flush(self):
        self.conn.commit()
        self._pending = 0

    def close(self):
        self.flush()
        self.conn.close()


# ---- 搜索 ---------------------------------------------------------------------
def run_search(task, combos, workers=1, checkpoint=None, chunksize=None, progress=None, flush_every=20):
    """评估全部参数组；返回按 key 排序的结果列表（与 workers 数、完成顺序无关）。"""
    combos = dedupe(combos)
    done = checkpoint.load(task["id"]) if checkpoint else {}
    todo = [c for c in combos if param_key(c) not in done]
    results = dict(done)
    if workers <= 1:
        _worker_init(task["data_spec"])
        it = map(_evaluate_star, [(task, c) for c in todo])
    else:
        ctx = multiprocessing.get_context("fork") if "fork" in multiprocessing.get_all_start_methods() else None
        pool = concurrent.futures.ProcessPoolExecutor(max_workers=workers, mp_context=ctx,
                                                      initializer=_worker_init, initargs=(task["data_spec"],))
        it = pool.map(_evaluate_star, [(task, c) for c in todo], chunksize=chunksize or max(1, len(todo) // (workers * 8)))
    try:
        for k, r in enumerate(it, 1):
            results[r["key"]] = r
            if checkpoint:
                checkpoint.save(task["id"], r, flush_every)
            if progress and k % progress == 0:
                print(f"[{k}/{len(todo)}] {r['key'][:60]} objective={r['objective']:.4f}", flush=True)
    finally:
        if workers > 1:
            pool.shutdown()
        if checkpoint:
            checkpoint.flush()
    wanted = {param_key(c) for c in combos}
    return [results[k] for k in sorted(results) if k in wanted]


def best(results, maximize=True):
    """最优结果；并列时按 key 字典序取最小，保证确定性。"""
    if not results:
        return None
    return sorted(results, key=lambda r: ((-r["objective"] if maximize else r["objective"]), r["key"]))[0]


# ---- walk-forward -------------------------------------------------------------
def walk_forward(task, combos, n_folds=3, train_frac=0.7, workers=1, checkpoint=None):
    """滚动 walk-forward：把数据等分为 n_folds 段，每段前 train_frac 为样本内（选最优参数），其余为样本外。
    返回 {"folds": [{"train": [a,b], "test": [b,c], "best": {...}, "in_sample": {...}, "out_of_sample": {...}}...],
          "oos_mean_objective": ..., "is_mean_objective": ...}"""
    if not isinstance(n_folds, int) or n_folds < 1 or not 0 < train_frac < 1:
        raise ConfigError("n_folds >= 1 and 0 < train_frac < 1 required")
    n = len(load_data(task["data_spec"]))
    fold_len = n // n_folds
    if fold_len < 10:
        raise ConfigError("too few bars per fold")
    folds = []
    for k in range(n_folds):
        a, c = k * fold_len, (k + 1) * fold_len if k < n_folds - 1 else n
        b = a + int((c - a) * train_frac)
        train_task = dict(task, slice=[a, b])
        train_task["id"] = task["id"] + f":wf{k}:train"
        res = run_search(train_task, combos, workers=workers, checkpoint=checkpoint)
        top = best(res)
        test_task = dict(task, slice=[b, c])
        _worker_init(task["data_spec"])
        oos = evaluate(test_task, top["params"])
        folds.append({"train": [a, b], "test": [b, c], "best": top["params"],
                      "in_sample": top["metrics"], "out_of_sample": oos["metrics"],
                      "is_objective": top["objective"],
                      "oos_objective": oos["raw_objective"] if oos["raw_objective"] is not None else 0.0})
    return {"folds": folds,
            "is_mean_objective": sum(f["is_objective"] for f in folds) / len(folds),
            "oos_mean_objective": sum(f["oos_objective"] for f in folds) / len(folds)}


# ---- 命令行 --------------------------------------------------------------------
def main(argv=None):
    from strategies import REGISTRY
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="ma_cross", choices=sorted(REGISTRY))
    ap.add_argument("--n", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--random", type=int, default=0, help="随机搜索组数；0 = 全网格")
    ap.add_argument("--limit", type=int, default=0, help="只取前 N 组（调试/判据用）")
    ap.add_argument("--checkpoint", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--objective", default="sharpe")
    ap.add_argument("--walk-forward", type=int, default=0, help="折数；0 = 普通搜索")
    args = ap.parse_args(argv)
    cls = REGISTRY[args.strategy]
    combos = random_search(cls.PARAM_SPACE, args.random, args.seed) if args.random else grid(cls.PARAM_SPACE)
    if args.limit:
        combos = combos[: args.limit]
    task = make_task(args.strategy, GenSpec(n=args.n, seed=args.seed).to_dict(), objective=args.objective)
    ck = Checkpoint(args.checkpoint) if args.checkpoint else None
    t0 = time.perf_counter()
    if args.walk_forward:
        out = walk_forward(task, combos, n_folds=args.walk_forward, workers=args.workers, checkpoint=ck)
    else:
        results = run_search(task, combos, workers=args.workers, checkpoint=ck, progress=50)
        out = {"task": task, "n_results": len(results), "best": best(results), "results": results}
    out["elapsed"] = round(time.perf_counter() - t0, 3)
    if ck:
        ck.close()
    text = json.dumps({k: v for k, v in out.items() if k != "elapsed"}, sort_keys=True, indent=1)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    print(f"done {len(combos)} combos in {out['elapsed']}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
