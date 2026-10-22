# qbt 接口契约（CONTRACT）

本文件是 `qbt` 包对外的**机器可核对**契约：给要把 qbt 接进自己系统的人看，也给判据看。
每张表前面有一行标记 `<!-- contract-table: 名字 -->`，`tests/test_contract.py` 解析标记后的表格并与代码逐项对照：
**表里少一行、代码里少一个字段 / 常量 / 键、值对不上，对应判据就红**（`test_contract.TestMeta.test_deleting_any_row_turns_red`
对每张表删掉一行后在临时副本上重跑，证明这一点）。表格单元格里不能出现 `|`。
行为细节以代码与 `README.md`「时间语义」为准；本文只摘录可核对的部分。

核对命令：`python3.12 -m unittest tests.test_contract tests.test_boundaries`（含在 `python3.12 -m qbt.cli test --quick` 里）。
`QBT_CONTRACT_PATH=<file>` 可让判据读另一份副本（用来演示删行必红，不必改真文件）。

---

## 1. 策略接口（`qbt/engine.py`）

### 1.1 `Strategy` 基类

子类覆盖 `on_start` / `on_bar`，声明 `name`、`DEFAULTS`（参数默认值，也决定参数类型）与 `PARAM_SPACE`（优化用的候选值，键 ⊆ DEFAULTS）。

<!-- contract-table: strategy_class -->

| 名字 | 层级 | 签名 / 类型 | 含义 |
|---|---|---|---|
| name | 类 | str | 策略名；`strategies.REGISTRY` 的键，写进 `Result.params[*].strategy` |
| DEFAULTS | 类 | dict | 参数默认值；每个参数的类型由默认值决定（见 1.2） |
| PARAM_SPACE | 类 | dict | 参数名 → 候选值列表（优化器 `grid` 展开）；内置策略 ≥ 4 维 |
| on_start | 类 | (ctx) | 回测开始、循环前调用一次；基类实现把 symbol 缺省为 ctx.symbols[0] 并校验 |
| on_bar | 类 | (ctx, i) | 每根 bar 调用一次（基类抛 NotImplementedError） |
| on_end | 类 | (ctx) | 循环结束后调用一次（基类空实现） |
| size_qty | 类 | (ctx, price, fraction) | 辅助：equity × fraction ÷ price；price ≤ 0 或 equity ≤ 0 返回 0.0 |
| params | 实例 | dict | DEFAULTS 与传入参数合并后的实际参数 |
| symbol | 实例 | str 或 None | 交易标的；None 在 on_start 时取 ctx.symbols[0] |

### 1.2 `Strategy.__init__(symbol=None, **params)` 的校验规则

示例用一个 `DEFAULTS = {"k": 5, "f": 0.5, "flag": true, "s": "x"}` 的探针策略；判据逐行实例化。

<!-- contract-table: strategy_init -->

| 规则 | 示例 kwargs（JSON） | 结果 |
|---|---|---|
| 未知参数名 | {"bogus": 1} | ConfigError |
| 默认值为 int 的参数：接受有限实数（含 float） | {"k": 2.5} | ok |
| 默认值为 int / float 的参数：拒绝 bool | {"k": true} | ConfigError |
| 默认值为 float 的参数：拒绝 NaN | {"f": NaN} | ConfigError |
| 默认值为 float 的参数：拒绝 inf | {"f": Infinity} | ConfigError |
| 默认值为 int / float 的参数：拒绝字符串 | {"f": "0.5"} | ConfigError |
| 默认值为 bool 的参数：只接受 bool | {"flag": 1} | ConfigError |
| 默认值为 str 的参数：只接受 str | {"s": 1} | ConfigError |
| symbol 必须是 str 或 None | {"symbol": 5} | ConfigError |
| symbol 为 None 时在 on_start 取第一个标的 | {"symbol": null} | ok |
| on_start：symbol 不在 ctx.symbols | {"symbol": "NOPE"} | ConfigError(on_start) |

### 1.3 调用时序与可见历史

主循环（`Engine.run`）：`for i in range(n): broker.on_bar(i); ctx.index = i; for s in strategies: s.on_bar(ctx, i)`。
`broker.on_bar(i)` 先用第 i 根撮合上一根收盘后提交的订单、收资金费、以 close[i] 盯市、查爆仓，**然后**策略才看到第 i 根。

<!-- contract-table: lifecycle -->

| 钩子 | 调用时机 | ctx.index | broker.bar | 可见历史（ctx.data，strict 模式强制） | 此处下的单最早成交于 |
|---|---|---|---|---|---|
| on_start | run() 开始、主循环前 | -1 | 0 | 无：strict 下读任何下标抛 LookaheadError（ctx.indicator 可用） | bar 0 的开盘（主循环第一步撮合） |
| on_bar | broker.on_bar(i) 之后（第 i 根已撮合、已盯市） | i | i | [0, i]：下标 > i 或切片越过 i 抛 LookaheadError；负下标按 len 折算后同样受限 | bar i+1（市价按 open[i+1] + 滑点；限价 / 止损按触发规则） |
| on_end | 主循环之后 | n-1 | n-1 | [0, n-1] | 永不（不再撮合，订单留在 pending） |

**禁止看到什么**：任何下标 > i 的 K 线列（ts / open / high / low / close / volume）。`Engine(strict=True)` 用 `GuardedColumn` 包裹六列，
越界访问立即抛 `LookaheadError`（`QBTError` 子类）。`ctx.indicator(...)` 返回的是在**全序列**上预先算好的因果指标 list，
strict 模式**不**守卫它——策略只能读 `ind[j]`，j ≤ i；这一条由两条篡改判据兜底：
`tests.test_indicators.TestGenericProperties.test_causality_future_tamper`（指标：篡改 i ≥ cut 后 [0, cut) 逐字节不变）与
`tests.test_engine.TestEngineSemantics.test_future_tamper_leaves_history_unchanged`（策略：篡改 i ≥ cut 的 K 线后，[0, cut) 的成交 / 权益 /
订单顺序逐条不变，6 个内置策略全跑）。

### 1.4 `Context`（策略在钩子里拿到的 ctx）

<!-- contract-table: context_api -->

| 名字 | 种类 | 签名 | 含义 |
|---|---|---|---|
| data | 属性 | dict | symbol → OHLCV（strict 模式下是 GuardedData，六列受守卫） |
| raw | 属性 | dict | symbol → 原始 OHLCV（不受守卫；仅供指标缓存用，策略不应直接读） |
| broker | 属性 | Broker | 账户 / 撮合器本体（positions、pending、orders、fills、equity、cash…） |
| index | 属性 | int | 当前 bar 下标；on_start 时为 -1 |
| symbols | 属性 | list | 标的名列表（data 的键顺序） |
| cache | 属性 | IndicatorCache | 进程级指标缓存（键 = 数据指纹 + 指标名 + 参数） |
| strict | 属性 | bool | 是否 strict 模式 |
| equity | property | float | broker.equity：最近一次盯市的权益 |
| cash | property | float | broker.cash：现金（可为负 = 借入） |
| position | 方法 | position(symbol) | 签名持仓数量（多为正、空为负） |
| indicator | 方法 | indicator(name, *args, symbol=None) | 取指标（名字见 §8，args 里的 'open'/'high'/'low'/'close'/'volume' 替换为该标的的列） |
| order | 方法 | order(symbol, side, qty, otype='market', **kw) | 下单，返回 Order；kw：price、display、expires、reduce_only、tag |
| cancel | 方法 | cancel(order) | 撤单；只对 pending / partial 生效，返回 bool |
| cancel_all | 方法 | cancel_all(symbol=None) | 撤全部（或某标的）挂单，返回撤销数量 |
| pending_signed | 方法 | pending_signed(symbol) | 未成交市价单的签名剩余量之和（限价 / 止损不计） |
| target | 方法 | target(symbol, qty, tag='') | 把 持仓 + 未成交市价单 调到目标数量，差额 > 1e-9 时发市价单，返回 Order 或 None |

---

## 2. 数据结构字段表（`qbt/broker.py`、`qbt/engine.py`）

### 2.1 `Order`（`broker.orders` 的元素；`to_dict()` 按 `__slots__` 顺序）

<!-- contract-table: order_fields -->

| 序号 | 字段 | 类型 | 含义 |
|---|---|---|---|
| 0 | id | int | 从 1 起连续编号（提交顺序，含被拒绝的单） |
| 1 | symbol | str | 标的 |
| 2 | side | int | +1 买 / -1 卖 |
| 3 | qty | float | 原始数量，> 0 |
| 4 | remaining | float | 未成交数量；filled 时为 0.0 |
| 5 | type | str | §3.1 的五种之一 |
| 6 | price | float 或 None | 限价 / 止损 / 止盈 / 冰山的价格；市价单 None |
| 7 | display | float 或 None | 冰山单每根 bar 的显示量；其他 None |
| 8 | status | str | §3.2 的五种之一 |
| 9 | created | int | 提交时的 broker.bar（= 策略看到的 i） |
| 10 | expires | int 或 None | 最后一个可撮合的 bar；bar > expires 时撤销（reason=expired） |
| 11 | reduce_only | bool | 只减仓：无仓 / 同向拒绝，数量封顶在持仓 |
| 12 | tag | str | 调用方自定义标签，原样写进 Fill.tag |
| 13 | reason | str | 拒绝 / 撤销原因（§3.2）；正常为空串 |

### 2.2 `Fill`（`broker.fills` / `Result.fills` 的元素）

<!-- contract-table: fill_fields -->

| 序号 | 字段 | 类型 | 含义 |
|---|---|---|---|
| 0 | order_id | int | 对应 Order.id；强平成交为 0 |
| 1 | symbol | str | 标的 |
| 2 | bar | int | 成交发生的 bar（≥ 订单 created + 1，on_start 下的单除外） |
| 3 | side | int | +1 买 / -1 卖 |
| 4 | qty | float | 本笔成交量，> 0 |
| 5 | price | float | 成交价（已含滑点） |
| 6 | fee | float | 手续费，≥ 0，已从 cash 扣除 |
| 7 | liquidity | str | maker 或 taker（决定费率） |
| 8 | tag | str | 订单 tag；强平为 liquidation |

### 2.3 `Position`（`broker.positions[symbol]`）

<!-- contract-table: position_fields -->

| 序号 | 字段 | 类型 | 含义 |
|---|---|---|---|
| 0 | symbol | str | 标的 |
| 1 | qty | float | 签名持仓（多正空负），平仓为 0.0 |
| 2 | avg_price | float | 当前持仓的加权均价；平仓后 0.0；反手后为反手成交价 |
| 3 | realized | float | 累计已实现盈亏（不含手续费、不含资金费） |

### 2.4 往返交易 dict（`broker.round_trips(fills)` = `Result.trades` 的元素）

<!-- contract-table: trade_fields -->

| 键 | 类型 | 含义 |
|---|---|---|
| symbol | str | 标的 |
| entry_bar | int | 开仓（持仓从 0 变非 0）的成交 bar |
| exit_bar | int | 平仓（回到 0 或反手）的成交 bar |
| side | int | 持仓方向 +1 多 / -1 空 |
| qty | float | 持仓峰值 |
| entry_price | float | 开仓均价 |
| exit_price | float | 最后一笔平仓成交价 |
| pnl | float | 含该往返全部手续费的盈亏 |
| bars | int | exit_bar - entry_bar |

### 2.5 事件元组（`broker.events` / `Result.events`）

<!-- contract-table: events -->

| 事件名 | 元组 | 含义 |
|---|---|---|
| liquidation | ("liquidation", bar, equity) | 权益 < 维持保证金率 × 总名义敞口：以 close[bar] 强平全部持仓、撤全部挂单 |
| bankrupt | ("bankrupt", bar, equity) | 强平后权益 ≤ 0：此后所有订单 rejected(bankrupt) |

### 2.6 `Result`（`run_backtest` / `Engine.run` 的返回值）

<!-- contract-table: result_attrs -->

| 属性 | 类型 | 含义 |
|---|---|---|
| symbols | list | 标的名列表 |
| n | int | bar 数 |
| ts | list | 第一个标的的时间戳列 |
| bar_seconds | int 或 None | 第一个标的的 bar 长度（秒）；≤ 1 根且未显式给出时为 None |
| equity | list | 每根 bar 收盘后的权益，len = n（n = 0 时为 []） |
| initial_cash | float | BrokerConfig.cash |
| final_equity | float | equity[-1]；n = 0 时等于 initial_cash |
| fills | list | Fill 列表（含强平） |
| orders | list | Order 列表（含被拒绝 / 撤销） |
| trades | list | §2.4 的往返交易 |
| fees | float | 累计手续费（含强平罚金） |
| funding | float | 累计资金费（签名，见 §3.3） |
| liquidations | int | 强平次数 |
| bankrupt | bool | 是否破产 |
| events | list | §2.5 的事件元组 |
| params | list | 每个策略一个 dict：strategy、symbol 加全部参数 |
| elapsed | float | run() 耗时（秒） |
| cache_stats | tuple | (指标缓存命中, 未命中) |

### 2.7 `Result.summary()`（也是报告 JSON 的 summary 段）

<!-- contract-table: summary_keys -->

| 键 | 来源 |
|---|---|
| n | Result.n |
| initial_cash | Result.initial_cash |
| final_equity | Result.final_equity |
| fills | len(Result.fills) |
| orders | len(Result.orders) |
| trades | len(Result.trades) |
| fees | Result.fees |
| funding | Result.funding |
| liquidations | Result.liquidations |
| bankrupt | Result.bankrupt |
| params | Result.params |
| elapsed | Result.elapsed |

---

## 3. 订单类型、状态与符号约定（`qbt/broker.py`）

### 3.1 订单类型（`ORDER_TYPES`）

撮合在第 bar 根的 (open, high, low, volume) 上进行，每根 bar 每单最多成交 `max_participation × volume`（volume = 0 则不成交）。

<!-- contract-table: order_types -->

| 类型 | 常量 | 触发与成交价 | 流动性 |
|---|---|---|---|
| market | MARKET | 总是成交：open × (1 + side × 滑点) | taker |
| limit | LIMIT | 买：low ≤ price → min(open, price)；卖：high ≥ price → max(open, price)；无滑点 | maker |
| stop | STOP | 买：high ≥ price → max(open, price) × (1 + 滑点)；卖：low ≤ price → min(open, price) × (1 - 滑点) | taker |
| take_profit | TAKE_PROFIT | 与 limit 相同规则（挂在有利方向的限价单） | maker |
| iceberg | ICEBERG | 与 limit 相同触发；每根 bar 最多成交 display | maker |

滑点 = `slippage + impact × (本笔成交量 ÷ bar 成交量)`。

### 3.2 订单状态与原因（status / reason）

<!-- contract-table: status -->

| 状态 | 常量 | reason | 触发 |
|---|---|---|---|
| pending | PENDING | (空) | 已提交、尚无成交 |
| partial | PARTIAL | (空) | 已部分成交、余量仍挂着 |
| filled | FILLED | (空) | remaining ≤ 1e-12，已从 pending 移除 |
| cancelled | CANCELLED | (空) | 调用方 cancel / cancel_all |
| cancelled | CANCELLED | expired | 撮合时 bar > expires |
| cancelled | CANCELLED | margin | 已有部分成交后余量触犯杠杆上限：保留已成交、撤余量 |
| cancelled | CANCELLED | reduce_only | 只减仓单成交后仓位已平，余量不得反手 |
| cancelled | CANCELLED | liquidation | 强平时撤销全部挂单 |
| rejected | REJECTED | bankrupt | 破产后提交的任何订单（提交时即拒） |
| rejected | REJECTED | short not allowed | allow_short=False：提交时卖出量 > 持仓，或撮合时已无多头可卖 |
| rejected | REJECTED | reduce_only | 只减仓单撮合时无持仓或与持仓同向 |
| rejected | REJECTED | margin | 开仓 / 加仓后总名义敞口 > 权益 × max_leverage（尚无成交时） |

### 3.3 符号约定

<!-- contract-table: signs -->

| 键 | 约定 | 判据 |
|---|---|---|
| side | +1 买 / -1 卖；Order.side、Fill.side 同义；其他值 OrderError | test_contract.TestSigns、tests.test_broker.TestConfigAndValidation.test_order_validation |
| qty | Order.qty、Fill.qty、往返 qty 恒为正；方向只在 side 里；持仓 Position.qty 与 ctx.position() 是签名量 | test_contract.TestSigns |
| fee | fee = qty × price × 费率（maker_fee 或 taker_fee，按 Fill.liquidity）≥ 0，成交时从 cash 扣除并累加到 fees_paid；强平费率 = taker_fee + liquidation_fee | test_contract.TestSigns、tests.test_broker.TestMatching.test_market_fill_at_next_open_with_slippage_and_taker_fee |
| slip_market | 市价单：买价 = open × (1 + 滑点)，卖价 = open × (1 - 滑点)——总是对自己不利 | test_contract.TestSigns |
| slip_stop | 止损单：买 = max(open, price) × (1 + 滑点)，卖 = min(open, price) × (1 - 滑点) | test_contract.TestSigns、tests.test_broker.TestMatching.test_stop_rules |
| slip_limit | 限价 / 止盈 / 冰山：无滑点，成交价 = 对自己更有利的 open 或 price | test_contract.TestSigns、tests.test_broker.TestMatching.test_limit_rules |
| funding | 每 funding_interval 根（(i+1) % interval == 0）按 pay = qty × close[i] × funding_rate 从 cash 扣除；多头付正费率，空头在正费率下收到（pay < 0） | test_contract.TestSigns、tests.test_broker.TestAccounting.test_funding |
| funding_sign | funding_paid / Result.funding 是签名累计：付出为正、收到为负 | test_contract.TestSigns |
| realized | Position.realized += (成交价 - avg_price) × 平仓量 × (多 +1 / 空 -1)，不含手续费与资金费；broker.realized_pnl() 为全部标的之和 | test_contract.TestSigns、tests.test_broker.TestAccounting.test_position_average_and_realized |
| unrealized | broker.unrealized_pnl() = Σ qty × (close[bar] - avg_price) | test_contract.TestSigns |
| cash_negative | cash 可为负（保证金借入，max_leverage > 1 时）；负现金全额计入 liabilities() | test_contract.TestSigns、tests.test_broker.TestAccounting.test_margin_rejection_and_leverage |
| liabilities | liabilities() = max(-cash, 0) + 空头市值；assets() = max(cash, 0) + 多头市值 | test_contract.TestSigns |
| identity | 每根 bar 收盘后：equity == cash + Σ qty × close == assets() - liabilities() == initial_cash + realized + unrealized - fees_paid - funding_paid | test_contract.TestSigns、tests.test_broker.TestRandomFlowIdentities.test_identities_hold |

### 3.4 `BrokerConfig`（frozen dataclass；`check()` 失败抛 ConfigError）

<!-- contract-table: config_fields -->

| 字段 | 默认值 | 合法范围 | 非法示例（JSON） |
|---|---|---|---|
| cash | 100000.0 | 有限实数 > 0（bool 不算实数） | {"cash": 0.0} |
| maker_fee | 0.0002 | [0, 1) | {"maker_fee": -0.1} |
| taker_fee | 0.0005 | [0, 1) | {"taker_fee": 1.0} |
| slippage | 0.0001 | [0, 1) | {"slippage": Infinity} |
| impact | 0.0 | [0, 1) | {"impact": -1.0} |
| max_participation | 0.1 | (0, 1] | {"max_participation": 0.0} |
| max_leverage | 1.0 | 有限实数 > 0 | {"max_leverage": 0.0} |
| maintenance_margin | 0.05 | [0, 1) | {"maintenance_margin": 1.0} |
| liquidation_fee | 0.01 | [0, 1) | {"liquidation_fee": 1.0} |
| funding_rate | 0.0 | 有限实数，绝对值 < 1 | {"funding_rate": 1.0} |
| funding_interval | 0 | 非负 int（bool 不算）；0 = 关闭 | {"funding_interval": -1} |
| allow_short | true | bool | {"allow_short": 1} |

---

## 4. 错误契约（`qbt/errors.py`）

规则（与 `fuzz/fuzz.py` 一致）：**QBTError 及其子类 = 受控拒绝；成功返回 = ok；其他任何异常 = 缺陷。**
受控结果只有三种：QBTError 子类、§3.2 的 rejected / cancelled 状态、本节明示的其他异常（文件不存在、dataclass 未知字段）。

<!-- contract-table: error_classes -->

| 类 | 定义处 | 基类 | 含义 |
|---|---|---|---|
| QBTError | qbt.errors | ValueError | 所有受控异常的基类（继承 ValueError 便于调用方按惯例捕获） |
| DataError | qbt.errors | QBTError | K 线数据非法 / 生成规格非法 / CSV 格式非法 |
| OrderError | qbt.errors | QBTError | 订单参数非法（side、qty、类型、价格、display、symbol） |
| ConfigError | qbt.errors | QBTError | 配置非法：BrokerConfig、策略参数、指标窗口、参数空间、walk-forward 参数、未知指标名、VaR alpha |
| LookaheadError | qbt.engine | QBTError | strict 模式下策略访问了下标 > ctx.index 的数据 |

### 4.1 哪个入口抛什么

<!-- contract-table: raises -->

| 入口 | 异常 / 状态 | 触发条件 | 判据位置 |
|---|---|---|---|
| OHLCV.validate | DataError | §6.2 的任一规则 | tests.test_data.TestValidation.test_rejects_nan_inf、tests.test_data.TestValidation.test_rejects_length_mismatch_and_types |
| OHLCV.from_csv | DataError、FileNotFoundError | 坏表头 / 字段数 ≠ 6 / 非数值 token / nan、1e400 时间戳 → DataError（含行号）；文件不存在 → FileNotFoundError（OSError，不转换） | tests.test_data.TestUtilities.test_csv_roundtrip_exact、B10、B11 |
| GenSpec.check / generate | DataError、TypeError | 字段越界 / 类型错 → DataError；未知字段名 → TypeError（dataclass 构造，不转换） | tests.test_data.TestValidation.test_spec_check、B06 |
| BrokerConfig.check / Broker() | ConfigError | §3.4 任一字段越界、bool 当实数、非有限 | tests.test_broker.TestConfigAndValidation.test_config_check |
| Broker.submit | OrderError、rejected | side ∉ {+1,-1}、qty 非正 / 非有限 / bool / str、未知类型、非市价缺价或价非正、冰山 display 非正、symbol 非 str 或未知 → OrderError；bankrupt / short not allowed → rejected | tests.test_broker.TestConfigAndValidation.test_order_validation、tests.test_broker.TestAccounting.test_liquidation_and_bankruptcy、tests.test_broker.TestAccounting.test_short_toggle |
| Broker.on_bar | rejected、cancelled | §3.2 的 margin / reduce_only / short not allowed / expired / liquidation | tests.test_broker.TestAccounting.test_margin_rejection_and_leverage、tests.test_broker.TestReduceOnlyAndMargin.test_partial_fill_then_margin_failure_is_cancelled_not_rejected、tests.test_broker.TestMatching.test_cancel_expire_and_order_sequence |
| Engine() / run_backtest | ConfigError、DataError | 无数据 / 无策略 / 各标的长度不一 → ConfigError；任一标的 validate 失败 → DataError | tests.test_engine.TestEngineSemantics.test_construction_errors |
| Strategy() / Strategy.on_start | ConfigError | §1.2 | test_contract.TestStrategyInterface.test_init_rules、tests.test_engine.TestEngineSemantics.test_strategy_helpers |
| Context.indicator | ConfigError | 未知指标名、指标参数非法；symbol=未知标的 → ConfigError | tests.test_engine.TestEngineSemantics.test_strategy_helpers、B24、B50 |
| Context.data（strict） | LookaheadError | 访问下标 > ctx.index | tests.test_engine.TestEngineSemantics.test_strict_mode_guards、tests.test_engine.TestEngineSemantics.test_guarded_column_semantics |
| Context.target | OrderError | qty 为 ±inf（差额非有限）；NaN 静默不下单；未知标的 → OrderError（ctx.position 同） | B27、B50 |
| indicators.REGISTRY[*] | ConfigError | 窗口非正 int、非窗口参数非有限或越界 | tests.test_indicators.TestGenericProperties.test_window_validation |
| metrics.var_cvar / metrics.compute | ConfigError | alpha ∉ (0, 1] 或非实数 | B32 |
| optimizer.grid / random_search | ConfigError | 空参数空间、某参数候选为空；random_search 的 n 为负 / 非 int | tests.test_optimizer.TestParamSpace.test_grid、B33、B48 |
| optimizer.walk_forward | ConfigError | n_folds < 1 或非 int、train_frac ∉ (0,1)、每折 < 10 根、空组合列表 | tests.test_optimizer.TestWalkForward.test_walk_forward_validation、B35、B36 |
| optimizer.make_task | ConfigError | 未知策略名；objective 不是 metrics.compute 的键 | B49 |
| optimizer.evaluate / load_data | DataError、ConfigError、FileNotFoundError | data_spec 非法 → DataError；broker / 策略参数非法 → ConfigError；csv 不存在 → FileNotFoundError | B37、B11 |

**类型前提（不保证受控，属调用方编程错误）**：给 `Broker.cancel` / `ctx.cancel` 传非 Order 对象；`ctx.position` / `ctx.target` /
`ctx.indicator(symbol=…)` 传不在数据里的标的已不属于类型前提（position / target → OrderError，indicator → ConfigError，见 B50）；
给 `Engine` 传非 Strategy 对象或非 dict / OHLCV 的数据；`metrics.compute` 的 `ts` 短于 `equity`；`report.equity_svg` 的 `ts` 短于 `equity`；
`report.fmt` 传非数值；`make_task` 的 data_spec 为 None；`Checkpoint` 路径所在目录不存在（sqlite3.OperationalError）；
`Broker.on_bar(i)` 的 i 超出数据长度（Engine 保证不会）。§9 当前没有标「待修」的行（审核 2026-10-22 修完 B10 / B25 / B31 / B35）。

---

## 5. 费用 / 滑点 / 资金费小结

见 §3.3；全部金额以计价货币计，`fees` 与 `funding` 都是「已经从 cash 里扣掉的累计量」，报告里不再重复扣除。

---

## 6. 数据格式（`qbt/data.py`）

### 6.1 `OHLCV` 列

<!-- contract-table: ohlcv_columns -->

| 名字 | 类型 | 规则 |
|---|---|---|
| symbol | str | 非空 |
| ts | list[int] | 秒级时间戳，严格递增 |
| open | list[float] | > 0，∈ [low, high] |
| high | list[float] | > 0，≥ open、close、low |
| low | list[float] | > 0，≤ open、close、high |
| close | list[float] | > 0，∈ [low, high] |
| volume | list[float] | ≥ 0（0 = 停牌：该 bar 任何订单不成交） |
| bar_seconds | int 或 None | 构造时未给且 n ≥ 2 则取 ts[1] - ts[0]；n ≤ 1 时为 None（metrics / report 需要它，接入方自己传） |

### 6.2 `validate()` 规则（任一失败抛 DataError；通过后缓存 `_validated`）

示例改动作用在 `generate(n=20, seed=1)` 上；判据逐个执行并核对消息，且每行恰好对应 `validate` / `_check_finite` 源码里的一条 `raise DataError`（8 条）。

<!-- contract-table: validation -->

| 规则 | 示例改动（分号分隔，逐个执行） | DataError 消息（包含） |
|---|---|---|
| 六列等长 | volume.pop() | columns have different lengths |
| symbol 非空 str | symbol="" | symbol must be a non-empty str |
| 元素必须是数值（str / None 不行） | close[10]="100"; close[10]=None; ts[10]="1" | non-numeric value |
| 元素必须有限（NaN / ±inf） | close[10]=nan; volume[10]=inf; open[10]=-inf; ts[10]=nan | non-finite value |
| 价格严格为正（0 / 负；只需查 low，其余列由区间规则兜住） | low[10]=0; low[10]=-1 | prices must be strictly positive |
| volume 非负 | volume[10]=-1e-9 | volume must be non-negative |
| low ≤ open, close ≤ high | high[10]=low[10]-1e-6; open[10]=high[10]+1e-6; close[10]=low[10]-1e-6; open[10]=low[10]-1e-6 | bar range violated |
| ts 严格递增（重复 / 倒退） | ts[10]=ts[9]; ts[10]=ts[9]-1 | timestamps must be strictly increasing |

n = 0 的空序列合法；volume = 0 合法。

### 6.3 CSV 格式（`to_csv` / `from_csv`）

<!-- contract-table: csv -->

| 项 | 值 |
|---|---|
| header | ts,open,high,low,close,volume |
| separator | , |
| ts_parse | int(float(x)) |
| value_parse | float(x) |
| write_format | repr(x)（最短往返表示：读回逐字节相同指纹） |
| bad_header | DataError |
| bad_row | DataError（字段数 ≠ 6，含空行） |
| missing_file | FileNotFoundError |
| symbol_arg | from_csv(path, symbol='CSV', bar_seconds=None)：symbol 与 bar_seconds 不在文件里，由参数给出 |

### 6.4 `GenSpec`（合成数据规格，frozen dataclass；`check()` 失败抛 DataError）

<!-- contract-table: genspec_fields -->

| 字段 | 默认值（JSON） | 约束 |
|---|---|---|
| n | 10000 | 非负 int（bool 不算） |
| seed | 0 | int / str / bytes |
| symbol | "SYN" | 非空 str |
| s0 | 100.0 | 有限实数 > 0 |
| mu | 0.05 | 有限实数（年化漂移） |
| sigma | 0.4 | 有限实数 ≥ 0（年化波动率） |
| bar_seconds | 60 | 有限实数 > 0 |
| start_ts | 1600000000 | 有限实数 |
| jump_prob | 0.001 | [0, 1] |
| jump_sigma | 0.02 | 有限实数 |
| halt_prob | 0.0005 | [0, 1]；1.0 = 全停牌 |
| halt_len | 5 | 有限实数 ≥ 1 |
| gap_prob | 0.002 | [0, 1] |
| gap_sigma | 0.005 | 有限实数 |
| base_volume | 1000.0 | 有限实数 > 0 |
| vol_sigma | 0.6 | 有限实数 |
| wick | 0.6 | 有限实数 |

---

## 7. 结果 JSON（`qbt/report.py`、`qbt/optimizer.py`）

### 7.1 `metrics.compute(equity, ts, bar_seconds, trades)` 的键（= 报告 JSON 的 metrics 段）

<!-- contract-table: metrics_keys -->

| 键 | 类型 | 含义 |
|---|---|---|
| bars | int | len(equity) |
| start_equity | float | equity[0]；空为 0.0 |
| end_equity | float | equity[-1]；空为 0.0 |
| total_return | float | end / start - 1 |
| cagr | float | 复合年化；end ≤ 0 → -1.0；< 2 根 → 0.0；溢出截断为 1e300 |
| sharpe | float | 年化夏普（ddof=1）；波动为 0 或 < 2 个收益 → 0.0 |
| sortino | float | 年化索提诺 |
| max_drawdown | float | 最大回撤比例 ≥ 0 |
| max_drawdown_bars | int | 最大回撤从峰值到恢复（或到末尾）的 bar 数 |
| max_drawdown_days | float | 上一项 ÷ 每日 bar 数 |
| drawdown_recovered | bool | 最大回撤是否已恢复 |
| longest_drawdown_bars | int | 最长水下 bar 数 |
| calmar | float 或 None | cagr ÷ max_drawdown；无回撤为 None |
| var_bar | float | 单根历史 VaR（正数 = 损失） |
| cvar_bar | float | 单根历史 CVaR |
| var_daily | float | 日频 VaR |
| cvar_daily | float | 日频 CVaR |
| alpha | float | VaR 置信参数（默认 0.05） |
| volatility_annual | float | 年化波动率 |
| monthly | list | [{year, month, ret}] 月度收益表 |
| n_trades | int | 往返交易数 |
| win_rate | float | 盈利往返占比；无交易 0.0 |
| profit_factor | float 或 None | 毛利 ÷ 毛亏；无亏损为 None |
| avg_win | float | 平均盈利（盈利往返） |
| avg_loss | float | 平均亏损（亏损往返，负数） |
| expectancy | float | 平均每笔 pnl |
| avg_bars | float | 平均持有 bar 数 |
| gross_profit | float | 盈利之和 |
| gross_loss | float | 亏损绝对值之和 |

### 7.2 `report.write_report(result, out_dir, name)` 写出的文件

<!-- contract-table: report_files -->

| 键 | 文件 / 内容 |
|---|---|
| txt | <name>.txt：文本报告（参数、账户、指标表、月度表） |
| svg | <name>.svg：权益折线 + 回撤面积（标准库拼接，确定性输出） |
| json | <name>.json：{"summary": Result.summary(), "metrics": §7.1}，sort_keys、indent=1 |

### 7.3 优化器输出（`qbt.optimizer`）

<!-- contract-table: optimizer_keys -->

| 对象 | 键（逗号分隔） |
|---|---|
| task | strategy, data_spec, broker, objective, symbol, slice, id |
| evaluate | key, params, metrics, objective, raw_objective |
| evaluate.metrics | total_return, cagr, sharpe, sortino, max_drawdown, calmar, win_rate, profit_factor, n_trades, cvar_daily, end_equity, fills |
| search_json | task, n_results, best, results |
| walk_forward | folds, is_mean_objective, oos_mean_objective |
| fold | train, test, best, in_sample, out_of_sample, is_objective, oos_objective |
| checkpoint_table | task, key, result |

`objective`：目标度量为 None 或 n_trades = 0 时置为 -1e300（不参与选优），原值保留在 `raw_objective`。
`run_search` 返回按 `key`（参数的规范 JSON）排序的列表，与进程数、完成顺序无关。

---

## 8. 指标注册表（`qbt/indicators.py` 的 `REGISTRY`，`ctx.indicator(name, ...)` 的 name）

输入为等长 list，输出等长；预热期为 None；全部因果。输出列数 > 1 的返回 tuple。

<!-- contract-table: indicators -->

| 名字 | 输出列数 |
|---|---|
| sma | 1 |
| ema | 1 |
| wma | 1 |
| hma | 1 |
| dema | 1 |
| tema | 1 |
| kama | 1 |
| rsi | 1 |
| macd | 3 |
| roc | 1 |
| momentum | 1 |
| trix | 1 |
| rolling_max | 1 |
| rolling_min | 1 |
| donchian | 3 |
| stochastic | 2 |
| williams_r | 1 |
| aroon | 2 |
| true_range | 1 |
| atr | 1 |
| rolling_std | 1 |
| bollinger | 3 |
| zscore | 1 |
| keltner | 3 |
| adx | 3 |
| cci | 1 |
| obv | 1 |
| vwap | 1 |
| mfi | 1 |
| cmf | 1 |
| ichimoku | 5 |
| parabolic_sar | 2 |
| supertrend | 2 |
| rolling_drawdown | 1 |
| rolling_max_drawdown | 1 |
| linreg_slope | 1 |

---

## 9. 边界 → 判据位置

规则：裸异常 = 红。受控 = QBTError 子类、§3.2 的状态、或本表写明的正常返回。
「判据位置」是 `tests.模块.类.方法`，或 `Bxx` 表示 `tests/test_boundaries.py` 里带 `# contract: Bxx` 标注的判据；
`test_contract` 核对每个引用都存在、Bxx 标注与本表一一对应、且「期望结果」标「待修」的行恰好是那些 `expectedFailure` 的判据
（修好代码后把该行的「待修」与判据上的装饰器一起删掉，否则 unexpected success 会红）。

<!-- contract-table: boundaries -->

| 编号 | 入口 | 边界 | 期望结果 | 判据位置 |
|---|---|---|---|---|
| B01 | data.generate / OHLCV.validate | 空数据 n=0 | 合法空序列：len 0，validate 通过 | tests.test_data.TestValidation.test_empty_is_valid |
| B02 | data.generate | 单根 n=1 | 1 根，bar_seconds = 规格值，validate 通过 | B02 |
| B03 | data.generate | 全停牌 halt_prob=1.0 | 全部 volume=0、OHLC 恒等于 s0，validate 通过 | B03 |
| B04 | OHLCV.validate | 价格 0 / 负 / NaN / ±inf，volume 负 / NaN / ±inf | DataError | tests.test_data.TestValidation.test_rejects_nan_inf、tests.test_data.TestValidation.test_rejects_non_positive_price、tests.test_data.TestValidation.test_rejects_negative_volume、B04 |
| B05 | data.GenSpec / generate | n < 0、s0 ≤ 0、sigma < 0、概率出 [0,1]、NaN / bool / str 字段 | DataError | tests.test_data.TestValidation.test_spec_check |
| B06 | data.GenSpec / GenSpec.from_dict | 未知字段名 | TypeError（dataclass 构造；类型前提，不转换） | B06 |
| B07 | OHLCV.from_csv | 只有表头 / 单行 | 0 根 / 1 根；bar_seconds=None（推断不出，需显式传入） | B07 |
| B08 | OHLCV.from_csv | 坏表头、字段数 ≠ 6、空行 | DataError | tests.test_data.TestUtilities.test_csv_roundtrip_exact |
| B09 | OHLCV.from_csv + validate | 价格 nan / inf / 0 / 负、volume 负 的数值 token | from_csv 解析通过，validate 抛 DataError | B09 |
| B10 | OHLCV.from_csv | 非数值 token（abc）、空字段、ts=nan、ts=1e400 | DataError（含行号；审核 2026-10-22 修复，此前为裸 ValueError / OverflowError） | B10 |
| B11 | OHLCV.from_csv / optimizer.evaluate(csv) | 文件不存在 | FileNotFoundError（OSError，不转换） | B11 |
| B12 | Broker.submit | qty 0 / 负 / -0.0 / NaN / inf / bool / str；qty=1e308 | OrderError；1e308 接受后撮合时 rejected(margin) | tests.test_broker.TestConfigAndValidation.test_order_validation、B12 |
| B13 | Broker.submit | 限价 / 止损 / 止盈 / 冰山的 price 0 / 负 / NaN / ±inf / bool，display 0 | OrderError | tests.test_broker.TestConfigAndValidation.test_order_validation、B13 |
| B14 | Broker(data={}) | 空数据集 | equity = cash，on_bar(0) 返回 cash，submit 任何标的 → OrderError | B14 |
| B15 | Broker.on_bar | 单根数据 | 订单永远 pending（没有下一根撮合），equity = cash | B15 |
| B16 | Broker.on_bar | 全停牌（volume 全 0） | 市价 / 限价 / 止损 / 冰山全部不成交、保持 pending，equity 恒等于 cash | tests.test_broker.TestMatching.test_zero_volume_bar_no_fill、B16 |
| B17 | Broker / BrokerConfig | max_leverage=1e6 | 巨量订单成交、cash 为负（借入）、恒等式成立；价格下跌触发 liquidation 与 bankrupt | B17 |
| B18 | Broker / BrokerConfig | max_leverage=1e-6；max_leverage=inf / 0 | 任何开仓单 rejected(margin)，减仓单仍允许；inf / 0 → ConfigError | tests.test_broker.TestConfigAndValidation.test_config_check、B18 |
| B19 | Engine / run_backtest | 空数据 n=0（6 个策略 × 普通 / strict） | Result.n=0、equity=[]、final_equity=cash、summary() 可 JSON | B19 |
| B20 | Engine / run_backtest | 单根 n=1（6 × 普通 / strict） | len(equity)=1，0 成交，summary() 可 JSON | B20 |
| B21 | Engine / run_backtest | 全停牌 n=300（6 × 普通 / strict） | 0 成交，equity 全等于 cash，fees=0 | B21 |
| B22 | Engine / run_backtest | 价格 0 / 负 / NaN / inf，volume NaN | DataError（Engine 构造时 validate） | tests.test_engine.TestEngineSemantics.test_construction_errors、B22 |
| B23 | Engine / run_backtest | max_leverage=1e6 与 1e-6（6 个策略） | 无异常；1e-6 下 0 成交且拒单 reason 全为 margin；1e6 下恒等式成立 | B23 |
| B24 | 策略参数 → Context.indicator | 窗口 0 / 负 | ConfigError（指标层 _check_window） | tests.test_indicators.TestGenericProperties.test_window_validation、B24 |
| B25 | strategies.grid | levels=0 / 负、spacing=0 / 负 | ConfigError（on_start 校验 levels ≥ 1 且 spacing > 0；qty_frac=0 合法，见 B26；审核修复，此前为裸 ZeroDivisionError） | B25 |
| B26 | 策略参数 | size=0 / qty_frac=0 / risk=0 | 0 成交、无异常（size_qty 返回 0 → target 不下单） | B26 |
| B27 | Context.target | qty = NaN / ±inf | NaN：静默不下单、返回 None；±inf：OrderError | B27 |
| B28 | metrics.compute | 零长度权益 | bars=0，其余 0.0 / None / []，无异常 | tests.test_metrics.TestMetricsEdges.test_drawdown_indices_and_empty_compute_fields |
| B29 | metrics.compute | 单点权益 | bars=1；无收益 → sharpe / sortino / cagr 0.0、var 0、monthly 1 行 | B29 |
| B30 | metrics.compute | 权益含 NaN / inf / 0 / 负 | 无异常；end ≤ 0 → cagr=-1.0；NaN 透传不抛 | B30 |
| B31 | metrics.compute / report.write_report | bar_seconds=None / 0 / 负 / NaN / inf / bool（from_csv 的 ≤ 1 行数据推断不出 bar_seconds） | ConfigError（periods_per_year 校验为有限正数；审核修复，此前为裸 TypeError / ZeroDivisionError） | B31 |
| B32 | metrics.var_cvar / compute | alpha ∉ (0, 1] 或非实数 | ConfigError | B32 |
| B33 | optimizer.grid / random_search | 空参数空间 / 某参数候选为空 | ConfigError | tests.test_optimizer.TestParamSpace.test_grid、B33 |
| B34 | optimizer.run_search / best | 空组合列表 | 返回 []，best([]) 为 None | B34 |
| B35 | optimizer.walk_forward | 空组合列表（含去重后为空） | ConfigError（审核修复，此前 best 返回 None 后被下标 → 裸 TypeError） | B35 |
| B36 | optimizer.walk_forward | n=0 / n=1 数据 | ConfigError（too few bars per fold） | B36 |
| B37 | optimizer.evaluate | n=0 / n=1 / 全停牌 数据 | 无异常；n_trades=0 → objective=-1e300 | B37 |
| B38 | optimizer.evaluate | max_leverage=1e6 / 1e-6 | 无异常；1e-6 → objective=-1e300 | B38 |
| B39 | report.equity_svg | 空 / 单点 | "no data" 文本 / 单点折线 | tests.test_report.TestSvg.test_svg_edge_cases |
| B40 | report.equity_svg | 权益含 NaN / inf / 全 0 | 可解析的 SVG，无异常 | B40 |
| B41 | report.write_report | n=0 / n=1 / 全停牌 的真实 Result | 三个文件齐全，JSON 可读且键集合符合 §7 | B41 |
| B42 | indicators.REGISTRY | 空输入 / 单根 | 等长输出（空 → []） | tests.test_indicators.TestGenericProperties.test_length_preserved_and_empty、tests.test_indicators.TestGenericProperties.test_single_bar |
| B43 | indicators.REGISTRY | 全停牌（恒定价、volume 0） | 无异常，输出无 NaN / inf | B43 |
| B44 | indicators.REGISTRY | 价格 0 / 负 | 无异常，输出有限（分母为 0 的档位按各指标退化规则给 None / 0 / 50） | B44 |
| B45 | indicators.REGISTRY | 价格 / volume 含 NaN / inf | 无异常；NaN / inf 透传（指标不校验数值，数据层负责） | B45 |
| B46 | indicators.REGISTRY | 窗口 > n | 无异常，等长输出 | B46 |
| B47 | indicators.REGISTRY | 窗口 0 / 负 / 非 int | ConfigError | tests.test_indicators.TestGenericProperties.test_window_validation |
| B48 | optimizer.random_search | n 为负 / 非 int / bool | ConfigError（审核新增；此前负 n 为裸 ValueError） | B48 |
| B49 | optimizer.make_task | 未知策略名；objective 不是 metrics.compute 的键 | ConfigError（审核新增；此前在 evaluate 里裸 KeyError） | B49 |
| B50 | ctx.position / ctx.target / ctx.indicator(symbol=…) | 不在数据里的标的 | position / target → OrderError；indicator → ConfigError（审核新增；此前裸 KeyError） | B50 |
