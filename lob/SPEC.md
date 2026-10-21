# 撮合语义规范（两套实现共同遵守）

价格 = 整数 tick，数量 = 整数 lot；不出现浮点。`BookConfig`：`tick_size, lot_size, min_qty, max_qty, band, ref_price, stp`。
`ref_price` 初值来自配置，之后 = 本簿最近一笔成交价（每个事件处理结束后更新）。

## NEW 校验顺序（第一个失败者决定状态码）
1. `kind ∉ {1,2,3}` → REJ_KIND；`side ∉ {1,2}` → REJ_SIDE。
2. 标志位：未知位 / MARKET 与 POST_ONLY 或 ICEBERG 同用 / IOC 与 FOK 同用 / POST_ONLY 与 IOC 或 FOK 同用 → REJ_FLAGS。
3. `oid` 与当前挂单重复 → REJ_DUP。
4. `qty < min_qty` 或 `qty > max_qty` 或 `qty % lot_size ≠ 0` → REJ_QTY。
5. 非市价单：`price ≤ 0` 或 `price % tick_size ≠ 0` → REJ_PRICE；`|price − ref| > band` → REJ_BAND。
6. 冰山：`aux`（显示量）需满足 `lot_size ≤ aux ≤ qty` 且为 lot 整数倍 → 否则 REJ_DISPLAY。
7. POST_ONLY：若对手方最优价与本单价格交叉（仅看价格，不考虑自成交）→ REJ_POST_ONLY。
8. FOK：先做**纯模拟**（不改簿）计算按下述撮合规则可成数量；不足 `qty` → REJ_FOK，簿不变。

## 撮合（价格优先，时间优先）
- 买单与卖方 `price ≤ limit` 的档位撮合，卖单对称；市价单无 limit，但只能吃 `ref ± band` 内的档位。
- 每档按 FIFO：队首订单 `o`：
  - 若 `o.trader == 新单.trader`（自成交）：`cancel_new` → 新单剩余全部撤销并停止撮合；`cancel_old` → 撤销 `o`（计入撤单量），继续。
  - 否则成交 `min(rem, o.vis)`，价格 = `o.price`。`o.rem == 0` → 移除；`o.rem > 0 且 o.vis == 0`（冰山露出）→ `o.vis = min(display, rem)`，**排到该档队尾并获得新的时间优先序号**。
- 档内可见量 `level.qty = Σ o.vis`，档内无单即删除档。
- 撮合后剩余：IOC / MARKET → 撤销（ST_PARTIAL_CANCELLED / ST_CANCELLED）；FOK 必为 0；其余挂入簿（冰山 `vis = min(aux, rem)`），获得新的优先序号。
- 状态：全成 FILLED；部分成交后挂入 PARTIAL_RESTED；未成交挂入 RESTED。

## CANCEL
`oid` 非挂单 → REJ_UNKNOWN；否则移除，撤单量 += rem → ST_ACK_CANCEL。记录中的 side / price 仅供参考，不参与判断。

## MODIFY（只改数量，`qty` 字段 = 新的剩余量）
`oid` 非挂单 → REJ_UNKNOWN；新量不满足 `min_qty ≤ q ≤ max_qty` 且 lot 整数倍 → REJ_QTY（簿不变）。
- `q == rem` → 无操作（ACK_MODIFY，保留队位）。
- `q < rem` → 减量**保留队位**：`rem = q`，`vis = min(vis, q)`，撤单量 += 差额。
- `q > rem` → 增量**排到队尾**（新优先序号）：提交量 += 差额，`rem = q`，`vis = min(display, q)`（非冰山 `vis = q`）。

## 守恒
`submitted = filled + resting + cancelled + rejected`，其中 filled = 2 × Σ成交量（买卖两腿各消耗一次提交量），resting = Σ 挂单 rem（含冰山隐藏量），rejected 计入被拒绝 NEW 的 qty，修改增量计入 submitted、修改减量计入 cancelled，自成交撤单计入 cancelled。

## 优先序号
簿内全局计数器 `prio`，在 挂入 / 冰山露出 / 增量改单 时递增并赋给订单；同档队列中存活订单的 `prio` 严格递增。
