# lob 接口契约（CONTRACT）

本文件是 `lob` 包对外的**机器可核对**契约。每张表前面都有一行标记
`<!-- contract-table: 名字 -->`，`tests/test_contract.py` 解析标记后的表格并与代码逐项对照：
**表里少一行、代码里少一个常量、值对不上，判据就红。** 表格单元格里不能出现 `|`。
行为描述以 `SPEC.md` 与代码为准；本文只摘录可核对的部分，不另立规则。

核对命令：`python3.12 -m unittest lob.tests.test_contract`（含在 `python3.12 -m lob.cli test --quick` 里）。

---

## 1. 事件流二进制格式（`events.py`）

事件流文件 = 定长 56 字节记录连续拼接，**没有文件头**，小端。
旁边的 `<stream>.meta.json` 描述流（见 §1.5）。`events.RECORD = struct.Struct('<QQHBBBBHQqqq')`。

<!-- contract-table: record -->

| 键 | 值 |
|---|---|
| record_size | 56 |
| struct_format | <QQHBBBBHQqqq |
| byte_order | little |
| n_fields | 12 |
| file_header | none |
| meta_suffix | .meta.json |
| trade_struct_format | <QIHBxQQqq |
| trade_record_size | 48 |

### 1.1 记录字节布局

`F_*` 是解码后 12 元组的下标（`events.RECORD.unpack(rec)` 的顺序）。`Book.process(rec)` 接收的就是这个 12 元组。

<!-- contract-table: record_fields -->

| 序号 | 常量 | 字段 | 偏移 | 长度 | 类型 | 含义 | 允许值 |
|---|---|---|---|---|---|---|---|
| 0 | F_SEQ | seq | 0 | 8 | u64 | 事件全局序号 | 1..n_events，等于记录在文件中的序号（1 起）；replay 用 seq−1 作为 status.bin 下标 |
| 1 | F_TS | ts_ns | 8 | 8 | u64 | 事件时间戳（ns） | 任意；小于本簿上一事件的 ts 计入 `ts_regressions`，不拒绝 |
| 2 | F_SYMBOL | symbol | 16 | 2 | u16 | 品种号 | 0..n_symbols−1，必须是 meta.books 的键（replay 全品种模式否则 ValueError，见 E27） |
| 3 | F_KIND | kind | 18 | 1 | u8 | 事件种类 | 1 NEW、2 CANCEL、3 MODIFY；其他 → REJ_KIND |
| 4 | F_SIDE | side | 19 | 1 | u8 | 买卖方向 | NEW：1 BUY、2 SELL，其他 → REJ_SIDE；CANCEL / MODIFY 不校验（生成器写原单方向，仅 queue_collect 使用） |
| 5 | F_FLAGS | flags | 20 | 1 | u8 | 订单标志位 | NEW：bit0..bit4 的允许组合（§1.3），其他 → REJ_FLAGS；CANCEL / MODIFY 忽略 |
| 6 | F_PAD | pad | 21 | 1 | u8 | 对齐填充 | `pack()` 固定写 0；读取时忽略 |
| 7 | F_TRADER | trader | 22 | 2 | u16 | 交易员号（自成交判定） | 0..65535 |
| 8 | F_OID | oid | 24 | 8 | u64 | 订单号 | 任意（0 合法）；NEW 不得与当前挂单重复 → REJ_DUP |
| 9 | F_PRICE | price | 32 | 8 | i64 | 价格（tick 整数） | NEW 非市价：> 0、tick 整数倍、在 ref ± band 内；MARKET、CANCEL、MODIFY 忽略 |
| 10 | F_QTY | qty | 40 | 8 | i64 | NEW：订单量；MODIFY：新的剩余量 | [min_qty, max_qty] 且 lot 整数倍；CANCEL 忽略 |
| 11 | F_AUX | aux | 48 | 8 | i64 | NEW+ICEBERG：显示量 | lot_size ≤ aux ≤ qty 且 lot 整数倍 → 否则 REJ_DISPLAY；其他事件忽略 |

### 1.2 枚举

<!-- contract-table: kind -->

| 常量 | 值 | 含义 |
|---|---|---|
| KIND_NEW | 1 | 新订单 |
| KIND_CANCEL | 2 | 撤单（整单） |
| KIND_MODIFY | 3 | 改量（qty = 新剩余量；不能改价） |

<!-- contract-table: side -->

| 常量 | 值 | 含义 |
|---|---|---|
| SIDE_BUY | 1 | 买 |
| SIDE_SELL | 2 | 卖 |

<!-- contract-table: flags -->

| 常量 | 位值 | 含义 |
|---|---|---|
| FLAG_MARKET | 1 | 市价单：无限价，只吃 ref ± band 内档位，剩余撤销 |
| FLAG_IOC | 2 | 立即成交否则撤销剩余 |
| FLAG_FOK | 4 | 全部成交否则整单拒绝（REJ_FOK，簿不变） |
| FLAG_POST_ONLY | 8 | 只挂不吃：价格交叉即拒绝（REJ_POST_ONLY） |
| FLAG_ICEBERG | 16 | 冰山：aux 为显示量 |

### 1.3 标志位组合规则（`common.flags_valid`）

`flags_valid(flags)` 为假 → NEW 返回 REJ_FLAGS。下表穷举 0、单标志、所有两两组合与一个未知位。

<!-- contract-table: flag_rules -->

| flags 值 | 组合 | 允许 | 说明 |
|---|---|---|---|
| 0 | 无 | 是 | 普通限价单 |
| 1 | MARKET | 是 | |
| 2 | IOC | 是 | |
| 4 | FOK | 是 | |
| 8 | POST_ONLY | 是 | |
| 16 | ICEBERG | 是 | |
| 3 | MARKET+IOC | 是 | 市价单本就撤销剩余 |
| 5 | MARKET+FOK | 是 | 市价 FOK：带内可成量 < qty 则 REJ_FOK |
| 9 | MARKET+POST_ONLY | 否 | 市价单不可能只挂不吃 |
| 17 | MARKET+ICEBERG | 否 | 市价单不挂簿 |
| 6 | IOC+FOK | 否 | 互斥 |
| 10 | IOC+POST_ONLY | 否 | 互斥 |
| 18 | IOC+ICEBERG | 是 | 冰山从不挂入（I9） |
| 12 | FOK+POST_ONLY | 否 | 互斥 |
| 20 | FOK+ICEBERG | 是 | |
| 24 | POST_ONLY+ICEBERG | 是 | 挂入后显示 aux |
| 32 | 未知位 bit5 | 否 | 任何 bit5..bit7 → REJ_FLAGS |

### 1.4 文件级规则

* `iter_records(path, symbols=None, start=0, stop=None)`：按记录号 [start, stop) 读取；`start`/`stop` 为负 → ValueError（E18）。
* 文件长度不是 56 的整数倍：`iter_records` / `count_records` **静默丢弃尾部残缺字节**（E19）；`replay` 则拒绝（E26）。
* 空文件：`iter_records` 产出 0 条，`count_records` = 0。
* 文件不存在：FileNotFoundError（E20、E31），不转换为 ValueError。
* `pack(...)`：任一字段超出定宽范围 → ValueError（E17）。

### 1.5 `<stream>.meta.json`

由 `events.generate` 写出；`replay` / `compare` / `queue-collect` 通过 `load_meta` 读取。

<!-- contract-table: meta_keys -->

| 键 | 类型 | 含义 |
|---|---|---|
| params | object | `GenParams.to_dict()`：全部生成器参数（含 seed、n_symbols） |
| n_events | int | 记录数；replay 要求文件恰好有这么多条（E26） |
| record_size | int | 56 |
| sha256 | string | 流文件的 sha256（同 seed 同参数任何机器逐字节相同） |
| counts | object | 生成的事件计数：new、cancel、modify、bad |
| last_oid | int | 生成器分配的最后一个 oid |
| epoch_ns | int | 时间戳起点 EPOCH_NS |
| regimes | array | [symbol, start_ns, end_ns, name] 列表（name ∈ withdraw、shock） |
| books | object | 每个品种的 BookConfig（键 = 品种号字符串，值 = §5 的 7 个字段，不含 symbol） |

`GenParams`：未知参数名 → KeyError（既有契约 E21）；越界 / 非 int → ValueError（`GenParams.validate`，E22）：
`n_events ≥ 0`、`1 ≤ n_symbols ≤ 65536`、`rate > 0`、`depth_scale > 0`、`tick_size, lot_size ≥ 1`、
`1 ≤ min_qty ≤ max_qty ≤ (2^63−1)//1000`、`min_qty % lot_size == 0`、`0 ≤ band ≤ 2^60`、`1 ≤ base_price ≤ 2^60`、
`1 ≤ n_traders ≤ 65536`、`target_resting ≥ 1`。

---

## 2. 状态码表（`common.py`）

`Book.process(rec)` / `ReferenceBook.process(rec)` 每个事件返回一个整数状态码；`replay` 把它写入 `status.bin`（每事件 1 字节）。
0..3 是"可能产生成交"的 NEW 结果（检查器只对这四种扫描成交），4 是无成交撤销，5/6 是 CANCEL / MODIFY 确认，10..20 是拒绝。

<!-- contract-table: status -->

| 码 | 常量 | 名称 | 含义 | 触发输入 | 计数器 |
|---|---|---|---|---|---|
| 0 | ST_RESTED | RESTED | NEW 零成交、全部挂入 | 不交叉的限价单、POST_ONLY、冰山 | submitted += qty，resting += qty |
| 1 | ST_FILLED | FILLED | NEW 剩余为 0 | 全部成交（含 FOK 成功、市价单吃满） | submitted += qty，filled += 2×成交量 |
| 2 | ST_PARTIAL_RESTED | PARTIAL_RESTED | NEW 部分成交、剩余挂入 | 限价单吃完交叉档位后剩余 | 同上 + resting += 剩余 |
| 3 | ST_PARTIAL_CANCELLED | PARTIAL_CANCELLED | NEW 部分成交、剩余撤销 | IOC / MARKET 流动性不足；cancel_new 自成交中途停止 | 同上 + cancelled += 剩余 |
| 4 | ST_CANCELLED | CANCELLED | NEW 零成交、整单撤销 | IOC / MARKET 无可成交（含空簿）；cancel_new 自成交在首笔前停止 | submitted += qty，cancelled += qty |
| 5 | ST_ACK_CANCEL | ACK_CANCEL | CANCEL 成功 | oid 在簿 | cancelled += rem，resting −= rem |
| 6 | ST_ACK_MODIFY | ACK_MODIFY | MODIFY 成功（含 q == rem 的无操作） | oid 在簿且新量合法 | 减量：cancelled += 差额；增量：submitted += 差额 |
| 10 | REJ_KIND | REJ_KIND | 未知事件种类 | kind ∉ {1,2,3} | 不变（只 n_events += 1） |
| 11 | REJ_SIDE | REJ_SIDE | 未知方向 | NEW 的 side ∉ {1,2} | submitted += qty，rejected += qty |
| 12 | REJ_FLAGS | REJ_FLAGS | 标志位非法 | NEW 的 flags 含未知位或 §1.3 的禁止组合 | 同上 |
| 13 | REJ_DUP | REJ_DUP | 订单号重复 | NEW 的 oid 已在簿 | 同上 |
| 14 | REJ_QTY | REJ_QTY | 数量非法 | NEW 的 qty 或 MODIFY 的新量 ∉ [min_qty, max_qty] 或非 lot 倍（含 0、负、2^63−1） | NEW：同上；MODIFY：不变 |
| 15 | REJ_PRICE | REJ_PRICE | 价格非法 | NEW 非市价 price ≤ 0 或非 tick 倍 | 同上 |
| 16 | REJ_BAND | REJ_BAND | 价格出带 | NEW 非市价 price − ref 的绝对值 > band | 同上 |
| 17 | REJ_DISPLAY | REJ_DISPLAY | 冰山显示量非法 | NEW+ICEBERG 的 aux < lot_size、> qty 或非 lot 倍 | 同上 |
| 18 | REJ_POST_ONLY | REJ_POST_ONLY | 只挂单会交叉 | NEW+POST_ONLY 价格 ≥ 最优卖（买）或 ≤ 最优买（卖），只看价格不看自成交 | 同上 |
| 19 | REJ_FOK | REJ_FOK | 全成单不可全成 | NEW+FOK 纯模拟可成量 < qty（含空簿、限价外、cancel_new 被自单挡住） | 同上，簿不变 |
| 20 | REJ_UNKNOWN | REJ_UNKNOWN | 订单不在簿 | CANCEL / MODIFY 的 oid 不在簿（从未出现、已成交、已撤） | 不变 |

守恒（检查器每事件核对）：`submitted == filled + resting + cancelled + rejected`。

### 2.1 NEW 校验顺序（第一个失败者决定状态码）

<!-- contract-table: new_order -->

| 步 | 条件 | 状态码 |
|---|---|---|
| 0 | kind ∉ {1,2,3}（对任何记录） | REJ_KIND |
| 1 | side ∉ {1,2} | REJ_SIDE |
| 2 | flags 非法（§1.3） | REJ_FLAGS |
| 3 | oid 与当前挂单重复 | REJ_DUP |
| 4 | qty < min_qty 或 qty > max_qty 或 qty % lot_size ≠ 0 | REJ_QTY |
| 5 | 非市价：price ≤ 0 或 price % tick_size ≠ 0 | REJ_PRICE |
| 6 | 非市价：price − ref 的绝对值 > band | REJ_BAND |
| 7 | ICEBERG：aux < lot_size 或 aux > qty 或 aux % lot_size ≠ 0 | REJ_DISPLAY |
| 8 | POST_ONLY：对手方最优价与本单价格交叉 | REJ_POST_ONLY |
| 9 | FOK：纯模拟可成量 < qty | REJ_FOK |

POST_ONLY 与 FOK 互斥（第 2 步已拒绝），所以 8、9 两步之间的先后不可观测；判据核对其余每对相邻步骤的先后，以及 7 在 8、9 之前。

### 2.2 通过校验的 NEW 的结果

<!-- contract-table: new_outcomes -->

| 情形 | 状态 |
|---|---|
| 撮合后剩余 = 0 | FILLED |
| 剩余 > 0，且（IOC 或 MARKET 或 cancel_new 自成交停止），已有成交 | PARTIAL_CANCELLED |
| 剩余 > 0，且（IOC 或 MARKET 或 cancel_new 自成交停止），无成交 | CANCELLED |
| 剩余 > 0，挂入簿，已有成交 | PARTIAL_RESTED |
| 剩余 > 0，挂入簿，无成交 | RESTED |

---

## 3. 撮合语义摘要（规范见 `SPEC.md`，两套引擎 `book.py` / `reference.py` 必须逐笔一致）

* **价格优先、时间优先。** 买单与卖方 `price ≤ limit` 的档位撮合（卖单对称），档内 FIFO。
  成交价 = 被动单价格。每簿一个全局计数器 `prio`，在挂入 / 冰山露出 / 增量改单时递增；同档存活订单 `prio` 严格递增。
* **参考价 `ref`**：初值 = `BookConfig.ref_price`，之后 = 本簿最近一笔成交价；价格带 = `[ref − band, ref + band]`。
* **MARKET**：无限价，limit = 带边界（买 `ref + band`，卖 `ref − band`）；剩余撤销（CANCELLED / PARTIAL_CANCELLED）。price 字段忽略。
* **IOC**：撮合后剩余撤销。**FOK**：先纯模拟（不改簿）可成量，不足则 REJ_FOK，簿不变；足则撮合，必 FILLED。
* **POST_ONLY**：只看价格是否与对手最优交叉（等价也算交叉），交叉即 REJ_POST_ONLY；不交叉则挂入。
* **ICEBERG**：挂入时 `vis = min(aux, rem)`；一片吃完且 `rem > 0` → `vis = min(aux, rem)`，**排到该档队尾并获得新 prio**；FOK 模拟计入隐藏量。
* **自成交（STP）**：对手队首 `trader` 与新单相同时：`cancel_new` → 新单剩余全部撤销、停止撮合；`cancel_old` → 撤销对手单（计 `cancelled` 与 `stp_cancels`），继续。品种号偶数用 `cancel_new`、奇数用 `cancel_old`（`events.book_config_for`）。
* **CANCEL**：oid 不在簿 → REJ_UNKNOWN；否则移除整单，`cancelled += rem`。记录的 side / price 不参与判断。
* **MODIFY（只改量）**：新量不合法 → REJ_QTY（簿不变）；`q == rem` 无操作；`q < rem` 减量**保留队位**（`vis = min(vis, q)`）；`q > rem` 增量**排到队尾**（新 prio，`vis = min(display, q)`）。
* **拒绝的 NEW 的 qty 计入 `submitted` 与 `rejected`**（含 qty ≤ 0 的情况，两边同加同减，守恒不破）。

---

## 4. 订单 / 成交 / 队列数据结构

### 4.1 `Book` 订单（`book.py` 的 `O_*`，`Book.live_orders()` 返回这种 list）

<!-- contract-table: order_slots -->

| 下标 | 常量 | 含义 |
|---|---|---|
| 0 | O_OID | 订单号 |
| 1 | O_SIDE | 1 买 2 卖 |
| 2 | O_PRICE | 价格（tick） |
| 3 | O_REM | 剩余总量（含冰山隐藏量） |
| 4 | O_VIS | 当前可见量（≤ rem；非冰山 = rem） |
| 5 | O_DISP | 冰山显示量（非冰山为 0） |
| 6 | O_TRADER | 交易员号 |
| 7 | O_PRIO | 时间优先序号 |
| 8 | O_SLOT | 在所在档位队列 list 中的下标 |

`ReferenceBook.live_orders()` 返回 `RefOrder` 对象，属性 `oid, side, price, rem, vis, display, trader, prio`，语义同上。

### 4.2 档位队列常量（`book.py`）

每档一个 list：`[head_index, dead_count, order, order, ...]`，撤单位置置 `None`（惰性删除），达到阈值后压实。

<!-- contract-table: queue_consts -->

| 常量 | 值 | 含义 |
|---|---|---|
| Q_HEAD | 0 | 队首下标所在槽位 |
| Q_DEAD | 1 | 队列中 None 槽位计数所在槽位 |
| Q_FIRST | 2 | 第一个订单槽位 |
| COMPACT_MIN_DEAD | 32 | 撤单压实阈值：dead ≥ 32 且 dead×2 ≥ 订单槽位数 |
| COMPACT_MIN_HEAD | 64 | 成交压实阈值：head ≥ 64 且 head×2 ≥ list 长度 |

### 4.3 内存成交元组（`common.py` 的 `T_*`，`book.trades` 与 `reference.trades` 的元素）

<!-- contract-table: trade_slots -->

| 下标 | 常量 | 含义 |
|---|---|---|
| 0 | T_SEQ | 产生成交的事件 seq |
| 1 | T_IDX | 同一事件内的成交序号（0 起，按撮合顺序） |
| 2 | T_SYMBOL | 品种号（= BookConfig.symbol） |
| 3 | T_AGGR_SIDE | 主动方 side（1 买 2 卖） |
| 4 | T_BUY_OID | 买方订单号 |
| 5 | T_SELL_OID | 卖方订单号 |
| 6 | T_PRICE | 成交价 = 被动单价格 |
| 7 | T_QTY | 成交量（≥ 1） |
| 8 | T_BUY_TRADER | 买方交易员 |
| 9 | T_SELL_TRADER | 卖方交易员 |

### 4.4 `trades.bin` 成交记录（`replay.TRADE`，48 字节，小端，按 (seq, idx) 升序）

交易员号**不**写入文件。

<!-- contract-table: trade_record -->

| 序号 | 字段 | 偏移 | 长度 | 类型 | 含义 |
|---|---|---|---|---|---|
| 0 | seq | 0 | 8 | u64 | 事件 seq |
| 1 | idx | 8 | 4 | u32 | 事件内序号 |
| 2 | symbol | 12 | 2 | u16 | 品种号 |
| 3 | aggr_side | 14 | 1 | u8 | 主动方 side |
| 4 | pad | 15 | 1 | pad | 填充 0 |
| 5 | buy_oid | 16 | 8 | u64 | 买方订单号 |
| 6 | sell_oid | 24 | 8 | u64 | 卖方订单号 |
| 7 | price | 32 | 8 | i64 | 成交价 |
| 8 | qty | 40 | 8 | i64 | 成交量 |

---

## 5. `BookConfig`（`common.py`）

整数配置；`from_dict(d, symbol)` 需要下表除 `symbol` 外的 7 个键，缺键 → ValueError（E16）。`to_dict()` 返回全部 8 个。

<!-- contract-table: config_fields -->

| 字段 | 默认值 | 约束 |
|---|---|---|
| tick_size | 1 | ≥ 1；限价必须是其整数倍 |
| lot_size | 1 | ≥ 1；qty、aux、MODIFY 新量必须是其整数倍 |
| min_qty | 1 | ≥ 1 且是 lot_size 的整数倍 |
| max_qty | 1000000000 | ≥ min_qty |
| band | 1000000 | ≥ 0；价格带半宽（0 表示只接受 price == ref） |
| ref_price | 1000 | ≥ 1；初始参考价（不要求是 tick 倍） |
| stp | "cancel_new" | "cancel_new" 或 "cancel_old" |
| symbol | 0 | 不校验；只作为标签写入成交元组 T_SYMBOL（文件格式限 u16） |

### 5.1 构造时的 ValueError（每行一个示例，判据逐行实例化）

<!-- contract-table: config_errors -->

| 条件 | 示例 kwargs（JSON） |
|---|---|
| tick_size < 1 | {"tick_size": 0} |
| lot_size < 1 | {"lot_size": 0} |
| min_qty < 1 | {"min_qty": 0} |
| band < 0 | {"band": -1} |
| min_qty % lot_size ≠ 0 | {"lot_size": 10, "min_qty": 5} |
| max_qty < min_qty | {"max_qty": 0} |
| ref_price < 1 | {"ref_price": 0} |
| stp 未知 | {"stp": "nope"} |

---

## 6. 回放 / 对拍的输出（`replay.py`、`compare.py`）

### 6.1 `replay(path, out_dir, ...)` / `replay_sharded(path, out_dir, processes, ...)` 写入 `out_dir`

单进程与按品种分片多进程的三个二进制 / 文本输出逐字节一致（品种相互独立）。

<!-- contract-table: replay_files -->

| 文件 | 内容 |
|---|---|
| trades.bin | 48 字节 TRADE 记录（§4.4），按 (seq, idx) 升序 |
| status.bin | 每事件 1 字节状态码（§2），下标 = seq − 1；未被处理的事件（品种子集模式）保持 0 |
| snapshots.jsonl | 每品种每模拟秒一行 Top-10 快照（§6.3），按 (t, sym) 排序 |
| stats.json | §6.2 的统计（含 books） |

### 6.2 返回值 / `stats.json` 的键

CLI `replay --json` 写出的是去掉 `books` 的同一字典。

<!-- contract-table: replay_stats -->

| 键 | 模式 | 含义 |
|---|---|---|
| mode | both | "single" 或 "sharded" |
| processes | sharded | 实际进程数（= 非空分片数） |
| shards | sharded | 每个分片的品种号列表 |
| events | both | 处理的事件数（= Σ 各簿 n_events） |
| trades | both | 成交笔数 |
| volume | both | 成交量（lot） |
| seconds | both | 墙钟秒（3 位小数；性能数字，与机器相关） |
| events_per_second | both | events / seconds（性能数字，与机器相关） |
| check_every | both | 不变量全扫描周期（0 = 关闭） |
| snapshots | both | 快照行数 |
| max_resting_orders | both | 每块读入后统计的挂单数峰值（分片模式为各分片峰值之和） |
| status_counts | both | 状态名 → 次数（只列出非零项） |
| books | both | 品种号字符串 → §6.2.1 |
| ru_maxrss_mb | single | 进程峰值 RSS（MB，性能数字） |

<!-- contract-table: book_stats -->

| 键 | 含义 |
|---|---|
| submitted | 提交量（含被拒 NEW 的 qty、增量改单差额） |
| filled | 成交消耗量 = 2 × Σ成交量 |
| cancelled | 撤销量（撤单、IOC / 市价剩余、减量改单、自成交撤单） |
| rejected | 被拒 NEW 的 qty 之和 |
| resting | 当前挂单剩余量之和（含冰山隐藏量） |
| stp_cancels | cancel_old 策略下被撤的对手单数 |
| ts_regressions | 时间戳回退的事件数 |
| live_orders | 当前挂单数 |
| events | 本簿处理的事件数 |
| ref | 当前参考价 |
| levels | 买卖档位数之和 |

### 6.3 `snapshots.jsonl` 每行

<!-- contract-table: snapshot_keys -->

| 键 | 含义 |
|---|---|
| t | 模拟秒序号（1 起，每品种独立计数） |
| sym | 品种号 |
| bids | [[price, visible_qty], ...] 最优在前，最多 10 档 |
| asks | [[price, visible_qty], ...] 最优在前，最多 10 档 |

### 6.4 `compare_stream(path, ...)` / `compare_sharded(path, processes, ...)` 返回值

`first_divergence` 为 `None` 表示两套引擎在全部事件上状态码、成交、档位、计数器、订单集合一致；CLI `compare` 据此返回 0 / 1。

<!-- contract-table: compare_result -->

| 键 | 模式 | 含义 |
|---|---|---|
| events | both | 对拍的事件数 |
| trades | both | 成交笔数（两引擎一致时计数） |
| volume | both | 成交量 |
| symbols | stream | 对拍的品种列表 |
| first_divergence | both | None 或 §6.5 的字典（分片模式取 seq 最小者） |
| seconds | both | 墙钟秒 |
| resting_orders | both | 结束时 Book 的挂单数 |
| full_checks | both | 不变量全扫描次数 |
| trades_checked | both | 检查器逐笔核对的成交数 |
| shards | sharded | 分片品种列表 |
| shard_seconds | sharded | 各分片墙钟秒 |

### 6.5 分歧字典

<!-- contract-table: divergence -->

| 键 | 含义 |
|---|---|
| seq | 首次分歧的事件 seq |
| symbol | 品种号 |
| what | 分歧种类（下表） |
| book | 快引擎一侧的值（repr，截断 400 字符） |
| reference | 参考引擎一侧的值（repr，截断 400 字符） |

<!-- contract-table: divergence_what -->

| 值 | 含义 |
|---|---|
| status | 同一事件的状态码不同 |
| trades | 同一事件产生的成交元组不同 |
| levels | 周期核对：档位 (price, visible_qty) 列表不同 |
| counters | 周期核对：(submitted, filled, cancelled, rejected, resting, stp_cancels, ref) 不同 |
| order ids | 周期核对：挂单 oid 集合不同 |

---

## 7. 错误契约：哪些输入抛 ValueError、哪些返回 REJ_*、哪些静默

规则：**裸异常（TypeError / IndexError / KeyError / ZeroDivisionError / struct.error / OSError 之外的意外异常）一律算缺陷。**
受控结果只有三种：REJ_* 状态码、本表列出的 ValueError、本表明示的其他异常或静默规则。
每行的编号出现在 `tests/test_boundaries.py` 对应判据的 `# contract: Exx` 标注里；`test_contract` 核对两边一一对应。
类型前提（例如传入 float 或 str 当整数）不在本表之内：那是调用方的编程错误，不做保证。

<!-- contract-table: errors -->

| 编号 | 入口 | 输入 | 结果 |
|---|---|---|---|
| E01 | Book.process / ReferenceBook.process | NEW qty = 0、负、> max_qty、非 lot 倍、2^63−1 | REJ_QTY |
| E02 | 同上 | NEW 非市价 price = 0、负、−2^63、非 tick 倍 | REJ_PRICE |
| E03 | 同上 | NEW 非市价 price 超出 ref ± band（含 2^63−1、band = 0 时的 ref ± 1） | REJ_BAND |
| E04 | 同上 | NEW oid 与挂单重复 | REJ_DUP |
| E05 | 同上 | kind ∉ {1,2,3}（0、9、255） | REJ_KIND，计数器不变 |
| E06 | 同上 | NEW side ∉ {1,2}（0、3、255） | REJ_SIDE |
| E07 | 同上 | NEW flags 未知位（32、255）或禁止组合 | REJ_FLAGS |
| E08 | 同上 | CANCEL / MODIFY 的 oid 不在簿（从未出现、已成交、已撤） | REJ_UNKNOWN，计数器不变 |
| E09 | 同上 | MODIFY 新量 0、负、> max_qty、非 lot 倍、2^63−1 | REJ_QTY，簿与计数器不变 |
| E10 | 同上 | FOK 可成量 < qty（空簿、流动性不足、限价外） | REJ_FOK，簿不变 |
| E11 | 同上 | POST_ONLY 价格与对手最优交叉（含等价） | REJ_POST_ONLY |
| E12 | 同上 | ICEBERG aux = 0、负、> qty、非 lot 倍、2^63−1 | REJ_DISPLAY |
| E13 | 同上 | MARKET 空簿 / 带内流动性不足 | CANCELLED / PARTIAL_CANCELLED（正常状态，不是错误） |
| E14 | 同上 | oid = 0、oid = 2^64−1、trader = 65535、ts 回退 | 正常处理；ts 回退只计入 ts_regressions |
| E15 | BookConfig(...) | §5.1 的任一非法组合 | ValueError |
| E16 | BookConfig.from_dict | 缺键 | ValueError |
| E17 | events.pack | 任一字段超出定宽范围（seq −1、price 2^63、symbol 70000、kind 300） | ValueError |
| E18 | events.iter_chunks / iter_records | start 或 stop < 0 | ValueError |
| E19 | events.iter_records / count_records | 空文件 → 0 条；长度非 56 倍 → 尾部残缺字节静默丢弃 | 无异常 |
| E20 | events.iter_records / load_meta | 文件不存在 | FileNotFoundError（OSError，不转换） |
| E21 | events.GenParams | 未知参数名 | KeyError（既有契约 test_events.test_unknown_param_rejected） |
| E22 | events.GenParams / generate | 参数越界或非 int（§1.5 的 validate 规则） | ValueError，不写任何文件 |
| E23 | generate + replay / replay_sharded / compare_stream / compare_sharded | n_events = 0 的空流 | 空文件 + meta；replay 四个输出文件齐全、events = 0；compare events = 0、first_divergence None |
| E24 | replay / replay_sharded / compare_stream / compare_sharded | 单事件流 | 正常；status.bin 恰好 1 字节 |
| E25 | replay / compare_stream | 全部事件被拒绝的流 | status_counts 只含 REJ_*，trades = 0，输出文件齐全 |
| E26 | replay / replay_sharded | 文件记录数 ≠ meta.n_events 或长度非 56 倍 | ValueError，写任何输出前 |
| E27 | replay（全品种）/ replay_sharded | 某记录的 symbol 不在 meta.books | ValueError（单进程在首个坏记录处；分片在合并前，并删除分片临时文件） |
| E28 | replay(symbols=子集) / Core(filter_symbols=True) | 记录的 symbol 不在子集 | 跳过；其 status.bin 字节保持 0 |
| E29 | replay（check_every > 0、timing 或 hook 或品种子集，即非纯模式） | seq ∉ [1, n_events] | ValueError；纯模式（check_every = 0、无 timing / hook、全品种）按记录位置写 status，不读 seq；seq = 0 任何模式都不检测 |
| E30 | compare_stream / compare_sharded | 某记录的 symbol 不在 meta.books | 静默过滤，只对拍 meta 中的品种 |
| E31 | replay / compare_stream | 流文件或 .meta.json 不存在 | FileNotFoundError |
| E32 | queue_model.run | 零探针 CSV（仅表头）或测试集为空（train_frac = 1.0） | 正常返回：n = 0、brier 为 None、SVG 标题 "Brier n/a" |
| E33 | queue_collect.ProbeCollector | 零事件流 | finalize 无异常，write_csv 返回 0（文件只有表头） |
| E34 | invariants.Checker | 簿状态违反不变量 | InvariantError（AssertionError 子类；内部一致性错误，不是输入错误） |
| E35 | lob.cli（argparse） | 未知子命令 / 未知选项 / 无子命令 | SystemExit 退出码 2 |
