# qdexsim 接口契约

## 1. 命令行

| 命令 | 作用 | 退出码 |
|---|---|---|
| `python3.12 -m qdexsim.selftest [--quick\|--full]` | 全部自检，最后总表 | 0 全部通过 / 1 任一步 FAIL |
| `python3.12 -m qdexsim.server --port P --db FILE [--config cfg.json] [--chaos chaos.json] [--chaos-log x.jsonl]` | 起仿真交易所，stdout 第一行 `QDEXSIM LISTENING http://127.0.0.1:<port>` | 常驻；Ctrl-C / SIGTERM 退出 0 |
| `python3.12 -m qdexsim.recon --local L --remote R [--map m.json] [--remote-map rm.json] [--ts-tol-ms 5000] [--out rep.json]` | 对账，stdout 文本表 | 0 无差异 / 1 有差异 / 2 参数错 |
| `python3.12 -m qdexsim.recon --selfcheck` | 合成数据 50 条差异自检 | 0 全找出且零误报 / 1 |
| `python3.12 -m qdexsim.tools.selfcheck g1\|g2\|g3\|g4\|knives` | 单独重跑某个目标的实测，写 `qdexsim/results/<step>.json` | 0 / 1 |
| `python3.12 qdexsim/tools/cov.py --fail-under 90 [--json out.json]` | 语句覆盖率（含子进程合并） | 0 达标 / 1 |
| `python3.12 qdexsim/mutants/run.py --per-module 30 --jobs 4` | 突变测试 → `qdexsim/results/mutation.json`、`mutation_survivors.md` | 0 |
| `python3.12 qdexsim/mutants/triage.py [--check]` | 由结果与 `triage_reasons.py` 生成 `mutants/triage.md` | 0 / 1（--check 不一致） |
| `python3.12 qdexsim/scenarios/gen.py [--check]` | 重新生成 / 校验 60 个场景文件 | 0 / 1 |

## 2. 服务端配置（`--config`）

```json
{"engine": {"wallet": 10000.0, "maker_fee": 0.0002, "taker_fee": 0.0005, "funding_rate": 0.0001, "mmr": 0.005,
            "leverage": 10, "margin_mode": "cross", "start": {"BTCUSDT": 60000.0, "ETHUSDT": 3000.0}},
 "symbols": {"BTCUSDT": {"symbol": "BTCUSDT", "base": "BTC", "quote": "USDT", "tick": 0.1, "step": 0.001, "min_qty": 0.001,
                         "min_notional": 5.0, "max_leverage": 125, "status": "TRADING", "ct_val": 0.01, "price_places": 1,
                         "qty_places": 3, "liquidity": 0.05}},
 "feed": {"mode": "random_walk|replay|fixed", "seed": 1, "interval_ms": 20, "vol": 0.0005, "drift": 0.0,
          "gaps": [{"at_tick": 50, "symbol": "BTCUSDT", "pct": -0.03}], "path": "ticks.csv", "loop": true},
 "chaos": {"...见 chaos.py..."},
 "creds": {"binance": {"api_key": "k", "secret": "s"}, "okx": {"api_key": "k", "secret": "s", "passphrase": "p"}}}
```

缺省凭据（`dialects/<name>.py` 的 `DEFAULT_CREDS`）：`<name>-key` / `<name>-secret` / `<name>-pass`（OKX、Bitget 有 passphrase）。

## 3. 管理口（只在仿真器上有，不是任何交易所的接口）

| 方法 路径 | 入 / 出 |
|---|---|
| GET `/__admin/state` | `engine.snapshot()`：`orders[]`、`fills[]`、`positions[]`、`account{}`、`mark{}`、`symbols{}`、`counts{}`、`time`、`real_time`、`chaos{}` |
| GET `/__admin/events?since=<seq>[&kind=chaos]` | 事件日志（订单事件 + 注入事件），每条带 `seq`、`ts`、`kind` |
| POST `/__admin/tick` | `{"n": k}` 推进 k 个 tick，或 `{"symbol", "price"}` 直接设价；返回 `{fills, mark}` |
| POST `/__admin/symbol` | `{"symbol", ...spec 字段}` → 新 spec（`status` 非 TRADING 时撤掉该品种所有挂单） |
| GET / POST / PUT / DELETE `/__chaos` | 查看 / 修改（`{"rules"}` 整体替换、`{"add"}` 追加、`{"clock_drift_ms"}`、`{"seed"}`）/ 清空 |

## 4. 规范模型（`engine.py`，判据只看这里）

- order：`id cid symbol side(BUY|SELL) type(MARKET|LIMIT) qty price reduce_only tif status(NEW|PARTIALLY_FILLED|FILLED|CANCELED|REJECTED) filled avg_price fee realized created updated reason origin`
- fill：`id order_id cid symbol side price qty fee maker realized ts visible_at reason`
- position：`symbol qty(带符号) entry leverage margin_mode realized updated` + 视图字段 `mark upl notional margin liq_price`
- account：`wallet upl equity position_margin order_margin available`
- EngineError.kind：`SYMBOL_UNKNOWN SYMBOL_SUSPENDED SYMBOL_DELISTED BAD_PARAM BAD_QTY BAD_PRICE MIN_NOTIONAL DUP_CID ORDER_NOT_FOUND ORDER_NOT_OPEN INSUFFICIENT REDUCE_ONLY BAD_LEVERAGE MARGIN_MODE_BUSY`

## 5. 混沌规则（`chaos.py`）

公共字段 `kind op(名|列表|"*") dialect p max from_req at_req`；种类与专有字段：
`unknown_result{mode: timeout|drop|502, hold_ms}`、`latency{ms, jitter_ms}`、`rate_limit{retry_after_ms}`、`server_error{status}`、`maintenance`、
`cancel_race`、`fill_delay{ms}`、`stale{ms}`、`zero_once`、`symbol{at_req, symbol, changes}`、`clock_drift{at_req, ms}`；顶层 `seed`、`clock_drift_ms`、`log`。
每条命中写一行事件（`kind="chaos"`）：`req_index op dialect rule injection detail executed`。

## 6. 场景文件（`scenarios/*.json`）与脚本步

```json
{"name": "okx/unknown_timeout", "dialect": "okx", "template": "unknown_timeout", "symbol": "BTCUSDT", "seed": 141, "timeout_s": 0.3,
 "chaos": {...}, "engine": {...可选...}, "price_base": 60000.0,
 "script": [{"op": "place", "intent": "i1", "side": "BUY", "type": "LIMIT", "qty": 0.01, "offset": -100, "reduce_only": false, "expect_placed": true},
            {"op": "cancel", "intent": "i1"}, {"op": "price", "offset": -120}, {"op": "price", "price": 59000}, {"op": "tick", "n": 3},
            {"op": "sleep", "ms": 50}, {"op": "settle"}, {"op": "balance"}, {"op": "positions"}, {"op": "restart_client"},
            {"op": "restart_server", "down_ms": 50}, {"op": "kill_switch", "grace_ms": 500}, {"op": "chaos", "patch": {...}},
            {"op": "symbol", "symbol": "BTCUSDT", "changes": {"tick": 1.0}}],
 "judges": ["no_duplicate", "intent_complete", "no_lost", "no_phantom", "position_consistent", "balance_consistent", "cancel_real",
            "status_consistent", "kill_switch", "rate_limit"],
 "expect": {"naive": "red", "correct": "green"}}
```

场景结果（`harness.run_scenario`）：`{scenario, client, ok, verdicts[{judge, ok, detail, numbers}], client_errors[], seconds, server_orders,
server_fills, chaos_events, requests, client_log[]}`；驾驭器自身出错 → `harness_error`（判为红）。

## 7. 被判客户端的最小接口（`clients.py` 顶部）

必需：`place(intent) -> local_id`、`cancel(local_id)`、`query(key) -> {local_id, server_id, cid, status, filled, ...} | None`
（key 可为 local_id / 服务端订单号 / clientOrderId；None = 不知道这张单）、`positions() -> {symbol: 带符号数量}`、`balance() -> float | None`。
可选：`restart()`、`kill_switch()`、`settle()`、`open_orders()`；场景用到而客户端没有 → 记为 client_error，相应判据按红算。
构造：`Client(dialect, base_url, state_dir, creds=None, timeout_s=0.3, log=None)`。

## 8. 适配器（`adapters.py`，客户端侧）

`make(dialect, base_url, creds=None, timeout_s=0.3)` → 具 `server_time instruments ticker klines account positions place cancel query
open_orders fills set_leverage set_margin_mode sync_time` 的对象；异常 `UnknownResult`（可能已执行）、`RateLimited(retry_after_ms)`、
`ExchangeError(kind, code, msg, http_status)`。只接受 `127.0.0.1` / `localhost`。

## 9. 对账（`recon.py`）

`reconcile(local_rows, remote_rows, mapping, remote_mapping, ts_tol_ms)` → `{summary{missing_local, phantom, mismatch, total, local_orders,
remote_orders, matched, ts_offset_ms}, diffs[{kind, key, fields, local, remote, action(补记|标失败|人工看)}]}`。映射见模块 docstring。
