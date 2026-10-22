# REPORT — qbt 回测与参数优化引擎（纯 Python 3.12 标准库）

> 所有数字来自仓库内脚本的真实运行输出（JSON 已入库），每个结论附复现命令。**所有数据均为合成数据，所有策略结果不构成任何投资结论。**

机器：Linux-6.18.44-fc-v140-x86_64-with-glibc2.39；CPU Intel(R) Xeon(R) Processor @ 2.10GHz（x86_64），`os.cpu_count()` = 4（4 物理核：8 进程目标在 4 核上实测）；内存 16 GB；Python 3.12.3；基准 JSON 内各自记录了生成时的 git 提交号（`bench/results/*.json` 的 `machine.git_commit`）。

## 1. 目标达成情况

| 目标 | 要求 | 实测 | 达成 | 复现命令 |
|---|---|---|---|---|
| 1 引擎正确（确定性） | 同策略同数据，任何执行顺序 / 进程数下结果逐字节一致 | 网格 2,000 组：1 进程 vs 8 进程（含不同提交顺序）结果 JSON 逐字节相同（`bytewise_identical = True`）；黄金文件逐字节回归；中途 SIGKILL 续跑输出与一次跑完逐字节相同；同 seed 两次生成数据逐字节相同 | ✅ | `python3.12 -m unittest tests.test_optimizer tests.test_golden tests.test_data tests.test_engine` |
| 2 够快 | 1M 根 × 1 策略单核 ≤ 3 s；8 进程 2,000 组网格 ≤ 90 s | 1,000,000 根 × ma_cross 单进程 **1.932 s**（冷缓存，含指标计算；热缓存 0.884 s，517,545 bars/s）；2,000 组 × 20,000 根网格 8 进程 **18.6 s**（1 进程 70.1 s，加速 3.78x） | ✅ | `python3.12 bench/bench.py --out bench/results/xx.json`（已入库：`bench/results/01..04*.json`，第 1 节引用 04） |
| 3 够硬 | 语句覆盖率 ≥ 90%；突变抓住率 ≥ 85%；模糊 10 万次零崩溃 | 覆盖率 **99.8%**（1915/1918 语句，`tools/cov.py`）；突变 **640 个突变体（8 模块 × 80），抓住 596（含 timeout 0），漏网 44，抓住率 93.1%**（多轮累计的最终合并结果，`mutants/results.json`，各轮原始 JSON 在 `mutants/history/`；漏网逐条说明见第 4 节）；模糊 **100,000 次，0 次未捕获异常** | ✅ | `python3.12 tools/cov.py --fail-under 90`（结果已入库：`results/coverage_final.json`）；`python3.12 mutants/run_mutations.py --per-module 80 --jobs 3 --tests -f tests.test_golden tests.test_indicators tests.test_broker tests.test_engine tests.test_metrics tests.test_optimizer tests.test_strategies tests.test_data tests.test_report tests.test_cli tests.test_limits tests.test_perf`；`python3.12 fuzz/fuzz.py --iterations 100000 --seed 0 --workers 2` |
| 4 够诚实 | 数字来自真实运行，结论可复现 | 本报告所有表格由 `bench/results/*.json`、`fuzz/results.json`、`mutants/results.json`（各轮中间结果在 `mutants/history/`）、`results/coverage_final.json`、`results/walk_forward.json` 生成，全部已入库；提交信息逐阶段记录判据数 / 覆盖率 / 抓住率（例外见第 7 节） | ✅ | `git log`；`python3.12 -m qbt.cli demo` |

测试套件：`python3.12 -m unittest discover -s tests` → **177 条判据**（test_data 33、test_indicators 33、test_broker 31、test_engine 12、test_strategies 9、test_metrics 18、test_golden 3、test_optimizer 17、test_report 6、test_cli 9、test_perf 3、test_limits 3），全部通过；性能判据在覆盖率监控 / 突变快速模式下跳过并注明原因（这两种环境下计时无意义），正常模式下真实运行并有阈值。

覆盖率明细（`python3.12 tools/cov.py`，sys.monitoring 语句覆盖；子进程内执行不计入，优化器因此保留串行路径覆盖同一代码）：

| 文件 | 语句 | 漏 | 覆盖 |
|---|---|---|---|
| `qbt/__init__.py` | 3 | 0 | 100.0% |
| `qbt/broker.py` | 328 | 0 | 100.0% |
| `qbt/cli.py` | 91 | 0 | 100.0% |
| `qbt/data.py` | 194 | 0 | 100.0% |
| `qbt/engine.py` | 206 | 0 | 100.0% |
| `qbt/errors.py` | 4 | 0 | 100.0% |
| `qbt/indicators.py` | 452 | 0 | 100.0% |
| `qbt/metrics.py` | 144 | 0 | 100.0% |
| `qbt/optimizer.py` | 181 | 1 | 99.4% |
| `qbt/report.py` | 97 | 0 | 100.0% |
| `strategies/__init__.py` | 8 | 0 | 100.0% |
| `strategies/bollinger.py` | 27 | 0 | 100.0% |
| `strategies/donchian.py` | 37 | 0 | 100.0% |
| `strategies/grid.py` | 42 | 1 | 97.6% |
| `strategies/ma_cross.py` | 32 | 0 | 100.0% |
| `strategies/rsi_reversion.py` | 29 | 0 | 100.0% |
| `strategies/trend_stop.py` | 43 | 1 | 97.7% |
| **合计** | 1918 | 3 | **99.8%** |

未覆盖行（共 3 行）：`qbt/optimizer.py:254`；`strategies/grid.py:29`；`strategies/trend_stop.py:45`（优化器 CLI 在 `main()` 内关闭断点的分支——“断点 + CLI”组合只在子进程 kill/续跑判据中执行，子进程不计入主进程覆盖；网格策略 level_qty ≤ 0、趋势策略 size ≤ 0 / 止损价 ≤ 0 的防御分支）。

## 2. 指标清单（36 个）与判据方式

所有指标位于 `qbt/indicators.py`，36 个中 35 个为 O(1)/bar 的单遍或滚动窗口实现；CCI 的“窗口内平均绝对偏差”是 O(w)/bar 的有界内层循环（整体 O(N·w)，不从头重算），这是对“全部 O(n)”条款的一个已知、已披露的例外（见第 7 节）。预热期显式 `None`，判据在 `tests/test_indicators.py`（参考实现在 `tests/_brute.py`）。
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
- **撮合与账户（阶段 2，31 条）**：每种订单类型的成交价规则与恰好触及边界；冰山分片、参与上限、零成交量不成交；冲击滑点单调；撤单/过期/提交顺序；均价与已实现盈亏（含反手）；资金费多付空收；保证金拒单、杠杆借入、reduce_only；空头开关两处封顶；强平罚金 + 撤单 + 破产；多标的敞口；随机订单流（5 组配置 × 多 seed）**逐 bar** 验证 `equity == cash + Σ qty·close`、`assets − liabilities == equity`、`initial + realized + unrealized − fees − funding == equity`（容差 1e-6，理由：百万笔不同顺序累加）、每笔成交量 ≤ 参与上限 × bar 成交量、maker 成交价 ∈ [low, high]、taker 成交价 ∈ [low·(1−slip), high·(1+slip)]，并从成交流独立重建现金与持仓。
- **引擎与策略（阶段 3，21 条）**：T 收盘下单、T+1 开盘成交；权益曲线与独立盯市逐根一致；**篡改 cut 之后的全部 K 线，6 个策略在 cut 之前的成交序列与权益逐字节不变**；strict 模式 6 个策略零 LookaheadError 且与非 strict 结果相同、偷看策略被拦截；多策略多标的；目标仓位处理部分成交；指标缓存命中/淘汰/不串台；确定性；每个策略的入场成交与指标信号逐笔对拍（按订单创建 bar）。
- **度量（阶段 4，18 条）**：≤ 20 根手算对拍（收益、夏普、索提诺（含 rf 与 ppy）、回撤与恢复、CAGR、Calmar、VaR/CVaR 顺序统计量、按日/月分组、交易统计）；随机权益上回撤与 O(n²) 暴力一致；月度复合 == 总收益；黄金文件。
- **优化器（阶段 5，17 条）**：见目标 1；另有断点 flush 语义、进度打印、walk-forward 折边界/单折/余数折/断点 id 隔离、CLI。
- **工程约束（3 条）**：每个函数 ≤ 60 行、每个文件 ≤ 800 行（AST 检查；代码行数 7111，复现：`cat qbt/*.py strategies/*.py tests/*.py tools/*.py mutants/*.py fuzz/*.py bench/*.py | wc -l`）；无第三方 import；无未显式 seed 的随机调用。

## 3. 性能优化前后对比

机器：Linux-6.18.44-fc-v140-x86_64-with-glibc2.39，x86_64，4 核（物理 4 核，8 进程目标在 4 核上实测），Python 3.12.3。

| 基准（bench/results/*.json） | 01 优化前 | 02 metrics 日历优化后 | 03 broker 热循环优化后 | 04 验收修复后 | 目标 |
|---|---|---|---|---|---|
| 1M 根 × ma_cross 单核，冷缓存（含指标计算） | 2.209s | 2.465s | 2.146s | 1.932s | ≤ 3s |
| 1M 根 × ma_cross 单核，热缓存 | 1.174s | 1.112s | 0.933s | 0.884s |  |
| 1M 根 metrics.compute | 5.41s | 1.81s | 1.58s | 1.39s |  |
| 吞吐（冷） | 452,671 bars/s | 405,720 bars/s | 465,919 bars/s | 517,545 bars/s |  |
| 2,000 组 × 20,000 根网格，8 进程 | 74.0s | 23.4s | 21.3s | 18.6s | ≤ 90s |
| 同上，1 进程 | 215.7s | 88.6s | 84.3s | 70.1s |  |
| 加速比（4 物理核） | 2.92x | 3.78x | 3.95x | 3.78x |  |
| 1 进程 vs 8 进程结果逐字节相同 | True | True | True | True | True |
| 1M 根数据生成 / 校验 | 1.89s / 0.38s | 2.00s / 0.53s | 1.83s / 0.32s | 1.73s / 0.36s |  |
| 1M 根 SMA / EMA / ATR / ADX | 0.13 / 0.08 / 0.37 / 1.35s | 0.23 / 0.11 / 0.55 / 1.66s | 0.12 / 0.08 / 0.37 / 1.24s | 0.11 / 0.06 / 0.32 / 1.07s |  |

每次改动的收益：
1. `metrics.period_returns` 去掉每根 bar 的 `datetime.fromtimestamp`，改为整数天序号 + Hinnant 民用历算法（与 datetime 逐周对拍 1962–2101 零差异）：metrics 1M 根 5.41s → 1.81s；网格每组开销随之下降，8 进程 74.0s → 23.4s，1 进程 215.7s → 88.6s。
2. `Broker.on_bar` 盯市热循环改用预构造的 (symbol, Position, close 列表) 行表，去掉每根 bar 的 `dict.items()` 与 `data[sym].close` 属性链：热回测 1.112s → 0.933s（cProfile 300k 根：broker.on_bar 0.362s → 0.226s）；网格 8 进程 23.4s → 21.3s。黄金文件逐字节不变。
3. 数据规模自始至终固定（1,000,000 根；2,000 组 × 20,000 根），没有缩小数据换达标；单核基线在优化前就已达标（2.209s），优化主要兑现在网格搜索上。
4. 基准 JSON 里的 `machine.git_commit` 是基准运行时的 HEAD：基准总是在把本轮改动提交之前运行，所以 02/03 记录的提交号是其父提交（被测代码在下一个提交里）。04 是验收修复后的重跑。

## 4. 突变测试

6. **验收评审后（第六、七轮，定向）**：独立验收评审（7 个维度 + 对抗复核，见第 7.0 节）确认的缺陷修复后，broker / indicators / metrics 三个改过源码的模块全量重抽样重跑并合并，`cli:301` 两个此前标为“不可观察”的 id 用 `unittest.mock` 判据定向重跑（全部抓住）；随后删掉 `_margin_ok` 里因修复而变死的 `reduce_only` 分支并补 4 条判据，broker 再次全量重跑合并；最后对补了判据但尚未重跑的 5 个 id 定向重跑（全部抓住）。下表是合并后的最终结果（`mutants/results.json`，各轮原始 JSON 在 `mutants/history/`）。

总数 **640**（8 个模块，每模块 80 个，抽样 seed=0），抓住 **596**（其中 timeout 0 个：死循环类突变体超过 4 倍基线时长判定），漏网 **44**，抓住率 **93.1%**。

| 模块 | 总数 | 抓住 | 其中 timeout | 漏网 | 抓住率 |
|---|---|---|---|---|---|
| `qbt/broker.py` | 80 | 73 | 0 | 7 | 91.2% |
| `qbt/cli.py` | 80 | 76 | 0 | 4 | 95.0% |
| `qbt/data.py` | 80 | 77 | 0 | 3 | 96.2% |
| `qbt/engine.py` | 80 | 77 | 0 | 3 | 96.2% |
| `qbt/indicators.py` | 80 | 78 | 0 | 2 | 97.5% |
| `qbt/metrics.py` | 80 | 73 | 0 | 7 | 91.2% |
| `qbt/optimizer.py` | 80 | 65 | 0 | 15 | 81.2% |
| `qbt/report.py` | 80 | 77 | 0 | 3 | 96.2% |

突变算子：二元运算符互换、比较运算符边界互换（< ↔ <=、== ↔ !=、is ↔ is not、in ↔ not in）、and ↔ or、删除 not / 负号、常量 ±1 / ×1.5 / 翻转、if/while 条件取反、语句删除（替换为 pass）、return 置 None、break ↔ continue。
运行方式：每个突变体复制整个 `qbt/` 到独立目录并软链其余包，子进程 `python -m unittest -f`（失败即停，先跑黄金文件与指标模块）。

漏网清单（每个给出“为什么等价”或“补了哪条判据”）：

| id | 行 | 变异 | 结论 |
|---|---|---|---|
| `broker:995:const:1.0` | 105 | `0.0` → `1.0` | 等价：Position.avg_price 初值在首笔成交时被 (0*avg + qty*price)/qty 覆盖 |
| `broker:2594:compare` | 223 | `qty < abs(held)` → `qty <= abs(held)` | 等价：qty == |held| 时封顶取值相同 |
| `broker:1969:compare` | 263 | `side < 0` → `side <= 0` | 等价：side 只取 ±1 |
| `broker:2685:const:1` | 263 | `0` → `1` | 等价：side 只取 ±1 |
| `broker:2086:compare` | 304 | `q > 0` → `q >= 0` | 等价：q == 0 已在前一个条件处理 |
| `broker:1461:compare` | 392 | `st['qty'] > 0` → `st['qty'] >= 0` | 等价：round_trips 平仓态已删除，st['qty'] 不会为 0 |
| `broker:850:binop` | 399 | `(f.price - st['avg']) * closing * (1.0 if st['qty'] > 0 else -1.0)` → `(f.price - st['avg']) * closing / (1.0 if st['qty'] > 0 else -1.0)` | 等价：乘以 ±1.0 与除以 ±1.0 相同 |
| `cli:738:const:0` | 50 | `1` → `0` | 等价（格式）：summary.json 的 indent |
| `cli:738:const:2` | 50 | `1` → `2` | 等价（格式）：summary.json 的 indent |
| `cli:946:const:2` | 70 | `1` → `2` | 等价（格式）：unittest verbosity |
| `cli:812:const:True` | 91 | `False` → `True` | 等价：委托子命令的 add_help 只影响 -h 透传 |
| `data:265:const:-1` | 206 | `0` → `-1` | 等价：halt_left 初值 0 与 -1 在 `halt_left > 0` 判定下等价，随后统一赋值 |
| `data:1481:compare` | 228 | `rnd() < jump_prob` → `rnd() <= jump_prob` | 等价：rnd() < p 与 <= p 只在 rnd() 恰等于 p 时不同（测度为零，且 p 为 0.001 这种不可精确表示的概率） |
| `data:1876:compare` | 232 | `o > c` → `o >= c` | 等价：o == c 时 (o if o > c else c) 与 >= 取值相同 |
| `engine:1226:const:1.5000000000000002e-09` | 157 | `1e-09` → `1.5000000000000002e-09` | 等价：1e-9 容差常数 ×1.5 |
| `engine:832:compare` | 206 | `ctx.equity <= 0` → `ctx.equity < 0` | 等价：权益 == 0 时 0*fraction/price == 0 |
| `engine:1468:const:-1` | 274 | `0` → `-1` | 等价：单策略快路径 on_bars[0] 与 on_bars[-1] 相同 |
| `indicators:1138:const:0` | 55 | `1` → `0` | 等价：ema 的循环从 0 开始时第 0 步 prev + a*(x0 - prev) == x0，输出不变 |
| `indicators:329:delete_stmt` | 567 | `_check_window(n)` → `pass` | 等价（冗余防御）：rolling_max_drawdown 内随后的 rolling_max(dd, n) 仍校验窗口 |
| `metrics:176:compare` | 46 | `growth > 700.0` → `growth >= 700.0` | 等价（测度零）：growth 恰等于 700.0 时 exp(700) 仍有限，截断与否都是有限大数 |
| `metrics:467:const:-1` | 86 | `0` → `-1` | 等价：peak_idx 初值在 i=0 必被覆盖 |
| `metrics:467:const:1` | 86 | `0` → `1` | 等价：peak_idx 初值在 i=0 必被覆盖 |
| `metrics:517:const:-1` | 105 | `0` → `-1` | 等价：longest_drawdown 的 start 初值在 i=0 必被覆盖 |
| `metrics:585:const:-1` | 146 | `0` → `-1` | 等价：alpha 已校验 ∈ (0,1]，ceil(alpha*n)-1 >= 0，max 的下界不起作用 |
| `metrics:1399:const:1` | 155 | `0` → `1` | 不可达：z == 0 对应公元 0 年 3 月 1 日 |
| `metrics:1403:const:146097` | 155 | `146096` → `146097` | 不可达：z < 0 对应公元 0 年 3 月之前 |
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
| indicators | ConfigError | 4404 |
| indicators | ok | 20596 |
| metrics | ok | 25000 |

总计 100000 次（seed=0，4 类目标轮转），未捕获异常 0 次，耗时 8.72s；复现：`python3.12 fuzz/fuzz.py --iterations 100000 --seed 0 --workers 2`（结果在 `fuzz/results.json`；验收后另用 seed=5 跑 30,000 次同样 0 崩溃）。
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
| trend_stop | 101 | 5.19 | -0.76 | 1.54% | 0.11% | 1.79% | 54 |
| trend_stop | 202 | -11.35 | -15.03 | -3.01% | -1.72% | 4.11% | 94 |
| trend_stop | 303 | -3.85 | 0.76 | -0.97% | -0.28% | 4.16% | 43 |

设置：每组 seed 生成 30000 根 1 分钟合成 K 线（含停牌/缺口/跳跃），3 折滚动 walk-forward，每折前 70% 样本内在 60 组随机参数中按夏普选优，后 30% 样本外评估；手续费 maker 2bp / taker 5bp，滑点 1bp。

复现：`python3.12 tools/walk_forward_report.py --n 30000 --combos 60 --folds 3 --workers 2 --out results/walk_forward.json`（已入库：`results/walk_forward.json` / `.md`）。
读法：样本内按夏普选优后样本外普遍显著变差（多数为负），是典型的参数过拟合表现；在带手续费与滑点的几何布朗运动合成数据上，没有任何策略应当有稳定正期望，这正是对"引擎不偷看未来、不漏算成本"的一个间接旁证。

## 7. 已知局限与下一步

### 7.0 验收评审发现并已修复的缺陷（独立评审，见 7.1）

- **reduce_only 可反手**：减仓单数量不封顶时，部分成交的入场配上全量止损会把多头打成空头（`trend_stop` 在默认 10% 参与上限下 46/72 次运行出现）。修复：成交时数量封顶在持仓、仓位归零后余量撤销（`cancelled/reduce_only`）、无仓或同向直接拒绝；判据 `tests/test_broker.py::TestReduceOnlyAndMargin`、`tests/test_strategies.py`（默认配置下仓位永不为负）。
- **`trend_stop` 看账户级挂单**：入场门槛用 `ctx.broker.pending`，与网格等常挂单的策略同账户时永远不入场。修复：只看自己的入场单；判据：与 `grid` 同跑仍有止损单。
- **`trend_stop` 在极端波动下抛 OrderError**（止损价 ≤ 0）：修复为不入场；判据：σ=80 的数据上跑完。
- **部分成交后保证金不足被标为 rejected**：改为 `cancelled/margin` 并保留已成交部分；判据见上。
- **多标的保证金盯市偷看本根收盘**：开盘撮合时其他标的改用上一根收盘（与权益口径一致）；判据：按本根收盘会通过、按上一根收盘必须拒绝的构造用例。
- **指标非窗口参数 / VaR 的 alpha 未校验**（原始 TypeError/ZeroDivisionError/IndexError）：改为 `ConfigError`，模糊器增加对应输入；`var_cvar` 去掉因此变死的 `min(n-1,·)` 守卫（原被误判为等价的漏网项）。
- 测试侧：删掉 2 条同义反复断言；14 处隐式默认容差改为显式 `delta` 并注明理由；性能阈值收紧到规格按比例折算值（200k 根冷缓存 ≤ 0.6s、指标 200k ≤ 0.25s、80 组×5k 根 4 进程 ≤ 8s）；`cli test` 失败返回码用 `unittest.mock` 判据覆盖。

### 7.1 关于提交信息里的数字（如实说明）

- 阶段 1/6/7 的提交信息引用的测试数/覆盖率来自提交时的**工作区**，其中包含了尚未提交的下一阶段测试文件（例如阶段 1 的“90 tests”含阶段 2 的测试）；阶段 6 拆成两个提交且没有写覆盖率/抓住率。历史已推送，不做改写；本节与第 1 节的数字以仓库内 JSON 为准。
- 基准 JSON 的 `git_commit` 是运行时的父提交（见第 3 节第 4 条）。


- **多标的时间轴**要求各标的 bar 数一致（对齐），不支持缺失 bar 的对齐/前向填充。
- **walk-forward 的样本外段**在切片内重新预热指标（前 n 根无信号），没有用训练段尾部做预热；折内参数空间用随机抽样而非全网格。
- **CCI 的平均绝对偏差**是窗口内 O(w) 内层循环（整体 O(N·w)，w 有界），其余 35 个指标均为 O(1)/bar。
- **黄金文件**跨机器可能因 libm（exp/log）最后一位 ulp 差异而失配；本仓库的逐字节判据在同一机器上成立，跨机器需用容差模式重建基线。
- **覆盖率工具**只统计主进程；进程池内执行的代码靠串行路径覆盖。
- **性能**：8 进程目标在 4 物理核上实测（加速比 ≈ 3.95x）；单核 1M 根冷缓存 2.1–2.5 s 之间波动（机器噪声 ±0.3 s）。
- **突变测试**：每模块抽样 80 个（候选总数 60–1,200+），抽样由 seed 决定；全量跑需数小时。等价突变体按条列出，未做自动等价判定。
- 下一步：Numba/NumPy 不在约束内；可做的是把盯市与撮合按"无挂单快路径"进一步内联、指标按需惰性计算、walk-forward 预热、以及用 `multiprocessing.shared_memory` 共享大数据以降低 worker 初始化成本。
