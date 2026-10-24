# qdexsim 实测报告

> 由 `python3.12 qdexsim/tools/build_report.py` 从 `qdexsim/results/*.json` 生成；所有数字都来自结果文件，没跑过的写“没跑”。
> 机器：{{machine}}。

## 0. 一条命令的结果

{{selftest}}

## 1. G1 仿真交易所

**实测（`results/g1_dialects.json`，真进程 + 真 socket）**：

{{g1}}

**撮合 / 账户口径（engine.py 顶部 docstring 是权威，这里摘要）**：没有对手盘，用“最新价 + 每 tick 流动性（BTC 0.05 / ETH 1.0，可配）”代替整本簿；
市价单按最新价全成（taker）；限价单下单即穿价 → 以最新价 taker 吃到流动性上限，剩余挂着；之后每个 tick 对穿价挂单按价格优先、时间优先以**挂单价**
maker 成交，吃完本 tick 流动性为止（部分成交由此而来，test_engine.TestMatching 逐条验证）。净持仓、均价、已实现盈亏、反手；起始保证金 = 名义 / 杠杆
（持仓与挂单都占），可用不足拒单；维持保证金率固定 0.5%，逐仓 / 全仓强平各自公式（test_engine.test_liq_price_formulas / test_liquidation）；
资金费在时钟跨过 8 小时边界时按 pos·mark·rate 结算（test_funding_every_8h）。状态写穿 SQLite，`orders.cid UNIQUE` 是幂等的第二道闸
（内存查重被突变掉时库仍拒绝：test_db_unique_cid_is_last_line_of_defense）。

**五方言**：13 个接口 × 5 = 65 个 handler，每个 handler 的 docstring 写了官方文档章节名；签名 / 时间窗 / 错误码形状按 SHAPES.md。
`tests/test_dialects.py` 对每家跑同一套 13 个操作（进程内，不开 socket）+ 各家鉴权错误码 + 报文形状细节。

**字段可信度（SHAPES.md 里各标记的个数，文档站不可达、全部凭记忆）**：

{{shapes}}

## 2. G2 混沌注入

11 种注入（unknown_result × 3 模式、latency、rate_limit、server_error、maintenance、cancel_race、fill_delay、stale、zero_once、symbol、clock_drift），
JSON 场景文件配置或 `/__chaos` 运行时改，概率全部出自 `random.Random(seed)`，每条命中写一行事件（`req_index / op / dialect / injection / detail /
executed`）。**实测（`results/g2_chaos.json`）**：{{g2}}。

线上表现（`tests/test_server.py`，真 socket）：结果未知三种模式下服务端都真的执行了下单、客户端拿到 UnknownResult；429 带各家重试头、窗口内再打被记违规；
5xx 在写操作上归一为“结果未知”；持仓旧值 / 余额 0 一次 / 成交推迟出现 / 撤单竞态 / 交易对改精度 / 时钟漂移导致签名窗错，逐项断言。

## 3. G3 判据套件

10 个判据（judges.py）：no_duplicate、intent_complete、no_lost、no_phantom、position_consistent、balance_consistent、cancel_real、status_consistent、
kill_switch、rate_limit；全部以服务端真相为准，客户端只需实现 `place / cancel / query / positions / balance`（CONTRACT.md §7）。

**实测（`results/g3_scenarios.json`）**：{{g3}}

天真客户端是故意写坏的（不带 clientOrderId、超时就重发、撤单报错也当撤了、持仓只在本地记一次、重启失忆）；正确客户端的做法见 clients.py 顶部。
判据“有牙”的证据：天真客户端在结果未知（三种模式）与客户端重启模板里 20/20 红，且红的是 no_duplicate / no_lost 这些该红的判据，不是别的。

## 4. G4 对账

**实测（`results/g4_recon.json`）**：{{g4}}。

三类差异的建议动作：服务端有本地无 → 补记；本地有服务端无 → 本地未终局标失败、本地已记成交人工看；两边都有不一致 → 本地状态落后于服务端终局 → 补记，
其余（价格 / 数量不一致）→ 人工看。拆单按 `group_by`（缺省 cid）聚合后再比；时区差按匹配对中位差识别（≥ 30 分钟且 15 分钟整数倍）。

## 5. G5 够硬、够诚实

### 5.1 测试与覆盖率（`results/coverage.json`）

{{coverage}}

### 5.2 突变测试（`results/mutation.json`）

{{mutation}}

### 5.3 三刀（`results/knives.json`）

去掉 clientOrderId 幂等（engine 内存查重 + store UNIQUE 一起去掉）、结果未知时不执行（server 在 handle 前就断连）、对账漏比状态字段：

{{knives}}

### 5.4 零依赖 / 零出网 / 零写仓外

`tests/test_constraints.py`：`^import|^from` 正则 + AST 两种方式只许标准库（tools / mutants 另许复用仓内 tools、mutants）；静态：只有 adapters 用
http.client（且构造时校验 host ∈ {127.0.0.1, localhost}）、只有 server 用 http.server（绑定写死 `common.LOOPBACK`）、库不 import socket；运行时：装
`sys.addaudithook` 跑“服务端子进程 + 五个适配器 + 一个场景 + 一次对账”，所有 open(写) / 删改名 / sqlite3.connect 都落在 tempfile 或 `qdexsim/results/`，
所有 socket.connect / socket.bind 都是 127.0.0.1。外加函数 ≤ 60 行 / 文件 ≤ 800 行。

## 6. 已知没仿到 / 简化了的行为

- **WebSocket 推送**（行情、订单、成交、持仓）：没做，客户端只能轮询。
- **双向持仓、组合保证金、多币种保证金、逐仓单独划转、自动追加保证金**：没做；只有单向净持仓 + 账户级 cross / 按品种 isolated 标志。
- **订单类型**：只有 MARKET / LIMIT GTC + reduceOnly；止损 / 止盈 / 条件单 / IOC / FOK / PostOnly / 冰山没做。
- **盘口**：没有深度，bid = ask = last；撮合用“每 tick 流动性”近似，真实的队列位置 / 滑点没仿。
- **限频模型**：只按概率注入 429，没有权重桶 / 按接口的限频表；Bybit 的 IP 级 403 没仿。
- **强平**：固定维持保证金率 0.5%，没有阶梯、没有保险基金 / ADL、没有强平手续费差异；资金费率固定、不随持仓量变化。
- **鉴权**：IP 白名单、API 权限位、子账户、OKX 模拟盘头 `x-simulated-trading`、Binance 的 `recvWindow` 上限 60000 校验没做。
- **各家细节**：Bybit 查不到单返回空 list（文档说法待核）；OKX 没有按品种切换保证金模式的接口，这里用 set-leverage 的 mgnMode 承载；Bitunix 服务器时间接口是自加的。
- **成交量 / 24h 统计 / 资金费率历史**：K 线成交量只是本账户成交量，行情里的量全是占位 0。

## 7. 取舍记录（做的时候拍的板，写在这里供复核）

1. **五个方言共用一个 engine 与一个账户**：Binance 下的单在 Bybit 接口里也能查到（订单号一致，symbol 名各自翻译）。好处是判据与对账只看一份真相；
   代价是“五个独立交易所”没仿。要分开可以起五个进程。
2. **OKX 的 sz 按张数**（ctVal BTC 0.01 / ETH 0.1），而不是把 sz 当成币数——这是 OKX 与另外四家最大的量纲差异，仿真器保留它，场景里所有数量都取 0.01 的倍数。
3. **结果未知的 502 模式回的是网关形状**（text/html “502 Bad Gateway”），不是交易所报文；适配器对写操作上的任何 5xx 都按“结果未知”处理——这是客户端应有的保守态度。
4. **Bybit 的限频 HTTP 状态用 200 + retCode 10006**（UID 级），没用 429；判据“限频守规矩”在适配器归一后的 RateLimited 上判，与各家 HTTP 状态无关。
5. **同码多义**（Bybit 10001 / OKX 51000 / Bitget 40808 / Bitunix 10008）在客户端侧统一反查成 BAD_PARAM，正确客户端对它“刷新交易对规则重新归整、
   没变化就终局”，而不是给每种情况猜一个码。Bitunix 时间窗错与参数错同码（10002），适配器嗅报文里的 "timestamp"。
6. **判据按意图匹配服务端订单用 (symbol, side, type, qty)**，不用 clientOrderId——天真客户端没有 clientOrderId；因此场景里每个意图数量唯一。
7. **正确客户端的持仓 = 自己所有订单成交量之和**，服务端持仓只做“连续 3 次读相同才采纳”的核对（跨过 stale 窗口）；这是对“持仓短时旧值”最省事的正确做法。
8. **急停判据的 grace 取 500 ms**：急停后 0.5 s 起服务端不得再有新单；场景里急停后还会故意再下一张单（客户端必须拒绝）。
9. **restart_server 会重置混沌计数与请求序号**（新进程），场景里重启后不再依赖 at_req 类规则。

## 8. 搬运建议

- 想接真交易所：`adapters.py` 的五个类是客户端侧 SDK 的骨架，但每个错误码 / 字段请先对照 SHAPES.md 的「待核」逐条核实，再改 `KIND_PRIORITY` 与 `ERRORS`。
- 想判你自己的客户端：实现 CONTRACT.md §7 的五个方法，`harness.run_suite(scenarios.load_all(dialect=...), client_names=())` 配合自定义 client 类；
  先跑 `unknown_timeout` 与 `client_restart` 两个模板，这两个最常把“看起来对”的客户端打红。
- 想加故障：在 `chaos.py` 的 `_apply` / `scheduled` 加一个 kind，在 `server.handle` 或 `Context` 里消费它，再在 `scenarios/gen.py` 加模板；`tools/selfcheck.G2_SCENARIO`
  要同步加进去（g2 判据要求 11/11 种都出现在事件日志里，加一种就改成 12/12）。
- 想对真交易所导出做对账：`recon.py --map` 把你的列名映射到规范字段，`--remote-map` 映射导出文件的列名；先用 `--selfcheck` 确认工具本身没坏。
- 覆盖率用 `tools/cov.py`（要以脚本方式跑，不要 `-m`）；突变用 `mutants/run.py --module <名> --per-module 30`，补完判据用 `--ids` 重跑漏网并写 `triage_reasons.py`。
