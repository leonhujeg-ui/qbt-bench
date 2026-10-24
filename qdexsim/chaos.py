"""混沌注入：按场景文件（JSON）或运行时 /__chaos 管理口配置，每条规则按种子可完全复现，每次注入写一行事件日志。

配置::

    {"seed": 7, "clock_drift_ms": 0, "log": null,
     "rules": [
       {"kind": "unknown_result", "op": "place", "p": 1.0, "max": 1, "mode": "timeout", "hold_ms": 1500},
       {"kind": "latency", "op": "*", "ms": 30, "jitter_ms": 20},
       {"kind": "rate_limit", "op": ["place", "cancel"], "p": 0.3, "retry_after_ms": 800},
       {"kind": "server_error", "op": "*", "p": 0.05, "status": 500},
       {"kind": "maintenance", "op": "*", "from_req": 20, "max": 3},
       {"kind": "cancel_race", "op": "cancel", "p": 1.0, "max": 1},
       {"kind": "fill_delay", "ms": 800},
       {"kind": "stale", "op": "positions", "ms": 1000},
       {"kind": "zero_once", "op": "account", "max": 1},
       {"kind": "symbol", "at_req": 5, "symbol": "BTCUSDT", "changes": {"status": "SUSPENDED"}},
       {"kind": "clock_drift", "at_req": 3, "ms": 20000}]}

规则公共字段：op（规范操作名 / 列表 / "*"，缺省 "*"）、dialect（名 / "*"）、p（命中概率，缺省 1）、max（最多命中次数）、
from_req（第几个请求起生效，按全局请求序号）、at_req（恰好在第几个请求触发，用于 symbol / clock_drift）。
概率全部来自 random.Random(seed)，请求序列相同 → 注入序列相同。

种类：
- unknown_result：服务端**已执行**，但不给客户端正常回包。mode = timeout（握住连接 hold_ms 后断开）/ drop（立即断开）/
  502（回一个网关形状的 502，不是交易所报文）；
- latency：固定 + 均匀抖动延迟；rate_limit：按各家形状回限频（带各家重试头），之后 retry_after_ms 内再来的请求记一次
  violation（判据“限频守规矩”看这个）；server_error：5xx；maintenance：各家“维护中”报文；
- cancel_race：撤单到达前把这张单剩余部分成交掉（客户端拿到的是“单不存在 / 已成”的报文，持仓却变了）；
- fill_delay：成交先改持仓、ms 后才出现在成交明细（最终一致）；stale：持仓查询在最近一次变化后的 ms 内返回旧值；
  zero_once：余额查询返回 0（max 次）；symbol：下架 / 暂停 / 改精度；clock_drift：服务端时间快 / 慢 ms。
"""
import json
import os
import random
import threading
import time

from qdexsim import common

REJECT_KINDS = {"rate_limit": "RATE_LIMIT", "server_error": "SERVER_ERROR", "maintenance": "MAINTENANCE"}
VIEW_KINDS = ("stale", "zero_once")
SCHEDULED_KINDS = ("symbol", "clock_drift")


class Decision:
    """一次请求上的注入结果（服务端按它执行）。"""

    def __init__(self):
        self.delay_ms = 0
        self.reject = None              # {"kind": "RATE_LIMIT"|..., "status": int|None, "retry_after_ms": int}
        self.unknown = None             # {"mode": "timeout"|"drop"|"502", "hold_ms": int}
        self.cancel_race = False
        self.stale_ms = 0
        self.zero_once = False
        self.fired = []                 # [(rule_index, rule)]

    def summary(self):
        return {"delay_ms": self.delay_ms, "reject": self.reject, "unknown": self.unknown, "cancel_race": self.cancel_race,
                "stale_ms": self.stale_ms, "zero_once": self.zero_once, "fired": [i for i, _ in self.fired]}


class Chaos:
    def __init__(self, cfg=None, store=None, log_path=None):
        self.store = store
        self.lock = threading.RLock()
        self.log_path = log_path
        self.violations = []
        self.limited_until = {}         # dialect → 真实时钟 ms
        self.events_written = 0
        self.configure(cfg or {})

    # ---- 配置 ----
    def configure(self, cfg):
        with self.lock:
            self.cfg = {"seed": int(cfg.get("seed", 0)), "clock_drift_ms": int(cfg.get("clock_drift_ms", 0)),
                        "rules": [dict(r) for r in cfg.get("rules", [])]}
            if cfg.get("log"):
                self.log_path = cfg["log"]
            self.rng = random.Random(self.cfg["seed"])
            self.counts = [0] * len(self.cfg["rules"])
            self.req_index = 0
            self.drift_ms = self.cfg["clock_drift_ms"]
            self._validate()

    def _validate(self):
        for r in self.cfg["rules"]:
            if r.get("kind") not in set(REJECT_KINDS) | set(VIEW_KINDS) | set(SCHEDULED_KINDS) | {
                    "unknown_result", "latency", "cancel_race", "fill_delay"}:
                raise ValueError(f"unknown chaos kind: {r.get('kind')!r}")
            if r["kind"] == "unknown_result" and r.get("mode", "timeout") not in ("timeout", "drop", "502"):
                raise ValueError(f"unknown_result mode {r.get('mode')!r}")

    def update(self, patch):
        """运行时改配置：{"rules": [...]} 整体替换规则，{"add": {...}} 追加一条，{"clock_drift_ms": n} 改漂移，{"seed": n} 重置随机序列。"""
        with self.lock:
            cfg = dict(self.cfg)
            if "rules" in patch:
                cfg["rules"] = list(patch["rules"])
            if "add" in patch:
                cfg["rules"] = cfg["rules"] + [patch["add"]]
            if "clock_drift_ms" in patch:
                cfg["clock_drift_ms"] = int(patch["clock_drift_ms"])
            if "seed" in patch:
                cfg["seed"] = int(patch["seed"])
            self.configure(cfg)
            return self.status()

    def status(self):
        with self.lock:
            return {"config": self.cfg, "counts": list(self.counts), "req_index": self.req_index, "drift_ms": self.drift_ms,
                    "violations": list(self.violations), "events_written": self.events_written}

    # ---- 匹配 ----
    def _matches(self, i, r, op, dialect):
        ops = r.get("op", "*")
        ops = [ops] if isinstance(ops, str) else list(ops)
        if "*" not in ops and op not in ops:
            return False
        if r.get("dialect", "*") not in ("*", dialect):
            return False
        if self.req_index < int(r.get("from_req", 0)):
            return False
        if "at_req" in r and self.req_index != int(r["at_req"]):
            return False
        if "max" in r and self.counts[i] >= int(r["max"]):
            return False
        return self.rng.random() < float(r.get("p", 1.0))

    def before(self, op, dialect):
        """请求进入时：按规则顺序逐条判定，返回 Decision；同一请求只取第一条“拒绝”和第一条“结果未知”。"""
        with self.lock:
            self.req_index += 1
            d = Decision()
            self._check_violation(op, dialect)
            for i, r in enumerate(self.cfg["rules"]):
                if r["kind"] in SCHEDULED_KINDS or r["kind"] == "fill_delay":
                    continue
                if not self._matches(i, r, op, dialect):
                    continue
                if not self._apply(d, r):
                    continue
                self.counts[i] += 1
                d.fired.append((i, r))
            return d

    def _apply(self, d, r):
        k = r["kind"]
        if k == "latency":
            d.delay_ms += int(r.get("ms", 0)) + (self.rng.randint(0, int(r["jitter_ms"])) if r.get("jitter_ms") else 0)
        elif k in REJECT_KINDS:
            if d.reject is not None:
                return False
            d.reject = {"kind": REJECT_KINDS[k], "status": r.get("status"), "retry_after_ms": int(r.get("retry_after_ms", 1000))}
        elif k == "unknown_result":
            if d.unknown is not None:
                return False
            d.unknown = {"mode": r.get("mode", "timeout"), "hold_ms": int(r.get("hold_ms", 1500))}
        elif k == "cancel_race":
            d.cancel_race = True
        elif k == "stale":
            d.stale_ms = max(d.stale_ms, int(r.get("ms", 1000)))
        elif k == "zero_once":
            d.zero_once = True
        return True

    def scheduled(self):
        """当前请求序号上到点的 symbol / clock_drift 规则（服务端在 before 之后调用并执行）。"""
        with self.lock:
            out = []
            for i, r in enumerate(self.cfg["rules"]):
                if r["kind"] in SCHEDULED_KINDS and int(r.get("at_req", -1)) == self.req_index and self.counts[i] == 0:
                    self.counts[i] += 1
                    out.append(r)
                    if r["kind"] == "clock_drift":
                        self.drift_ms = int(r["ms"])
            return out

    def fill_delay_ms(self):
        with self.lock:
            return max([int(r.get("ms", 0)) for r in self.cfg["rules"] if r["kind"] == "fill_delay"] or [0])

    # ---- 限频守规矩 ----
    def _check_violation(self, op, dialect):
        until = self.limited_until.get(dialect)
        now = common.now_ms()
        if until is not None and now < until:
            self.violations.append({"dialect": dialect, "op": op, "early_by_ms": until - now, "req_index": self.req_index})

    def note_rate_limited(self, dialect, retry_after_ms):
        with self.lock:
            self.limited_until[dialect] = common.now_ms() + int(retry_after_ms)

    # ---- 事件日志 ----
    def log(self, op, dialect, decision, executed, now_ms):
        """每条命中的规则写一行：时间、接口、注入类型、真实执行了什么。"""
        with self.lock:
            for i, r in decision.fired:
                doc = {"req_index": self.req_index, "op": op, "dialect": dialect, "rule": i, "injection": r["kind"],
                       "detail": {k: v for k, v in r.items() if k != "kind"}, "executed": executed}
                self._write(now_ms, doc)

    def log_scheduled(self, rule, executed, now_ms):
        with self.lock:
            self._write(now_ms, {"req_index": self.req_index, "op": "-", "dialect": "-", "injection": rule["kind"],
                                 "detail": {k: v for k, v in rule.items() if k != "kind"}, "executed": executed})

    def _write(self, now_ms, doc):
        self.events_written += 1
        if self.store is not None:
            self.store.add_event(now_ms, "chaos", doc)
        if self.log_path:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(dict(doc, ts=now_ms, wall=time.strftime("%Y-%m-%dT%H:%M:%S")), ensure_ascii=False) + "\n")


def load(path):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    if "log" in cfg and cfg["log"] and not os.path.isabs(cfg["log"]):
        cfg["log"] = os.path.join(os.path.dirname(os.path.abspath(path)), cfg["log"])
    return cfg
