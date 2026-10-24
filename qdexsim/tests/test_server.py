"""真 socket 的服务端测试：HTTP 分发、管理口、混沌在线上的表现、重启恢复、行情线程、命令行入口。"""
import json
import os
import sqlite3
import subprocess
import sys
import time
import unittest
import urllib.request

from qdexsim import adapters, common
from qdexsim.adapters import ExchangeError, RateLimited, UnknownResult
from qdexsim.server import LISTEN_LINE, Simulator
from qdexsim.tests._util import REPO, tmpdir


def http(url, method="GET", body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


class TestHttp(unittest.TestCase):
    def setUp(self):
        self.sim = Simulator(feed_cfg={"mode": "fixed"}, run_feed=False).start()
        self.addCleanup(self.sim.stop)

    def test_admin_endpoints(self):
        u = self.sim.url
        st, body = http(u + "/__admin/state")
        self.assertEqual(st, 200)
        self.assertEqual(body["account"]["wallet"], 10000.0)
        self.assertIn("chaos", body)
        st, t = http(u + "/__admin/tick", "POST", {"n": 2})
        self.assertEqual(t["mark"]["BTCUSDT"], 60000.0)                              # fixed feed
        st, t = http(u + "/__admin/tick", "POST", {"symbol": "BTCUSDT", "price": 61000})
        self.assertEqual(t["mark"]["BTCUSDT"], 61000.0)
        st, sym = http(u + "/__admin/symbol", "POST", {"symbol": "ETHUSDT", "tick": 0.5})
        self.assertEqual(sym["tick"], 0.5)
        st, ev = http(u + "/__admin/events?since=0")
        self.assertEqual(ev[-1]["kind"], "symbol_change")
        st, ev2 = http(u + "/__admin/events?since=0&kind=chaos")
        self.assertEqual(ev2, [])
        st, c = http(u + "/__chaos")
        self.assertEqual(c["config"]["rules"], [])
        st, c = http(u + "/__chaos", "POST", {"add": {"kind": "latency", "ms": 1}, "clock_drift_ms": 7})
        self.assertEqual((len(c["config"]["rules"]), c["drift_ms"]), (1, 7))
        st, c = http(u + "/__chaos", "POST", {"add": {"kind": "bogus"}})
        self.assertEqual(st, 400)
        st, c = http(u + "/__chaos", "DELETE")
        self.assertEqual((c["config"]["rules"], c["drift_ms"]), ([], 0))
        self.assertEqual(http(u + "/__chaos", "PUT", {"rules": []})[0], 200)
        self.assertEqual(http(u + "/__nope")[0], 404)
        self.assertEqual(http(u + "/elsewhere")[0], 404)

    def test_all_dialects_over_sockets(self):
        for name in adapters.ADAPTERS:
            ad = adapters.make(name, self.sim.url, timeout_s=3)
            o = ad.place("BTCUSDT", "BUY", "LIMIT", 0.01, 59000.0, cid=f"s-{name}")
            self.assertEqual(ad.query("BTCUSDT", cid=f"s-{name}")["id"], o["id"])
            self.assertEqual(ad.cancel("BTCUSDT", order_id=o["id"])["status"], "CANCELED")
        self.assertEqual(self.sim.requests, 15)

    def test_unknown_result_modes(self):
        ad = adapters.make("binance", self.sim.url, timeout_s=0.3)
        for mode in ("timeout", "drop", "502"):
            self.sim.chaos.update({"rules": [{"kind": "unknown_result", "op": "place", "max": 1, "mode": mode, "hold_ms": 600}]})
            before = len(self.sim.engine.all_orders())
            t0 = time.perf_counter()
            with self.assertRaises(UnknownResult) as cm:
                ad.place("BTCUSDT", "BUY", "LIMIT", 0.01, 59000.0, cid=f"u-{mode}")
            self.assertEqual(len(self.sim.engine.all_orders()), before + 1, mode)           # 服务端真的执行了
            self.assertIsNotNone(ad.query("BTCUSDT", cid=f"u-{mode}"))
            if mode == "502":
                self.assertEqual(cm.exception.http_status, 502)
            if mode == "timeout":
                self.assertGreaterEqual(time.perf_counter() - t0, 0.25)
            if mode == "drop":
                self.assertLess(time.perf_counter() - t0, 0.25)                               # 立即断连，不是等客户端超时
            ev = self.sim.store.events(kind="chaos")
            self.assertIn("response withheld", ev[-1]["executed"])
            self.assertEqual(ev[-1]["injection"], "unknown_result")

    def test_rate_limit_server_error_maintenance(self):
        u = self.sim.url
        self.sim.chaos.update({"rules": [{"kind": "rate_limit", "op": "place", "max": 1, "retry_after_ms": 2000}]})
        ad = adapters.make("binance", u, timeout_s=3)
        with self.assertRaises(RateLimited) as cm:
            ad.place("BTCUSDT", "BUY", "LIMIT", 0.01, 59000.0)
        self.assertEqual(cm.exception.retry_after_ms, 2000)
        self.assertEqual(len(self.sim.engine.all_orders()), 0)                              # 没执行
        ad.server_time()                                                                     # 窗口内再打 → 违规
        self.assertEqual(self.sim.chaos.status()["violations"][0]["dialect"], "binance")
        for name, code_key in (("bybit", "retCode"), ("okx", "code"), ("bitget", "code"), ("bitunix", "code")):
            self.sim.chaos.update({"rules": [{"kind": "rate_limit", "op": "time", "max": 1, "retry_after_ms": 1000}]})
            a = adapters.make(name, u, timeout_s=3)
            with self.assertRaises(RateLimited) as cm:
                a.server_time()
            self.assertGreater(cm.exception.retry_after_ms, 0)
        self.sim.chaos.update({"rules": [{"kind": "server_error", "op": "time", "max": 1}, {"kind": "maintenance", "op": "time", "max": 1}]})
        with self.assertRaises(ExchangeError) as cm:
            ad.server_time()
        self.assertEqual((cm.exception.kind, cm.exception.http_status), ("SERVER_ERROR", 500))
        with self.assertRaises(ExchangeError) as cm:
            ad.server_time()
        self.assertEqual((cm.exception.kind, cm.exception.http_status), ("MAINTENANCE", 503))
        self.sim.chaos.update({"rules": [{"kind": "server_error", "op": "place", "max": 1, "status": 503}]})
        with self.assertRaises(UnknownResult):                                              # 写操作上的 5xx = 结果未知
            ad.place("BTCUSDT", "BUY", "LIMIT", 0.01, 59000.0)
        self.sim.chaos.update({"rules": [{"kind": "latency", "op": "account", "ms": 1}]})
        with self.assertRaises(ExchangeError):
            adapters.make("binance", u, creds={"api_key": "nope"}, timeout_s=3).account()
        self.assertIn("auth failed", self.sim.store.events(kind="chaos")[-1]["executed"])  # 鉴权失败也要留注入日志

    def test_view_chaos_stale_zero_delay_race(self):
        ad = adapters.make("binance", self.sim.url, timeout_s=3)
        self.sim.chaos.update({"rules": [{"kind": "stale", "op": "positions", "ms": 400}, {"kind": "zero_once", "op": "account", "max": 1},
                                         {"kind": "fill_delay", "ms": 400}]})
        self.assertEqual(ad.account()["wallet"], 0.0)                                       # 余额 0 一次
        self.assertEqual(ad.account()["wallet"], 10000.0)
        ad.place("BTCUSDT", "BUY", "LIMIT", 0.01, 59000.0)
        self.sim.set_price("BTCUSDT", 58990.0)
        self.assertEqual(self.sim.engine.position("BTCUSDT")["qty"], 0.01)
        self.assertEqual(ad.positions().get("BTCUSDT", 0.0), 0.0)                            # 旧值
        self.assertEqual(ad.fills("BTCUSDT"), [])                                            # 成交还没出现
        time.sleep(0.45)
        self.assertEqual(ad.positions()["BTCUSDT"], 0.01)
        self.assertEqual(len(ad.fills("BTCUSDT")), 1)
        self.sim.chaos.update({"rules": [{"kind": "cancel_race", "op": "cancel", "max": 1}]})
        o = ad.place("BTCUSDT", "SELL", "LIMIT", 0.01, 61000.0)
        with self.assertRaises(ExchangeError) as cm:
            ad.cancel("BTCUSDT", order_id=o["id"])
        self.assertEqual(cm.exception.kind, "ORDER_NOT_OPEN")
        self.assertEqual(ad.query("BTCUSDT", order_id=o["id"])["status"], "FILLED")
        self.assertEqual(self.sim.engine.position("BTCUSDT")["qty"], 0.0)

    def test_scheduled_symbol_and_clock_drift(self):
        ad = adapters.make("okx", self.sim.url, timeout_s=3)
        self.sim.chaos.update({"rules": [{"kind": "symbol", "at_req": 2, "symbol": "BTCUSDT", "changes": {"tick": 1.0}},
                                         {"kind": "clock_drift", "at_req": 3, "ms": 45000}]})
        ad.server_time()
        ad.server_time()                                                                     # 第 2 个请求：精度变了
        self.assertEqual(self.sim.engine.symbols["BTCUSDT"]["tick"], 1.0)
        t1 = ad.server_time()                                                                # 第 3 个：时钟漂移
        self.assertGreater(t1 - common.now_ms(), 40000)
        with self.assertRaises(ExchangeError) as cm:
            ad.positions()
        self.assertEqual(cm.exception.kind, "BAD_TIMESTAMP")
        ad.sync_time()
        self.assertIsInstance(ad.positions(), dict)
        ev = self.sim.store.events(kind="chaos")
        self.assertEqual([e["injection"] for e in ev], ["symbol", "clock_drift"])

    def test_latency(self):
        ad = adapters.make("bitget", self.sim.url, timeout_s=3)
        self.sim.chaos.update({"rules": [{"kind": "latency", "op": "time", "ms": 120}]})
        t0 = time.perf_counter()
        ad.server_time()
        self.assertGreaterEqual(time.perf_counter() - t0, 0.1)


class TestLifecycle(unittest.TestCase):
    def test_restart_keeps_state_and_port(self):
        d = tmpdir(self)
        db = os.path.join(d, "s.db")
        sim = Simulator(db, run_feed=False).start()
        ad = adapters.make("bybit", sim.url, timeout_s=3)
        ad.place("BTCUSDT", "BUY", "LIMIT", 0.01, 59000.0, cid="keep")
        ad.place("BTCUSDT", "BUY", "MARKET", 0.02)
        port = sim.port
        sim.stop()
        sim2 = Simulator(db, port, run_feed=False).start()
        self.addCleanup(sim2.stop)
        self.assertEqual(sim2.port, port)
        with self.assertRaises(sqlite3.ProgrammingError):
            sim.store.conn.execute("SELECT 1")                                               # stop() 关掉了库连接
        self.assertEqual(ad.query("BTCUSDT", cid="keep")["status"], "NEW")
        self.assertEqual(ad.positions()["BTCUSDT"], 0.02)
        self.assertEqual(len(ad.fills("BTCUSDT")), 1)
        with self.assertRaises(ExchangeError) as cm:
            ad.place("BTCUSDT", "SELL", "LIMIT", 0.01, 61000.0, cid="keep")
        self.assertEqual(cm.exception.kind, "DUP_CID")

    def test_feed_thread_moves_prices(self):
        sim = Simulator(feed_cfg={"mode": "random_walk", "seed": 5, "interval_ms": 5, "vol": 0.01}, run_feed=True).start()
        try:
            ad = adapters.make("binance", sim.url, timeout_s=3)
            ad.place("BTCUSDT", "BUY", "LIMIT", 0.01, 59990.0)
            deadline = time.time() + 10
            while time.time() < deadline and (not sim.candles.get("BTCUSDT") or sim.feed.n < 5):   # 机器忙时行情线程慢，等条件而不是等时间
                time.sleep(0.02)
            self.assertTrue(sim.candles.get("BTCUSDT"))
            self.assertGreaterEqual(sim.feed.n, 5)
            self.assertNotEqual(ad.ticker("BTCUSDT"), 60000.0)
        finally:
            sim.stop()

    def test_positions_before(self):
        sim = Simulator(run_feed=False)
        self.assertIsNone(sim.positions_before(common.now_ms() + 10))     # 窗口内没变化 → 没有旧值
        self.assertEqual(sim.positions_before(0), [])                      # 都比截止点新 → 最早那份（空持仓）
        time.sleep(0.02)
        sim.engine.place("BTCUSDT", "BUY", "MARKET", 0.01)
        sim.record_positions()
        self.assertEqual(sim.positions_before(sim.history[0][0]), [])      # 变化在截止点之后 → 变化前的快照
        self.assertIsNone(sim.positions_before(common.now_ms() + 10))
        sim.store.close()


class TestMain(unittest.TestCase):
    def test_cli_prints_listen_line_and_serves(self):
        d = tmpdir(self)
        cfg = os.path.join(d, "cfg.json")
        common.write_json(cfg, {"engine": {"wallet": 5000}, "feed": {"mode": "fixed"}, "creds": {"binance": {"api_key": "K", "secret": "S"}}})
        chaos = os.path.join(d, "chaos.json")
        common.write_json(chaos, {"seed": 1, "rules": [{"kind": "latency", "ms": 1}]})
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.Popen([sys.executable, "-m", "qdexsim.server", "--db", os.path.join(d, "x.db"), "--config", cfg, "--chaos", chaos,
                                 "--chaos-log", os.path.join(d, "chaos.jsonl")], cwd=REPO, env=env, stdout=subprocess.PIPE, text=True)
        try:
            line = proc.stdout.readline().strip()
            self.assertTrue(line.startswith(LISTEN_LINE + "http://127.0.0.1:"), line)
            url = line[len(LISTEN_LINE):]
            ad = adapters.make("binance", url, creds={"api_key": "K", "secret": "S"}, timeout_s=3)
            self.assertEqual(ad.account()["wallet"], 5000.0)
            with self.assertRaises(ExchangeError):
                adapters.make("binance", url, timeout_s=3).account()                     # 缺省凭据不再有效
            self.assertEqual(http(url + "/__chaos")[1]["config"]["rules"][0]["kind"], "latency")
            with open(os.path.join(d, "chaos.jsonl"), encoding="utf-8") as f:
                self.assertEqual(json.loads(f.readline())["injection"], "latency")
        finally:
            proc.terminate()
            self.assertEqual(proc.wait(timeout=10), 0)                                      # SIGTERM → sys.exit(0)，atexit 能跑


if __name__ == "__main__":
    unittest.main()
