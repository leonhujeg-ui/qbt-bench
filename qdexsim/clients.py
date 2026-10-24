"""两个参考客户端（都只通过 adapters 说话，实现判据套件要求的最小接口）：

    place(intent) -> local_id      intent = {"intent", "symbol", "side", "type", "qty", "price", "reduce_only"}
    cancel(local_id)
    query(key) -> {"local_id", "server_id", "cid", "status", "filled", "symbol", "side", "type", "qty"} | None
                                   key 可以是 local_id、服务端订单号或 clientOrderId；None = 客户端不知道这张单
    positions() -> {symbol: 带符号数量}      客户端**自己认为**的持仓
    balance() -> float | None               客户端自己认为的钱包余额
    可选：restart()（模拟进程重启）、kill_switch()（急停）、settle()（把手头的重试 / 对账做完）、open_orders()

NaiveClient（天真）：不带 clientOrderId；超时 / 限频就立刻重试；下单回包说什么就信什么，之后不再查；撤单报错也当成撤掉了；
  持仓 / 余额只在本地记一次；重启 = 内存清零。——判据套件应当让它在结果未知 / 重启场景里红。
CorrectClient（正确）：clientOrderId = 确定性派生自 intent；**先写日志再发**；结果未知 → 先按 clientOrderId 查，查不到才用同一
  clientOrderId 重发（服务端幂等兜底）；限频按重试头退避；时间窗错 → 对时重发；精度错 → 刷新交易对规则重新归整；
  撤单报“不存在 / 不可撤” → 以查单结果为准；重启 → 读日志、逐单按 clientOrderId 对账；急停 → 拒新单 + 循环撤到服务端未成交为空；
  持仓 = 自己所有订单的成交量之和，并与服务端持仓多读几次（跨过“短时旧值”窗口）核对。
"""
import json
import os
import time

from qdexsim import adapters, common
from qdexsim.adapters import ExchangeError, RateLimited, UnknownResult

FINAL = ("FILLED", "CANCELED", "REJECTED", "NOT_SENT")
OPEN = ("NEW", "PARTIALLY_FILLED")


class Base:
    NAME = "base"

    def __init__(self, dialect, base_url, state_dir, creds=None, timeout_s=0.3, log=None):
        self.ad = adapters.make(dialect, base_url, creds, timeout_s)
        self.dialect, self.state_dir, self.log = dialect, state_dir, log if log is not None else []
        self.killed = False
        self.orders = {}             # local_id → record
        self.ledger = {}             # symbol → qty
        os.makedirs(state_dir, exist_ok=True)

    def note(self, what, **kw):
        self.log.append(dict(kw, client=self.NAME, what=what, t=common.now_ms()))

    def _rec(self, intent, cid=None):
        return {"local_id": intent["intent"], "server_id": None, "cid": cid, "status": None, "filled": 0.0,
                "symbol": intent["symbol"], "side": intent["side"], "type": intent["type"], "qty": float(intent["qty"]),
                "price": intent.get("price"), "reduce_only": bool(intent.get("reduce_only")), "phase": "pending"}

    def query(self, key):
        key = str(key)
        for r in self.orders.values():
            if key in (str(r["local_id"]), str(r["server_id"]), str(r["cid"])) and r["status"] not in ("NOT_SENT",):
                return dict(r)
        return None

    def positions(self):
        return {k: v for k, v in self.ledger.items() if abs(v) > 1e-12}

    def open_orders(self):
        return [dict(r) for r in self.orders.values() if r["status"] in OPEN]

    def settle(self):
        pass


# =============================================================================================
class NaiveClient(Base):
    NAME = "naive"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._balance = None
        self._bootstrap()

    def _bootstrap(self):
        try:
            self._balance = self.ad.account()["wallet"]
        except Exception as e:                       # noqa: BLE001  天真：读不到就算了
            self.note("balance_read_failed", error=repr(e))

    def place(self, intent):
        if self.killed:
            raise RuntimeError("killed")
        rec = self._rec(intent)
        for attempt in range(3):
            try:
                o = self.ad.place(intent["symbol"], intent["side"], intent["type"], intent["qty"], intent.get("price"),
                                  cid=None, reduce_only=intent.get("reduce_only", False))
            except (UnknownResult, RateLimited) as e:           # 天真：不知道结果 / 被限频 → 立刻再发一张
                self.note("retry_blind", intent=intent["intent"], attempt=attempt, error=repr(e))
                continue
            except ExchangeError as e:
                self.note("rejected", intent=intent["intent"], error=repr(e))
                return None
            rec.update(server_id=o["id"], status=o["status"] or ("FILLED" if intent["type"] == "MARKET" else "NEW"), phase="acked")
            if rec["status"] == "FILLED":            # 信回包：市价单当全成，之后不再查
                rec["filled"] = rec["qty"]
                self._apply(rec, rec["qty"])
            self.orders[rec["local_id"]] = rec
            return rec["local_id"]
        self.note("gave_up", intent=intent["intent"])
        return None

    def _apply(self, rec, qty):
        sign = 1.0 if rec["side"] == "BUY" else -1.0
        self.ledger[rec["symbol"]] = round(self.ledger.get(rec["symbol"], 0.0) + sign * qty, 10)

    def cancel(self, local_id):
        rec = self.orders.get(local_id)
        if rec is None:
            return
        try:
            self.ad.cancel(rec["symbol"], order_id=rec["server_id"])
        except Exception as e:                       # noqa: BLE001  天真：报错也当撤掉了
            self.note("cancel_error_ignored", local_id=local_id, error=repr(e))
        rec["status"] = "CANCELED"

    def balance(self):
        if self._balance is None:                    # 启动时没读到就再读一次，之后一直信这个数
            self._bootstrap()
        return self._balance

    def restart(self):
        self.orders, self.ledger = {}, {}            # 没落盘，重启后什么都不记得
        self.killed = False
        self._bootstrap()

    def kill_switch(self):
        self.killed = True
        for r in list(self.orders.values()):
            if r["status"] in OPEN:
                self.cancel(r["local_id"])


# =============================================================================================
class CorrectClient(Base):
    NAME = "correct"
    MAX_RETRY = 12

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.journal_path = os.path.join(self.state_dir, "journal.json")
        self.specs = {}
        self._load_journal()

    # ---- 日志（先写再发）----
    def _load_journal(self):
        if os.path.exists(self.journal_path):
            self.orders = common.read_json(self.journal_path)

    def _save(self):
        common.write_json(self.journal_path, self.orders)

    # ---- 带退避的调用 ----
    def _call(self, fn, *args, **kw):
        """读操作 / 幂等写操作的通用重试：限频按头退避、时间窗错对时、5xx 短退避。"""
        for attempt in range(self.MAX_RETRY):
            try:
                return fn(*args, **kw)
            except RateLimited as e:
                self.note("backoff", ms=e.retry_after_ms, attempt=attempt)
                time.sleep(e.retry_after_ms / 1000.0 + 0.01)
            except ExchangeError as e:
                if e.kind == "BAD_TIMESTAMP":
                    self.ad.sync_time()
                    self.note("resync", offset_ms=self.ad.offset_ms)
                elif e.kind in ("SERVER_ERROR", "MAINTENANCE"):
                    time.sleep(0.05 * (attempt + 1))
                else:
                    raise
            except UnknownResult as e:
                if fn.__name__ in ("place", "cancel", "set_leverage", "set_margin_mode"):
                    raise
                self.note("read_retry", error=repr(e), attempt=attempt)
                time.sleep(0.02)
        raise RuntimeError("retries exhausted")

    def _refresh(self, rec):
        """用服务端状态覆盖本地记录（按 clientOrderId 查，服务端订单号只是附带学到的）。→ 是否找到。"""
        o = self._call(self.ad.query, rec["symbol"], cid=rec["cid"])
        if o is None:
            return False
        rec.update(server_id=o["id"], status=o["status"], filled=o["filled"], phase="final" if o["status"] in FINAL else "acked")
        return True

    # ---- 下单 ----
    def place(self, intent):
        if self.killed:
            raise RuntimeError("kill switch engaged: refusing new orders")
        rec = self._rec(intent, cid=f"{intent['intent']}")
        rec["phase"] = "pending"
        self.orders[rec["local_id"]] = rec
        self._save()
        self._send(rec)
        self._save()
        return rec["local_id"]

    def _send(self, rec):
        for attempt in range(self.MAX_RETRY):
            rec["phase"] = "sent"
            self._save()
            try:
                o = self.ad.place(rec["symbol"], rec["side"], rec["type"], rec["qty"], rec["price"], cid=rec["cid"],
                                  reduce_only=rec["reduce_only"])
            except UnknownResult as e:               # 结果未知：先查，查到就认；查不到再用同一 clientOrderId 重发
                self.note("unknown_result", intent=rec["local_id"], error=repr(e))
                if self._refresh(rec):
                    return
                continue
            except RateLimited as e:
                time.sleep(e.retry_after_ms / 1000.0 + 0.01)
                continue
            except ExchangeError as e:
                if self._handle_reject(rec, e, attempt):
                    continue
                return
            rec.update(server_id=o["id"], status=o["status"], phase="acked")
            if rec["status"] is None:
                self._refresh(rec)
            return
        rec["status"], rec["phase"] = "REJECTED", "final"

    def _handle_reject(self, rec, e, attempt):
        """→ True 表示应重试（已修正参数 / 对时），False 表示已终局。"""
        if e.kind == "DUP_CID":                      # 服务端已有这张单（上次其实成功了）→ 按 clientOrderId 认领
            self._refresh(rec)
            return False
        if e.kind == "BAD_TIMESTAMP":
            self.ad.sync_time()
            return True
        if e.kind in ("SERVER_ERROR", "MAINTENANCE"):
            time.sleep(0.05 * (attempt + 1))
            return True
        if e.kind in ("BAD_PRICE", "BAD_QTY", "BAD_PARAM") and attempt < 2 and self._requantize(rec):
            return True                              # 精度 / 参数错：按最新交易对规则重新归整，有变化才重发
        self.note("rejected", intent=rec["local_id"], kind=e.kind, code=e.code)
        rec["status"], rec["phase"], rec["reject_kind"] = "REJECTED", "final", e.kind
        return False

    def _requantize(self, rec):
        """刷新交易对规则并重新归整价格 / 数量；→ 是否有变化。"""
        for s in self._call(self.ad.instruments):
            self.specs[s["symbol"]] = s
        spec = self.specs.get(rec["symbol"])
        if not spec:
            return False
        before = (rec["price"], rec["qty"])
        if rec["price"] is not None and spec["tick"]:
            rec["price"] = common.round_step(rec["price"], spec["tick"])
        if spec["step"]:
            rec["qty"] = max(spec["min_qty"], common.round_step(rec["qty"], spec["step"]))
        changed = (rec["price"], rec["qty"]) != before
        self.note("requantized", intent=rec["local_id"], price=rec["price"], qty=rec["qty"], changed=changed)
        return changed

    # ---- 撤单 ----
    def cancel(self, local_id):
        rec = self.orders.get(local_id)
        if rec is None:
            raise KeyError(local_id)
        for attempt in range(self.MAX_RETRY):
            if rec["status"] in FINAL:
                break
            try:
                self._call(self.ad.cancel, rec["symbol"], order_id=rec["server_id"], cid=None if rec["server_id"] else rec["cid"])
            except UnknownResult:
                pass
            except ExchangeError as e:
                if e.kind not in ("ORDER_NOT_FOUND", "ORDER_NOT_OPEN"):
                    raise
            self._refresh(rec)                      # 以查单结果为准：可能已撤、也可能撤之前已经全成
            if rec["status"] in FINAL:
                break
            time.sleep(0.02)
        self._save()

    # ---- 持仓 / 余额 ----
    def settle(self):
        for rec in self.orders.values():
            if rec["status"] not in FINAL and rec["cid"]:
                self._refresh(rec)
        self._save()

    def positions(self):
        self.settle()
        ledger = {}
        for r in self.orders.values():
            if r["status"] in ("NOT_SENT", "REJECTED") or not r["filled"]:
                continue
            sign = 1.0 if r["side"] == "BUY" else -1.0
            ledger[r["symbol"]] = round(ledger.get(r["symbol"], 0.0) + sign * r["filled"], 10)
        server = self._stable_positions()
        if server is not None and any(abs(server.get(s, 0.0) - ledger.get(s, 0.0)) > 1e-9 for s in set(server) | set(ledger)):
            self.note("position_adopted_from_server", ledger=ledger, server=server)
            ledger = server
        self.ledger = ledger
        return {k: v for k, v in ledger.items() if abs(v) > 1e-12}

    def _stable_positions(self, reads=3, gap_s=0.25):
        """连续 reads 次读到相同的服务端持仓才算数（跨过“短时返回旧值”的窗口）；读不稳就返回 None（以自己的账本为准）。"""
        seen = []
        for i in range(reads):
            try:
                seen.append({k: v for k, v in self._call(self.ad.positions).items() if abs(v) > 1e-12})
            except (ExchangeError, UnknownResult, RuntimeError) as e:
                self.note("positions_read_failed", error=repr(e))
                return None
            if i < reads - 1:
                time.sleep(gap_s)
        return seen[-1] if all(s == seen[-1] for s in seen) else None

    def balance(self):
        last = None
        for _ in range(4):
            w = self._call(self.ad.account)["wallet"]
            if last is not None and abs(w - last) < 1e-9:
                return w
            last = w
            time.sleep(0.02)
        return last

    # ---- 重启 / 急停 ----
    def restart(self):
        self.orders, self.ledger, self.killed = {}, {}, False
        self._load_journal()
        self.ad.sync_time()
        for rec in list(self.orders.values()):
            if rec["status"] in FINAL:
                continue
            if not self._refresh(rec):
                if rec["phase"] in ("pending", "sent"):   # 发出去之前 / 没等到回包就挂了，服务端没有 → 用同一 clientOrderId 补发
                    self.note("resend_after_restart", intent=rec["local_id"])
                    self._send(rec)
                else:
                    rec["status"], rec["phase"] = "NOT_SENT", "final"
        self._save()

    def kill_switch(self, rounds=30):
        self.killed = True
        for _ in range(rounds):
            try:
                open_ = self._call(self.ad.open_orders)
            except (ExchangeError, UnknownResult):
                time.sleep(0.05)
                continue
            if not open_:
                break
            for o in open_:
                try:
                    self._call(self.ad.cancel, o["symbol"], order_id=o["id"])
                except (ExchangeError, UnknownResult):
                    pass
            time.sleep(0.02)
        self.settle()


CLIENTS = {"naive": NaiveClient, "correct": CorrectClient}
