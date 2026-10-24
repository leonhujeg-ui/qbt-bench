"""场景驾驭器：起仿真器 → 按脚本驱动被判客户端与行情 / 混沌 → 收服务端真相 → 跑判据。

场景文件（qdexsim/scenarios/*.json）::

    {"name": "binance/unknown_timeout", "dialect": "binance", "symbol": "BTCUSDT", "seed": 11, "timeout_s": 0.3,
     "chaos": {...chaos.py 的配置...}, "engine": {...可选 engine 配置...},
     "script": [{"op": "place", "intent": "i1", "side": "BUY", "type": "LIMIT", "qty": 0.01, "offset": -100},
                {"op": "price", "price": 59890}, {"op": "cancel", "intent": "i1"}, {"op": "sleep", "ms": 50},
                {"op": "restart_client"}, {"op": "restart_server"}, {"op": "kill_switch", "grace_ms": 500}, {"op": "balance"}, {"op": "positions"},
                {"op": "chaos", "patch": {...}}, {"op": "symbol", "symbol": "BTCUSDT", "changes": {...}}, {"op": "settle"}],
     "judges": ["no_duplicate", "no_lost", ...], "expect": {"naive": "red", "correct": "green"}}

价格 = price_base（缺省 60000）+ offset。行情不自己走（run_feed=False），全由脚本的 price / tick 步推动——同一场景同一种子
注入序列与成交序列都可复现。客户端抛出的异常被记录、不中断脚本（被判的是客户端，不是驾驭器）。
"""
import os
import shutil
import tempfile
import time
import traceback

from qdexsim import clients, common, judges
from qdexsim.server import Simulator

PRICE_BASE = 60000.0


class Run:
    def __init__(self, scn, client_cls, work):
        self.scn, self.client_cls, self.work = scn, client_cls, work
        self.db = os.path.join(work, "sim.db")
        self.sim = None
        self.client = None
        self.intents, self.errors, self.kill, self.clog = {}, [], None, []
        self.symbol = scn.get("symbol", "BTCUSDT")
        self.base = float(scn.get("price_base", PRICE_BASE))

    def start_sim(self, port=0):
        self.sim = Simulator(self.db, port, config={"engine": self.scn.get("engine")}, feed_cfg={"mode": "fixed"},
                             chaos_cfg=self.scn.get("chaos"), run_feed=False).start()
        return self.sim

    def make_client(self):
        self.client = self.client_cls(self.scn["dialect"], self.sim.url, os.path.join(self.work, "client"),
                                      timeout_s=float(self.scn.get("timeout_s", 0.3)), log=self.clog)

    # ---- 步 ----
    def step(self, st):
        op = st["op"]
        fn = getattr(self, "op_" + op, None)
        if fn is None:
            raise ValueError(f"unknown script op {op!r}")
        try:
            fn(st)
        except Exception as e:                        # noqa: BLE001  客户端的异常是被判对象的行为，不是驾驭器的错
            if op in ("place", "cancel", "restart_client", "kill_switch", "settle", "balance", "positions"):
                self.errors.append({"op": op, "intent": st.get("intent"), "error": repr(e)})
            else:
                raise

    def op_place(self, st):
        it = {"intent": st["intent"], "symbol": st.get("symbol", self.symbol), "side": st["side"], "type": st["type"],
              "qty": float(st["qty"]), "price": None if st["type"] == "MARKET" else self.base + float(st.get("offset", 0)),
              "reduce_only": bool(st.get("reduce_only", False))}
        self.intents[st["intent"]] = dict(it, expect_placed=bool(st.get("expect_placed", True)))
        self.client.place(it)

    def op_cancel(self, st):
        self.client.cancel(st["intent"])

    def op_price(self, st):
        self.sim.set_price(st.get("symbol", self.symbol), float(st["price"]) if "price" in st else self.base + float(st["offset"]))

    def op_tick(self, st):
        self.sim.tick(int(st.get("n", 1)))

    def op_sleep(self, st):
        time.sleep(float(st["ms"]) / 1000.0)

    def op_settle(self, st):
        self.client.settle()

    def op_balance(self, st):
        self.client.balance()

    def op_positions(self, st):
        self.client.positions()

    def op_restart_client(self, st):
        if not hasattr(self.client, "restart"):
            raise RuntimeError("client has no restart()")
        self.client.restart()

    def op_restart_server(self, st):
        port = self.sim.port
        self.sim.stop()
        time.sleep(float(st.get("down_ms", 50)) / 1000.0)
        self.start_sim(port)

    def op_kill_switch(self, st):
        self.kill = {"t_ms": self.sim.now(), "grace_ms": int(st.get("grace_ms", 500))}
        if not hasattr(self.client, "kill_switch"):
            raise RuntimeError("client has no kill_switch()")
        self.client.kill_switch()

    def op_chaos(self, st):
        self.sim.chaos.update(st["patch"])

    def op_symbol(self, st):
        self.sim.engine.set_symbol(st.get("symbol", self.symbol), **st["changes"])

    # ---- 整体 ----
    def execute(self):
        t0 = time.perf_counter()
        self.start_sim()
        try:
            self.make_client()
            for st in self.scn["script"]:
                self.step(st)
            try:
                self.client.settle()
            except Exception as e:                    # noqa: BLE001
                self.errors.append({"op": "final_settle", "error": repr(e)})
            truth = self.sim.engine.snapshot()
            ctx = judges.JudgeContext(truth, self.client, self.intents, self.scn["dialect"], self.sim.chaos.status(), self.kill)
            verdicts = judges.run(self.scn["judges"], ctx)
            chaos_events = self.sim.store.events(kind="chaos")
        finally:
            self.sim.stop()
        return {"scenario": self.scn["name"], "client": self.client_cls.NAME, "ok": all(v.ok for v in verdicts),
                "verdicts": [v.to_dict() for v in verdicts], "client_errors": self.errors, "seconds": round(time.perf_counter() - t0, 2),
                "server_orders": len(truth["orders"]), "server_fills": len(truth["fills"]), "chaos_events": len(chaos_events),
                "requests": self.sim.requests, "client_log": self.clog[-50:]}


def run_scenario(scn, client="correct", keep_work=False):
    cls = clients.CLIENTS[client] if isinstance(client, str) else client
    work = tempfile.mkdtemp(prefix="qdexsim-scn-")
    try:
        return Run(scn, cls, work).execute()
    except Exception:
        return {"scenario": scn["name"], "client": cls.NAME, "ok": False, "verdicts": [], "client_errors": [],
                "harness_error": traceback.format_exc(), "seconds": 0.0}
    finally:
        if not keep_work:
            shutil.rmtree(work, ignore_errors=True)


def run_suite(scenarios, client_names=("naive", "correct"), progress=None):
    """→ {"results": [...], "summary": {client: {"green", "red", "expected_mismatch": [...]}}}"""
    results, summary = [], {}
    for scn in scenarios:
        for cname in client_names:
            r = run_scenario(scn, cname)
            results.append(r)
            s = summary.setdefault(cname, {"green": 0, "red": 0, "expected_mismatch": []})
            s["green" if r["ok"] else "red"] += 1
            want = scn.get("expect", {}).get(cname)
            if want and (want == "green") != r["ok"]:
                s["expected_mismatch"].append(scn["name"])
            if progress:
                progress(r)
    return {"results": results, "summary": summary, "machine": common.machine()}
