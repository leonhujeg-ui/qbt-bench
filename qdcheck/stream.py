"""随机订单流生成器 + 账本对拍运行器。

每条流：合成 K 线（synth，带停牌 / 缺口 / 跳跃）+ 一个小撮合器：
- 下单：市价 / 限价，开仓 / 加仓 / 减仓 / 平仓 / 反手，按权益 * 杠杆 * 比例定整数数量；
- 撤单：每根以固定概率撤掉挂单，另有到期失效；
- 部分成交：每根成交量 * 参与率 是本根所有订单共享的上限，大单跨多根成交；停牌（量 = 0）不成交；
- 保证金：加仓部分按可用保证金缩量，缩到 0 则拒单（rejected_margin）；
- 跨日结算：每 bars_per_day 根在收盘处记资金费（多头付正费率）与 / 或 settlement（浮盈转已实现、均价重置）；
- 强平：收盘时 仓位权益 <= 维持保证金 即以收盘价全平，强平费封顶在剩余仓位权益，撤掉全部挂单。
撮合器用 ledger.Ledger 增量维护状态做决策；对拍时用全新的 ledger.run 与独立的 ledger_alt.run 各重算一遍。

    python3.12 -m qdcheck.stream --streams 200 --bars 3000 --out qdcheck/results/g3_streams.json

只用 random.random() 与四则运算：同 seed 同结果。所有数据都是合成的，不构成任何交易结论。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time

from qdcheck import ledger, ledger_alt, synth

SIM_DEFAULTS = {"p_order": 0.15, "p_cancel": 0.03, "participation": 0.1, "slippage": 0.0005, "taker": 0.0005,
                "maker": 0.0002, "liq_fee": 0.005, "bars_per_day": 96, "funding": True, "settlement": False,
                "funding_rate_max": 0.0005, "size_max": 0.6, "limit_band": 0.01, "expire_max": 40}


def profile(seed):
    """按 seed 抽一条流的账本配置与撮合参数。"""
    rnd = random.Random(f"qdcheck-stream-profile:{seed}")
    mode = rnd.choice(("isolated", "isolated", "isolated", "cross", "none"))
    lev = {"isolated": rnd.choice((1, 2, 3, 5, 10, 20)), "cross": rnd.choice((1, 2, 3, 5)), "none": 1}[mode]
    cfg = {"initial_cash": rnd.choice((10_000.0, 100_000.0)), "margin_mode": mode, "leverage": lev,
           "mmr": rnd.choice((0.005, 0.01, 0.025))}
    sim = dict(SIM_DEFAULTS, p_order=rnd.choice((0.05, 0.15, 0.3)), participation=rnd.choice((0.02, 0.1, 0.3)),
               bars_per_day=rnd.choice((48, 96, 288)), funding=rnd.random() < 0.8, settlement=rnd.random() < 0.4,
               size_max=rnd.choice((0.3, 0.6, 0.95)), maker=rnd.choice((0.0002, -0.0001)))
    data_kw = {"gap_prob": rnd.choice((0.002, 0.01)), "jump_prob": rnd.choice((0.002, 0.01, 0.02)),
               "base_volume": 20_000.0}
    return cfg, sim, data_kw


class _Order:
    __slots__ = ("oid", "side", "remaining", "otype", "limit", "created", "expire")

    def __init__(self, oid, side, qty, otype, limit, created, expire):
        self.oid, self.side, self.remaining, self.otype, self.limit = oid, side, qty, otype, limit
        self.created, self.expire = created, expire


class _Sim:
    def __init__(self, seed, n_bars, cfg, sim, data_kw):
        self.rnd = random.Random(f"qdcheck-stream:{seed}").random
        self.cfg, self.sim = cfg, sim
        self.bars = synth.ohlcv(synth.generate(n_bars, seed, **data_kw))
        self.led = ledger.Ledger(**cfg)
        self.orders, self.records, self.next_id = [], [], 1
        self.stats = {k: 0 for k in ("submitted", "market", "limit", "trades", "partial_fills", "cancelled_user",
                                     "expired", "rejected_margin", "cancelled_by_liquidation", "funding_records",
                                     "settlements", "liquidations")}
        self.funding_rate = 0.0

    # ---- 撮合 -----------------------------------------------------------------
    def _fill_price(self, order, o, h, lo):
        if order.otype == "market":
            return o * (1.0 + order.side * self.sim["slippage"])
        if order.side > 0 and lo <= order.limit:
            return min(o, order.limit)
        if order.side < 0 and h >= order.limit:
            return max(o, order.limit)
        return None

    def _affordable(self, side, q, p, fee_rate):
        """加仓部分按可用保证金缩量；减仓部分不受限。"""
        led, mode = self.led, self.cfg["margin_mode"]
        Q = led.qty
        inc = q if Q == 0 or (Q > 0) == (side > 0) else max(0, q - abs(Q))
        if inc == 0:
            return q
        equity = led.cash + Q * p
        if mode == "isolated":                     # 新保证金只能来自钱包的可用部分（不含浮盈）
            avail = led.wallet() - led.margin - abs(q - inc) * p * abs(fee_rate)
            per_unit = p / self.cfg["leverage"] + p * abs(fee_rate)
            max_inc = int(max(0.0, avail) / per_unit)
        else:
            lev = self.cfg["leverage"] if mode == "cross" else 3.0
            base = abs(Q) if Q == 0 or (Q > 0) == (side > 0) else 0      # 反手时原仓位先全部平掉
            room = max(0.0, (equity - q * p * abs(fee_rate)) * lev - base * p)
            max_inc = int(room / p)
        return (q - inc) + min(inc, max_inc)

    def _record(self, t, kind, side=0, qty=0, price=None, fee=0.0, funding=0.0, oid=None):
        rec = {"bar": t, "kind": kind, "side": {1: "buy", -1: "sell", 0: "none"}[side], "qty": qty, "price": price,
               "fee": fee, "funding": funding}
        if oid is not None:
            rec["order_id"] = oid
        self.led.apply(rec)
        self.records.append(rec)

    def _match(self, t):
        o, h, lo, v = (self.bars[k][t] for k in ("open", "high", "low", "volume"))
        cap = int(v * self.sim["participation"])
        for order in list(self.orders):
            if order.expire < t:
                self.orders.remove(order)
                self.stats["expired"] += 1
                continue
            price = self._fill_price(order, o, h, lo)
            if price is None or cap <= 0:
                continue
            rate = self.sim["taker"] if order.otype == "market" else self.sim["maker"]
            q = self._affordable(order.side, min(order.remaining, cap), price, rate)
            if q <= 0:
                self.orders.remove(order)
                self.stats["rejected_margin"] += 1
                continue
            self._record(t, "trade", order.side, q, price, rate * q * price, oid=order.oid)
            cap -= q
            order.remaining -= q
            self.stats["trades"] += 1
            if order.remaining > 0:
                self.stats["partial_fills"] += 1
            else:
                self.orders.remove(order)

    def _day_end(self, t, close):
        sim, Q = self.sim, self.led.qty
        if Q != 0 and sim["settlement"]:
            self._record(t, "settlement", price=close)
            self.stats["settlements"] += 1
        if Q != 0 and sim["funding"]:
            self._record(t, "funding", funding=self.funding_rate * Q * close)
            self.stats["funding_records"] += 1
        self.funding_rate = (2.0 * self.rnd() - 1.0) * sim["funding_rate_max"]

    def _liquidation_check(self, t, close):
        led = self.led
        if self.cfg["margin_mode"] == "none" or led.qty == 0:
            return
        pe, mm = led.position_equity(close), led.maint_margin(close)
        if pe > mm:
            return
        q, side = abs(led.qty), (-1 if led.qty > 0 else 1)
        fee = min(self.sim["liq_fee"] * q * close, max(0.0, pe))
        self._record(t, "liquidation", side, q, close, fee)
        self.stats["liquidations"] += 1
        self.stats["cancelled_by_liquidation"] += len(self.orders)
        self.orders.clear()

    # ---- 决策 -----------------------------------------------------------------
    def _new_orders(self, t, close):
        rnd, sim, led = self.rnd, self.sim, self.led
        for order in list(self.orders):
            if rnd() < sim["p_cancel"]:
                self.orders.remove(order)
                self.stats["cancelled_user"] += 1
        if rnd() >= sim["p_order"] or self.bars["volume"][t] == 0:
            return
        Q, equity = led.qty, led.cash + led.qty * close
        lev = self.cfg["leverage"] if self.cfg["margin_mode"] != "none" else 2.0
        size = max(1, int(max(0.0, equity) * lev * (0.02 + rnd() * sim["size_max"]) / close))
        u = rnd()
        if Q == 0 or u < 0.45:                                    # 开仓 / 加仓
            side = (1 if rnd() < 0.5 else -1) if Q == 0 else (1 if Q > 0 else -1)
            qty = size
        elif u < 0.65:                                            # 减仓
            side, qty = (-1 if Q > 0 else 1), max(1, int(abs(Q) * rnd()))
        elif u < 0.8:                                             # 平仓
            side, qty = (-1 if Q > 0 else 1), abs(Q)
        else:                                                     # 反手
            side, qty = (-1 if Q > 0 else 1), abs(Q) + size
        if rnd() < 0.5:
            otype, limit = "market", None
        else:
            otype = "limit"
            limit = close * (1.0 - side * sim["limit_band"] * rnd())
        self.orders.append(_Order(self.next_id, side, qty, otype, limit, t,
                                  t + 1 + int(rnd() * sim["expire_max"])))
        self.next_id += 1
        self.stats["submitted"] += 1
        self.stats[otype] += 1

    def run(self):
        closes = self.bars["close"]
        bpd = self.sim["bars_per_day"]
        for t, close in enumerate(closes):
            self._match(t)
            if (t + 1) % bpd == 0:
                self._day_end(t, close)
            self._liquidation_check(t, close)
            self.led.mark(t, close)
            self._new_orders(t, close)
        return self


def generate(seed, n_bars=3000, cfg=None, sim=None, data_kw=None):
    """一条随机流：{"seed", "config"(账本配置), "sim", "closes", "records", "stats", "counters"}。"""
    p_cfg, p_sim, p_data = profile(seed)
    s = _Sim(seed, n_bars, cfg or p_cfg, dict(p_sim, **(sim or {})), data_kw or p_data).run()
    return {"seed": seed, "n_bars": n_bars, "config": s.cfg, "sim": s.sim, "closes": list(s.bars["close"]),
            "records": s.records, "stats": s.stats, "counters": dict(s.led.counters),
            "sim_violations": list(s.led.violations)}


# ---- 脚本化场景（ledger 向量用） ------------------------------------------------------
def scripted(cfg, closes, script, liq_fee=0.005):
    """script = {bar: [record, ...]}（不含强平）；每根收盘按与随机流相同的规则自动插入强平记录。"""
    led = ledger.Ledger(**cfg)
    records = []
    for t, close in enumerate(closes):
        for rec in script.get(t, ()):
            rec = dict({"bar": t, "kind": "trade", "side": "none", "qty": 0, "price": None, "fee": 0.0,
                        "funding": 0.0}, **rec)
            led.apply(rec)
            records.append(rec)
        if cfg["margin_mode"] != "none" and led.qty != 0:
            pe, mm = led.position_equity(close), led.maint_margin(close)
            if pe <= mm:
                q = abs(led.qty)
                rec = {"bar": t, "kind": "liquidation", "side": "sell" if led.qty > 0 else "buy", "qty": q,
                       "price": close, "fee": min(liq_fee * q * close, max(0.0, pe)), "funding": 0.0}
                led.apply(rec)
                records.append(rec)
        led.mark(t, close)
    return records


def _t(side, qty, price, fee=0.0):
    return {"kind": "trade", "side": side, "qty": qty, "price": price, "fee": fee}


def ledger_scenarios():
    """[(name, 描述, config, closes, records)]：4 个手写场景 + 3 段随机流。"""
    none = {"initial_cash": 100_000.0, "margin_mode": "none", "leverage": 1, "mmr": 0.005}
    iso = {"initial_cash": 10_000.0, "margin_mode": "isolated", "leverage": 20, "mmr": 0.005}
    cross = {"initial_cash": 10_000.0, "margin_mode": "cross", "leverage": 3, "mmr": 0.05}
    out = [
        ("long_add_partial_close", "多头开仓 -> 加仓 -> 部分平仓 -> 全平，带手续费", none,
         [101.0, 112.0, 118.0, 95.0, 96.0, 97.0],
         {0: [_t("buy", 10, 100.0, 0.5)], 1: [_t("buy", 5, 110.0, 0.3)], 2: [_t("sell", 8, 120.0, 0.4)],
          3: [_t("sell", 7, 94.0, 0.3)]}),
        ("short_reverse_funding_settlement", "空头 -> 资金费（空头收取）-> 跨日结算 -> 反手做多 -> 平仓", none,
         [99.0, 98.0, 97.5, 96.0, 95.5, 99.5, 99.0],
         {0: [_t("sell", 10, 100.0, 0.5)], 2: [{"kind": "funding", "funding": -1.25}],
          3: [{"kind": "settlement", "price": 96.0}, {"kind": "funding", "funding": -0.75}],
          4: [_t("buy", 15, 95.0, 0.7)], 5: [_t("sell", 5, 99.0, 0.25)]}),
        ("isolated_x20_liquidations", "逐仓 20 倍：跳空穿仓（保险基金补足）与普通强平各一次", iso,
         [100.0, 99.0, 96.0, 92.0, 93.0, 95.0, 97.5, 97.0],
         {0: [_t("buy", 1000, 100.0, 50.0)], 4: [_t("sell", 500, 93.0, 23.25)]}),
        ("cross_x3_liquidation", "全仓 3 倍：权益 <= 5% 名义额时强平", cross,
         [100.0, 90.0, 80.0, 72.0, 68.0, 70.0],
         {0: [_t("buy", 290, 100.0, 14.5)]}),
    ]
    out = [(n, d, c, closes, scripted(c, closes, sc)) for n, d, c, closes, sc in out]
    streams = [
        ("stream_isolated_x20", "随机流：逐仓 20 倍，含强平", 1001,
         {"initial_cash": 10_000.0, "margin_mode": "isolated", "leverage": 20, "mmr": 0.01},
         {"p_order": 0.3, "size_max": 0.95, "settlement": False}),
        ("stream_none_settlement", "随机流：无保证金，含跨日结算与资金费", 1002,
         {"initial_cash": 100_000.0, "margin_mode": "none", "leverage": 1, "mmr": 0.005},
         {"p_order": 0.3, "settlement": True, "funding": True, "bars_per_day": 48}),
        ("stream_cross_x5", "随机流：全仓 5 倍", 1003,
         {"initial_cash": 100_000.0, "margin_mode": "cross", "leverage": 5, "mmr": 0.025},
         {"p_order": 0.3, "size_max": 0.95}),
    ]
    for name, desc, seed, cfg, sim in streams:
        st = generate(seed, 400, cfg=cfg, sim=sim, data_kw={"gap_prob": 0.01, "jump_prob": 0.02,
                                                            "base_volume": 20_000.0})
        out.append((name, desc, cfg, st["closes"], st["records"]))
    return out


# ---- 对拍 -------------------------------------------------------------------------
_PRICE_FIELDS = ("close", "avg_price", "liq_price", "qty")      # 只用相对容差的字段（价格与浮点数量）


def _close(a, b, rel_tol, scale):
    return abs(a - b) <= rel_tol * max(abs(a), abs(b), scale)


def compare_rows(rows_a, rows_b, initial_cash, rel_tol=1e-9):
    """逐根逐字段：整数字段（bar / fills / liquidations，及两边都是整数的 qty）完全相等；
    None 位置一致；价格与浮点数量只用相对容差；金额字段容差以 max(|a|, |b|, 初始资金, |持仓市值|) 为尺度。"""
    if len(rows_a) != len(rows_b):
        return {"ok": False, "bar": None, "field": "length", "a": len(rows_a), "b": len(rows_b)}
    checked = 0
    for ra, rb in zip(rows_a, rows_b):
        money_scale = max(1.0, abs(initial_cash), abs(ra["market_value"]), abs(rb["market_value"]))
        for f in ledger.ROW_FIELDS:
            a, b = ra[f], rb[f]
            checked += 1
            if f in ledger.INT_FIELDS or (f == "qty" and isinstance(a, int) and isinstance(b, int)):
                ok = a == b and type(a) is type(b)
            elif a is None or b is None:
                ok = a is None and b is None
            else:
                ok = _close(a, b, rel_tol, 0.0 if f in _PRICE_FIELDS else money_scale)
            if not ok:
                return {"ok": False, "bar": ra["bar"], "field": f, "a": a, "b": b, "checked": checked}
    return {"ok": True, "checked": checked}


def check_stream(st, rel_tol=1e-9):
    ref = ledger.run(st["records"], st["closes"], **st["config"], rel_tol=rel_tol)
    alt = ledger_alt.run(st["records"], st["closes"], **st["config"], rel_tol=rel_tol)
    cmp = compare_rows(ref["rows"], alt["rows"], st["config"]["initial_cash"], rel_tol)
    return {"seed": st["seed"], "bars": len(st["closes"]), "records": len(st["records"]),
            "violations": ref["violations"][:5], "n_violations": len(ref["violations"]),
            "alt_issues": alt["issues"][:5], "n_alt_issues": len(alt["issues"]), "row_compare": cmp,
            "final_equity": ref["rows"][-1]["equity"] if ref["rows"] else None, "warnings": len(ref["warnings"]),
            "config": st["config"], "stats": st["stats"], "counters": ref["counters"]}


def run_many(n_streams=200, n_bars=3000, seed0=0):
    t0 = time.perf_counter()
    results = []
    for k in range(n_streams):
        st = generate(seed0 + k, n_bars)
        res = check_stream(st)
        res["sim_violations"] = len(st["sim_violations"])
        results.append(res)
    elapsed = time.perf_counter() - t0
    agg_stats, agg_counters = {}, {}
    for r in results:
        for k, v in r["stats"].items():
            agg_stats[k] = agg_stats.get(k, 0) + v
        for k, v in r["counters"].items():
            agg_counters[k] = agg_counters.get(k, 0) + v
    modes = {}
    for r in results:
        key = f"{r['config']['margin_mode']}x{r['config']['leverage']}"
        modes[key] = modes.get(key, 0) + 1
    return {"n_streams": n_streams, "n_bars": n_bars, "seed0": seed0, "seconds": round(elapsed, 2),
            "total_bars": sum(r["bars"] for r in results), "total_records": sum(r["records"] for r in results),
            "identity_violations": sum(r["n_violations"] for r in results),
            "alt_issues": sum(r["n_alt_issues"] for r in results),
            "row_mismatches": sum(not r["row_compare"]["ok"] for r in results),
            "values_compared": sum(r["row_compare"].get("checked", 0) for r in results),
            "sim_violations": sum(r["sim_violations"] for r in results),
            "warnings_bankrupt_cross": sum(r["warnings"] for r in results),
            "stats": agg_stats, "counters": agg_counters, "modes": dict(sorted(modes.items())),
            "failures": [r for r in results if r["n_violations"] or r["n_alt_issues"] or not r["row_compare"]["ok"]][:10],
            "per_stream": [{k: r[k] for k in ("seed", "records", "final_equity", "n_violations", "n_alt_issues")}
                           | {"mode": r["config"]["margin_mode"], "lev": r["config"]["leverage"],
                              "liq": r["counters"]["liquidation"], "capped": r["counters"]["bankrupt_capped"]}
                           for r in results]}


def build_parser():
    ap = argparse.ArgumentParser(description="随机订单流 x 账本对拍")
    ap.add_argument("--streams", type=int, default=200)
    ap.add_argument("--bars", type=int, default=3000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default=None)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    rep = run_many(args.streams, args.bars, args.seed)
    print(json.dumps({k: rep[k] for k in ("n_streams", "n_bars", "seconds", "total_bars", "total_records",
                                          "identity_violations", "alt_issues", "row_mismatches", "values_compared",
                                          "counters", "stats")}, sort_keys=True))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(dict(rep, command="python3.12 -m qdcheck.stream " + " ".join(argv or sys.argv[1:])), f,
                      indent=1, sort_keys=True)
    bad = rep["identity_violations"] or rep["alt_issues"] or rep["row_mismatches"]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
