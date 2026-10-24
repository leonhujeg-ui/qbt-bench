# qdexsim 实测报告

> 由 `python3.12 qdexsim/tools/build_report.py` 从 `qdexsim/results/*.json` 生成；所有数字都来自结果文件，没跑过的写“没跑”。
> 机器：Linux-6.18.44-fc-v153-x86_64-with-glibc2.39，Python 3.12.3，SQLite 3.45.1，4 CPU，提交 `8946d7a`，2026-10-24T14:01:40。

## 0. 一条命令的结果

`python3.12 -m qdexsim.selftest --full`，108.4 s，全部通过：

| 步 | 规模 | 秒 | 结果 | 关键数字 |
|---|---|---|---|---|
| constraints | 3 tests (deps / network / writes) | 0.0 | OK | stdlib-only ok; no-network ok; writes+binds+connects audit ok |
| coverage | 3002 statements, 158 tests | 20.5 | OK | 98.03% (>= 90% required); child processes merged 3 |
| g1_dialects | 5 dialects x (bad sign, expired ts, place, partial fill, cancel) + process restart | 0.5 | OK | sign/ts codes binance:-1022/-1021, bybit:10004/10002, okx:50113/50102, bitget:40009/40008, bitunix:10007/10002; partial 0.05/0.06 x5; restart orders 5 fills 5 equal=True dup-after-restart=DUP_CID |
| g2_chaos | 11 injection kinds, same seed x2 + other seed | 3.0 | OK | events 20 identical=True; other seed differs=True; kinds logged 11/11 |
| g3_judges | 60 scenarios x 2 clients | 62.7 | OK | correct green 60/60; naive red 60/60; naive must-red 20 ok=True |
| g4_recon | synthetic 200 orders, 50 planted diffs | 0.0 | OK | found 50/50, missed 0, false positives 0, tz offset 28800000 ms |
| knives | 3 hand-made mutants | 1.3 | OK | remove_cid_idempotency red=True; unknown_result_not_executed red=True; recon_skips_status red=True |
| unittest | 158 tests | 20.4 | OK | OK; tmp dirs left 0 |

## 1. G1 仿真交易所

**实测（`results/g1_dialects.json`，真进程 + 真 socket）**：

| 方言 | 错签名码 | 过期时间戳码 | 限价 0.06 被 0.05 流动性打成 | 撤单 | 撤后状态 | 结果 |
|---|---|---|---|---|---|---|
| binance | -1022 (期望 -1022) | -1021 (期望 -1021) | PARTIALLY_FILLED 0.05/0.06 | CANCELED | CANCELED | ✅ |
| bitget | 40009 (期望 40009) | 40008 (期望 40008) | PARTIALLY_FILLED 0.05/0.06 | CANCELED | CANCELED | ✅ |
| bitunix | 10007 (期望 10007) | 10002 (期望 10002) | PARTIALLY_FILLED 0.05/0.06 | CANCELED | CANCELED | ✅ |
| bybit | 10004 (期望 10004) | 10002 (期望 10002) | PARTIALLY_FILLED 0.05/0.06 | CANCELED | CANCELED | ✅ |
| okx | 50113 (期望 50113) | 50102 (期望 50102) | PARTIALLY_FILLED 0.05/0.06 | CANCELED | CANCELED | ✅ |

进程 kill 后同库同端口重启：订单 5、成交 5、BTC 持仓 0.25，与重启前快照相等 = True，同端口 = True，重启后用旧 clientOrderId 再下单 → DUP_CID；耗时 0.4 s

**撮合 / 账户口径（engine.py 顶部 docstring 是权威，这里摘要）**：没有对手盘，用“最新价 + 每 tick 流动性（BTC 0.05 / ETH 1.0，可配）”代替整本簿；
市价单按最新价全成（taker）；限价单下单即穿价 → 以最新价 taker 吃到流动性上限，剩余挂着；之后每个 tick 对穿价挂单按价格优先、时间优先以**挂单价**
maker 成交，吃完本 tick 流动性为止（部分成交由此而来，test_engine.TestMatching 逐条验证）。净持仓、均价、已实现盈亏、反手；起始保证金 = 名义 / 杠杆
（持仓与挂单都占），可用不足拒单；维持保证金率固定 0.5%，逐仓 / 全仓强平各自公式（test_engine.test_liq_price_formulas / test_liquidation）；
资金费在时钟跨过 8 小时边界时按 pos·mark·rate 结算（test_funding_every_8h）。状态写穿 SQLite，`orders.cid UNIQUE` 是幂等的第二道闸
（内存查重被突变掉时库仍拒绝：test_db_unique_cid_is_last_line_of_defense）。

**五方言**：13 个接口 × 5 = 65 个 handler，每个 handler 的 docstring 写了官方文档章节名；签名 / 时间窗 / 错误码形状按 SHAPES.md。
`tests/test_dialects.py` 对每家跑同一套 13 个操作（进程内，不开 socket）+ 各家鉴权错误码 + 报文形状细节。

**字段可信度（SHAPES.md 里各标记的个数，文档站不可达、全部凭记忆）**：

| 方言 | 标「稿」 | 标「待核」 | 标「自定」 |
|---|---|---|---|
| 1. Binance USDⓈ-M Futures | 29 | 8 | 1 |
| 2. Bybit v5 linear | 27 | 10 | 0 |
| 3. OKX v5 SWAP | 26 | 9 | 3 |
| 4. Bitget v2 mix USDT-FUTURES | 23 | 13 | 0 |
| 5. Bitunix Futures | 10 | 20 | 6 |

## 2. G2 混沌注入

11 种注入（unknown_result × 3 模式、latency、rate_limit、server_error、maintenance、cancel_race、fill_delay、stale、zero_once、symbol、clock_drift），
JSON 场景文件配置或 `/__chaos` 运行时改，概率全部出自 `random.Random(seed)`，每条命中写一行事件（`req_index / op / dialect / injection / detail /
executed`）。**实测（`results/g2_chaos.json`）**：用到全部 11 种注入的场景同种子跑两遍：注入事件 20 / 20 条，逐条相同 = True（请求数 21）；换种子 99 → 25 条，与种子 0 不同 = True；事件日志里出现的注入种类 11/11（缺 无）；耗时 3.0 s。

线上表现（`tests/test_server.py`，真 socket）：结果未知三种模式下服务端都真的执行了下单、客户端拿到 UnknownResult；429 带各家重试头、窗口内再打被记违规；
5xx 在写操作上归一为“结果未知”；持仓旧值 / 余额 0 一次 / 成交推迟出现 / 撤单竞态 / 交易对改精度 / 时钟漂移导致签名窗错，逐项断言。

## 3. G3 判据套件

10 个判据（judges.py）：no_duplicate、intent_complete、no_lost、no_phantom、position_consistent、balance_consistent、cancel_real、status_consistent、
kill_switch、rate_limit；全部以服务端真相为准，客户端只需实现 `place / cancel / query / positions / balance`（CONTRACT.md §7）。

**实测（`results/g3_scenarios.json`）**：60 个场景（12 模板 × 5 方言），两个客户端各跑一遍，共 62.7 s。正确客户端绿 60/60，红 0；天真客户端红 60/60，绿 0；要求必红的 20 个场景（unknown_timeout / unknown_drop / unknown_502 / client_restart）全红 = True。

天真客户端被哪些判据抓红（场景数）：intent_complete 26，position_consistent 25，no_lost 20，no_duplicate 15，status_consistent 15，balance_consistent 5，kill_switch 5，rate_limit 5，cancel_real 3

| 模板 | 天真红 | 正确绿 | 抓红天真客户端的判据 | 秒（两客户端合计） |
|---|---|---|---|---|
| baseline | 5/5 | 5/5 | position_consistent, status_consistent | 3.3 |
| cancel_race | 5/5 | 5/5 | position_consistent, status_consistent | 3.2 |
| client_restart | 5/5 | 5/5 | no_lost, position_consistent | 3.2 |
| clock_drift | 5/5 | 5/5 | intent_complete | 0.7 |
| kill_switch | 5/5 | 5/5 | kill_switch | 11.0 |
| rate_limit | 5/5 | 5/5 | cancel_real, intent_complete, rate_limit | 19.4 |
| server_restart | 5/5 | 5/5 | position_consistent | 4.3 |
| stale_views | 5/5 | 5/5 | balance_consistent, position_consistent, status_consistent | 3.4 |
| symbol_rules | 5/5 | 5/5 | intent_complete | 0.7 |
| unknown_502 | 5/5 | 5/5 | intent_complete, no_duplicate, no_lost | 3.2 |
| unknown_drop | 5/5 | 5/5 | intent_complete, no_duplicate, no_lost | 3.2 |
| unknown_timeout | 5/5 | 5/5 | intent_complete, no_duplicate, no_lost | 6.8 |

天真客户端是故意写坏的（不带 clientOrderId、超时就重发、撤单报错也当撤了、持仓只在本地记一次、重启失忆）；正确客户端的做法见 clients.py 顶部。
判据“有牙”的证据：天真客户端在结果未知（三种模式）与客户端重启模板里 20/20 红，且红的是 no_duplicate / no_lost 这些该红的判据，不是别的。

## 4. G4 对账

**实测（`results/g4_recon.json`）**：合成 200 张服务端订单 / 200 条本地聚合后记录（含 10 张拆成两行的单），埋 50 条差异：找出 50，漏 0，误报 0；三类各 15 / 15 / 20；本地时间戳整体 +8h 被识别为常数偏移 28800000 ms、不算差异；耗时 0.0 s。

三类差异的建议动作：服务端有本地无 → 补记；本地有服务端无 → 本地未终局标失败、本地已记成交人工看；两边都有不一致 → 本地状态落后于服务端终局 → 补记，
其余（价格 / 数量不一致）→ 人工看。拆单按 `group_by`（缺省 cid）聚合后再比；时区差按匹配对中位差识别（≥ 30 分钟且 15 分钟整数倍）。

## 5. G5 够硬、够诚实

### 5.1 测试与覆盖率（`results/coverage.json`）

158 个测试，子进程覆盖合并 3 份；库模块合计 **98.03%**（要求 ≥ 90%）。

| 文件 | 语句 | 未覆盖 | 覆盖率 |
|---|---|---|---|
| qdexsim/__init__.py | 0 | 0 | 100.0% |
| qdexsim/adapters.py | 476 | 5 | 98.9% |
| qdexsim/chaos.py | 151 | 0 | 100.0% |
| qdexsim/clients.py | 279 | 12 | 95.7% |
| qdexsim/common.py | 133 | 25 | 81.2% |
| qdexsim/dialects/__init__.py | 5 | 0 | 100.0% |
| qdexsim/dialects/base.py | 116 | 0 | 100.0% |
| qdexsim/dialects/binance.py | 113 | 0 | 100.0% |
| qdexsim/dialects/bitget.py | 135 | 0 | 100.0% |
| qdexsim/dialects/bitunix.py | 135 | 0 | 100.0% |
| qdexsim/dialects/bybit.py | 146 | 1 | 99.3% |
| qdexsim/dialects/okx.py | 154 | 0 | 100.0% |
| qdexsim/engine.py | 334 | 1 | 99.7% |
| qdexsim/feed.py | 82 | 0 | 100.0% |
| qdexsim/harness.py | 109 | 3 | 97.2% |
| qdexsim/judges.py | 90 | 0 | 100.0% |
| qdexsim/recon.py | 226 | 0 | 100.0% |
| qdexsim/server.py | 246 | 12 | 95.1% |
| qdexsim/store.py | 72 | 0 | 100.0% |
| **合计** | 3002 | 59 | **98.03%** |

### 5.2 突变测试（`results/mutation.json`）

17 个模块 × 每模块 30 个 = 510 个突变体（`python3.12 qdexsim/mutants/run.py --per-module 30 --jobs 4`，4 路并行，基线 19.7 s，首轮 905.3 s）；抓住 476（其中超时 0），漏网 34，**抓住率 93.3%**（要求 ≥ 85%）。首轮漏网、补判据后重跑（`--ids`，3 轮）被抓住的 115 个；最终漏网逐条判定：等价 34、待补 0（见 [mutants/triage.md](mutants/triage.md)）。

| 模块 | 总数 | 抓住 | 漏网 | 抓住率 |
|---|---|---|---|---|
| adapters | 30 | 25 | 5 | 83.3% |
| chaos | 30 | 29 | 1 | 96.7% |
| clients | 30 | 28 | 2 | 93.3% |
| common | 30 | 30 | 0 | 100.0% |
| dialects/base | 30 | 29 | 1 | 96.7% |
| dialects/binance | 30 | 30 | 0 | 100.0% |
| dialects/bitget | 30 | 29 | 1 | 96.7% |
| dialects/bitunix | 30 | 30 | 0 | 100.0% |
| dialects/bybit | 30 | 29 | 1 | 96.7% |
| dialects/okx | 30 | 29 | 1 | 96.7% |
| engine | 30 | 25 | 5 | 83.3% |
| feed | 30 | 30 | 0 | 100.0% |
| harness | 30 | 26 | 4 | 86.7% |
| judges | 30 | 26 | 4 | 86.7% |
| recon | 30 | 27 | 3 | 90.0% |
| server | 30 | 28 | 2 | 93.3% |
| store | 30 | 26 | 4 | 86.7% |

### 5.3 三刀（`results/knives.json`）

去掉 clientOrderId 幂等（engine 内存查重 + store UNIQUE 一起去掉）、结果未知时不执行（server 在 handle 前就断连）、对账漏比状态字段：

| 手工突变 | 结果 | 先抓住它的测试 |
|---|---|---|
| remove_cid_idempotency | 红 ✅ | FAIL: test_db_unique_cid_is_last_line_of_defense (qdexsim.tests.test_engine.TestPersistence.test_db_unique_cid_is_last_line_of_defense) |
| unknown_result_not_executed | 红 ✅ | FAIL: test_unknown_result_modes (qdexsim.tests.test_server.TestHttp.test_unknown_result_modes) |
| recon_skips_status | 红 ✅ | FAIL: test_files_and_main (qdexsim.tests.test_recon.TestCli.test_files_and_main) |

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
