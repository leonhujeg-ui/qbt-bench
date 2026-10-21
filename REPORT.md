# REPORT — qbt 回测与参数优化引擎（纯 Python 3.12 标准库）

> 所有数字来自仓库内脚本的真实运行输出（JSON 已入库），每个结论附复现命令。**所有数据均为合成数据，所有策略结果不构成任何投资结论。**

机器：Linux-6.18.44-fc-v140-x86_64-with-glibc2.39；CPU Intel(R) Xeon(R) Processor @ 2.10GHz（x86_64），`os.cpu_count()` = 4（4 物理核：8 进程目标在 4 核上实测）；内存 16 GB；Python 3.12.3；基准 JSON 内各自记录了生成时的 git 提交号（`bench/results/*.json` 的 `machine.git_commit`）。

## 1. 目标达成情况

| 目标 | 要求 | 实测 | 达成 | 复现命令 |
|---|---|---|---|---|
| 1 引擎正确（确定性） | 同策略同数据，任何执行顺序 / 进程数下结果逐字节一致 | 网格 2,000 组：1 进程 vs 8 进程（含不同提交顺序）结果 JSON 逐字节相同（`bytewise_identical = True`）；黄金文件逐字节回归；中途 SIGKILL 续跑输出与一次跑完逐字节相同；同 seed 两次生成数据逐字节相同 | ✅ | `python3.12 -m unittest tests.test_optimizer tests.test_golden tests.test_data tests.test_engine` |
| 2 够快 | 1M 根 × 1 策略单核 ≤ 3 s；8 进程 2,000 组网格 ≤ 90 s | 1,000,000 根 × ma_cross 单进程 **2.146 s**（冷缓存，含指标计算；热缓存 0.933 s，465,919 bars/s）；2,000 组 × 20,000 根网格 8 进程 **21.3 s**（1 进程 84.3 s，加速 3.95x） | ✅ | `python3.12 bench/bench.py --out bench/results/xx.json`（已入库：`bench/results/01..03*.json`） |
| 3 够硬 | 语句覆盖率 ≥ 90%；突变抓住率 ≥ 85%；模糊 10 万次零崩溃 | 覆盖率 **99.8%**（1877/1880 语句，`tools/cov.py`）；突变 **640 个突变体（8 模块 × 80），抓住 590（含 timeout 1），漏网 50，抓住率 92.2%**（五轮累计的最终合并结果，`mutants/results.json`；漏网逐条说明见第 4 节）；模糊 **100,000 次，0 次未捕获异常** | ✅ | `python3.12 tools/cov.py --fail-under 90`；`python3.12 mutants/run_mutations.py --per-module 80 --jobs 3 --tests -f tests.test_golden tests.test_indicators tests.test_broker tests.test_engine tests.test_metrics tests.test_optimizer tests.test_strategies tests.test_data tests.test_report tests.test_cli tests.test_limits tests.test_perf`；`python3.12 fuzz/fuzz.py --iterations 100000 --seed 0 --workers 2` |
| 4 够诚实 | 数字来自真实运行，结论可复现 | 本报告所有表格由 `bench/results/*.json`、`fuzz/results.json`、`mutants/results.json`、`out/coverage_final.json`、`out/walk_forward.json` 生成；提交信息逐阶段记录判据数 / 覆盖率 / 抓住率 | ✅ | `git log`；`python3.12 -m qbt.cli demo` |

测试套件：`python3.12 -m unittest discover -s tests` → **172 条判据**（test_data 33、test_indicators 33、test_broker 27、test_engine 12、test_strategies 9、test_metrics 18、test_golden 3、test_optimizer 17、test_report 6、test_cli 8、test_perf 3、test_limits 3），全部通过；性能判据在覆盖率监控 / 突变快速模式下跳过并注明原因（这两种环境下计时无意义），正常模式下真实运行并有阈值。

覆盖率明细（`python3.12 tools/cov.py`，sys.monitoring 语句覆盖；子进程内执行不计入，优化器因此保留串行路径覆盖同一代码）：

| 文件 | 语句 | 漏 | 覆盖 |
|---|---|---|---|
| `qbt/__init__.py` | 3 | 0 | 100.0% |
| `qbt/broker.py` | 315 | 0 | 100.0% |
| `qbt/cli.py` | 89 | 0 | 100.0% |
| `qbt/data.py` | 194 | 0 | 100.0% |
| `qbt/engine.py` | 206 | 0 | 100.0% |
| `qbt/errors.py` | 4 | 0 | 100.0% |
| `qbt/indicators.py` | 434 | 0 | 100.0% |
| `qbt/metrics.py` | 141 | 0 | 100.0% |
| `qbt/optimizer.py` | 181 | 1 | 99.4% |
| `qbt/report.py` | 97 | 0 | 100.0% |
| `strategies/__init__.py` | 8 | 0 | 100.0% |
| `strategies/bollinger.py` | 27 | 0 | 100.0% |
| `strategies/donchian.py` | 37 | 0 | 100.0% |
| `strategies/grid.py` | 42 | 1 | 97.6% |
| `strategies/ma_cross.py` | 32 | 0 | 100.0% |
| `strategies/rsi_reversion.py` | 29 | 0 | 100.0% |
| `strategies/trend_stop.py` | 41 | 1 | 97.6% |
| **合计** | 1880 | 3 | **99.8%** |

未覆盖行（共 3 行）：`qbt/optimizer.py:254`；`strategies/grid.py:29`；`strategies/trend_stop.py:43`（分别为优化器 CLI 在 `main()` 内关闭断点的分支——“断点 + CLI”组合只在子进程 kill/续跑判据中执行，子进程不计入主进程覆盖；网格策略 level_qty ≤ 0 与趋势策略 size ≤ 0 的防御分支）。

## 2. 指标清单（36 个）与判据方式

所有指标位于 `qbt/indicators.py`，全部 O(n) 单遍或滚动窗口实现（预热期显式 `None`），判据在 `tests/test_indicators.py`（参考实现在 `tests/_brute.py`）。
通用判据（对注册表全体 36 个）：长度保持；空输入 / 单根输入；**篡改 i ≥ cut 的全部列后，输出 [0, cut) 逐字节不变（因果性）**；窗口参数非法抛 `ConfigError`；默认参数用签名钉住。

| # | 指标 | 实现要点 | 专项判据 |
|---|---|---|---|
| 1 | SMA | 滚动和 | 与暴力 `sum(window)/n` 逐根对拍（n=1,2,7,50）；预热期 None 数量；常数序列恒等 |
| 2 | EMA | 递推 α=2/(n+1) | 与闭式解 `α Σ(1-α)^k x_{i-k} + (1-α)^i x_0` 逐根对拍；自定义 α |
| 3 | WMA | 滚动和 + 滚动加权和 O(1) 更新 | 与暴力加权平均对拍 |
| 4 | HMA | WMA(2·WMA(n/2) − WMA(n), √n) | 与用暴力 WMA 拼出的组合对拍；常数序列恒等 |
| 5 | DEMA | 2·EMA − EMA(EMA) | 斜坡输入渐近零滞后（<1e-6），常数恒等 |
| 6 | TEMA | 3E1 − 3E2 + E3 | 同上；EMA 稳态滞后 = (1−α)/α·slope 作对照 |
| 7 | KAMA | 滚动 |Δx| 和 → ER → 自适应平滑 | 单调序列 ER=1 的闭式递推；随机序列与按定义重算对拍；介于窗口极值之间 |
| 8 | RSI | Wilder 递推 | 与按定义逐根重算对拍；∈[0,100]；单边上涨=100、下跌=0、横盘=50 |
| 9 | MACD | EMA 差 + 信号 EMA | 与闭式解 EMA 组合对拍；hist = macd − signal |
| 10 | ROC | x_i/x_{i−n} − 1 | 暴力对拍；除零为 None |
| 11 | Momentum | x_i − x_{i−n} | 暴力对拍 |
| 12 | TRIX | 三重 EMA 的变化率 | 指数增长序列收敛到 e^g − 1 |
| 13 | rolling_max | 单调队列 | 与 `max(window)` 精确相等（含平手） |
| 14 | rolling_min | 单调队列 | 与 `min(window)` 精确相等 |
| 15 | Donchian | 滚动极值 + 中线 | 上下轨与暴力极值精确相等；upper ≥ lower |
| 16 | Stochastic | 滚动极值 + SMA | %K 公式对拍，∈[0,100]；%D = SMA(%K)；平盘=50 |
| 17 | Williams %R | 滚动极值 | = %K − 100，∈[−100,0] |
| 18 | Aroon | 单调队列带下标 | 与 argmax/argmin（最近者优先）对拍；斜坡=100 |
| 19 | True Range | 单遍 | 按定义对拍；≥0；缺口被覆盖 |
| 20 | ATR | TR 的 Wilder 平滑 | 与暴力 Wilder 对拍；≥0；预热 None |
| 21 | rolling_std | West 滑窗均值/二阶矩 + 每 n 根精确重算 | 与两遍暴力对拍（rel 1e-7 / abs 1e-7，理由见测试头）；常数=0；ddof |
| 22 | Bollinger | SMA ± k·std | 中轨=SMA；上轨=mid+k·std 对拍；**上轨−中轨 == 中轨−下轨（对称）** |
| 23 | Z-score | (x−SMA)/std | 暴力对拍；std=0 → 0 |
| 24 | Keltner | EMA ± k·ATR | 中轨=EMA；带宽=k·ATR（上下对称） |
| 25 | ADX / +DI / −DI | Wilder 平滑的 DM/TR | 与独立实现对拍；三者 ∈[0,100]；单边上行 ADX→100、−DI=0；全平=0；极小 DI 仍 DX=100 |
| 26 | CCI | 典型价 SMA + 窗口内平均绝对偏差（O(w) 内层，窗口有界） | 暴力对拍；平盘=0 |
| 27 | OBV | 累计 | 按定义对拍；平盘不变 |
| 28 | VWAP | 累计 / 滚动 PV 与 V 和 | 两种模式暴力对拍；零量 None；介于 [min low, max high] |
| 29 | MFI | 正负资金流 SMA | 暴力对拍；∈[0,100]；单边涨=100 / 跌=0；平盘=50 |
| 30 | CMF | MFV/Volume 的 SMA 比 | 暴力对拍；∈[−1,1]；极端 K 线=±1 |
| 31 | Ichimoku 五线 | 滚动极值中线；先行线前移 disp（因果）；迟行线定义为 close_i − close_{i−disp} | 转换/基准线与暴力中线对拍；先行线 = 原始值前移 disp；迟行线按定义对拍；短序列全 None |
| 32 | Parabolic SAR | Wilder 状态机 | 与独立显式状态机实现对拍（值与趋势）；多头延续 SAR ≤ low、翻转时 ≤ 前一根 low（空头对称） |
| 33 | SuperTrend | ATR 带 + 方向状态 | 与独立实现对拍；多头时 close ≥ 线、空头时 close ≤ 线；存在翻转 |
| 34 | rolling_drawdown | 滚动最高点回撤 | 暴力对拍；≤0 |
| 35 | rolling_max_drawdown | 累计峰值回撤序列的滚动最大（单调队列） | 与 O(n·w) 暴力对拍；∈[0,1] |
| 36 | linreg_slope | 维护 Σy、Σ(i·y) 的 O(1) 更新 + 每 n 根精确重算 | 与最小二乘暴力对拍（rel 1e-7）；斜坡输入斜率精确；n=1 为 None |


### 其他判据（非指标）

- **数据（阶段 0，33 条）**：同 seed 逐字节一致；长序列前缀 == 短序列（每根固定消耗随机数）；GBM 对数收益均值/方差在 5 个标准误内；标准正态分数位（|z|<1、<2）与峰度；影线半正态矩；成交量与 |收益| 相关；停牌段平价零量、缺口、跳跃方差放大；逐类非法输入拒绝（NaN/inf/负价/零价/负量/high<low/时间戳不递增或重复/长度不一致/非数值/空 symbol）；CSV 往返逐字节；黄金指纹钉住。
- **撮合与账户（阶段 2，27 条）**：每种订单类型的成交价规则与恰好触及边界；冰山分片、参与上限、零成交量不成交；冲击滑点单调；撤单/过期/提交顺序；均价与已实现盈亏（含反手）；资金费多付空收；保证金拒单、杠杆借入、reduce_only；空头开关两处封顶；强平罚金 + 撤单 + 破产；多标的敞口；随机订单流（5 组配置 × 多 seed）**逐 bar** 验证 `equity == cash + Σ qty·close`、`assets − liabilities == equity`、`initial + realized + unrealized − fees − funding == equity`（容差 1e-6，理由：百万笔不同顺序累加）、每笔成交量 ≤ 参与上限 × bar 成交量、maker 成交价 ∈ [low, high]、taker 成交价 ∈ [low·(1−slip), high·(1+slip)]，并从成交流独立重建现金与持仓。
- **引擎与策略（阶段 3，21 条）**：T 收盘下单、T+1 开盘成交；权益曲线与独立盯市逐根一致；**篡改 cut 之后的全部 K 线，6 个策略在 cut 之前的成交序列与权益逐字节不变**；strict 模式 6 个策略零 LookaheadError 且与非 strict 结果相同、偷看策略被拦截；多策略多标的；目标仓位处理部分成交；指标缓存命中/淘汰/不串台；确定性；每个策略的入场成交与指标信号逐笔对拍（按订单创建 bar）。
- **度量（阶段 4，18 条）**：≤ 20 根手算对拍（收益、夏普、索提诺（含 rf 与 ppy）、回撤与恢复、CAGR、Calmar、VaR/CVaR 顺序统计量、按日/月分组、交易统计）；随机权益上回撤与 O(n²) 暴力一致；月度复合 == 总收益；黄金文件。
- **优化器（阶段 5，17 条）**：见目标 1；另有断点 flush 语义、进度打印、walk-forward 折边界/单折/余数折/断点 id 隔离、CLI。
- **工程约束（3 条）**：每个函数 ≤ 60 行、每个文件 ≤ 800 行（AST 检查，全仓库 6676 行代码）；无第三方 import；无未显式 seed 的随机调用。

## 3. 性能优化前后对比

机器：Linux-6.18.44-fc-v140-x86_64-with-glibc2.39，x86_64，4 核（物理 4 核，8 进程目标在 4 核上实测），Python 3.12.3。

| 基准（bench/results/*.json） | 01 优化前 | 02 metrics 日历优化后 | 03 broker 热循环优化后 | 目标 |
|---|---|---|---|---|
| 1M 根 × ma_cross 单核，冷缓存（含指标计算） | 2.209s | 2.465s | 2.146s | ≤ 3s |
| 1M 根 × ma_cross 单核，热缓存 | 1.174s | 1.112s | 0.933s |  |
| 1M 根 metrics.compute | 5.41s | 1.81s | 1.58s |  |
| 吞吐（冷） | 452,671 bars/s | 405,720 bars/s | 465,919 bars/s |  |
| 2,000 组 × 20,000 根网格，8 进程 | 74.0s | 23.4s | 21.3s | ≤ 90s |
| 同上，1 进程 | 215.7s | 88.6s | 84.3s |  |
| 加速比（4 物理核） | 2.92x | 3.78x | 3.95x |  |
| 1 进程 vs 8 进程结果逐字节相同 | True | True | True | True |
| 1M 根数据生成 / 校验 | 1.89s / 0.38s | 2.00s / 0.53s | 1.83s / 0.32s |  |
| 1M 根 SMA / EMA / ATR / ADX | 0.13 / 0.08 / 0.37 / 1.35s | 0.23 / 0.11 / 0.55 / 1.66s | 0.12 / 0.08 / 0.37 / 1.24s |  |

每次改动的收益：
1. `metrics.period_returns` 去掉每根 bar 的 `datetime.fromtimestamp`，改为整数天序号 + Hinnant 民用历算法（与 datetime 对拍 1970–2100 零差异）：metrics 1M 根 5.41s → 1.81s；网格每组开销随之下降，8 进程 74.0s → 23.4s，1 进程 215.7s → 88.6s。
2. `Broker.on_bar` 盯市热循环改用预构造的 (symbol, Position, close 列表) 行表，去掉每根 bar 的 `dict.items()` 与 `data[sym].close` 属性链：热回测 1.112s → 0.933s（cProfile 300k 根：broker.on_bar 0.362s → 0.226s）；网格 8 进程 23.4s → 21.3s。黄金文件逐字节不变。
3. 数据规模自始至终固定（1,000,000 根；2,000 组 × 20,000 根），没有缩小数据换达标；单核基线在优化前就已达标（2.209s），优化主要兑现在网格搜索上。

## 4. 突变测试

五轮运行的过程（全部日志与 JSON 在 `out/` 与 `mutants/` 中）：

1. **第一轮全量**（`out/mut_final_results_before_patch.json`）：640 个突变体，抓住 522，漏网 118，抓住率 81.6%。其中 64 个“抓住”是假阳性——我在运行期间往 `tests/test_broker.py` 里加断言时有几十秒窗口测试本身是红的，所有当时跑的突变体都被误判抓住；这一轮因此只用于 triage，不作为达标依据。
2. **第二轮**（`out/mut_run2_results.json`）：按 triage 补了 60 余条判据并删除只会产生等价突变体的死代码后，对改过源码的 4 个模块全量重跑、对其余模块的漏网与上述 64 个可疑项定向重跑（`--ids`）：合并后 622 个（常量 ±1 两个变体共用 id 导致合并丢了 18 条记录，已修）抓住 553，漏网 69，抓住率 88.9%。
3. **第三轮全量**（`out/mut_run3_results.json`）：修正 id 方案、再补一批判据后，对全部 8 个模块重新抽样全量跑一遍：640 个，抓住 579，漏网 61，抓住率 90.5%。
4. **第四轮（定向）**：对第三轮仍漏网的 15 个未说明项补判据（demo 参数常量化并钉住、起始权益 ∈ (0,1] 的总收益、非正峰值回撤、x 刻度位置）后，对源码改动过的 `cli.py` 全量重跑并合并，对 metrics/report 的 3 个 id 定向重跑：640 个，抓住 586，漏网 54，抓住率 91.6%。
5. **第五轮（最终，定向）**：逐个把“备注说已补判据但仍漏网”的 6 项用“应用突变体 + 跑目标测试”直接验证，发现 2 项是同一行上另一个常量的等价变体（改为等价说明），4 项的判据确实没打到点（趋势同但 SAR 值不同、小时线同日看不出 ts[1]、标签 y、强平后权益 ∈ (0,1] 不算破产）——补准后对这 4 个 id 定向重跑，全部抓住。下表是合并后的最终结果（`mutants/results.json`）。

总数 **640**（8 个模块，每模块 80 个，抽样 seed=0），抓住 **590**（其中 timeout 1 个：死循环类突变体超过 4 倍基线时长判定），漏网 **50**，抓住率 **92.2%**。

| 模块 | 总数 | 抓住 | 其中 timeout | 漏网 | 抓住率 |
|---|---|---|---|---|---|
| `qbt/broker.py` | 80 | 74 | 0 | 6 | 92.5% |
| `qbt/cli.py` | 80 | 74 | 0 | 6 | 92.5% |
| `qbt/data.py` | 80 | 77 | 0 | 3 | 96.2% |
| `qbt/engine.py` | 80 | 77 | 0 | 3 | 96.2% |
| `qbt/indicators.py` | 80 | 72 | 1 | 8 | 90.0% |
| `qbt/metrics.py` | 80 | 74 | 0 | 6 | 92.5% |
| `qbt/optimizer.py` | 80 | 65 | 0 | 15 | 81.2% |
| `qbt/report.py` | 80 | 77 | 0 | 3 | 96.2% |

突变算子：二元运算符互换、比较运算符边界互换（< ↔ <=、== ↔ !=、is ↔ is not、in ↔ not in）、and ↔ or、删除 not / 负号、常量 ±1 / ×1.5 / 翻转、if/while 条件取反、语句删除（替换为 pass）、return 置 None、break ↔ continue。
运行方式：每个突变体复制整个 `qbt/` 到独立目录并软链其余包，子进程 `python -m unittest -f`（失败即停，先跑黄金文件与指标模块）。

漏网清单（每个给出“为什么等价”或“补了哪条判据”）：

| id | 行 | 变异 | 结论 |
|---|---|---|---|
| `broker:1117:compare` | 159 | `side < 0` → `side <= 0` | 等价：side 只取 ±1 |
| `broker:1934:compare` | 248 | `side > 0` → `side >= 0` | 等价：side 只取 ±1 |
| `broker:1938:compare` | 250 | `side < 0` → `side <= 0` | 等价：side 只取 ±1 |
| `broker:1957:const:1` | 255 | `0` → `1` | 等价：side 只取 ±1，side<0 与 side<1 相同 |
| `broker:3254:compare` | 317 | `pos.qty > 0` → `pos.qty >= 0` | 等价：qty == 0 对资产贡献 0 |
| `broker:2253:compare` | 383 | `st['qty'] > 0` → `st['qty'] >= 0` | 等价：round_trips 平仓态已删除，st['qty'] 不会为 0 |
| `cli:738:const:0` | 50 | `1` → `0` | 等价（格式）：summary.json 的 indent |
| `cli:738:const:2` | 50 | `1` → `2` | 等价（格式）：summary.json 的 indent |
| `cli:946:const:2` | 70 | `1` → `2` | 等价（格式）：unittest verbosity |
| `cli:301:const:0` | 71 | `1` → `0` | 不可观察：失败套件的返回码需要套件内存在失败用例 |
| `cli:301:const:2` | 71 | `1` → `2` | 不可观察：失败套件的返回码需要套件内存在失败用例 |
| `cli:812:const:True` | 91 | `False` → `True` | 等价：委托子命令的 add_help 只影响 -h 透传 |
| `data:265:const:-1` | 206 | `0` → `-1` | 等价：halt_left 初值 0 与 -1 在 `halt_left > 0` 判定下等价，随后统一赋值 |
| `data:1481:compare` | 228 | `rnd() < jump_prob` → `rnd() <= jump_prob` | 等价：rnd() < p 与 <= p 只在 rnd() 恰等于 p 时不同（测度为零，且 p 为 0.001 这种不可精确表示的概率） |
| `data:1876:compare` | 232 | `o > c` → `o >= c` | 等价：o == c 时 (o if o > c else c) 与 >= 取值相同 |
| `engine:1226:const:1.5000000000000002e-09` | 157 | `1e-09` → `1.5000000000000002e-09` | 等价：1e-9 容差常数 ×1.5 |
| `engine:832:compare` | 206 | `ctx.equity <= 0` → `ctx.equity < 0` | 等价：权益 == 0 时 0*fraction/price == 0 |
| `engine:1468:const:-1` | 274 | `0` → `-1` | 等价：单策略快路径 on_bars[0] 与 on_bars[-1] 相同 |
| `indicators:2065:const:0` | 72 | `1` → `0` | 等价：n>=1 时 int(sqrt(n))>=1，max(1,·) 冗余 |
| `indicators:2191:compare` | 127 | `d > 0` → `d >= 0` | 等价：d == 0 时 (d, 0.0) 与 (0.0, -d) 相同（-0.0 == 0.0） |
| `indicators:4696:binop` | 312 | `i + 1` → `i - 1` | 等价：rolling_std 周期性精确重算的时机（(i+1)%n 与 (i-1)%n）只影响浮点漂移与耗时，不改变语义 |
| `indicators:5839:binop` | 315 | `w - mean` → `w + mean` | 等价（数学恒等）：Σ(w+m)(w−m) = Σw² − n·m² = Σ(w−m)²，只差舍入 |
| `indicators:268:delete_stmt` | 450 | `_check_window(n)` → `pass` | 等价（冗余防御）：cmf 内的 _check_window 删除后，随后的 sma(mfv, n) 仍会对非法窗口抛同样的 ConfigError |
| `indicators:5001:const:1.5` | 457 | `1.0` → `1.5` | 等价（实际上）：cmf 的 1.0 上钳只用于吸收滚动和的浮点噪声，真实值不会超过 1，1.5 的钳位不可观察 |
| `indicators:5137:compare` | 527 | `bl > lower` → `bl >= lower` | 等价：bl == lower 时更新与否取值相同 |
| `indicators:3079:compare` | 550 | `v > peak` → `v >= peak` | 等价：峰值相等时更新与否不改变回撤值 |
| `metrics:425:const:1.5e-14` | 61 | `1e-14` → `1.5e-14` | 等价：零波动阈值 1e-14 的 1.5 倍，无可观察差异 |
| `metrics:514:const:True` | 104 | `False` → `True` | 等价：under 初值在 i=0 时无影响（i-start=0） |
| `metrics:1360:const:0` | 143 | `1` → `0` | 等价：k 由 ceil(alpha*n)-1 构造，永远 <= n-1，min 的上界不起作用 |
| `metrics:594:const:146098` | 152 | `146097` → `146098` | 等价（取值范围内）：Hinnant 算法对 doe 略超 146096 自洽；1962–2101 逐周对拍无差异 |
| `metrics:991:binop` | 152 | `z - 146096` → `z + 146096` | 不可达：z<0 对应公元 0 年 3 月之前的日期，时间戳输入不会到达 |
| `metrics:1041:const:86399` | 178 | `86400` → `86399` | 等价：period_returns 里“天是否变化”的缓存判定用 86399 只会更频繁地重算键，键本身由 key_fn 用 86400 计算，结果不变 |
| `optimizer:2064:const:False` | 78 | `True` → `False` | 等价：task id 的 sha256 输入键序改变只改变 id 的具体值，同输入仍稳定、不同输入仍不同 |
| `optimizer:1845:const:False` | 142 | `True` → `False` | 等价（JSON 语义）：断点里存储的 JSON 键序，读回后再排序输出 |
| `optimizer:310:delete_stmt` | 164 | `_worker_init(task['data_spec'])` → `pass` | 等价：串行路径的 _worker_init 只是预加载，evaluate 内 load_data 会惰性加载同一数据 |
| `optimizer:967:compare` | 167 | `'fork' in multiprocessing.get_all_start_methods()` → `'fork' not in multiprocessing.get_all_start_methods()` | 等价（平台）：Linux 上 multiprocessing 默认上下文就是 fork，取反后走默认分支结果相同 |
| `optimizer:2038:const:7` | 170 | `8` → `7` | 等价（调度）：进程池 chunksize 只影响任务切分，结果按键排序与之无关 |
| `optimizer:995:const:2` | 179 | `1` → `2` | 等价（资源）：workers==2 时不显式 shutdown 只影响进程回收，结果不变 |
| `optimizer:336:const:2` | 195 | `1` → `2` | 等价：walk_forward 的 workers 默认值；任何 workers 取值结果逐字节相同（这正是并行确定性判据），不可观察 |
| `optimizer:358:delete_stmt` | 214 | `_worker_init(task['data_spec'])` → `pass` | 等价：同上（walk-forward 样本外评估前的预加载） |
| `optimizer:1534:const:49` | 250 | `50` → `49` | 等价（日志）：进度打印间隔 50 改 49 |
| `optimizer:1534:const:51` | 250 | `50` → `51` | 等价（日志）：进度打印间隔 50 改 49 |
| `optimizer:753:const:2` | 252 | `3` → `2` | 等价：elapsed 的 round(…, 3) 改 2 只影响日志字符串的小数位 |
| `optimizer:753:const:4` | 252 | `3` → `4` | 等价：elapsed 的 round(…, 3) 改 2 只影响日志字符串的小数位 |
| `optimizer:1188:const:0` | 255 | `1` → `0` | 等价（格式）：输出 JSON 的 indent 只改变空白 |
| `optimizer:1188:const:2` | 255 | `1` → `2` | 等价（格式）：输出 JSON 的 indent 只改变空白 |
| `optimizer:1199:const:False` | 259 | `True` → `False` | 等价（缓冲）：print 的 flush 参数 |
| `report:1465:const:True` | 47 | `False` → `True` | 等价（JSON 语义）：策略参数行的 ensure_ascii 只影响非 ASCII 转义，参数均为 ASCII |
| `report:501:const:1.5` | 93 | `1.0` → `1.5` | 等价：max(dd) or 1.0 只在无回撤时生效，此时所有回撤为 0，分母取值不可观察 |
| `report:1206:const:True` | 134 | `False` → `True` | 等价（JSON 语义）：ensure_ascii 只改变非 ASCII 字符的转义形式，json.load 读回相同 |

## 5. 模糊测试

| 目标 | 结果类别 | 次数 |
|---|---|---|
| broker | ConfigError | 12390 |
| broker | ok | 12610 |
| engine | ConfigError | 4409 |
| engine | DataError | 7173 |
| engine | ok | 13418 |
| indicators | ConfigError | 3686 |
| indicators | ok | 21314 |
| metrics | ok | 25000 |

总计 100000 次（seed=0，4 类目标轮转），未捕获异常 0 次，耗时 5.98s；复现：`python3.12 fuzz/fuzz.py --iterations 100000 --seed 0 --workers 2`（结果在 `fuzz/results.json`）。
模糊测试发现并修复的 bug（均有对应单元判据）：

1. `BrokerConfig.check` 对非数值字段（字符串 / 列表 / bool / None）抛 `TypeError` 而非受控的 `ConfigError` → 增加 `_real()` 逐字段类型校验（`tests/test_broker.py::test_config_check`）。
2. `GenSpec.check` 同类问题（bool/NaN/非数值字段）→ 逐字段有限实数校验。
3. `metrics.cagr` 对极端权益比 `x ** (1/years)` 抛 `OverflowError`；比值下溢为 0 时 `log` 抛 `ValueError` → 改为对数形式，>700 截断为 1e300，比值 ≤ 0 返回 −1（`tests/test_metrics.py`）。
4. `metrics._std` / `sortino` 用 `** 2` 在 1e200 量级收益上抛 `OverflowError` → 改为乘法（float 溢出为 inf 而非异常）。
5. 策略参数 `ma="x"` 之类进入 `IndicatorCache.get` 抛 `KeyError` → 未知指标名抛 `ConfigError`；同时 `Strategy.__init__` 按默认值类型校验参数（bool/实数/str）并拒绝 NaN/inf（`tests/test_engine.py::test_strategy_helpers`）。

另有一处由随机订单流判据（非模糊器）发现的会计 bug：`allow_short=False` 时多笔挂单可把多头卖成空头 → 成交时再次封顶（`tests/test_broker.py::test_short_toggle`）。

## 6. 6 个策略 × 3 组 seed 的 walk-forward 结果（合成数据，不构成任何投资结论）

| 策略 | seed | 样本内夏普(均值) | 样本外夏普(均值) | 样本内收益(均值/折) | 样本外收益(均值/折) | 样本外最大回撤(最差折) | 样本外交易数 |
|---|---|---|---|---|---|---|---|
| bollinger_reversion | 101 | 3.10 | -0.35 | 0.66% | -0.67% | 4.40% | 35 |
| bollinger_reversion | 202 | 8.17 | -12.92 | 3.13% | -2.96% | 9.05% | 43 |
| bollinger_reversion | 303 | 3.29 | 5.17 | 1.14% | -0.08% | 9.14% | 35 |
| donchian_breakout | 101 | -3.96 | -28.01 | -2.14% | -4.13% | 8.83% | 90 |
| donchian_breakout | 202 | -13.39 | -3.58 | -7.81% | -0.58% | 4.77% | 103 |
| donchian_breakout | 303 | -7.97 | -18.33 | -3.65% | -4.22% | 7.96% | 148 |
| grid | 101 | 7.34 | -1.86 | 0.68% | -0.00% | 1.58% | 0 |
| grid | 202 | 5.30 | -10.21 | -2.31% | -1.10% | 3.21% | 5 |
| grid | 303 | 7.85 | 10.47 | 0.72% | -0.03% | 2.10% | 9 |
| ma_cross | 101 | 9.15 | -3.11 | 2.82% | 0.30% | 2.43% | 48 |
| ma_cross | 202 | 4.50 | -4.62 | 1.14% | 0.39% | 4.93% | 60 |
| ma_cross | 303 | 7.58 | -6.95 | 4.30% | -2.19% | 7.83% | 65 |
| rsi_reversion | 101 | -1.46 | -7.63 | -0.40% | -0.79% | 2.33% | 19 |
| rsi_reversion | 202 | 2.89 | -13.34 | 1.33% | -1.51% | 4.96% | 44 |
| rsi_reversion | 303 | 2.05 | -16.07 | 0.33% | -1.48% | 3.87% | 35 |
| trend_stop | 101 | 2.16 | -1.72 | 0.63% | 0.01% | 1.84% | 70 |
| trend_stop | 202 | -12.80 | -14.96 | -3.47% | -1.69% | 4.03% | 104 |
| trend_stop | 303 | -1.59 | 0.66 | -0.56% | -0.45% | 5.50% | 74 |

设置：每组 seed 生成 30000 根 1 分钟合成 K 线（含停牌/缺口/跳跃），3 折滚动 walk-forward，每折前 70% 样本内在 60 组随机参数中按夏普选优，后 30% 样本外评估；手续费 maker 2bp / taker 5bp，滑点 1bp。

复现：`python3.12 tools/walk_forward_report.py --n 30000 --combos 60 --folds 3 --workers 2 --out out/walk_forward.json`。
读法：样本内按夏普选优后样本外普遍显著变差（多数为负），是典型的参数过拟合表现；在带手续费与滑点的几何布朗运动合成数据上，没有任何策略应当有稳定正期望，这正是对"引擎不偷看未来、不漏算成本"的一个间接旁证。

## 7. 已知局限与下一步

- **多标的时间轴**要求各标的 bar 数一致（对齐），不支持缺失 bar 的对齐/前向填充。
- **walk-forward 的样本外段**在切片内重新预热指标（前 n 根无信号），没有用训练段尾部做预热；折内参数空间用随机抽样而非全网格。
- **CCI 的平均绝对偏差**是窗口内 O(w) 内层循环（整体 O(N·w)，w 有界），其余 35 个指标均为 O(1)/bar。
- **黄金文件**跨机器可能因 libm（exp/log）最后一位 ulp 差异而失配；本仓库的逐字节判据在同一机器上成立，跨机器需用容差模式重建基线。
- **覆盖率工具**只统计主进程；进程池内执行的代码靠串行路径覆盖。
- **性能**：8 进程目标在 4 物理核上实测（加速比 ≈ 3.95x）；单核 1M 根冷缓存 2.1–2.5 s 之间波动（机器噪声 ±0.3 s）。
- **突变测试**：每模块抽样 80 个（候选总数 60–1,200+），抽样由 seed 决定；全量跑需数小时。等价突变体按条列出，未做自动等价判定。
- 下一步：Numba/NumPy 不在约束内；可做的是把盯市与撮合按"无挂单快路径"进一步内联、指标按需惰性计算、walk-forward 预热、以及用 `multiprocessing.shared_memory` 共享大数据以降低 worker 初始化成本。
