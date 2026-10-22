"""profile_gate 判据：目标解析、函数名规范化（去行号 / 临时路径、两份可比）、回归闸的容差与判定、CLI 退出码与首行格式、
真进程测量能抓到故意放慢。"""
import copy
import json
import os
import re
import shutil
import unittest

from qdperf import profile_gate as G
from qdperf.tests._util import TIMING, captured, tmpdir

DEMO = "qdperf.examples.demo_work:spin"
CALLS = []


def count_calls(n=0):
    """测试用目标：记下每次调用（worker 在本进程内运行时可数调用次数）。"""
    CALLS.append(n)
REG_LINE = re.compile(r"^REGRESSION (hot\.wall_s|cold\.wall_s|memory\.peak_bytes)=[\d.e+-]+→[\d.e+-]+ \(\+[\d.]+%\)$")
OK_LINE = re.compile(r"^OK hot\.wall_s=[\d.e+-]+→[\d.e+-]+ \([+-][\d.]+%\) tol=[\d.]+%$")
PROFILE_LINE = re.compile(r"^PROFILE target=\S+ hot_wall_s=[\d.e+-]+ cold_wall_s=([\d.e+-]+|-) peak_bytes=\d+ runs=\d+ "
                          r"noise_hot=([\d.]+%|-)$")


def doc(hot=1.0, cold=2.0, peak=1000, noise=None, top=("a:f", "a:g"), target="m:f", args=None):
    d = {"format": G.FORMAT, "target": target, "args": args or {"n": 1}, "hot": {"wall_s": hot},
         "memory": {"peak_bytes": peak}, "profile": {"top": [{"name": n, "cumtime": 1.0 / (k + 1)}
                                                              for k, n in enumerate(top)]}, "noise": noise}
    if cold is not None:
        d["cold"] = {"wall_s": cold}
    return d


class TestResolve(unittest.TestCase):
    def test_resolve(self):
        from qdperf.examples import demo_work
        self.assertIs(G.resolve(DEMO), demo_work.spin)
        self.assertEqual(G.resolve("qdperf.examples.demo_work:Worker.run")(n=4), 0 + 1 + 4 + 9)
        for bad in ("nocolon", "a:b:c", ":f", "m:", 3, None):
            with self.assertRaises(G.GateError):
                G.resolve(bad)
        for bad in ("qdperf.no_such_module:f", "qdperf.examples.demo_work:nope", "qdperf.examples.demo_work:NOT_CALLABLE"):
            with self.assertRaises(G.GateError):
                G.resolve(bad)


class TestNames(unittest.TestCase):
    def test_qualnames(self):
        p = os.path.join(tmpdir(self), "q.py")
        with open(p, "w") as f:
            f.write("import functools\n\n\ndef top():\n    def inner():\n        pass\n    return inner\n\n\n"
                    "class C:\n    @staticmethod\n    @functools.cache\n    def m():\n        pass\n\n"
                    "    class D:\n        async def n(self):\n            pass\n")
        q = G.qualnames(p)
        self.assertEqual(q[4], "top")
        self.assertEqual(q[5], "top.<locals>.inner")
        self.assertEqual(q[11], "C.m")
        self.assertEqual(q[12], "C.m")
        self.assertEqual(q[13], "C.m")
        self.assertEqual(q[17], "C.D.n")
        self.assertNotIn(10, q)
        self.assertIs(G.qualnames(p), q)                                  # 缓存
        bad = os.path.join(tmpdir(self), "bad.py")
        with open(bad, "w") as f:
            f.write("def (:\n")
        self.assertEqual(G.qualnames(bad), {})
        self.assertEqual(G.qualnames(os.path.join(tmpdir(self), "missing.py")), {})

    def test_func_name(self):
        import json as js
        roots = G.path_roots()
        self.assertEqual(G.func_name(("~", 0, "<built-in method builtins.len>"), roots),
                         "<builtin>:<built-in method builtins.len>")
        self.assertEqual(G.func_name(("<string>", 1, "<module>"), roots), "<builtin>:<module>")
        self.assertEqual(G.func_name((js.__file__, js.dumps.__code__.co_firstlineno, "dumps"), roots),
                         "<stdlib>/json/__init__.py:dumps")
        from qdperf.examples import demo_work
        self.assertEqual(G.func_name((demo_work.__file__, demo_work.Worker.run.__code__.co_firstlineno, "run"), roots),
                         "qdperf/examples/demo_work.py:Worker.run")
        self.assertEqual(G.func_name((demo_work.__file__, 99999, "zzz"), roots), "qdperf/examples/demo_work.py:zzz")
        self.assertEqual(G.func_name(("/nonexistent/dir/x.py", 3, "f"), roots), "x.py:f")
        self.assertEqual(roots[0][0], max((r[0] for r in roots), key=len))
        self.assertIn("<stdlib>/", [t for _r, t in roots])

    def test_top_functions_merge_and_order(self):
        import cProfile
        from qdperf.examples import demo_work
        prof = cProfile.Profile()
        prof.enable()
        demo_work.spin(n=20000, burn=5000)
        fs = [lambda: 1,                                                   # 同名（<lambda>）同文件、不同行 → 合并
              lambda: 2]
        for f in fs * 3:
            f()
        prof.disable()
        top, total = G.top_functions(prof, 50)
        names = [t["name"] for t in top]
        self.assertIn("qdperf/examples/demo_work.py:spin", names)
        self.assertIn("qdperf/examples/demo_work.py:extra", names)
        inner = next(t for t in top if t["name"] == "qdperf/examples/demo_work.py:_inner")
        self.assertEqual(inner["ncalls"], 100)
        lam = [t for t in top if t["name"].endswith("<lambda>")]
        self.assertEqual(len(lam), 1)
        self.assertEqual(lam[0]["ncalls"], 6)
        self.assertEqual([t["cumtime"] for t in top], sorted((t["cumtime"] for t in top), reverse=True))
        self.assertFalse(any("_lsprof" in n for n in names))
        self.assertEqual(total, len(names))
        self.assertLessEqual(len(G.top_functions(prof, 2)[0]), 2)
        self.assertFalse(any(re.search(r":\d+\(", n) for n in names))

    def test_top_defaults_and_rounding(self):
        import cProfile
        ns = {}
        exec("\n".join(f"def f{i}(): return sum(range({i} * 50))" for i in range(40)), ns)
        prof = cProfile.Profile()
        prof.enable()
        for i in range(40):
            ns[f"f{i}"]()
        prof.disable()
        top, total = G.top_functions(prof)
        self.assertGreaterEqual(total, 40)
        self.assertEqual(len(top), 30)                                         # 缺省前 30（契约）
        for t in top:                                                          # 秒，6 位小数（JSON 可读、可 diff）
            self.assertEqual((t["tottime"], t["cumtime"]), (round(t["tottime"], 6), round(t["cumtime"], 6)))


class TestCompare(unittest.TestCase):
    def test_ok_and_first_line(self):
        r = G.compare(doc(1.0), doc(1.05))
        self.assertEqual(r["verdict"], "OK")
        self.assertEqual(r["lines"][0], "OK hot.wall_s=1→1.05 (+5.0%) tol=10.0%")
        self.assertRegex(r["lines"][0], OK_LINE)
        self.assertEqual([m["name"] for m in r["metrics"]], ["hot.wall_s", "cold.wall_s", "memory.peak_bytes"])
        self.assertEqual(r["new_top"], [])

    def test_red_hot_and_worst(self):
        r = G.compare(doc(1.0, 2.0, 1000), doc(1.12, 2.5, 1100))
        self.assertEqual(r["verdict"], "REGRESSION")
        self.assertEqual(r["lines"][0], "REGRESSION cold.wall_s=2→2.5 (+25.0%)")      # 超出容差最多的那个
        self.assertRegex(r["lines"][0], REG_LINE)
        self.assertEqual([m["red"] for m in r["metrics"]], [True, True, False])
        self.assertTrue(r["lines"][1].startswith("RED hot.wall_s"))
        self.assertTrue(r["lines"][3].startswith("ok  memory.peak_bytes 1000→1100 +10.0%"))

    def test_boundaries(self):
        self.assertEqual(G.compare(doc(1.0), doc(1.1), gate=("hot",))["verdict"], "OK")       # 恰好等于容差不算红
        self.assertEqual(G.compare(doc(1.0), doc(1.1001), gate=("hot",))["verdict"], "REGRESSION")
        self.assertEqual(G.compare(doc(peak=1000), doc(peak=1200), gate=("mem",))["verdict"], "OK")
        r = G.compare(doc(peak=1000), doc(peak=1201), gate=("mem",))
        self.assertEqual(r["lines"][0], "REGRESSION memory.peak_bytes=1000→1201 (+20.1%)")
        self.assertEqual(G.compare(doc(1.0), doc(0.5))["verdict"], "OK")
        self.assertEqual(G.compare(doc(0.0), doc(0.0), gate=("hot",))["verdict"], "OK")
        self.assertEqual(G.compare(doc(0.0), doc(0.1), gate=("hot",))["lines"][0], "REGRESSION hot.wall_s=0→0.1 (+inf%)")

    def test_noise_widens_tolerance(self):
        noise = {"hot_wall_rel": 0.08, "cold_wall_rel": 0.01, "peak_rel": 0.15}
        r = G.compare(doc(1.0, noise=noise), doc(1.15))
        hot = r["metrics"][0]
        self.assertAlmostEqual(hot["tolerance"], 0.16)
        self.assertAlmostEqual(hot["noise"], 0.08)
        self.assertEqual(r["verdict"], "OK")
        self.assertEqual(r["lines"][1], "ok  hot.wall_s 1→1.15 +15.0% tol=16.0% (manual 10.0%, 2×noise 16.0%)")
        self.assertAlmostEqual(r["metrics"][1]["tolerance"], 0.10)
        self.assertAlmostEqual(r["metrics"][2]["tolerance"], 0.30)
        self.assertEqual(G.compare(doc(1.0, noise=noise), doc(1.17))["verdict"], "REGRESSION")
        r0 = G.compare(doc(1.0, noise=noise), doc(1.05), wall_tol=0.0)          # 纯噪声容差
        self.assertAlmostEqual(r0["metrics"][0]["tolerance"], 0.16)
        r1 = G.compare(doc(1.0, noise={"hot_wall_rel": 0.01}), doc(1.05), wall_tol=0.0, gate=("hot",))
        self.assertEqual(r1["verdict"], "REGRESSION")
        self.assertEqual(G.compare(doc(1.0, noise={"hot_wall_rel": None}), doc(1.05), gate=("hot",))["verdict"], "OK")

    def test_new_top_named(self):
        base = doc(top=["a", "b", "c"])
        new = doc(top=["a", "x", "b", "y", "c"])
        r = G.compare(base, new, top_new=3)
        self.assertEqual([t["name"] for t in r["new_top"]], ["x"])
        self.assertEqual(r["lines"][-1], "NEW_TOP3 x cum=0.5s")
        self.assertEqual([t["name"] for t in G.compare(base, new, top_new=10)["new_top"]], ["x", "y"])
        self.assertEqual(G.function_names(new), {"a", "x", "b", "y", "c"})
        names = [f"f{i}" for i in range(12)]
        late = doc(top=names[:9] + ["n10", "n11"] + names[9:])                # n10 排第 10、n11 排第 11
        self.assertEqual([t["name"] for t in G.compare(doc(top=names), late)["new_top"]], ["n10"])   # 缺省只看前 10

    def test_errors_and_missing(self):
        with self.assertRaises(G.GateError):
            G.compare(doc(target="a:b"), doc(target="a:c"))
        with self.assertRaises(G.GateError):
            G.compare(doc(args={"n": 1}), doc(args={"n": 2}))
        bad = doc()
        bad["format"] = "x"
        with self.assertRaises(G.GateError):
            G.compare(bad, doc())
        with self.assertRaises(G.GateError):
            G.compare(doc(), [])
        with self.assertRaises(G.GateError):
            G.compare(doc(), doc(), wall_tol=-0.1)
        with self.assertRaises(G.GateError):
            G.compare(doc(), doc(), mem_tol=-0.1)
        with self.assertRaises(G.GateError):
            G.compare(doc(), doc(), gate=("cpu",))
        r = G.compare(doc(cold=None), doc(cold=None))
        self.assertEqual([m["metric"] for m in r["metrics"]], ["hot", "mem"])
        with self.assertRaises(G.GateError):
            G.compare(doc(cold=None), doc(), gate=("cold",))


class TestMeasure(unittest.TestCase):
    def test_measure_structure_and_names(self):
        d = G.measure(DEMO, {"n": 50_000, "alloc_kb": 256}, repeat=2)
        self.assertEqual((d["format"], d["target"], d["repeat"], d["runs"]), (G.FORMAT, DEMO, 2, 1))
        self.assertEqual(len(d["hot"]["wall_samples"]), 2)
        self.assertEqual((d["hot"]["processes"], d["inner"]), (2, 1))
        self.assertEqual(d["hot"]["wall_s"], min(d["hot"]["wall_samples"]))
        self.assertEqual(d["cold"]["wall_s"], min(d["cold"]["wall_samples"]))
        self.assertEqual(len(d["cold"]["process_wall_samples"]), 2)
        self.assertGreaterEqual(d["memory"]["peak_bytes"], 256 * 1024)
        self.assertLess(d["memory"]["peak_bytes"], 256 * 1024 * 3)
        names = G.function_names(d)
        self.assertIn("qdperf/examples/demo_work.py:spin", names)
        self.assertIn("qdperf/examples/demo_work.py:_inner", names)
        self.assertEqual(d["profile"]["top"][0]["name"], "qdperf/examples/demo_work.py:spin")
        self.assertIsNone(d["noise"])
        nd = G.measure(DEMO, {"n": 50_000}, repeat=1, cold=False, top=3)
        self.assertNotIn("cold", nd)
        self.assertEqual(len(nd["profile"]["top"]), 3)
        with self.assertRaises(G.GateError):
            G.measure(DEMO, {}, repeat=0)
        with self.assertRaises(G.GateError):
            G.measure("qdperf.examples.demo_work:boom", {}, repeat=1, cold=False)

    def test_names_comparable_across_temp_copies(self):
        """同一个模块放在两个不同的临时目录里各测一遍：函数名集合完全相同，且不含临时路径与行号。"""
        from qdperf.examples import demo_work
        sets = []
        for _ in range(2):
            d = tmpdir(self)
            shutil.copyfile(demo_work.__file__, os.path.join(d, "tmpwork.py"))
            doc_ = G.measure("tmpwork:spin", {"n": 20_000}, repeat=1, cold=False, paths=[d])
            names = G.function_names(doc_)
            self.assertFalse(any(d in n or "/tmp" in n for n in names), names)
            self.assertIn("tmpwork.py:_inner", names)
            sets.append(names)
        self.assertEqual(sets[0], sets[1])

    def test_baseline_noise_and_catch_slowdown(self):
        base = G.baseline(DEMO, {"n": 200_000}, repeat=3, runs=3)
        self.assertEqual(base["runs"], 3)
        per = base["noise"]["per_run"]
        self.assertEqual(len(per), 3)
        hot = sorted(p["hot_wall_s"] for p in per)
        self.assertEqual(base["hot"]["wall_s"], hot[1])
        self.assertAlmostEqual(base["noise"]["hot_wall_rel"], (hot[2] - hot[0]) / hot[0])
        self.assertIn("cold_wall_rel", base["noise"])
        self.assertEqual(base["cold"]["wall_s"], sorted(p["cold_wall_s"] for p in per)[1])
        with self.assertRaises(G.GateError):
            G.baseline(DEMO, {}, runs=0)
        same = G.measure(DEMO, {"n": 200_000}, repeat=3)
        self.assertEqual(G.function_names(same), G.function_names(base))
        slow_args = {"n": 200_000, "burn": 0}
        slow = G.measure(DEMO, dict(slow_args, burn=3_000_000), repeat=3)
        slow["args"] = base["args"]                         # 放慢版本加了参数；闸只看测量值
        r = G.compare(base, slow)
        self.assertEqual(r["verdict"], "REGRESSION")
        self.assertRegex(r["lines"][0], REG_LINE)
        self.assertIn("qdperf/examples/demo_work.py:extra", [t["name"] for t in r["new_top"]])
        if TIMING:
            self.assertEqual(G.compare(base, same, wall_tol=0.25, mem_tol=0.25)["verdict"], "OK")


class TestCli(unittest.TestCase):
    def test_run_baseline_compare(self):
        d = tmpdir(self)
        base, new = os.path.join(d, "b.json"), os.path.join(d, "n.json")
        with captured() as (o, _e):
            self.assertEqual(G.main(["baseline", DEMO, "--args", '{"n": 30000}', "--repeat", "2", "--runs", "2",
                                     "--out", base]), 0)
        self.assertRegex(o.getvalue().splitlines()[0], PROFILE_LINE)
        self.assertIn("runs=2", o.getvalue().splitlines()[0])
        self.assertRegex(o.getvalue(), r"累计s +自身s +调用 +函数")                  # 首行之后是函数表（人读）
        self.assertIn("qdperf/examples/demo_work.py:spin", o.getvalue())
        with captured() as (o, _e):
            self.assertEqual(G.main(["run", DEMO, "--args", '{"n": 30000}', "--repeat", "2", "--no-cold", "--out", new]), 0)
        self.assertIn("cold_wall_s=- ", o.getvalue())
        self.assertIn(" runs=1 ", o.getvalue().splitlines()[0])               # run 缺省只跑 1 遍（baseline / pair 缺省 3）
        with captured() as (o, _e):
            rc = G.main(["compare", base, new, "--wall-tol", "1000", "--gate", "hot,mem", "--json",
                         os.path.join(d, "c.json")])                    # 只验退出码与首行格式：容差放到无穷大
        self.assertEqual(rc, 0)
        self.assertRegex(o.getvalue().splitlines()[0], OK_LINE)
        self.assertTrue(os.path.exists(os.path.join(d, "c.json")))
        with open(new) as f:
            slow = json.load(f)
        slow["hot"]["wall_s"] *= 100                                     # 远超任何噪声撑开的容差
        slow_path = os.path.join(d, "s.json")
        with open(slow_path, "w") as f:
            json.dump(slow, f)
        with captured() as (o, _e):
            self.assertEqual(G.main(["compare", base, slow_path]), 1)
        self.assertRegex(o.getvalue().splitlines()[0], REG_LINE)
        other = copy.deepcopy(slow)
        other["target"] = "x:y"
        other_path = os.path.join(d, "o.json")
        with open(other_path, "w") as f:
            json.dump(other, f)
        with captured() as (o, _e):
            self.assertEqual(G.main(["compare", base, other_path]), 2)
        self.assertTrue(o.getvalue().startswith("PROFILE_GATE ERROR GateError"))
        with captured() as (o, _e):
            self.assertEqual(G.main(["compare", base, os.path.join(d, "missing.json")]), 2)
        with captured() as (o, _e):
            self.assertEqual(G.main(["run", DEMO, "--args", "[1]", "--out", new]), 2)
        with captured() as (o, _e):
            self.assertEqual(G.main(["run", "qdperf.examples.demo_work:boom", "--repeat", "1", "--out", new]), 2)
        self.assertIn("RuntimeError", o.getvalue())
        with captured() as (o, _e):
            self.assertEqual(G.main(["run", DEMO, "--args", "{bad", "--out", new]), 2)

    def test_cli_pair(self):
        d = tmpdir(self)
        a, b = os.path.join(d, "a.json"), os.path.join(d, "b.json")
        with captured() as (o, _e):
            self.assertEqual(G.main(["pair", DEMO, "--args", '{"n": 20000}', "--runs", "1", "--repeat", "1",
                                     "--env-b", "QDPERF_DEMO_BURN=200000", "--out", a, "--out-b", b]), 0)
        lines = o.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("PROFILE_B "))                      # 首行是 B（本次），见 CONTRACT
        self.assertRegex(lines[0].replace("PROFILE_B ", "PROFILE ", 1), PROFILE_LINE)
        self.assertRegex(lines[1], PROFILE_LINE)
        with open(a) as f:
            da = json.load(f)
        with open(b) as f:
            db = json.load(f)
        self.assertTrue("cold" in da and "cold" in db)                          # pair 两边都量冷启动
        self.assertIn("qdperf/examples/demo_work.py:extra", G.function_names(db))
        self.assertNotIn("qdperf/examples/demo_work.py:extra", G.function_names(da))

    def test_worker_entry(self):
        d = tmpdir(self)
        out = os.path.join(d, "w.json")
        self.assertEqual(G.main(["_worker", "sample", DEMO, '{"n": 1000}', "3", "5", out]), 0)
        with open(out) as f:
            res = json.load(f)
        self.assertEqual(set(res), {"cold_wall", "cold_cpu", "hot_wall", "hot_cpu", "hot_inner_samples"})
        self.assertEqual(len(res["hot_inner_samples"]), 3)
        self.assertEqual(res["hot_wall"], min(res["hot_inner_samples"]))
        self.assertGreater(res["cold_wall"], 0)
        self.assertEqual(G.main(["_worker", "deep", DEMO, '{"n": 1000}', "1", "4", out]), 0)
        with open(out) as f:
            res = json.load(f)
        self.assertEqual(set(res), {"peak_bytes", "current_bytes", "top", "functions"})
        self.assertLessEqual(len(res["top"]), 4)
        with self.assertRaises(G.GateError):
            G.worker("cold", DEMO, {}, 1, 5)

    def test_worker_call_count(self):
        del CALLS[:]
        res = G.worker("sample", "qdperf.tests.test_profile_gate:count_calls", {"n": 1}, 3, 5)
        self.assertEqual(len(CALLS), 1 + 3)                                     # 冷样本 = 导入 + 第一次调用；热样本 3 次
        self.assertEqual(len(res["hot_inner_samples"]), 3)

    def test_runs_defaults(self):
        from unittest import mock
        with mock.patch.object(G, "measure", side_effect=lambda *a, **k: "m"), \
                mock.patch.object(G, "aggregate", side_effect=list):
            self.assertEqual(len(G.baseline(DEMO)), 3)                         # 缺省同机连跑 3 遍（噪声口径）
            self.assertEqual(len(G.baseline(DEMO, runs=1)), 1)
            with self.assertRaises(G.GateError):
                G.baseline(DEMO, runs=0)
        with mock.patch.object(G, "interleaved", side_effect=lambda arms, *a, **k: list(range(len(arms)))), \
                mock.patch.object(G, "aggregate", side_effect=list):
            self.assertEqual(G.pair(DEMO), ([0, 2, 4], [1, 3, 5]))             # 缺省各 3 遍，A B 交替
            self.assertEqual(G.pair(DEMO, runs=1), ([0], [1]))

    def test_aggregate_and_pair(self):
        def m(hot, cold, peak):
            return {"format": G.FORMAT, "target": "t:f", "args": {}, "hot": {"wall_s": hot, "cpu_s": hot},
                    "cold": {"wall_s": cold, "cpu_s": cold}, "memory": {"peak_bytes": peak},
                    "profile": {"top": [{"name": f"x{hot}", "cumtime": hot}]}, "runs": 1, "noise": None}
        agg = G.aggregate([m(1.0, 2.0, 100), m(1.2, 2.4, 110), m(1.1, 2.1, 90)])
        self.assertEqual((agg["runs"], agg["hot"]["wall_s"], agg["cold"]["wall_s"], agg["memory"]["peak_bytes"]),
                         (3, 1.1, 2.1, 100))
        self.assertAlmostEqual(agg["noise"]["hot_wall_rel"], 0.2)
        self.assertAlmostEqual(agg["noise"]["cold_wall_rel"], 0.2)
        self.assertAlmostEqual(agg["noise"]["peak_rel"], 20 / 90)
        self.assertEqual(agg["profile"]["top"][0]["name"], "x1.1")                 # 剖析取热墙钟居中的那一遍
        nocold = m(1.0, 2.0, 100)
        del nocold["cold"]
        self.assertNotIn("cold_wall_rel", G.aggregate([nocold, m(1.1, 2.0, 100)])["noise"])
        with self.assertRaises(G.GateError):
            G.aggregate([])
        with self.assertRaises(G.GateError):
            G.pair(DEMO, {}, runs=0)
        with self.assertRaises(G.GateError):
            G.interleaved([], repeat=1)
        with self.assertRaises(G.GateError):
            G.interleaved([{"target": DEMO, "args": {}}], repeat=0)
        arm = {"target": DEMO, "args": {"n": 20_000}}
        docs = G.interleaved([arm, dict(arm, env={"QDPERF_DEMO_SLOW": "3.0"}), arm], repeat=2, cold=False, top=5)
        self.assertEqual(len(docs), 3)
        self.assertTrue(all("cold" not in d and d["hot"]["processes"] == 2 for d in docs))
        if TIMING:                                                             # 机器满载时（突变测试并行）不比时长
            self.assertGreater(docs[1]["hot"]["wall_s"], 2.0 * max(docs[0]["hot"]["wall_s"], docs[2]["hot"]["wall_s"]))
        self.assertIn("qdperf/examples/demo_work.py:spin_until", G.function_names(docs[1]))
        a, b = G.pair(DEMO, {"n": 20_000}, runs=2, repeat=2, env_b={"QDPERF_DEMO_BURN": "200000"})
        self.assertEqual((a["runs"], b["runs"]), (2, 2))
        self.assertEqual(len(a["noise"]["per_run"]), 2)
        if TIMING:
            self.assertGreater(b["hot"]["wall_s"], a["hot"]["wall_s"])
        self.assertIn("qdperf/examples/demo_work.py:extra", G.function_names(b))
        self.assertNotIn("qdperf/examples/demo_work.py:extra", G.function_names(a))


if __name__ == "__main__":
    unittest.main()
