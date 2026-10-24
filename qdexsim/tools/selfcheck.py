"""各目标的实测（selftest 的 g1–g4 与“三刀”步骤都调这里；也可单独跑：python3.12 -m qdexsim.tools.selfcheck g1|g2|g3|g4|knives）。

g1  仿真器各方言签名 / 撮合 / 重启：起真服务端子进程，五个方言各自：错签名 / 过期时间戳 → 各家错误码；正确签名下单 → 行情推过去
    部分成交 → 撤单；杀掉进程、同库同端口重启 → 单子 / 成交 / 持仓都在，clientOrderId 跨重启仍幂等。
g2  混沌可复现：一个用到全部 11 种注入的场景，同种子跑两遍 → 注入事件序列逐条相同；换种子 → 不同；每条注入都有事件日志。
g3  判据套件：两个参考客户端跑全部场景；正确客户端必须全绿，天真客户端在 unknown_* / client_restart 模板里必须全红。
g4  对账自检：合成数据 50 条已知差异全部找出、零误报。
knives  三刀：手工突变（去掉 clientOrderId 幂等 / 结果未知时不执行 / 对账漏比状态字段）各自必须让测试红。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

from qdexsim import adapters, chaos as chaos_mod, common, harness, recon, scenarios
from qdexsim.adapters import ExchangeError
from qdexsim.dialects import base as dbase
from qdexsim.server import LISTEN_LINE

EXPECT_BAD_SIGN = {"binance": -1022, "bybit": 10004, "okx": "50113", "bitget": "40009", "bitunix": 10007}
EXPECT_BAD_TS = {"binance": -1021, "bybit": 10002, "okx": "50102", "bitget": "40008", "bitunix": 10002}
ALL_KINDS = ("unknown_result", "latency", "rate_limit", "server_error", "maintenance", "cancel_race", "fill_delay", "stale",
             "zero_once", "symbol", "clock_drift")


def start_server(db, port=0):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.Popen([sys.executable, "-m", "qdexsim.server", "--db", db, "--port", str(port)], cwd=common.REPO, env=env,
                            stdout=subprocess.PIPE, text=True)
    url = proc.stdout.readline().strip()[len(LISTEN_LINE):]
    return proc, url


def stop_server(proc):
    proc.terminate()
    proc.wait(timeout=10)


def _tamper(ad, kind):
    """让适配器下一次签名出错：kind = sign（密钥错）/ ts（时间戳过期 60 秒）。"""
    if kind == "sign":
        ad.creds["secret"] = "wrong"
        ad.d.creds["secret"] = "wrong"
    else:
        ad.offset_ms = -60000


def _set_price(url, price):
    adapters.Transport(url, 5).request("POST", "/__admin/tick", {"Content-Type": "application/json"},
                                       json.dumps({"symbol": "BTCUSDT", "price": price}).encode())


def g1_dialect(name, url):
    out = {}
    for kind, table in (("sign", EXPECT_BAD_SIGN), ("ts", EXPECT_BAD_TS)):
        ad = adapters.make(name, url, timeout_s=5)
        _tamper(ad, kind)
        try:
            ad.account()
            out[f"bad_{kind}_code"] = None
        except ExchangeError as e:
            out[f"bad_{kind}_code"] = e.code
        out[f"bad_{kind}_expected"] = table[name]
    ad = adapters.make(name, url, timeout_s=5)
    _set_price(url, 60000)                                            # 上一个方言把价推下去了：先回到起点再挂单
    o = ad.place("BTCUSDT", "BUY", "LIMIT", 0.06, 59000.0, cid=f"g1-{name}")
    _set_price(url, 58990)                                            # 穿价：本 tick 流动性 0.05 → 部分成交
    q = ad.query("BTCUSDT", cid=f"g1-{name}")
    out["partial_fill"] = {"status": q["status"], "filled": q["filled"], "qty": q["qty"]}
    out["cancel_status"] = ad.cancel("BTCUSDT", order_id=o["id"])["status"]
    out["after_cancel"] = ad.query("BTCUSDT", order_id=o["id"])["status"]
    out["ok"] = (out["bad_sign_code"] == EXPECT_BAD_SIGN[name] and out["bad_ts_code"] == EXPECT_BAD_TS[name]
                 and q["status"] == "PARTIALLY_FILLED" and q["filled"] == 0.05 and out["after_cancel"] == "CANCELED")
    return out


def g1(out_dir=None):
    d = tempfile.mkdtemp(prefix="qdexsim-g1-")
    t0 = time.perf_counter()
    try:
        db = os.path.join(d, "sim.db")
        proc, url = start_server(db)
        try:
            per = {name: g1_dialect(name, url) for name in adapters.ADAPTERS}
            snap_before = json.loads(adapters.make("binance", url, timeout_s=5).t.request("GET", "/__admin/state")[2])
        finally:
            stop_server(proc)
        port = int(url.rsplit(":", 1)[1])
        proc, url2 = start_server(db, port)
        try:
            snap_after = json.loads(adapters.make("binance", url2, timeout_s=5).t.request("GET", "/__admin/state")[2])
            dup = None
            try:
                adapters.make("okx", url2, timeout_s=5).place("BTCUSDT", "BUY", "LIMIT", 0.01, 59000.0, cid="g1-okx")
            except ExchangeError as e:
                dup = e.kind
            pos_after = adapters.make("bybit", url2, timeout_s=5).positions()
        finally:
            stop_server(proc)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    keys = ("orders", "fills", "positions")
    restart = {"same_port": url2 == url, "orders": len(snap_after["orders"]), "fills": len(snap_after["fills"]),
               "position_btc": pos_after.get("BTCUSDT"), "state_equal": all(snap_before[k] == snap_after[k] for k in keys),
               "dup_cid_after_restart": dup}
    summary = {"dialects": per, "restart": restart, "seconds": round(time.perf_counter() - t0, 1),
               "ok": all(p["ok"] for p in per.values()) and restart["state_equal"] and restart["same_port"] and dup == "DUP_CID"
               and restart["orders"] == 5 and restart["fills"] == 5}
    if out_dir:
        common.write_json(os.path.join(out_dir, "g1_dialects.json"), dict(summary, machine=common.machine()))
    return summary


G2_SCENARIO = {
    "name": "selfcheck/all_kinds", "dialect": "bybit", "symbol": "BTCUSDT", "seed": 0, "timeout_s": 0.3, "judges": ["no_duplicate"],
    "expect": {"correct": "green"},
    "chaos": {"seed": 0, "rules": [
        {"kind": "latency", "op": "*", "ms": 1, "jitter_ms": 2, "p": 0.5},
        {"kind": "unknown_result", "op": "place", "p": 1.0, "max": 1, "mode": "drop"},
        {"kind": "rate_limit", "op": "query", "p": 0.5, "max": 2, "retry_after_ms": 30},
        {"kind": "server_error", "op": "query", "p": 0.5, "max": 2}, {"kind": "maintenance", "op": "account", "p": 1.0, "max": 1},
        {"kind": "cancel_race", "op": "cancel", "p": 1.0, "max": 1}, {"kind": "fill_delay", "ms": 50},
        {"kind": "stale", "op": "positions", "ms": 100}, {"kind": "zero_once", "op": "account", "max": 1},
        {"kind": "symbol", "at_req": 4, "symbol": "ETHUSDT", "changes": {"tick": 0.5}},
        {"kind": "clock_drift", "at_req": 6, "ms": 100}]},
    "script": [{"op": "place", "intent": "i1", "side": "BUY", "type": "LIMIT", "qty": 0.01, "offset": -100},
               {"op": "place", "intent": "i2", "side": "SELL", "type": "LIMIT", "qty": 0.02, "offset": 100},
               {"op": "price", "offset": -120}, {"op": "price", "offset": 0}, {"op": "cancel", "intent": "i2"},
               {"op": "place", "intent": "i3", "side": "BUY", "type": "LIMIT", "qty": 0.03, "offset": -200},
               {"op": "cancel", "intent": "i3"}, {"op": "balance"}, {"op": "positions"}, {"op": "settle"}],
}


def _chaos_trace(scn):
    """跑一遍，返回注入事件序列 [(req_index, op, injection)]（从场景结果里拿不到 → 自己起仿真器拿）。"""
    r = harness.Run(scn, harness.clients.CorrectClient, tempfile.mkdtemp(prefix="qdexsim-g2-"))
    r.start_sim()
    try:
        r.make_client()
        for st in scn["script"]:
            r.step(st)
        events = r.sim.store.events(kind="chaos")
        return [(e["req_index"], e["op"], e["injection"]) for e in events], r.sim.requests
    finally:
        r.sim.stop()
        shutil.rmtree(r.work, ignore_errors=True)


def g2(out_dir=None):
    t0 = time.perf_counter()
    a, na = _chaos_trace(G2_SCENARIO)
    b, nb = _chaos_trace(G2_SCENARIO)
    other = dict(G2_SCENARIO, chaos=dict(G2_SCENARIO["chaos"], seed=99))
    c, _nc = _chaos_trace(other)
    kinds = sorted({k for _r, _o, k in a})
    summary = {"events_seed0_run1": len(a), "events_seed0_run2": len(b), "identical": a == b, "events_seed99": len(c),
               "differs_with_other_seed": a != c, "kinds_logged": kinds, "kinds_missing": sorted(set(ALL_KINDS) - set(kinds)),
               "requests": na, "seconds": round(time.perf_counter() - t0, 1)}
    summary["ok"] = summary["identical"] and summary["differs_with_other_seed"] and not summary["kinds_missing"] and len(a) > 5 and na == nb
    if out_dir:
        common.write_json(os.path.join(out_dir, "g2_chaos.json"), dict(summary, trace=a, machine=common.machine()))
    return summary


MUST_RED = ("unknown_timeout", "unknown_drop", "unknown_502", "client_restart")


def g3(out_dir=None, progress=None):
    t0 = time.perf_counter()
    scns = scenarios.load_all()
    res = harness.run_suite(scns, progress=progress)
    by = {(r["scenario"], r["client"]): r for r in res["results"]}
    naive_must_red = [s["name"] for s in scns if s["template"] in MUST_RED]
    naive_red_ok = all(not by[(n, "naive")]["ok"] for n in naive_must_red)
    teeth = {}
    for r in res["results"]:
        if r["client"] == "naive":
            for v in r["verdicts"]:
                if not v["ok"]:
                    teeth[v["judge"]] = teeth.get(v["judge"], 0) + 1
    s = res["summary"]
    summary = {"scenarios": len(scns), "dialects": sorted({x["dialect"] for x in scns}), "templates": sorted({x["template"] for x in scns}),
               "correct": s["correct"], "naive": s["naive"], "naive_must_red_scenarios": len(naive_must_red), "naive_must_red_ok": naive_red_ok,
               "naive_red_judges": teeth, "seconds": round(time.perf_counter() - t0, 1),
               "ok": s["correct"]["red"] == 0 and s["correct"]["green"] == len(scns) and naive_red_ok and not s["naive"]["expected_mismatch"]}
    if out_dir:
        common.write_json(os.path.join(out_dir, "g3_scenarios.json"), dict(summary, results=res["results"], machine=res["machine"]))
    return summary


def g4(out_dir=None):
    t0 = time.perf_counter()
    res = recon.selfcheck()
    res["seconds"] = round(time.perf_counter() - t0, 2)
    if out_dir:
        common.write_json(os.path.join(out_dir, "g4_recon.json"), dict(res, machine=common.machine()))
    return res


KNIVES = (
    ("remove_cid_idempotency", {"engine.py": ('if cid is not None and cid in self.by_cid:', 'if False:'),
                                "store.py": ("cid TEXT UNIQUE", "cid TEXT")}, ["test_engine", "test_dialects", "test_clients"]),
    ("unknown_result_not_executed", {"server.py": ("        resp = d.handle(op, req, ctx)\n",
                                                   "        if decision.unknown is not None:\n            return None, decision\n"
                                                   "        resp = d.handle(op, req, ctx)\n")}, ["test_server", "test_clients"]),
    ("recon_skips_status", {"recon.py": ('    if l["status"] is not None and r["status"] is not None and l["status"] != r["status"]:\n'
                                         '        fields.append("status")\n', "")}, ["test_recon"]),
)


def knives(out_dir=None):
    """三把手工刀：每把都要让对应测试红（退出码非 0）；记下是哪个测试先抓住的。"""
    t0 = time.perf_counter()
    work = tempfile.mkdtemp(prefix="qdexsim-knives-")
    results = []
    try:
        for name, patches, tests in KNIVES:
            wd = os.path.join(work, name)
            shutil.copytree(common.PKG, os.path.join(wd, "qdexsim"), ignore=shutil.ignore_patterns("__pycache__", "results"))
            os.makedirs(os.path.join(wd, "qdexsim", "results"))
            for fname, (old, new) in patches.items():
                p = os.path.join(wd, "qdexsim", fname)
                with open(p, encoding="utf-8") as f:
                    src = f.read()
                if old not in src:
                    raise RuntimeError(f"knife {name}: pattern not found in {fname}")
                with open(p, "w", encoding="utf-8") as f:
                    f.write(src.replace(old, new))
            env = dict(os.environ, PYTHONPATH="", QDEXSIM_FAST="1", PYTHONDONTWRITEBYTECODE="1")
            env.pop("QDEXSIM_COV_DIR", None)
            proc = subprocess.run([sys.executable, "-m", "unittest", "-f"] + ["qdexsim.tests." + t for t in tests], cwd=wd, env=env,
                                  capture_output=True, text=True, timeout=600)
            first = next((line for line in proc.stderr.splitlines() if line.startswith(("FAIL:", "ERROR:"))), "")
            results.append({"knife": name, "red": proc.returncode != 0, "caught_by": first[:160], "tests": tests})
    finally:
        shutil.rmtree(work, ignore_errors=True)
    summary = {"knives": results, "ok": all(r["red"] for r in results) and len(results) == 3, "seconds": round(time.perf_counter() - t0, 1)}
    if out_dir:
        common.write_json(os.path.join(out_dir, "knives.json"), dict(summary, machine=common.machine()))
    return summary


def main(argv=None):
    which = (argv or sys.argv[1:] or ["g1"])[0]
    fn = {"g1": g1, "g2": g2, "g3": g3, "g4": g4, "knives": knives}[which]
    res = fn(common.RESULTS)
    print(json.dumps({k: v for k, v in res.items() if k not in ("results", "trace")}, ensure_ascii=False, indent=1, default=str))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
