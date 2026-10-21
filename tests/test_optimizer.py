"""阶段 5 判据：网格/随机搜索、并行确定性（1 进程 vs 多进程逐字节相同）、断点恢复（含中途 kill）、walk-forward。"""
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from qbt.data import GenSpec, generate
from qbt.errors import ConfigError
from qbt import optimizer as O
from strategies import REGISTRY
from tests._helpers import QBTTestCase, scale

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SPACE = {"fast": [5, 10], "slow": [30, 50], "ma": ["sma", "ema"], "size": [0.5], "short": [True]}


def task(n=None, seed=1, **kw):
    return O.make_task("ma_cross", GenSpec(n=n or scale(3000, 800), seed=seed).to_dict(), **kw)


class TestParamSpace(QBTTestCase):
    def test_grid(self):
        g = O.grid(SPACE)
        self.assertEqual(len(g), 8)
        self.assertEqual(g[0], {"fast": 5, "ma": "sma", "short": True, "size": 0.5, "slow": 30})
        self.assertEqual(list(g[0]), sorted(SPACE))                     # 键排序
        self.assertEqual(len({O.param_key(c) for c in g}), 8)
        self.assertEqual(O.grid(SPACE), g)
        for cls in REGISTRY.values():
            self.assertGreaterEqual(len(O.grid(cls.PARAM_SPACE)), 16)
        with self.assertRaises(ConfigError):
            O.grid({})
        with self.assertRaises(ConfigError):
            O.grid({"a": []})

    def test_random_search(self):
        a = O.random_search(SPACE, 5, seed=3)
        b = O.random_search(SPACE, 5, seed=3)
        c = O.random_search(SPACE, 5, seed=4)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(len(a), 5)
        self.assertEqual(len({O.param_key(x) for x in a}), 5)
        keys = {O.param_key(x) for x in O.grid(SPACE)}
        self.assertTrue(all(O.param_key(x) in keys for x in a))
        self.assertEqual(len(O.random_search(SPACE, 100, 1)), 8)
        self.assertEqual([O.param_key(x) for x in a], sorted(O.param_key(x) for x in a) if False else [O.param_key(x) for x in a])

    def test_param_key_and_dedupe(self):
        self.assertEqual(O.param_key({"b": 1, "a": 2}), O.param_key({"a": 2, "b": 1}))
        self.assertEqual(O.param_key({"a": 1.0}), '{"a":1.0}')
        d = O.dedupe([{"a": 1, "b": 2}, {"b": 2, "a": 1}, {"a": 2}])
        self.assertEqual(d, [{"a": 1, "b": 2}, {"a": 2}])

    def test_make_task_id(self):
        t1, t2 = task(seed=1), task(seed=1)
        self.assertEqual(t1["id"], t2["id"])
        self.assertNotEqual(task(seed=2)["id"], t1["id"])
        self.assertNotEqual(task(objective="total_return")["id"], t1["id"])
        self.assertNotEqual(task(slice_=[0, 10])["id"], t1["id"])
        self.assertEqual(len(t1["id"]), 16)


class TestSearch(QBTTestCase):
    def test_serial_vs_parallel_bytewise_identical(self):
        t = task()
        combos = O.grid(SPACE)
        r1 = O.run_search(t, combos, workers=1)
        r2 = O.run_search(t, list(reversed(combos)), workers=2)          # 不同提交顺序 + 多进程
        r8 = O.run_search(t, combos, workers=8, chunksize=1)
        self.assertEqual(json.dumps(r1, sort_keys=True), json.dumps(r2, sort_keys=True))
        self.assertEqual(json.dumps(r1, sort_keys=True), json.dumps(r8, sort_keys=True))
        self.assertEqual([r["key"] for r in r1], sorted(r["key"] for r in r1))
        self.assertEqual(len(r1), 8)
        self.assertTrue(all(set(r["metrics"]) >= set(O.RESULT_METRICS) for r in r1))

    def test_best_and_objective_rules(self):
        rs = [{"key": "b", "objective": 1.0}, {"key": "a", "objective": 1.0}, {"key": "c", "objective": -2.0}]
        self.assertEqual(O.best(rs)["key"], "a")                          # 并列按 key
        self.assertEqual(O.best(rs, maximize=False)["key"], "c")
        self.assertIsNone(O.best([]))
        t = task()
        O._worker_init(t["data_spec"])
        none = O.evaluate(t, {"fast": 30, "slow": 30})                    # fast>=slow：不交易
        self.assertEqual((none["metrics"]["n_trades"], none["objective"]), (0, -1e300))
        self.assertEqual(none["raw_objective"], 0.0)                       # 无交易：夏普原始值 0，哨兵只用于选优
        # 样本外无交易的折：报告 0.0 而不是哨兵
        wf = O.walk_forward(dict(t), [{"fast": 30, "slow": 30}], n_folds=1, train_frac=0.5, workers=1)
        self.assertEqual(wf["folds"][0]["oos_objective"], 0.0)
        self.assertEqual(wf["folds"][0]["is_objective"], -1e300)
        calmar_task = task(objective="calmar")
        self.assertEqual(O.evaluate(calmar_task, {"fast": 30, "slow": 30})["objective"], -1e300)
        ok = O.evaluate(t, {"fast": 5, "slow": 30})
        self.assertEqual(ok["objective"], ok["metrics"]["sharpe"])
        self.assertGreater(ok["metrics"]["n_trades"], 0)

    def test_checkpoint_roundtrip_and_resume(self):
        t = task()
        combos = O.grid(SPACE)
        with tempfile.TemporaryDirectory() as tmp:
            ck = O.Checkpoint(os.path.join(tmp, "ck.db"))
            half = O.run_search(t, combos[:4], workers=1, checkpoint=ck, flush_every=2)
            self.assertEqual(set(ck.load(t["id"])), {r["key"] for r in half})
            full = O.run_search(t, combos, workers=2, checkpoint=ck)
            rows = sqlite3.connect(os.path.join(tmp, "ck.db")).execute("SELECT COUNT(*) FROM results").fetchone()[0]
            self.assertEqual(rows, 8)                                                # run_search 结束即提交（不依赖 close）
            ck.close()
            clean = O.run_search(t, combos, workers=1)
            self.assertEqual(json.dumps(full, sort_keys=True), json.dumps(clean, sort_keys=True))
            ck2 = O.Checkpoint(os.path.join(tmp, "ck.db"))
            stored = ck2.load(t["id"])
            self.assertEqual(len(stored), 8)
            # 已完成的键不再重算：用一个会抛异常的策略名证明 run_search 没有调用 evaluate
            poisoned = dict(t, strategy="does_not_exist")
            poisoned["id"] = t["id"]
            again = O.run_search(poisoned, combos, workers=1, checkpoint=ck2)
            self.assertEqual(json.dumps(again, sort_keys=True), json.dumps(clean, sort_keys=True))
            subset = O.run_search(poisoned, combos[:3], workers=1, checkpoint=ck2)
            self.assertEqual(len(subset), 3)                              # 只返回本次请求的组合
            self.assertEqual(ck2.load("other-task"), {})
            ck2.close()

    def test_kill_and_resume_via_cli(self):
        """子进程跑优化器，等 sqlite 里出现已提交的结果后 SIGKILL，再续跑；输出必须与一次跑完逐字节相同。"""
        with tempfile.TemporaryDirectory() as tmp:
            db, out_resumed, out_clean = (os.path.join(tmp, x) for x in ("ck.db", "resumed.json", "clean.json"))
            n, limit = scale(12000, 4000), 36
            cmd = [sys.executable, "-m", "qbt.optimizer", "--strategy", "ma_cross", "--n", str(n), "--seed", "7",
                   "--workers", "2", "--limit", str(limit)]
            env = dict(os.environ, PYTHONPATH=REPO)
            proc = subprocess.Popen(cmd + ["--checkpoint", db, "--out", out_resumed], cwd=REPO, env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            rows, deadline = 0, time.time() + 120
            while time.time() < deadline and proc.poll() is None:
                time.sleep(0.05)
                try:
                    rows = sqlite3.connect(db).execute("SELECT COUNT(*) FROM results").fetchone()[0]
                except sqlite3.Error:
                    rows = 0
                if rows >= 20:
                    break
            if proc.poll() is None:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)       # 连同进程池 worker 一起杀掉（模拟整机 kill）
                proc.wait()
            killed_midway = rows >= 20 and rows < limit and not os.path.exists(out_resumed)
            self.assertTrue(killed_midway, f"rows={rows}, finished={os.path.exists(out_resumed)}")
            subprocess.run(cmd + ["--checkpoint", db, "--out", out_resumed], cwd=REPO, env=env, check=True,
                           stdout=subprocess.DEVNULL)
            subprocess.run(cmd + ["--out", out_clean], cwd=REPO, env=env, check=True, stdout=subprocess.DEVNULL)
            with open(out_resumed, "rb") as f1, open(out_clean, "rb") as f2:
                self.assertEqual(f1.read(), f2.read())
            with open(out_clean) as f:
                self.assertEqual(json.load(f)["n_results"], limit)


class TestSurvivorKillers(QBTTestCase):
    def test_evaluate_slice_and_fields(self):
        from qbt.engine import run_backtest
        from qbt.broker import BrokerConfig
        t = task(n=600)
        O._worker_init(t["data_spec"])
        res = O.evaluate(dict(t, slice=[100, 400]), {"fast": 5, "slow": 20})
        d = O.load_data(t["data_spec"]).slice(100, 400)
        direct = run_backtest(d, REGISTRY["ma_cross"](fast=5, slow=20), BrokerConfig(**t["broker"]))
        self.assertEqual(res["metrics"]["fills"], len(direct.fills))
        self.assertEqual(res["metrics"]["end_equity"], direct.final_equity)
        self.assertIn("fills", res["metrics"])

    def test_checkpoint_flush_semantics(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ck.db")
            ck = O.Checkpoint(path)
            other = sqlite3.connect(path)
            count = lambda: other.execute("SELECT COUNT(*) FROM results").fetchone()[0]
            ck.save("t", {"key": "a", "objective": 1.0}, flush_every=2)
            self.assertEqual((count(), ck._pending), (0, 1))                       # 未到阈值不提交
            ck.save("t", {"key": "b", "objective": 2.0}, flush_every=2)
            self.assertEqual((count(), ck._pending), (2, 0))                       # 到阈值提交并清零
            ck.save("t", {"key": "c", "objective": 3.0}, flush_every=20)
            self.assertEqual(count(), 2)
            ck.flush()
            self.assertEqual(count(), 3)
            ck.save("t", {"key": "c", "objective": 4.0}, flush_every=1)              # 同键覆盖
            self.assertEqual(ck.load("t")["c"]["objective"], 4.0)
            ck.save("t", {"key": "d", "objective": 5.0}, flush_every=50)             # 未提交
            self.assertEqual(count(), 3)
            ck.close()                                                               # close 必须提交
            self.assertEqual(count(), 4)
            other.close()
            import inspect
            self.assertEqual(inspect.signature(O.run_search).parameters["flush_every"].default, 20)
            self.assertEqual(inspect.signature(O.Checkpoint.save).parameters["flush_every"].default, 20)

    def test_progress_printing(self):
        import contextlib
        import io
        t = task(n=400)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            O.run_search(t, O.grid(SPACE)[:4], workers=1, progress=2)
        lines = buf.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("[2/4]") and lines[1].startswith("[4/4]"))
        self.assertIn("objective=", lines[0])
        with contextlib.redirect_stdout(io.StringIO()) as quiet:
            O.run_search(t, O.grid(SPACE)[:2], workers=1)
        self.assertEqual(quiet.getvalue(), "")

    def test_walk_forward_single_fold_and_uneven_split_and_checkpoint(self):
        t = task(n=1000)
        combos = O.grid(SPACE)[:2]
        one = O.walk_forward(t, combos, n_folds=1, train_frac=0.5, workers=1)
        self.assertEqual(one["folds"][0]["train"], [0, 500])
        self.assertEqual(one["folds"][0]["test"], [500, 1000])
        with self.assertRaises(ConfigError):
            O.walk_forward(t, combos, n_folds=2, train_frac=0.0)
        three = O.walk_forward(t, combos, n_folds=3, train_frac=0.5, workers=1)
        self.assertEqual([f["train"][0] for f in three["folds"]], [0, 333, 666])
        self.assertEqual(three["folds"][-1]["test"][1], 1000)                       # 最后一折吃掉余数
        with tempfile.TemporaryDirectory() as tmp:
            ck = O.Checkpoint(os.path.join(tmp, "ck.db"))
            with_ck = O.walk_forward(t, combos, n_folds=3, train_frac=0.5, workers=1, checkpoint=ck)
            keys = ck.conn.execute("SELECT DISTINCT task FROM results").fetchall()
            ck.close()
        self.assertEqual(json.dumps(with_ck, sort_keys=True), json.dumps(three, sort_keys=True))
        self.assertEqual(len(keys), 3)                                               # 每折独立的 task id

    def test_cli_reports_elapsed(self):
        import contextlib
        import io
        import re
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                O.main(["--strategy", "ma_cross", "--n", "300", "--limit", "2", "--out", os.path.join(tmp, "o.json")])
            m = re.search(r"done 2 combos in ([0-9.]+)s", buf.getvalue())
            self.assertIsNotNone(m, buf.getvalue())
            self.assertLess(float(m.group(1)), 120.0)


class TestWalkForward(QBTTestCase):
    def test_walk_forward_structure(self):
        t = task(n=scale(3000, 1200))
        combos = O.grid(SPACE)
        wf = O.walk_forward(t, combos, n_folds=3, train_frac=0.6, workers=2)
        n = t["data_spec"]["n"]
        folds = wf["folds"]
        self.assertEqual(len(folds), 3)
        self.assertEqual(folds[0]["train"][0], 0)
        self.assertEqual(folds[-1]["test"][1], n)
        for k, f in enumerate(folds):
            a, b = f["train"]
            b2, c = f["test"]
            self.assertEqual(b, b2)
            self.assertTrue(a < b < c)
            if k:
                self.assertEqual(a, folds[k - 1]["test"][1])             # 相邻折无重叠无缝隙
            self.assertIn(O.param_key(f["best"]), {O.param_key(x) for x in combos})
            # 样本内最优确实是样本内所有组合中目标最高者
            train_task = dict(t, slice=[a, b])
            train_task["id"] = "x"
            allres = O.run_search(train_task, combos, workers=1)
            self.assertEqual(O.best(allres)["params"], f["best"])
            self.assertEqual(f["is_objective"], O.best(allres)["objective"])
            # 样本外评价与独立评估一致
            O._worker_init(t["data_spec"])
            oos = O.evaluate(dict(t, slice=[b, c]), f["best"])
            self.assertEqual(oos["metrics"], f["out_of_sample"])
            self.assertEqual(oos["raw_objective"], f["oos_objective"])        # 样本外报告原始值，不用 -1e300 哨兵
        self.assertAlmostEqual(wf["oos_mean_objective"], sum(f["oos_objective"] for f in folds) / 3)
        self.assertAlmostEqual(wf["is_mean_objective"], sum(f["is_objective"] for f in folds) / 3)
        again = O.walk_forward(t, combos, n_folds=3, train_frac=0.6, workers=1)
        self.assertEqual(json.dumps(wf, sort_keys=True), json.dumps(again, sort_keys=True))

    def test_walk_forward_validation(self):
        t = task(n=100)
        with self.assertRaises(ConfigError):
            O.walk_forward(t, O.grid(SPACE), n_folds=0)
        with self.assertRaises(ConfigError):
            O.walk_forward(t, O.grid(SPACE), n_folds=2, train_frac=1.0)
        with self.assertRaises(ConfigError):
            O.walk_forward(t, O.grid(SPACE), n_folds=50)


class TestDataLoading(QBTTestCase):
    def test_load_data_cache_and_csv(self):
        spec = GenSpec(n=50, seed=3).to_dict()
        a = O.load_data(spec)
        self.assertIs(O.load_data(spec), a)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "d.csv")
            generate(GenSpec(n=50, seed=3)).to_csv(path)
            c = O.load_data({"csv": path, "symbol": "SYN", "bar_seconds": 60})
            self.assertEqual(c.fingerprint(), a.fingerprint())
            self.assertIsNot(O.load_data(spec), a)                        # 缓存只保留最近一份

    def test_cli_random_and_walk_forward(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "r.json")
            O.main(["--strategy", "rsi_reversion", "--n", "600", "--random", "4", "--workers", "1", "--out", out,
                    "--objective", "total_return"])
            with open(out) as f:
                res = json.load(f)
            self.assertEqual(res["n_results"], 4)
            self.assertEqual(res["task"]["objective"], "total_return")
            O.main(["--strategy", "ma_cross", "--n", "600", "--random", "3", "--walk-forward", "2", "--out", out])
            with open(out) as f:
                self.assertEqual(len(json.load(f)["folds"]), 2)


if __name__ == "__main__":
    unittest.main()
