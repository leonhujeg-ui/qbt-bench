"""客户端判据：场景跑完后，以仿真器**服务端真实状态**为准判定客户端行为。与具体客户端无关——客户端只要实现
clients.py 顶部的最小接口（place / cancel / query / positions / balance）就能被判。

每个判据 judge_<name>(ctx) → Verdict(name, ok, detail, numbers)；ctx 是 harness 在场景结束时组装的 JudgeContext：
    truth     engine.snapshot()（订单 / 成交 / 持仓 / 账户）
    client    被判客户端
    intents   {intent_id: {"symbol", "side", "type", "qty", "price", "expect_placed"}}
    kill      {"t_ms": 急停时服务端时间, "grace_ms": N} | None
    chaos     chaos.status()
    dialect

判据：
- no_duplicate     每个意图在服务端最多一张单（按 symbol/side/type/qty 匹配——天真客户端没有 clientOrderId）
- intent_complete  标了 expect_placed 的意图在服务端恰好一张单（“进度”：客户端没有因限频 / 对时 / 精度错而放弃）
- no_lost          服务端存在、客户端不知道（query(订单号) 与 query(clientOrderId) 都是 None）的单数 = 0
- no_phantom       客户端认为存在（NEW / PARTIALLY_FILLED / FILLED）但服务端没有的单数 = 0
- position_consistent 客户端认为的持仓 == 服务端持仓（1e-9）
- balance_consistent  客户端认为的钱包余额 == 服务端钱包（1e-6）
- cancel_real      客户端认为已撤的单在服务端不是“仍挂着”
- status_consistent 客户端知道的每张单，状态 == 服务端状态
- kill_switch      急停后 grace 毫秒起服务端不再有新单，且场景结束时未成交单 = 0
- rate_limit       至少被限频一次，且退避窗口内没有再打（chaos.violations 为空）
"""
from qdexsim import common

OPEN = ("NEW", "PARTIALLY_FILLED")
KNOWN = ("NEW", "PARTIALLY_FILLED", "FILLED")


class Verdict:
    def __init__(self, name, ok, detail="", **numbers):
        self.name, self.ok, self.detail, self.numbers = name, bool(ok), detail, numbers

    def to_dict(self):
        return {"judge": self.name, "ok": self.ok, "detail": self.detail, "numbers": self.numbers}

    def __repr__(self):
        return f"{'OK  ' if self.ok else 'FAIL'} {self.name}: {self.detail}"


class JudgeContext:
    def __init__(self, truth, client, intents, dialect, chaos=None, kill=None):
        self.truth, self.client, self.intents, self.dialect = truth, client, intents, dialect
        self.chaos, self.kill = chaos or {}, kill

    def user_orders(self):
        return [o for o in self.truth["orders"] if o.get("origin") != "system"]

    def client_knows(self, o):
        """客户端是否知道这张服务端订单：先按订单号，再按 clientOrderId。"""
        r = self.client.query(str(o["id"]))
        if r is None and o.get("cid"):
            r = self.client.query(o["cid"])
        return r


def intent_matches(o, it):
    return (o["symbol"] == it["symbol"] and o["side"] == it["side"] and o["type"] == it["type"]
            and abs(float(o["qty"]) - float(it["qty"])) < 1e-9)


def judge_no_duplicate(ctx):
    counts = {k: sum(1 for o in ctx.user_orders() if intent_matches(o, it)) for k, it in ctx.intents.items()}
    dups = {k: n for k, n in counts.items() if n > 1}
    return Verdict("no_duplicate", not dups, f"orders per intent {counts}", max_per_intent=max(counts.values(), default=0),
                   duplicated_intents=len(dups))


def judge_intent_complete(ctx):
    want = {k: it for k, it in ctx.intents.items() if it.get("expect_placed")}
    counts = {k: sum(1 for o in ctx.user_orders() if intent_matches(o, it)) for k, it in want.items()}
    missing = [k for k, n in counts.items() if n != 1]
    return Verdict("intent_complete", not missing, f"expected 1 server order each: {counts}", missing=len(missing), expected=len(want))


def judge_no_lost(ctx):
    lost = [o["id"] for o in ctx.user_orders() if ctx.client_knows(o) is None]
    return Verdict("no_lost", not lost, f"server orders unknown to client: {lost}", lost=len(lost), server_orders=len(ctx.user_orders()))


def judge_no_phantom(ctx):
    by_id = {str(o["id"]): o for o in ctx.truth["orders"]}
    by_cid = {o["cid"]: o for o in ctx.truth["orders"] if o.get("cid")}
    phantoms = []
    for k in ctx.intents:
        r = ctx.client.query(k)
        if r is None or r["status"] not in KNOWN:
            continue
        if str(r.get("server_id")) not in by_id and r.get("cid") not in by_cid:
            phantoms.append(k)
    return Verdict("no_phantom", not phantoms, f"client orders absent on server: {phantoms}", phantoms=len(phantoms))


def judge_position_consistent(ctx):
    server = {p["symbol"]: p["qty"] for p in ctx.truth["positions"] if abs(p["qty"]) > 1e-12}
    mine = {k: v for k, v in (ctx.client.positions() or {}).items() if abs(v) > 1e-12}
    diff = {s: (mine.get(s, 0.0), server.get(s, 0.0)) for s in set(server) | set(mine)
            if abs(mine.get(s, 0.0) - server.get(s, 0.0)) > 1e-9}
    return Verdict("position_consistent", not diff, f"client vs server mismatches: {diff}", mismatched_symbols=len(diff))


def judge_balance_consistent(ctx):
    mine, server = ctx.client.balance(), ctx.truth["account"]["wallet"]
    ok = mine is not None and abs(float(mine) - server) < 1e-6
    return Verdict("balance_consistent", ok, f"client {mine} vs server {common.fnum(server)}", diff=None if mine is None else abs(float(mine) - server))


def judge_cancel_real(ctx):
    by_id = {str(o["id"]): o for o in ctx.truth["orders"]}
    by_cid = {o["cid"]: o for o in ctx.truth["orders"] if o.get("cid")}
    still_open = []
    for k in ctx.intents:
        r = ctx.client.query(k)
        if r is None or r["status"] != "CANCELED":
            continue
        o = by_id.get(str(r.get("server_id"))) or by_cid.get(r.get("cid"))
        if o is not None and o["status"] in OPEN:
            still_open.append(k)
    return Verdict("cancel_real", not still_open, f"believed canceled but still open on server: {still_open}", still_open=len(still_open))


def judge_status_consistent(ctx):
    bad = {}
    for o in ctx.user_orders():
        r = ctx.client_knows(o)
        if r is not None and r["status"] != o["status"]:
            bad[o["id"]] = (r["status"], o["status"])
    return Verdict("status_consistent", not bad, f"client status vs server: {bad}", mismatched=len(bad))


def judge_kill_switch(ctx):
    if ctx.kill is None:
        return Verdict("kill_switch", False, "scenario never engaged the kill switch")
    cutoff = ctx.kill["t_ms"] + ctx.kill["grace_ms"]
    late = [o["id"] for o in ctx.user_orders() if o["created"] >= cutoff]
    open_ = [o["id"] for o in ctx.truth["orders"] if o["status"] in OPEN]
    return Verdict("kill_switch", not late and not open_, f"orders created after kill+grace: {late}; still open: {open_}",
                   late_orders=len(late), open_orders=len(open_))


def judge_rate_limit(ctx):
    rules = ctx.chaos.get("config", {}).get("rules", [])
    counts = ctx.chaos.get("counts", [])
    issued = sum(c for r, c in zip(rules, counts) if r.get("kind") == "rate_limit")
    bad = [v for v in ctx.chaos.get("violations", []) if v["dialect"] == ctx.dialect]
    return Verdict("rate_limit", issued > 0 and not bad, f"429 issued {issued}, requests inside backoff window {len(bad)}",
                   issued=issued, violations=len(bad))


JUDGES = {f.__name__[6:]: f for f in (judge_no_duplicate, judge_intent_complete, judge_no_lost, judge_no_phantom,
                                      judge_position_consistent, judge_balance_consistent, judge_cancel_real,
                                      judge_status_consistent, judge_kill_switch, judge_rate_limit)}


def run(names, ctx):
    return [JUDGES[n](ctx) for n in names]
