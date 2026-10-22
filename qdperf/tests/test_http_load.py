"""http_load / fake_backend 判据：清单校验、只打本机、分层延迟表的分位精确、错误率、并发上限、拐点、A/B 判定、CLI。"""
import http.client
import json
import os
import re
import statistics
import threading
import time
import unittest

from qdperf import fake_backend as B
from qdperf import http_load as H
from qdperf.tests._util import TIMING, captured, scale, tmpdir

MANIFEST = {"format": H.FORMAT, "targets": [{"name": "fast", "path": "/fast", "weight": 3},
                                            {"name": "post", "method": "POST", "path": "/post", "body": {"x": 1}}]}


def cfg(routes, **kw):
    return dict({"format": B.FORMAT, "seed": 1, "routes": routes}, **kw)


class TestManifest(unittest.TestCase):
    def test_defaults(self):
        m = H.load_manifest({"format": H.FORMAT, "targets": [{"path": "/a"}]})
        self.assertEqual(m["targets"][0], {"path": "/a", "method": "GET", "name": "/a", "weight": 1, "headers": {},
                                           "expect": None})

    def test_errors(self):
        bad = [{"format": "x"}, {"format": H.FORMAT, "targets": []}, {"format": H.FORMAT, "targets": [1]},
               {"format": H.FORMAT, "targets": [{"path": "a"}]},
               {"format": H.FORMAT, "targets": [{"path": "/a", "method": "FETCH"}]},
               {"format": H.FORMAT, "targets": [{"path": "/a"}, {"path": "/a"}]},
               {"format": H.FORMAT, "targets": [{"path": "/a", "weight": 0}]},
               {"format": H.FORMAT, "targets": [{"path": "/a", "weight": True}]},
               {"format": H.FORMAT, "targets": [{"path": "/a", "weight": float("inf")}]},
               {"format": H.FORMAT, "targets": [{"path": "/a", "headers": []}]},
               {"format": H.FORMAT, "targets": [{"path": "/a", "expect": [200, "x"]}]},
               {"format": H.FORMAT, "targets": [{"path": "/a", "expect": 200}]}]
        for m in bad:
            with self.assertRaises(H.LoadError, msg=m):
                H.load_manifest(m)
        with self.assertRaises(H.LoadError):
            H.load_manifest(os.path.join(tmpdir(self), "nope.json"))

    def test_loopback_only(self):
        self.assertEqual(H.parse_base("http://127.0.0.1:8080/api/"), ("127.0.0.1", 8080, "/api"))
        self.assertEqual(H.parse_base("http://localhost"), ("localhost", 80, ""))
        for bad in ("http://example.com:80", "https://127.0.0.1:1", "ftp://x", "127.0.0.1:80", "http://10.0.0.1"):
            with self.assertRaises(H.LoadError):
                H.parse_base(bad)
        self.assertEqual(H.parse_base("http://example.com:81", allow_remote=True), ("example.com", 81, ""))

    def test_pick_and_encode(self):
        ts = [{"name": "a", "weight": 1}, {"name": "b", "weight": 3}]
        cum = H.cumulative(ts)
        self.assertEqual(cum, [0.25, 1.0])
        self.assertEqual([H.pick(ts, cum, r)["name"] for r in (0.0, 0.2499, 0.25, 0.999, 1.0)], ["a", "a", "b", "b", "b"])
        three = [{"name": n, "weight": 1} for n in "xyz"]
        self.assertEqual(H.pick(three, H.cumulative(three), 1.0)["name"], "z")      # 累计权重的浮点尾差：兜底取最后一个
        body, headers = H._encode({"body": {"x": 1}, "headers": {"X": "1"}})
        self.assertEqual((body, headers), (b'{"x": 1}', {"X": "1", "Content-Type": "application/json"}))
        self.assertEqual(H._encode({"body": "raw", "headers": {}}), (b"raw", {}))
        self.assertEqual(H._encode({"body": None, "headers": {}}), (None, {}))
        self.assertEqual(H.doubling(32), [1, 2, 4, 8, 16, 32])
        self.assertEqual(H.doubling(12), [1, 2, 4, 8, 12])
        self.assertEqual(H.doubling(1), [1])


class TestBackendLogic(unittest.TestCase):
    def test_quantiles(self):
        self.assertAlmostEqual(B.quantile({"kind": "fixed", "ms": 50}, 0.3), 0.05)
        self.assertAlmostEqual(B.quantile({"kind": "uniform", "lo_ms": 10, "hi_ms": 30}, 0.25), 0.015)
        ln = {"kind": "lognormal", "p50_ms": 30, "p99_ms": 120}
        self.assertAlmostEqual(B.quantile(ln, 0.5), 0.030)
        self.assertAlmostEqual(B.quantile(ln, 0.99), 0.120)
        with self.assertRaises(B.ConfigError):
            B.quantile({"kind": "pareto"}, 0.5)

    def test_stratified_schedule_exact_quantiles(self):
        """每连续 1000 个请求的延迟恰好是目标分布的 1000 个分层点：p99 与注入值只差分层离散化。"""
        r = B.Route("/x", B.load_config(cfg({"/x": {"latency": {"kind": "lognormal", "p50_ms": 30, "p99_ms": 120},
                                                    "error_rate": 0.05}}))["routes"]["/x"], seed=1)
        draws = [r.draw() for _ in range(2000)]
        lat = sorted(d[0] for d in draws[:1000])
        self.assertAlmostEqual(lat[989] * 1000, B.quantile(r.spec["latency"], 0.9895) * 1000)
        self.assertLess(abs(statistics.quantiles(lat, n=100)[98] * 1000 - 120.0), 2.0)
        self.assertEqual(sum(d[1] for d in draws[:1000]), 50)
        self.assertEqual(sum(d[1] for d in draws[1000:]), 50)
        self.assertEqual([d[2] for d in draws[:3]], [0, 1, 2])
        self.assertEqual(sorted(d[0] for d in draws[:1000]), sorted(d[0] for d in draws[1000:]))
        r2 = B.Route("/x", dict(r.spec, schedule="random"), seed=1)
        self.assertNotEqual([r2.draw()[0] for _ in range(5)], [d[0] for d in draws[:5]])

    def test_config_validation(self):
        c = B.load_config(cfg({"/a": {}}))
        self.assertEqual(c["routes"]["/a"]["latency"], {"kind": "fixed", "ms": 0})
        self.assertIn("*", c["routes"])
        self.assertEqual(c["routes"]["*"]["latency"], {"kind": "fixed", "ms": 0})   # 没配 default：未匹配路径零延迟
        self.assertEqual((c["seed"], c["max_concurrency"]), (1, 0))               # 缺省：不限全局并发
        sub_ms = B.load_config(cfg({"/a": {"latency": {"kind": "lognormal", "p50_ms": 0.5, "p99_ms": 2}}}))
        self.assertEqual(sub_ms["routes"]["/a"]["latency"]["p50_ms"], 0.5)       # 亚毫秒的中位数合法（只要求 > 0）
        self.assertIsNone(B.Backend(cfg({"/a": {}})).sem)
        self.assertEqual((c["routes"]["/a"]["error_rate"], c["routes"]["/a"]["max_concurrency"],
                          c["routes"]["/a"]["body_bytes"], c["routes"]["/a"]["schedule"]), (0.0, 0, 64, "stratified"))
        bad = [{"format": "x", "routes": {}}, cfg({"a": {}}), cfg([]), cfg({"/a": 3}),
               cfg({"/a": {"latency": {"kind": "fixed"}}}), cfg({"/a": {"latency": {"kind": "fixed", "ms": -1}}}),
               cfg({"/a": {"latency": {"kind": "fixed", "ms": True}}}),
               cfg({"/a": {"latency": {"kind": "uniform", "lo_ms": 5, "hi_ms": 1}}}),
               cfg({"/a": {"latency": {"kind": "lognormal", "p50_ms": 50, "p99_ms": 10}}}),
               cfg({"/a": {"latency": {"kind": "lognormal", "p50_ms": 0, "p99_ms": 10}}}),
               cfg({"/a": {"latency": "fast"}}), cfg({"/a": {"error_rate": 1.5}}), cfg({"/a": {"error_rate": True}}),
               cfg({"/a": {"max_concurrency": -1}}), cfg({"/a": {"max_concurrency": 2.5}}),
               cfg({"/a": {"latency": {"kind": "fixed", "ms": 1_000_000}}}),                     # 上界 1e6 ms 不含
               cfg({"/a": {"error_rate": -0.1}}),                                                  # 错误率不能为负
               cfg({"/a": {"body_bytes": -1}}), cfg({"/a": {"body_bytes": "x"}}), cfg({"/a": {"body_bytes": [1]}}),
               cfg({"/a": {"body_bytes": True}}), cfg({"/a": {"schedule": "zigzag"}})]
        for x in bad:
            with self.assertRaises(B.ConfigError, msg=x):
                B.load_config(x)

    def test_route_matching(self):
        b = B.Backend(cfg({"/api": {}, "/api/orders": {}}))
        self.assertEqual(b.route("/api/orders?x=1").path, "/api/orders")
        self.assertEqual(b.route("/api/orders/5").path, "/api/orders")
        self.assertEqual(b.route("/api/users").path, "/api")
        self.assertEqual(b.route("/apix").path, "*")
        self.assertEqual(b.route("/").path, "*")


class TestServer(unittest.TestCase):
    def test_in_process_server(self):
        srv, url = B.serve(cfg({"/fast": {"latency": {"kind": "fixed", "ms": 1}, "body_bytes": 300},
                                "/err": {"error_rate": 1.0}}))
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        host, port, _p = H.parse_base(url)
        c = http.client.HTTPConnection(host, port, timeout=5)
        for method, path, body in (("GET", "/fast", None), ("POST", "/fast", b'{"a":1}'), ("DELETE", "/err", None)):
            c.request(method, path, body=body)
            r = c.getresponse()
            data = r.read()
            if path == "/err":
                self.assertEqual(r.status, 500)
                self.assertEqual(json.loads(data), {"ok": False, "route": "/err", "n": 0})     # 错误响应体也是约定的一部分
            else:
                self.assertEqual(r.status, 200)
                self.assertEqual(len(data), 300)
                self.assertEqual(json.loads(data)["method"], method)
        c.request("GET", "/__stats")
        stats = json.loads(c.getresponse().read())
        self.assertEqual(stats["/fast"], 2)
        self.assertEqual(stats["/err"], 1)
        c.request("GET", "/__health")
        self.assertEqual(c.getresponse().status, 200)
        c.close()

    def test_body_padding_bounds(self):
        """body_bytes：能补就补到恰好这么多；差不到 9 字节（pad 字段的最小开销）就不补，绝不比要求的长。"""
        natural = len(json.dumps({"ok": True, "route": "/e", "n": 0, "method": "GET"}).encode())
        srv, url = B.serve(cfg({"/e": {"body_bytes": natural}, "/s": {"body_bytes": natural + 4},
                                "/p": {"body_bytes": natural + 9}, "/q": {"body_bytes": natural + 40}}))
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        host, port, _p = H.parse_base(url)
        c = http.client.HTTPConnection(host, port, timeout=5)
        got = []
        for path in ("/e", "/s", "/p", "/q"):
            c.request("GET", path)
            data = c.getresponse().read()
            json.loads(data)                                                 # 补过的也仍是合法 JSON
            got.append(len(data))
        c.close()
        self.assertEqual(got, [natural, natural, natural + 9, natural + 40])

    def test_cli_requires_config(self):
        with captured() as (_o, e), self.assertRaises(SystemExit) as cm:
            B.main([])
        self.assertEqual(cm.exception.code, 2)                               # argparse：缺必填的 --config
        self.assertIn("required", e.getvalue())

    def test_concurrency_limit_serializes(self):
        srv, url = B.serve(cfg({"/one": {"latency": {"kind": "fixed", "ms": 60}, "max_concurrency": 1}}))
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        host, port, _p = H.parse_base(url)
        done = []

        def hit():
            c = http.client.HTTPConnection(host, port, timeout=5)
            c.request("GET", "/one")
            c.getresponse().read()
            done.append(time.perf_counter())
            c.close()

        t0 = time.perf_counter()
        ths = [threading.Thread(target=hit) for _ in range(3)]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        self.assertGreaterEqual(max(done) - t0, 0.18)                  # 3 × 60 ms 串行

    def test_subprocess_backend_start_failure(self):
        """子进程起不来（配置非法）→ start 立即抛错并带上子进程的 stderr，不能一直等。"""
        box = {}

        def go():
            try:
                B.start({"format": "x"}, timeout=20.0)
            except RuntimeError as exc:
                box["err"] = str(exc)

        t = threading.Thread(target=go, daemon=True)
        t.start()
        t.join(30)
        self.assertIn("fake backend failed to start", box.get("err", "(still waiting)"))
        self.assertIn("config.format must be", box.get("err", ""))                # 带上子进程报的原因

    def test_start_failure_carries_stderr(self):
        """子进程起不来时，异常里带上它的 stdout 与 stderr（崩溃的 Traceback 在 stderr 上）。"""
        import subprocess
        import sys
        from unittest import mock
        real = subprocess.Popen
        crash = [sys.executable, "-c", "import sys; sys.stderr.write('boom on stderr'); sys.exit(3)"]
        with mock.patch.object(B.subprocess, "Popen", side_effect=lambda args, **kw: real(crash, **kw)):
            with self.assertRaises(RuntimeError) as cm:
                B.start(cfg({"/a": {}}), timeout=20.0)
        self.assertIn("boom on stderr", str(cm.exception))

    def test_stop_escalation(self):
        """停不下来的子进程：SIGTERM → 最多等 10 s → SIGKILL → 再等（回收，避免僵尸、returncode 有值）。"""
        import subprocess
        calls = []

        class Stubborn:
            stdout = stderr = None

            def terminate(self):
                calls.append("term")

            def kill(self):
                calls.append("kill")

            def wait(self, timeout):
                calls.append(("wait", timeout))
                if "kill" not in calls:
                    raise subprocess.TimeoutExpired("backend", timeout)

        B.stop(Stubborn())
        self.assertEqual(calls, ["term", ("wait", 10), "kill", ("wait", 10)])

    def test_stop_is_graceful_and_child_flushes(self):
        """stop：先 SIGTERM（子进程把它转成 KeyboardInterrupt，走 finally 关服务器、写出覆盖率），秒级退出、退出码 0。"""
        import time
        from unittest import mock
        d = tmpdir(self)
        with mock.patch.dict(os.environ, {"QDPERF_COV_DIR": d}):
            proc, _url = B.start(cfg({"/a": {}}))
            t0 = time.monotonic()
            B.stop(proc)
        self.assertEqual(proc.returncode, 0)                                 # 不是被 SIGKILL（-9）
        self.assertLess(time.monotonic() - t0, 5.0)
        self.assertTrue(any(n.endswith(".json") for n in os.listdir(d)))      # 子进程的 child_finish 写出了覆盖率

    def test_subprocess_backend_and_main_errors(self):
        proc, url = B.start(cfg({"/a": {}}))
        try:
            self.assertTrue(url.startswith("http://127.0.0.1:"))
            host, port, _p = H.parse_base(url)
            self.assertGreaterEqual(port, 1024)                              # --port 缺省 0：由系统分配临时端口
            c = http.client.HTTPConnection(host, port, timeout=5)
            c.request("GET", "/a")
            self.assertEqual(c.getresponse().status, 200)
            c.close()
        finally:
            B.stop(proc)
        self.assertIsNotNone(proc.returncode)
        self.assertTrue(proc.stdout.closed and proc.stderr.closed)             # stop 收尾：管道关掉、临时目录删掉
        self.assertFalse(os.path.exists(proc.qdperf_work))
        p = os.path.join(tmpdir(self), "bad.json")
        with open(p, "w") as f:
            json.dump({"format": "x"}, f)
        with captured() as (o, _e):
            self.assertEqual(B.main(["--config", p]), 2)
        self.assertTrue(o.getvalue().startswith("FAKE_BACKEND ERROR"))
        good = os.path.join(tmpdir(self), "good.json")
        with open(good, "w") as f:
            json.dump(cfg({"/a": {}}), f)
        ready = os.path.join(tmpdir(self), "ready")
        with captured() as (o, _e):
            self.assertEqual(B.main(["--config", good, "--seconds", "0.2", "--ready-file", ready]), 0)
        self.assertRegex(o.getvalue(), r"^FAKE_BACKEND LISTENING http://127\.0\.0\.1:\d+")
        with open(ready) as f:
            self.assertTrue(f.read().startswith("http://127.0.0.1:"))


class TestLoad(unittest.TestCase):
    def test_step_stats_and_errors(self):
        srv, url = B.serve(cfg({"/fast": {"latency": {"kind": "fixed", "ms": 2}},
                                "/post": {"latency": {"kind": "fixed", "ms": 2}, "error_rate": 0.5}}))
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        s = H.run_step(url, MANIFEST, c=2, seconds=0.6, warmup=0.1)
        tot = s["total"]
        self.assertEqual(tot["requests"], s["per_target"]["fast"]["requests"] + s["per_target"]["post"]["requests"])
        self.assertGreater(s["per_target"]["fast"]["requests"], s["per_target"]["post"]["requests"])
        self.assertEqual(s["per_target"]["fast"]["errors"], 0)
        self.assertTrue(0.1 < s["per_target"]["post"]["error_rate"] < 0.9)      # 分层错误表只在每 1000 个请求上精确
        self.assertEqual(s["per_target"]["post"]["statuses"].get("500", 0), s["per_target"]["post"]["errors"])
        self.assertAlmostEqual(tot["rps"], tot["requests"] / 0.6)
        self.assertGreaterEqual(s["per_target"]["fast"]["latency_ms"]["p50"], 2.0)
        m = dict(MANIFEST, targets=[dict(MANIFEST["targets"][1], expect=[500])])
        s2 = H.run_step(url, m, c=1, seconds=0.3, warmup=0.0)
        self.assertEqual(s2["per_target"]["post"]["ok"], s2["per_target"]["post"]["statuses"].get("500", 0))

    def test_latency_matches_injection(self):
        """固定 20 ms 的路径，客户端量到的 p50 应在 20 ms 附近（防 Nagle / 延迟 ACK 那种每个响应多 40 ms 的回归）。"""
        srv, url = B.serve(cfg({"/x": {"latency": {"kind": "fixed", "ms": 20}, "body_bytes": 2000}}))
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        s = H.run_step(url, {"format": H.FORMAT, "targets": [{"path": "/x"}, {"path": "/x", "name": "p", "method": "POST",
                                                                                "body": {"k": "v" * 100}}]},
                       c=2, seconds=0.6, warmup=0.1)
        for name in ("/x", "p"):
            lat = s["per_target"][name]["latency_ms"]
            self.assertGreaterEqual(lat["min"], 20.0)
            if TIMING:
                self.assertLess(lat["p50"], 30.0, (name, lat))

    def test_connection_errors_counted(self):
        srv, url = B.serve(cfg({}))
        srv.shutdown()
        srv.server_close()
        s = H.run_step(url, {"format": H.FORMAT, "targets": [{"path": "/x"}]}, c=1, seconds=0.2, warmup=0.0, timeout=1)
        self.assertGreater(s["total"]["requests"], 0)
        self.assertEqual(s["total"]["error_rate"], 1.0)
        self.assertEqual(s["total"]["statuses"], {"error": s["total"]["requests"]})
        self.assertIsNone(s["total"]["latency_ms"]["p99"])

    def test_ab_loopback_only_by_default(self):
        from unittest import mock
        seen = []

        def fake_step(base, m, c, seconds, warmup, seed, allow_remote=None):
            seen.append(allow_remote)
            raise H.LoadError("stop before any socket")

        with mock.patch.object(H, "run_step", side_effect=fake_step):
            with self.assertRaises(H.LoadError):
                H.ab("http://192.0.2.1:9", "http://192.0.2.2:9", MANIFEST)
            with self.assertRaises(H.LoadError):
                H.ramp("http://192.0.2.1:9", MANIFEST, steps=[1])
        self.assertEqual(seen, [False, False])                           # 缺省只许打本机（allow_remote 要显式打开）

    def test_cli_required_args(self):
        for argv in (["ramp", "--manifest", "missing.json"],
                     ["ab", "--b", "http://127.0.0.1:9", "--manifest", "missing.json"],
                     ["ab", "--a", "http://127.0.0.1:9", "--manifest", "missing.json"]):
            with captured() as (_o, e), self.assertRaises(SystemExit) as cm:
                H.main(argv)
            self.assertEqual(cm.exception.code, 2)                       # argparse 拒绝：缺必填参数（不走到读清单）
            self.assertIn("required", e.getvalue())

    def test_find_knee(self):
        def st(c, rps):
            return {"c": c, "total": {"ok_rps": rps}}
        self.assertEqual(H.find_knee([st(1, 10), st(2, 20), st(4, 40), st(8, 80), st(16, 81), st(32, 80)]), 8)
        self.assertEqual(H.find_knee([st(1, 10), st(2, 10.9), st(4, 40)]), 1)
        self.assertIsNone(H.find_knee([st(1, 10), st(2, 11), st(4, 40)]))              # 恰好 +10% 不算“不再增长”
        self.assertEqual(H.find_knee([st(1, 10), st(2, 11.0001)]), None)
        self.assertEqual(H.find_knee([st(1, 10), st(2, 11.0001)], growth=0.2), 1)
        self.assertIsNone(H.find_knee([st(1, 10), st(2, 20)]))
        self.assertIsNone(H.find_knee([st(1, 10)]))

    def test_judge(self):
        def run(**lat):
            return {"per_target": {k: {"latency_ms": {"p50": v[0], "p99": v[1]}} for k, v in lat.items()}}
        a1, a2 = run(x=(10, 100), y=(5, 50)), run(x=(10, 104), y=(5, 50))
        v = H.judge(a1, a2, run(x=(10.5, 110), y=(5, 50)), 0.10)
        self.assertEqual(v["verdict"], "OK")
        self.assertEqual(v["first_line"], "OK x.p99_ms=102.00→110.00 (+7.8%) tol=10.0%")
        v = H.judge(a1, a2, run(x=(13, 130), y=(5, 50)), 0.10)
        self.assertEqual(v["verdict"], "REGRESSION")
        self.assertEqual(v["first_line"], "REGRESSION x.p50_ms=10.00→13.00 (+30.0%)")
        self.assertRegex(v["first_line"], r"^REGRESSION \S+\.(p50|p99)_ms=[\d.]+→[\d.]+ \(\+[\d.]+%\)$")
        edge = H.judge(run(x=(10, 100)), run(x=(10, 100)), run(x=(10, 110)), 0.10)
        self.assertEqual(edge["verdict"], "OK")                                   # 变化恰好等于容差：不判红
        noisy = H.judge(run(x=(10, 100)), run(x=(10, 130)), run(x=(10, 140)), 0.10)
        row = next(r for r in noisy["rows"] if r["q"] == "p99")
        self.assertAlmostEqual(row["tolerance"], 0.6)
        self.assertEqual(noisy["verdict"], "OK")
        miss = H.judge(run(x=(None, None)), run(x=(10, 100)), run(x=(10, 100)), 0.1)
        self.assertEqual(miss["first_line"], "REGRESSION x.p99_ms=-→- (no successful requests)")    # 并列时取 p99
        self.assertEqual(H.judge(a1, a2, run(x=(10, 110.16), y=(5, 50)), 0.10)["verdict"], "OK")   # +8% 在 10% 容差内
        self.assertEqual(H.judge(a1, a2, run(x=(10, 110.16)), 0.10)["first_line"],
                         "REGRESSION y.p99_ms=-→- (no successful requests)")                     # B 缺路径 = 红
        with self.assertRaises(H.LoadError):
            H.judge(a1, a2, a1, -0.1)

    def test_ramp_ab_and_cli(self):
        srv, url = B.serve(cfg({"/fast": {"latency": {"kind": "fixed", "ms": 20}},
                                "/post": {"latency": {"kind": "fixed", "ms": 20}},
                                "/one": {"latency": {"kind": "fixed", "ms": 20}, "max_concurrency": 2}}))
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        one = {"format": H.FORMAT, "targets": [{"path": "/one"}]}
        r = H.ramp(url, one, steps=[1, 2, 4, 8], seconds=scale(1.0, 0.6), warmup=0.1)
        self.assertEqual([s["c"] for s in r["steps"]], [1, 2, 4, 8])
        if TIMING:
            self.assertEqual(r["knee"], 2)                               # 并发上限 2 → C=2 之后翻倍不再增长
        self.assertRegex(H.first_line_ramp(r), r"^HTTP_LOAD knee=(\d+|none) max_rps=[\d.]+ at_c=\d+ p99_ms=[\d.]+ "
                                               r"errors=[\d.]+$")
        with self.assertRaises(H.LoadError):
            H.ramp(url, MANIFEST, steps=[2, 1])
        with self.assertRaises(H.LoadError):
            H.ramp(url, MANIFEST, steps=[0, 1])
        slow, slow_url = B.serve(cfg({"/fast": {"latency": {"kind": "fixed", "ms": 40}},
                                      "/post": {"latency": {"kind": "fixed", "ms": 20}}}))
        self.addCleanup(slow.server_close)
        self.addCleanup(slow.shutdown)
        res = H.ab(url, slow_url, MANIFEST, c=2, seconds=0.6, warmup=0.1)
        self.assertEqual(res["verdict"], "REGRESSION")
        self.assertRegex(res["first_line"], r"^REGRESSION (fast|post)\.p(50|95|99)_ms=[\d.]+→[\d.]+ \(\+[\d.]+%\)$")
        p50 = next(r for r in res["rows"] if (r["target"], r["q"]) == ("fast", "p50"))
        self.assertTrue(p50["red"])                                       # 20 → 40 ms：中位数翻倍（不受偶发长尾左右）
        self.assertEqual(set(res["runs"]), {"a1", "b", "a2"})
        d = tmpdir(self)
        mpath = os.path.join(d, "m.json")
        with open(mpath, "w") as f:
            json.dump(MANIFEST, f)
        with captured() as (o, _e):
            rc = H.main(["ramp", "--base", url, "--manifest", mpath, "--steps", "1,2", "--seconds", "0.3",
                         "--json", os.path.join(d, "r.json")])
        self.assertEqual(rc, 0)
        self.assertTrue(o.getvalue().startswith("HTTP_LOAD knee="))
        with captured() as (o, _e):
            rc = H.main(["ab", "--a", url, "--b", slow_url, "--manifest", mpath, "--c", "2", "--seconds", "0.4"])
        self.assertEqual(rc, 1)
        self.assertRegex(o.getvalue().splitlines()[0], r"^REGRESSION ")
        with captured() as (o, _e):
            rc = H.main(["ab", "--a", url, "--b", url, "--manifest", mpath, "--c", "2", "--seconds", "0.4",
                         "--tol", "1000"])                                # A/A：只验退出码与首行格式（容差放到无穷大）
        self.assertEqual(rc, 0)
        self.assertTrue(o.getvalue().startswith("OK "))
        with captured() as (o, _e):
            self.assertEqual(H.main(["ramp", "--base", "http://example.com", "--manifest", mpath]), 2)
        self.assertIn("non-loopback", o.getvalue())
        with captured() as (o, _e):
            self.assertEqual(H.main(["ramp", "--base", url, "--manifest", os.path.join(d, "missing.json")]), 2)
        with captured() as (o, _e):
            self.assertEqual(H.main(["ramp", "--base", url, "--manifest", mpath, "--steps", "1,x"]), 2)

    def test_cli_error_rate_limit(self):
        """ramp 的退出码 1 = 某路径错误率超过 --max-error-rate（错误是注入的，与负载无关）。"""
        d = tmpdir(self)
        mpath = os.path.join(d, "m.json")
        with open(mpath, "w") as f:
            json.dump(MANIFEST, f)
        err_srv, err_url = B.serve(cfg({"/fast": {"error_rate": 1.0}, "/post": {}}))
        self.addCleanup(err_srv.server_close)
        self.addCleanup(err_srv.shutdown)
        with captured() as (o, _e):
            self.assertEqual(H.main(["ramp", "--base", err_url, "--manifest", mpath, "--steps", "1", "--seconds", "0.2"]),
                             1)
        with captured() as (o, _e):                                      # /fast 错误率恰好 100% = 上限 → 不算超限
            self.assertEqual(H.main(["ramp", "--base", err_url, "--manifest", mpath, "--steps", "1", "--seconds", "0.2",
                                     "--max-error-rate", "1.0"]), 0)


if __name__ == "__main__":
    unittest.main()
