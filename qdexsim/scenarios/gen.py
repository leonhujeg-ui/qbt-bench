"""生成场景文件：12 个模板 × 5 个方言 = 60 个场景（python3.12 qdexsim/scenarios/gen.py [--check]）。

模板（每个都写明预期：天真客户端红 / 绿，正确客户端绿；判据套件“有牙”的证据就是 expect.naive == "red" 的那些）：
  baseline        无混沌：限价挂单被行情打成、市价平仓、撤单
  unknown_timeout 下单结果未知（服务端已执行，握住连接不回）→ 天真客户端重发 → 重复下单
  unknown_drop    同上，直接断连
  unknown_502     同上，回网关 502
  client_restart  挂两张单，一张成交，客户端重启，再撤另一张 → 天真客户端忘了所有单
  server_restart  服务端重启（SQLite 恢复），单子 / 持仓都还在
  cancel_race     撤单到达前剩余部分已成交 → 客户端以为撤了，其实成了
  rate_limit      下单 / 撤单 / 查单按概率 429（重试头 600 ms）→ 退避窗口内重打记违规
  kill_switch     挂三张单后急停（撤单也会被限频 / 结果未知）→ 急停后不得再有新单、未成交必须清零
  stale_views     持仓查询短时返回旧值 + 余额查询返回 0 一次 + 成交推迟出现在明细
  clock_drift     服务端时钟快 45 秒（超过五家里最宽的 30 秒窗） → 签名时间窗错，客户端须对时
  symbol_rules    下单途中价格精度变粗（0.1 → 1.0）、随后交易对暂停 → 须重新归整价格、不得留幻单
"""
import argparse
import json
import os
import sys

DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(DIR))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

DIALECTS = ("binance", "bybit", "okx", "bitget", "bitunix")
ALL_JUDGES = ["no_duplicate", "intent_complete", "no_lost", "no_phantom", "position_consistent", "cancel_real"]


def place(intent, side, otype, qty, offset=0, **kw):
    st = {"op": "place", "intent": intent, "side": side, "type": otype, "qty": qty}
    if otype == "LIMIT":
        st["offset"] = offset
    st.update(kw)
    return st


def unknown(mode):
    def build(seed):
        return {"seed": seed, "rules": [{"kind": "unknown_result", "op": "place", "p": 1.0, "max": 1, "mode": mode, "hold_ms": 600}]}
    return build


TEMPLATES = {
    "baseline": {
        "chaos": lambda seed: {"seed": seed, "rules": []},
        "script": [place("i1", "BUY", "LIMIT", 0.01, -100), place("i2", "SELL", "LIMIT", 0.02, 300),
                   {"op": "price", "offset": -110}, {"op": "price", "offset": 0},
                   place("i3", "SELL", "MARKET", 0.01, reduce_only=True), {"op": "cancel", "intent": "i2"}],
        "judges": ALL_JUDGES + ["status_consistent"], "expect": {"naive": "red", "correct": "green"}},
    "unknown_timeout": {
        "chaos": unknown("timeout"),
        "script": [place("i1", "BUY", "LIMIT", 0.01, -100), {"op": "sleep", "ms": 50}, place("i2", "SELL", "LIMIT", 0.02, 200)],
        "judges": ALL_JUDGES, "expect": {"naive": "red", "correct": "green"}},
    "unknown_drop": {
        "chaos": unknown("drop"),
        "script": [place("i1", "BUY", "LIMIT", 0.01, -100), place("i2", "SELL", "LIMIT", 0.02, 200)],
        "judges": ALL_JUDGES, "expect": {"naive": "red", "correct": "green"}},
    "unknown_502": {
        "chaos": unknown("502"),
        "script": [place("i1", "BUY", "LIMIT", 0.01, -100), place("i2", "SELL", "LIMIT", 0.02, 200)],
        "judges": ALL_JUDGES, "expect": {"naive": "red", "correct": "green"}},
    "client_restart": {
        "chaos": lambda seed: {"seed": seed, "rules": []},
        "script": [place("i1", "BUY", "LIMIT", 0.01, -100), place("i2", "BUY", "LIMIT", 0.02, -500),
                   {"op": "price", "offset": -120}, {"op": "price", "offset": 0}, {"op": "restart_client"},
                   {"op": "cancel", "intent": "i2"}],
        "judges": ["no_lost", "no_phantom", "position_consistent", "cancel_real", "status_consistent"],
        "expect": {"naive": "red", "correct": "green"}},
    "server_restart": {
        "chaos": lambda seed: {"seed": seed, "rules": []},
        "script": [place("i1", "BUY", "LIMIT", 0.01, -100), place("i2", "BUY", "LIMIT", 0.02, -500),
                   {"op": "price", "offset": -120}, {"op": "price", "offset": 0}, {"op": "restart_server"},
                   {"op": "cancel", "intent": "i2"}, place("i3", "SELL", "MARKET", 0.01, reduce_only=True)],
        "judges": ALL_JUDGES, "expect": {"naive": "red", "correct": "green"}},
    "cancel_race": {
        "chaos": lambda seed: {"seed": seed, "rules": [{"kind": "cancel_race", "op": "cancel", "p": 1.0, "max": 1}]},
        "script": [place("i1", "BUY", "LIMIT", 0.01, -100), {"op": "cancel", "intent": "i1"}],
        "judges": ["no_duplicate", "no_lost", "cancel_real", "position_consistent", "status_consistent"],
        "expect": {"naive": "red", "correct": "green"}},
    "rate_limit": {
        "chaos": lambda seed: {"seed": seed, "rules": [{"kind": "rate_limit", "op": ["place", "cancel", "query"], "p": 0.5,
                                                        "max": 4, "retry_after_ms": 600}]},
        "script": [place("i1", "BUY", "LIMIT", 0.01, -100), place("i2", "BUY", "LIMIT", 0.02, -200),
                   place("i3", "SELL", "LIMIT", 0.03, 200), {"op": "cancel", "intent": "i1"}, {"op": "cancel", "intent": "i2"},
                   {"op": "cancel", "intent": "i3"}],
        "judges": ["rate_limit", "no_duplicate", "intent_complete", "cancel_real"], "expect": {"naive": "red", "correct": "green"}},
    "kill_switch": {
        "chaos": lambda seed: {"seed": seed, "rules": [{"kind": "rate_limit", "op": "cancel", "p": 1.0, "max": 1, "retry_after_ms": 300},
                                                       {"kind": "unknown_result", "op": "cancel", "p": 1.0, "max": 1, "mode": "drop"}]},
        "script": [place("i1", "BUY", "LIMIT", 0.01, -100), place("i2", "BUY", "LIMIT", 0.02, -200),
                   place("i3", "SELL", "LIMIT", 0.03, 200), {"op": "kill_switch", "grace_ms": 500},
                   place("i4", "BUY", "LIMIT", 0.04, -300, expect_placed=False), {"op": "sleep", "ms": 600}],
        "judges": ["kill_switch", "no_duplicate"], "expect": {"naive": "red", "correct": "green"}},
    "stale_views": {
        "chaos": lambda seed: {"seed": seed, "rules": [{"kind": "stale", "op": "positions", "ms": 500},
                                                       {"kind": "zero_once", "op": "account", "max": 1},
                                                       {"kind": "fill_delay", "ms": 400}]},
        "script": [place("i1", "BUY", "LIMIT", 0.01, -100), {"op": "price", "offset": -120}, {"op": "price", "offset": 0}],
        "judges": ["position_consistent", "balance_consistent", "no_lost", "status_consistent"],
        "expect": {"naive": "red", "correct": "green"}},
    "clock_drift": {
        "chaos": lambda seed: {"seed": seed, "clock_drift_ms": 45000, "rules": []},
        "script": [place("i1", "BUY", "LIMIT", 0.01, -100), place("i2", "SELL", "LIMIT", 0.02, 200), {"op": "cancel", "intent": "i1"}],
        "judges": ["intent_complete", "no_duplicate", "no_phantom", "cancel_real"], "expect": {"naive": "red", "correct": "green"}},
    "symbol_rules": {
        "chaos": lambda seed: {"seed": seed, "rules": []},
        "script": [place("i1", "BUY", "LIMIT", 0.01, -100.5), {"op": "symbol", "changes": {"tick": 1.0}},
                   place("i2", "BUY", "LIMIT", 0.02, -200.5), {"op": "symbol", "changes": {"status": "SUSPENDED"}},
                   place("i3", "BUY", "LIMIT", 0.03, -300, expect_placed=False)],
        "judges": ["intent_complete", "no_duplicate", "no_phantom", "no_lost"], "expect": {"naive": "red", "correct": "green"}},
}


def build(dialect, template, seed):
    t = TEMPLATES[template]
    return {"name": f"{dialect}/{template}", "dialect": dialect, "template": template, "symbol": "BTCUSDT", "seed": seed,
            "timeout_s": 0.3, "chaos": t["chaos"](seed), "script": t["script"], "judges": t["judges"], "expect": t["expect"]}


def all_scenarios():
    out = []
    for i, d in enumerate(DIALECTS):
        for j, tname in enumerate(TEMPLATES):
            out.append(build(d, tname, 100 + i * 20 + j))
    return out


def render(scn):
    return json.dumps(scn, indent=1, ensure_ascii=False, sort_keys=True) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只校验磁盘文件与生成结果一致")
    args = ap.parse_args(argv)
    bad = 0
    for scn in all_scenarios():
        path = os.path.join(DIR, scn["name"].replace("/", "__") + ".json")
        text = render(scn)
        if args.check:
            with open(path, encoding="utf-8") as f:
                if f.read() != text:
                    print("stale:", path)
                    bad += 1
        else:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
    print(f"{len(all_scenarios())} scenarios, {len(TEMPLATES)} templates x {len(DIALECTS)} dialects" + (f", stale {bad}" if args.check else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
