# qdcheck 接口契约（CONTRACT）

本文写死四块的输入 / 输出格式与数值约定；`qdcheck/tests/test_contract.py` 逐条核对本文与代码一致
（字段名、枚举值、退出码、第一行格式），`qdcheck/vectors/schema.json` + `qdcheck/vector_schema.py` 核全部向量文件。
全文的数值比较只用容差和形状；所有数据合成，不构成任何交易结论。

## 0. 通用数值约定

| 约定 | 取值 |
|---|---|
| 相对容差 `rel_tol` | 1e-9：`|a − e| ≤ rel_tol · max(|a|, |e|)` |
| 绝对兜底 `abs_tol`（只在 compare 里） | 1e-12：`|a − e| ≤ max(rel_tol · max(|a|, |e|), abs_tol)`，只为数学上恰为 0 的点兜底 |
| 整数 / 字符串 / 布尔 / None / 顺序 | 完全相等，int 与 float 不互换，bool 不算数 |
| “无值”（预热期 / 未定义） | JSON 里一律 `null`；Python 里 `None`（`warmup="none"`）或 `NaN`（`warmup="nan"`）；compare 默认两者都接受 |
| 数值文本 | 向量文件里每个浮点字面量都是 `%.17g`（≤ 17 位有效数字，`float()` 可精确往返；整数值补 `.0`） |
| 非有限值 | 向量输入 / 输出里不出现 NaN / inf；实际输出里出现 inf / NaN（期望有值处）判不一致 |

## 1. 指标向量 JSON

### 1.1 文件结构（`vectors/schema.json` 的 `indicator_file` / `indicator_vector` / `ohlcv`）

```
{"format": "qdcheck.indicator-vectors.v1", "indicator": "<指标名>", "generator": "<命令>", "seed": 20261022,
 "variants": ["<variant>", ...], "lines": ["<line>", ...], "count": <向量条数>,
 "vectors": [
   {"id": "<indicator>|<variant>|<line>|<series>[|<k>=<v>,...]",
    "indicator": "dema|tema|trix|stochrsi|cci|ao|force|kdj|ichimoku",
    "variant": "k1=v1;k2=v2"            # 键按字母序；全部合法取值 = indicators_ref.VARIANTS[indicator]
    "line": "value|k|d|j|tenkan|kijun|senkou_a|senkou_b|chikou",
    "series": "<合成序列名>",
    "params": {<参数名>: <数值>},        # 键名只能是下面 1.3 的表
    "input": {"open": [...], "high": [...], "low": [...], "close": [...], "volume": [...]},   # 五列等长
    "output": [null | <float>, ...]}     # 长度 == 输入长度；null = 预热期 / 未定义
 ]}
```

- 一条向量 = 一个指标 × 一个口径（variant）× 一条输出线 × 一组输入；多输出线的指标每条线一条向量，`output` 永远是一维数组。
- `input` 满足 `low ≤ min(open, close) ≤ max(open, close) ≤ high`，`low > 0`，`volume ≥ 0`（停牌为 0）。
- `null` 的位置是契约的一部分：你的实现必须在同样的位置无值（compare 的 `reason=warmup`）。
- `manifest.json`：序列清单（名字 / 描述 / 生成参数）、每个指标的文件名 / variants / lines / 条数、账本向量清单、容差。

### 1.2 variant 字符串

`键=值` 用 `;` 连接、键按字母序（`indicators_ref.canonical_variant` 会重排）。全部 61 个合法取值与每个的预热期见
`REPORT.md` 第 3 节（由代码生成），口径语义见 `REPORT.md` 第 3 节“口径说明”。预热期的 None / NaN 不进 variant。

### 1.3 `params` 键名与缺省值

| 指标 | 键 | 缺省 |
|---|---|---|
| dema / tema | `period` | 5 |
| trix | `period`, `scale` | 5, 100.0 |
| stochrsi | `rsi_period`, `stoch_period`, `k_period`, `d_period`, `scale`, `range_eps` | 14, 14, 3, 3, 100.0, 1e-9 |
| cci | `period`, `constant` | 20, 0.015 |
| ao | `fast`, `slow` | 5, 34 |
| force | `period` | 13 |
| kdj | `n`, `m1`, `m2` | 9, 3, 3 |
| ichimoku | `tenkan`, `kijun`, `senkou_b`, `displacement` | 9, 26, 52, 26 |

窗口类参数必须是 ≥ 1 的 int（bool 不算）；`scale` / `constant` / `range_eps` 是有限非负实数。

### 1.4 预热期公式（非退化数据上前导 `null` 的个数；`props.expected_warmup` 实现，`test_warmup_positions_match_formula` 逐变体核对）

记 `ℓ(n) = n − 1`（当 `ma=sma`，或 `ma=ema|rma` 且 `seed=sma`）否则 `0`；`off` = `displacement`（shift=forward）/ `displacement − 1`（forward_tv）/ `0`（none）。

| 指标 | 线 | 预热期 |
|---|---|---|
| dema | value | 2·ℓ(period) |
| tema | value | 3·ℓ(period) |
| trix | value | 3·ℓ(period) + 1 |
| stochrsi | k / d | k = rsi_period + stoch_period − 1 + k_period − 1；d = k + d_period − 1 |
| cci | value | period − 1 |
| ao | value | max(fast, slow) − 1 |
| force | value | `ma=raw`：1；否则 1 + ℓ(period) |
| kdj | k / d / j | k = n − 1 + (m1 − 1 若 ma=sma)；d = k + (m2 − 1 若 ma=sma)；j = d |
| ichimoku | tenkan / kijun / senkou_a / senkou_b / chikou | tenkan − 1；kijun − 1；max(tenkan, kijun) − 1 + off；senkou_b − 1 + off；0（但末尾 off 个点为 null） |

退化数据（常数 / 零极差 / 零偏差）上某些口径会多出 `null`（例如 `zero_dev=null`、`flat=null`），以向量为准。

### 1.5 Ichimoku 的下标对齐（5 根例子）

参数 `tenkan=2, kijun=3, senkou_b=4, displacement=1`，输入 `high=[2,3,5,4,6]`, `low=[0.5,1,2,2.5,3]`, `close=[1,2,4,3,5]`
（`test_decimal.test_ichimoku_forward_shift_1` 用 Decimal 复算了这组数）：

```
t            0      1      2      3      4
tenkan     null   1.75   3.0    3.5    4.25        (max(h[t-1..t]) + min(l[t-1..t])) / 2
kijun      null   null   2.75   3.0    4.0         窗口 3
base_a     null   null   2.875  3.25   4.125       (tenkan + kijun) / 2，“在 t 算出”
base_b     null   null   null   2.75   3.5         窗口 4
shift=forward (off = 1):
senkou_a   null   null   null   2.875  3.25        senkou_a[t] = base_a[t − off]   ← 在 t−off 算出、画在 t
senkou_b   null   null   null   null   2.75        senkou_b[t] = base_b[t − off]
chikou     2.0    4.0    3.0    5.0    null        chikou[t]   = close[t + off]    ← 画在 t 的是 t+off 的收盘
shift=none (off = 0):  senkou_a = base_a，senkou_b = base_b，chikou = close
```

前移后超出输入长度的 `off` 个“未来云”不输出（数组长度恒等于输入长度）；`forward_tv` 把 `off` 改成 `displacement − 1`。
前移口径的 `chikou[t]` 依赖 `t+off` 的数据，按定义非因果，因果性判据对它改用“恰为 `close[t+off]`”。

## 2. compare（指标对拍）

### 2.1 命令行

```
python3.12 -m qdcheck.compare --vector <向量 JSON> --actual <实际输出 JSON | ->  [--id ID] [--rel 1e-9] [--abs 1e-12]
                              [--warmup either|none|nan] [--strict] [--json 汇总.json]
```

- `--vector`：单条向量对象、`{"vectors": [...]}` 文件或向量数组。
- `--actual`：文件或 `-`（stdin）；内容是裸数组（向量文件只有一条，或用 `--id` 选一条）或 `{vector_id: 数组}`。
- 退出码：**0** 全部一致（`--strict` 时还要求向量文件里每一条都给了实际输出）；**1** 有不一致；**2** 输入不合法
  （文件不存在 / 不是 JSON / actual 不是数组或映射 / `--id` 未知 / 裸数组却有多条向量且没给 `--id` / 没有任何 id 对上）。
- **stdout 第一行固定格式**（`compare.first_line`）：

```
OK n=<有值点数> max_rel=<最大相对误差，%.3e>
MISMATCH idx=<下标> expected=<期望> actual=<实际> rel=<相对误差 %.3e | nan> reason=<length|warmup|type|value>
```

  多条向量时末尾附 ` id=<向量 id>`；有不一致时第一行是第一条不一致的向量。`expected` / `actual` 用 `repr`（最短可往返），
  无值写 `null`；`reason=length` 时 `idx` = 较短的长度、`expected` / `actual` = 两边的长度、`rel=nan`；
  `warmup` / `type` 时 `rel=nan`。随后每条不一致一行（同格式），最后一行汇总
  `checked a/b  passed p  failed f  missing m  unknown u  max_rel_err r`。

### 2.2 Python

`compare.compare_series(expected, actual, rel_tol=1e-9, abs_tol=1e-12, warmup="either")` →
`{"ok", "reason", "index", "expected", "actual", "checked", "max_rel_err", "max_abs_err", "max_tol_ratio", "detail"}`；
`compare.compare_file(vectors, {id: array})` → `{"total", "checked", "passed", "failed", "missing", "unknown", "failures", ...}`。
判定顺序：长度 → 逐点（期望 null 处实际必须无值；期望有值处实际必须是有限实数且在容差内）。

## 3. fuzz（入参模糊器）

### 3.1 Python 接口

```python
report = fuzz.run(validate, schema, n=20000, seed=0, is_controlled=MyError)     # -> fuzz.Report
```

| 参数 | 含义 |
|---|---|
| `validate` | `callable(obj) -> Any`；抛 `is_controlled` 判定的异常 = 受控错误，其他任何异常（含 SystemExit）= 裸异常，KeyboardInterrupt 照常抛出 |
| `schema` | 见 3.2；`fuzz.check_schema` 先校验 |
| `n`, `seed` | 用例数；第 i 个用例只由 `(schema, seed, i)` 决定（`fuzz.make_case(schema, seed, i)` 重放），与 PYTHONHASHSEED 无关，同 seed 两次运行的用例序列逐字节相同 |
| `is_controlled` | 异常类、异常类元组或 `callable(exc) -> bool`；`None` = 所有异常都算裸异常。旧名 `controlled=` 仍接受 |
| 其他关键字 | `shrink=True`, `shrink_budget=3000`（每个签名的缩小尝试上限）, `max_signatures=50`, `check_accepted=True`（与参考判定 `schema_errors` 对照） |

`Report` 是 `dict` 子类：`report.ok`（无裸异常）、`report.to_json(path=None)`（ASCII、键排序、缩进 1 的严格 JSON 文本，给 path 则同时写文件）。
`Report.FIELDS`（`to_json()` 的顶层键，`test_report_type_fields_and_json` 核对）：

| 字段 | 含义 |
|---|---|
| `tool`, `format` | `"qdcheck.fuzz"`, 2 |
| `validate` | 被测函数的 `模块:限定名` |
| `seed`, `n` | 输入参数 |
| `elapsed_s`, `per_case_us` | 生成 + 调用 + 参考判定的墙钟秒数（不含缩小）、每用例微秒 |
| `outcomes` | `{"pass", "controlled", "bare"}` 计数，和为 n |
| `bare_by_type` | 裸异常按类型名计数 |
| `categories` | 每类乱喂的 `{"cases", "pass", "controlled", "bare"}`（一个用例可含多类） |
| `field_coverage` | `{schema 路径: {类别: 次数}}`，路径形如 `$.risk.window`、`$.symbols[]`；`$` 为顶层对象本身 |
| `fields_under_4_kinds` | 被乱过的方式少于 4 种的字段路径（`$` 除外）；2 万次下应为空 |
| `schema_valid_cases` | 按参考判定合法的用例数（`check_accepted=False` 时 null） |
| `accepted_invalid` | `{"count", "first_cases"}`：validate 放过了 schema 判为非法的输入（误收） |
| `rejected_valid` | `{"count", "first_cases"}`：validate 拒绝了 schema 判为合法的输入（误拒） |
| `bare_unrecorded` | 超过 `max_signatures` 之后没归组的裸异常个数 |
| `bare` | 每个签名一项：`exc_type`, `message`, `signature`, `count`, `first_case`, `categories`, `top3`（栈顶三行）, `original_size`, `minimal_size`, `ratio`, `shrink_calls`, `minimal_input`（编码，`fuzz.decode` 还原）, `minimal_preview` |

乱喂类别（`fuzz.CATEGORIES`）：`valid`, `type_wrong`, `nan`, `inf`, `neg_zero`, `huge`, `negative`, `boundary`, `empty`,
`missing_key`, `extra_key`, `key_case`, `nested_garbage`, `long_string`, `unicode`, `numeric_string`。

### 3.2 schema

```
{"type": "object", "fields": {<键>: <节点>, ...}, "extra": "forbid"|"allow"}
节点: {"type": "int"|"float"|"bool"|"str"|"enum"|"list"|"object", "required": true|false(缺省 true),
       "min"/"max" (int/float), "min_len"/"max_len" (str/list), "charset": "ascii_printable" (str),
       "choices": [...] (enum), "items": <节点> (list), "fields"/"extra" (object)}
```

`fuzz.schema_errors(schema, obj)` 是这套语义的参考判定（int 不接受 bool，float 接受 int 但不接受 bool / NaN / inf，键名区分大小写，
object 缺省不许多键）。命令行：`python3.12 -m qdcheck.fuzz 模块:函数 --schema 文件|模块:变量 [--controlled 模块:类] [--n] [--seed] [--out 报告.json]`。

## 4. ledger（参考账本）

### 4.1 成交流记录

| 字段 | 类型 / 单位 | 约定 |
|---|---|---|
| `bar` | int ≥ 0 | 根序号；记录按 bar 非降序，同一根内按给出顺序处理，处理完该根全部记录后以 `closes[bar]` 盯市 |
| `kind` | `trade` / `funding` / `settlement` / `liquidation` | 缺省 `trade` |
| `side` | `buy` / `sell` / `none`（也接受 `1` / `-1` / `0`） | trade / liquidation 必须 buy 或 sell；funding / settlement 必须 none |
| `qty` | 数量（合约张数 / 币数，与 price 相乘得报价货币金额） | trade / liquidation 必须 > 0（**数量为 0 的成交被拒绝**，LedgerError）；funding / settlement 必须 0 |
| `price` | 报价货币 / 单位数量，> 0 | trade / liquidation 的成交价；settlement 的结算价；funding 可为 null |
| `fee` | 报价货币 | **正数 = 从现金扣减**，负数 = 返佣（加到现金）；累计进 `fees` |
| `funding` | 报价货币 | **正数 = 账户支付**（现金减少），负数 = 账户收取；累计进 `funding`；不限于 kind=funding 的记录 |
| `order_id` | int，可选 | 只透传，不参与计算 |

持仓 `qty` **多头为正、空头为负**；`avg_price` 恒 ≥ 0，平仓后为 0。

### 4.2 配置

`initial_cash > 0`；`margin_mode ∈ {none, isolated, cross}`；`leverage > 0`（isolated 时 1–20）；`0 ≤ mmr < 1`（维持保证金率）；`rel_tol`（缺省 1e-9）。

### 4.3 每根输出行（`ledger.ROW_FIELDS`）

`bar, close, cash, qty, avg_price, unrealized, realized, fees, funding, insurance, equity, market_value, margin_used,
maint_margin, position_equity, liq_price, fills, liquidations`。整数字段：`bar, fills, liquidations`（及整数数量的 `qty`）；
`position_equity` 在 `margin_mode=none` 下为 null；`liq_price` 无仓 / 不可能触发时为 null。

### 4.4 记账数学

记第 t 根收盘价 \(C_t\)，带符号持仓 \(q\)（多正空负），均价 \(A\)，现金 \(X\)，已实现 \(R\)，累计手续费 \(F\)，累计资金费 \(U\)，
保险补偿 \(I\)（计入 \(R\) 与 \(X\)，单独累计便于核对），初始资金 \(X_0\)。

成交 \((s, k, p, f)\)，\(s \in \{+1, -1\}\)，\(k > 0\)：

\[
X \leftarrow X - s\,k\,p - f,\qquad F \leftarrow F + f
\]

- 同向或无仓：\(A \leftarrow \dfrac{|q|\,A + k\,p}{|q| + k}\)，\(q \leftarrow q + s\,k\)（平均成本法，加仓更新均价）。
- 反向：平掉 \(c = \min(k, |q|)\)，\(R \leftarrow R + c\,(p - A)\,\operatorname{sign}(q)\)，\(q' = q + s\,k\)（残差 \(|q'| \le 10^{-12}\max(1,|q|)\) 归零）；
  \(q' = 0\) 则 \(A \leftarrow 0\)；同号（部分平仓）则 \(A\) 不变；异号（反手）则 \(A \leftarrow p\)。

资金费 \(u\)：\(X \leftarrow X - u\)，\(U \leftarrow U + u\)。结算（价 \(p\)）：\(R \leftarrow R + q\,(p - A)\)，\(A \leftarrow p\)（现金、权益不变）。

盯市：浮动 \(V = q\,(C - A)\)，持仓市值 \(M = q\,C\)，权益 \(E = X + M\)，钱包 \(W = X + q\,A = X_0 + R - F - U\)。

逐仓保证金 \(m\)（`margin_used`）：开 / 加仓 \(m \leftarrow m + k\,p / L\)；减仓 \(m \leftarrow m - m\,c/|q|\)；反手 \(m \leftarrow (k - c)\,p / L\)；
结算 \(m \leftarrow |q|\,p / L\)；平仓 \(m \leftarrow 0\)。恒有 \(m = |q|\,A / L\)（I4 核对）。有效保证金 \(m_{\mathrm{eff}} = \min(m, W)\)。

仓位权益 \(PE\) = \(m_{\mathrm{eff}} + V\)（isolated）或 \(E\)（cross）；维持保证金 \(MM = \mathrm{mmr}\cdot|q|\cdot C\)。
**强平线：\(PE \le MM\)**。强平价（`liq_price`）解 \(PE(P) = MM(P)\)：
isolated \(P = \dfrac{q\,A - m_{\mathrm{eff}}}{q\,(1 \mp \mathrm{mmr})}\)（多头取 −，空头取 +），cross \(P = \dfrac{-X}{q\,(1 \mp \mathrm{mmr})}\)；\(P \le 10^{-9} A\) 视为不可触发（null）。

逐仓强平成交 \((s, k, p, f)\) 后：\(\Delta = -(m_{\mathrm{eff}} + q\,(p - A) - f)\)，若 \(\Delta > 0\)（穿仓）：\(I \leftarrow I + \Delta\)，\(R \leftarrow R + \Delta\)，\(X \leftarrow X + \Delta\)。
因此强平后 \(X \ge 0\)。全仓不封顶，强平后 \(E < 0\) 记 warning `bankrupt`。

### 4.5 每根恒等式（`ledger.CHECKS`）

容差 \(\tau = \mathrm{rel\_tol}\cdot\max(1, X_0, |X|, |M|, |R|, |F|, |U|, |E|, |V|)\)。“权益”另取第三条独立路径
\(\hat E_t = \hat E_{t-1} + q_{t-1}(C_t - C_{t-1}) + \sum_{\text{本根成交}} s\,k\,(C_t - p) - \text{本根 } f - \text{本根 } u + \text{本根 } \Delta\)，\(\hat E_{-1} = X_0\)。

| 名称 | 条件 |
|---|---|
| `I1_equity_eq_initial_realized_unrealized_fees_funding` | \(|\hat E - (X_0 + R + V - F - U)| \le \tau\) |
| `I2_equity_eq_cash_plus_mark` | \(|\hat E - (X + M)| \le \tau\) |
| `I3_flat_state` | \(q = 0 \Rightarrow A = 0,\ V = 0,\ m = 0,\ |X - E| \le \tau\) |
| `I4_margin_consistency` | isolated：\(|m - |q|A/L| \le \tau\) |
| `I4_missed_liquidation` | 有仓且未强平：\(PE \ge MM - \tau\)（否则漏强平） |
| `premature_liquidation` | 强平记录处理前：\(PE \le MM + \tau\)（否则提前强平） |
| `liquidation_not_full` | 强平必须一次平掉全部持仓（方向相反、数量等于 \(|q|\)） |
| `negative_balance_after_liquidation` | isolated 强平后 \(X \ge -\tau\) |

**边界**：\(PE = MM\) 恰好落在强平线上时，强平与不强平都在 \(\pm\tau\) 内被接受（`test_liquidation_exactly_at_the_boundary`）。

### 4.6 Python

```python
ledger.check(records, closes, **config) -> [ {"bar": int, "check": <CHECKS 之一>, "lhs": ..., "rhs": ..., "detail": str}, ... ]   # [] = 通过
ledger.run(records, closes, **config)   -> {"rows": [行...], "violations": [...], "warnings": [{"bar", "check": "bankrupt", "equity"}], "counters": {...}, "config": {...}}
ledger.check_engine(records, closes, engine_equity, **config) -> {"ok", "bars", "first_mismatch": {"bar", "expected", "actual", "field"} | None, "violations", "max_rel_err"}
ledger.check_vector(vec, {字段: [逐根值]}) -> {"ok", ...}      # 用 vectors/ledger_*.json 测你的账本
```

格式错误（字段缺失 / 类型错 / 顺序错 / 数量为 0 / 价格非正）抛 `ledger.LedgerError`（ValueError 子类），不是违背。

### 4.7 账本向量文件（`schema.json` 的 `ledger_file`）

`{"format": "qdcheck.ledger-vector.v1", "name", "description", "generator", "config", "counters", "closes": [...],
"records": [4.1 的记录...], "expected": {ROW_FIELDS 每个字段: [逐根值]}}`；`expected` 每列长度 = `len(closes)`。

## 5. determinism（跨进程 / 跨哈希种子一致性）

- **任务描述**：任何 `json.dumps` 可序列化的值（通常 dict）；harness 先做一次 JSON 往返再传给被测函数（子进程通过 stdin 收到同样的 JSON）。
  任务里应带自己的随机种子——harness 只对“哈希顺序 / 进程”敏感，与任务无关的随机性（如引擎内部 `random.seed(os.getpid())` 的无关计算）不会误报（`test_unrelated_process_randomness_does_not_trip_the_harness`）。
- **可调用路径**：`包.模块:函数` 或 `包.模块:对象.属性`；冒号必需。`determinism.load_target` 用内建 `__import__` 导入。
- **子进程怎么找到被测模块**：`cwd` = 仓库根（qdcheck 的上一级），`PYTHONPATH` = 仓库根 + `paths=(...)` 里给的目录（顺序如此），
  `PYTHONHASHSEED` = 0 / 4242（`hash_seeds=` 可改）。你的模块不在本仓时传 `paths=("/你的/代码根",)`。子进程把自己看到的
  `PYTHONHASHSEED` 和 `hash("qdcheck-hash-probe")` 一并返回；与设定不符直接 `HarnessError`，两个子进程探针相同则 `hash_seeds_effective=False` 且 `ok=False`。
- **返回值要求**：必须能 `json.dumps`（tuple 当 list；set / bytes / 自定义对象直接 `HarnessError`——harness 不替你排序，排序会掩盖迭代顺序问题）。
- **比较规则**：int / str / bool / None 完全相等（int ≠ float，bool ≠ int）；float 相对容差 `rel_tol=1e-9`（NaN 等于 NaN，inf 必须同号；`abs_tol` 缺省 0）；
  list 顺序与长度；dict 键集合（键顺序缺省不比，`dict_order=True` 则比）。
- **差异路径格式**：`trades[17].price`、`a.b[0]`、`["非标识符键"]`、根为 `<root>`；按前序遍历（dict 键字典序）报告第一处，另列最多 10 处。
  `reason` ∈ `value` / `type <a> != <b>` / `length <a> != <b>` / `key only in a|b` / `dict key order differs`。
- **结果**：`{"ok", "hash_seeds_effective", "runs": [{"name", "hash_seed", "probe", "seconds", "seed_env"?}], "pairs": [{"a", "b", "ok", "first_diff", "diffs", "leaves"}], ...}`。
- 命令行：`python3.12 -m qdcheck.determinism 模块:函数 [--task '{...}' | --task @task.json] [--seeds 0 4242] [--rel 1e-9] [--dict-order] [--json out.json]`；退出码 0 一致 / 1 不一致 / 2 输入或导入错误。

## 6. synth / stream（合成数据与随机流）

`synth.generate(n, seed, segments=[(start, len, kind)], events={i: kind}, **参数)` 只用 `random.random()` 与四则运算（无 libm）：
同 seed 在任何 IEEE-754 机器上逐位相同，且前缀稳定。`stream.generate(seed, n_bars)` 返回 4.1 格式的 `records` + `closes` + `config`。
