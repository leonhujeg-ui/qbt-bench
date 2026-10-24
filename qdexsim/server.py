"""仿真交易所进程：一个 http.server 同时服务五种方言 + qdexsim 自己的管理口，只监听 127.0.0.1。

    python3.12 -m qdexsim.server --port 0 --db /tmp/sim.db [--config sim.json] [--chaos chaos.json]
    → 第一行 stdout：QDEXSIM LISTENING http://127.0.0.1:<port>

路径按前缀分流：/fapi → binance，/v5 → bybit，/api/v5 → okx，/api/v2 → bitget，/api/v1/futures → bitunix。
管理口（不是任何交易所的接口，判据与场景驱动用）：
    GET  /__admin/state                 engine.snapshot()：订单 / 成交 / 持仓 / 账户 / 标记价 / 服务器时间（真相）
    GET  /__admin/events?since=<seq>    事件日志（订单事件 + chaos 注入事件）
    POST /__admin/tick {"n": k} | {"symbol", "price"} 推进行情 k 个 tick / 直接设价
    POST /__admin/symbol {"symbol", ...changes}       下架 / 暂停 / 改精度
    GET/POST /__chaos                   查看 / 修改混沌配置（见 chaos.py）
请求处理顺序：chaos.before → 延迟 → 拒绝类注入（限频 / 5xx / 维护）→ 鉴权 → 执行 → 结果未知注入（已执行、不回包）。
"""
import argparse
import http.server
import json
import os
import signal
import sys
import threading
import time

from qdexsim import chaos as chaos_mod, common, dialects, engine as engine_mod, feed as feed_mod, store as store_mod
from qdexsim.dialects import base

LISTEN_LINE = "QDEXSIM LISTENING "
GATEWAY_502 = b"<html><head><title>502 Bad Gateway</title></head><body><center><h1>502 Bad Gateway</h1></center></body></html>"


class Context:
    """一次请求的上下文：engine、带漂移的时间、受混沌影响的视图。"""

    def __init__(self, sim, dialect_name, decision):
        self.sim, self.engine, self.candles = sim, sim.engine, sim.candles
        self.dialect_name, self.decision = dialect_name, decision

    def now(self):
        return self.sim.now()

    def positions_view(self):
        stale = self.decision.stale_ms
        if stale:
            old = self.sim.positions_before(common.now_ms() - stale)
            if old is not None:
                return old
        return self.engine.all_positions()

    def account_view(self):
        a = self.engine.account()
        if self.decision.zero_once:
            return {k: 0.0 for k in a}
        return a

    def fills_view(self, symbol=None):
        return self.engine.user_trades(symbol, visible_before=common.now_ms())

    def cancel(self, order_id=None, cid=None):
        if self.decision.cancel_race:
            o = self.engine.get_order(order_id=order_id, cid=cid)
            self.engine.fill_remaining(o["id"])
            self.sim.record_positions()
        return self.engine.cancel(order_id=order_id, cid=cid)


class Simulator:
    def __init__(self, db_path=":memory:", port=0, config=None, feed_cfg=None, chaos_cfg=None, creds=None, chaos_log=None,
                 run_feed=True):
        config = dict(config or {})
        self.store = store_mod.Store(db_path)
        self.chaos = chaos_mod.Chaos(chaos_cfg or {}, store=self.store, log_path=chaos_log)
        self.engine = engine_mod.Engine(self.store, clock=self.now, config=config.get("engine"), symbols=config.get("symbols"))
        ticks = {s: v["tick"] for s, v in self.engine.symbols.items()}
        self.feed = feed_mod.Feed(dict(feed_cfg or {}, start=dict(self.engine.mark, **(feed_cfg or {}).get("start", {}))), ticks)
        self.engine.mark.update(self.feed.prices)
        self.candles = feed_mod.Candles()
        self.dialects = dialects.make_all(creds)
        self.lock = threading.RLock()
        self.history = []                       # [(真实 ms, 持仓视图)]，给 stale 混沌用
        self.record_positions()
        self.port, self.run_feed = port, run_feed
        self.httpd = self.thread = self.feed_thread = None
        self.stopping = threading.Event()
        self.requests = 0

    # ---- 时钟 / 行情 ----
    def now(self):
        return common.now_ms() + self.chaos.drift_ms

    def record_positions(self):
        with self.lock:
            self.history.append((common.now_ms(), self.engine.all_positions()))
            del self.history[:-500]

    def positions_before(self, real_ms):
        """“旧值”：最近一次变化若发生在 real_ms 之后，返回 real_ms 之前最新的快照（都更新则返回最早那份）；
        最新快照本身就 ≤ real_ms（窗口内没变化）→ None。"""
        with self.lock:
            if self.history[-1][0] <= real_ms:
                return None
            older = [h for h in self.history if h[0] <= real_ms]
            return (older[-1] if older else self.history[0])[1]

    def tick(self, n=1):
        """推进行情 n 个 tick（行情线程每 interval_ms 调一次；测试与场景脚本也可直接调）。"""
        fills = []
        for _ in range(n):
            delay = self.chaos.fill_delay_ms()
            for sym, px, liq in self.feed.step():
                if sym not in self.engine.symbols:
                    continue
                got = self.engine.on_price(sym, px, self.now(), liq, visible_delay_ms=delay)
                self.candles.add(sym, px, self.now(), sum(f["qty"] for f in got))
                self._after_fills(got, delay)
                fills += got
        return fills

    def set_price(self, symbol, price):
        self.feed.set_price(symbol, price)
        delay = self.chaos.fill_delay_ms()
        got = self.engine.on_price(symbol, self.feed.prices[symbol], self.now(), None, visible_delay_ms=delay)
        self.candles.add(symbol, self.feed.prices[symbol], self.now(), sum(f["qty"] for f in got))
        self._after_fills(got, delay)
        return got

    def _after_fills(self, got, delay):
        if not got:
            return
        self.record_positions()
        if delay:                                   # 被动注入也要有事件日志：哪些成交被推迟了多久才可见
            self.chaos.log_scheduled({"kind": "fill_delay", "ms": delay},
                                     f"fills {[f['id'] for f in got]} hidden from fills endpoint for {delay} ms", self.now())

    def _feed_loop(self):
        while not self.stopping.wait(self.feed.interval_ms / 1000.0):
            try:
                self.tick()
            except Exception as e:                      # pragma: no cover  行情线程不能死
                sys.stderr.write(f"feed error: {e!r}\n")

    # ---- 分发 ----
    def dialect_for(self, path):
        for d in self.dialects.values():
            if d.owns(path):
                return d
        return None

    def handle(self, req):
        """→ (Response | None, decision)；None = 不回包（断连）。线程安全（engine 自己有锁）。"""
        self.requests += 1
        if req.path.startswith("/__"):
            return self.admin(req), None
        d = self.dialect_for(req.path)
        if d is None:
            return base.Response(404, {"error": "unknown path", "path": req.path}), None
        route = d.route(req) or (None, False)
        op, need_auth = route
        decision = self.chaos.before(op or "unknown", d.NAME)
        for rule in self.chaos.scheduled():
            self._run_scheduled(rule)
        if decision.delay_ms:
            time.sleep(decision.delay_ms / 1000.0)
        if decision.reject is not None:
            rj = decision.reject
            resp = d.error(rj["kind"], "", Context(self, d.NAME, decision), retry_after_ms=rj["retry_after_ms"])
            if rj.get("status"):
                resp.status = int(rj["status"])
            if rj["kind"] == "RATE_LIMIT":
                self.chaos.note_rate_limited(d.NAME, rj["retry_after_ms"])
            self.chaos.log(op, d.NAME, decision, f"rejected with {rj['kind']} before execution", self.now())
            return resp, decision
        if op is None:
            return d.error("NOT_FOUND", req.path, Context(self, d.NAME, decision)), decision
        ctx = Context(self, d.NAME, decision)
        if need_auth:
            err = d.auth(req, ctx)
            if err is not None:
                self.chaos.log(op, d.NAME, decision, f"auth failed ({err.body}); nothing executed", self.now())
                return err, decision
        resp = d.handle(op, req, ctx)
        if op in ("place", "cancel", "leverage", "margin_mode"):
            self.record_positions()
        self.chaos.log(op, d.NAME, decision, self._executed(op, resp, decision), self.now())
        if decision.unknown is not None:
            return None if decision.unknown["mode"] != "502" else base.Response(502, None, {"X-Qdexsim-Unknown": "502"}), decision
        return resp, decision

    @staticmethod
    def _executed(op, resp, decision):
        body = json.dumps(resp.body, ensure_ascii=False)[:200] if resp.body is not None else ""
        tail = f"; response withheld ({decision.unknown['mode']})" if decision.unknown else ""
        return f"{op} executed, HTTP {resp.status}, body {body}{tail}"

    def _run_scheduled(self, rule):
        if rule["kind"] == "symbol":
            spec = self.engine.set_symbol(rule["symbol"], **rule.get("changes", {}))
            self.chaos.log_scheduled(rule, f"symbol {rule['symbol']} now {json.dumps(spec)}", self.now())
        elif rule["kind"] == "clock_drift":
            self.chaos.log_scheduled(rule, f"server clock drift set to {rule['ms']} ms", self.now())

    # ---- 管理口 ----
    def admin(self, req):
        p = req.path
        if p == "/__admin/state" and req.method == "GET":
            snap = self.engine.snapshot()
            snap["chaos"] = self.chaos.status()
            snap["real_time"] = common.now_ms()
            return base.Response(200, snap)
        if p == "/__admin/events" and req.method == "GET":
            since = int(req.get("since", 0))
            return base.Response(200, self.store.events(since, kind=req.get("kind")))
        if p == "/__admin/tick" and req.method == "POST":
            if req.get("symbol") is not None:
                fills = self.set_price(req.get("symbol"), float(req.get("price")))
            else:
                fills = self.tick(int(req.get("n", 1)))
            return base.Response(200, {"fills": fills, "mark": self.engine.mark})
        if p == "/__admin/symbol" and req.method == "POST":
            changes = {k: v for k, v in req.params.items() if k != "symbol"}
            return base.Response(200, self.engine.set_symbol(req.get("symbol"), **changes))
        if p == "/__chaos":
            if req.method == "GET":
                return base.Response(200, self.chaos.status())
            if req.method in ("POST", "PUT"):
                try:
                    return base.Response(200, self.chaos.update(req.json or {}))
                except (ValueError, KeyError, TypeError) as e:
                    return base.Response(400, {"error": str(e)})
            if req.method == "DELETE":
                return base.Response(200, self.chaos.update({"rules": [], "clock_drift_ms": 0}))
        return base.Response(404, {"error": "unknown admin path", "path": p})

    # ---- 生命周期 ----
    def start(self):
        sim = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):            # 静音
                pass

            def _serve(self):
                path, _, query = self.path.partition("?")
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                req = base.Request(self.command, path, query, dict(self.headers.items()), body)
                resp, decision = sim.handle(req)
                if resp is None:                    # 结果未知：握住或直接断
                    if decision is not None and decision.unknown and decision.unknown["mode"] == "timeout":
                        sim.stopping.wait(decision.unknown["hold_ms"] / 1000.0)
                    self.close_connection = True
                    return
                data = GATEWAY_502 if resp.status == 502 and resp.body is None else resp.bytes()
                self.send_response(resp.status)
                self.send_header("Content-Type", "text/html" if resp.status == 502 and resp.body is None else "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in resp.headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_DELETE = do_PUT = _serve

        self.httpd = http.server.ThreadingHTTPServer((common.LOOPBACK, self.port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        if self.run_feed:
            self.feed_thread = threading.Thread(target=self._feed_loop, daemon=True)
            self.feed_thread.start()
        return self

    @property
    def url(self):
        return f"http://{common.LOOPBACK}:{self.port}"

    def stop(self):
        self.stopping.set()
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self.feed_thread is not None:
            self.feed_thread.join(timeout=5)
        self.store.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description="qdexsim 仿真交易所")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--db", default=":memory:")
    ap.add_argument("--config", default=None, help='JSON：{"engine": {...}, "symbols": {...}, "feed": {...}, "creds": {...}}')
    ap.add_argument("--chaos", default=None, help="混沌场景 JSON（见 chaos.py）")
    ap.add_argument("--chaos-log", default=None, help="注入事件 JSONL 追加写到这个文件")
    args = ap.parse_args(argv)
    common.child_init()
    signal.signal(signal.SIGTERM, lambda *_a: sys.exit(0))          # terminate() 也走 atexit（子进程覆盖率要写出）
    cfg = common.read_json(args.config) if args.config else {}
    chaos_cfg = chaos_mod.load(args.chaos) if args.chaos else cfg.get("chaos")
    sim = Simulator(args.db, args.port, config=cfg, feed_cfg=cfg.get("feed"), chaos_cfg=chaos_cfg, creds=cfg.get("creds"),
                    chaos_log=args.chaos_log).start()
    print(LISTEN_LINE + sim.url, flush=True)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:                        # pragma: no cover
        pass
    finally:
        sim.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
