# REPORT — qbt 回测与参数优化引擎（纯 Python 3.12 标准库）

> 所有数字来自仓库内脚本的真实运行输出（JSON 已入库），每个结论附复现命令。**所有数据均为合成数据，所有策略结果不构成任何投资结论。**

机器：Linux-6.18.44-fc-v140-x86_64-with-glibc2.39；CPU Intel(R) Xeon(R) Processor @ 2.10GHz（x86_64），`os.cpu_count()` = 4（4 物理核：8 进程目标在 4 核上实测）；内存 16 GB；Python 3.12.3；基准 JSON 内各自记录了生成时的 git 提交号（`bench/results/*.json` 的 `machine.git_commit`）。

## 1. 目标达成情况

**口径（2026-10-22 审核改写，审核记录在仓库根 `REVIEW_QBT_LOB.md`）**

- 平台无关的数字：整数计数（成交笔数、订单数、判据数、覆盖语句数、突变体数与抓住数、模糊迭代数）、判据通过 / 失败，以及「同一台机器上两次运行逐字节一致」。
- 平台相关的数字：所有计时、吞吐、加速比；浮点结果的逐字节 / sha256 形式。x86_64 Linux 与 Apple Silicon macOS 的 libm / FMA 差异在 1e-16 量级，经滚动和、乘方放大后可到 1e-9 量级。本审核之前的黄金文件 / 数据指纹判据把浮点 sha256 当成跨机判据，在 macOS arm64 + Python 3.12.15 上红了 3 条，CCI 对拍容差过紧又红 1 条；已全部改成「整数精确 + 浮点相对容差」（§1.1，`tests/_compare.py`），并给每条容差配了「放水判据」（故意把实现改错 1e-6 必须变红）。
- 两台机器：**Linux x86_64** = 开发与审核机（Intel Xeon @ 2.10GHz 虚拟机，`os.cpu_count()` = 4，CPython 3.12.3，Linux 6.18.44，本审核在它上面复测了下面每个数字）；**macOS arm64** = 题面给出的事实（Apple Silicon，Python 3.12.15）。审核员没有这台 macOS 机器，凡写「未复测」的，就是没有在它上面跑过，不是「预计通过」的委婉说法。

| 目标 | 要求 | 判据 | 平台相关？ | Linux x86_64（本机实测） | macOS arm64 | 达成 |
|---|---|---|---|---|---|---|
| 1a 确定性：同机逐字节 | 同策略同数据，任何执行顺序 / 进程数下结果一致 | 同 seed 两次生成数据逐字节相同（进程内两次 + 子进程各一次，`test_data`）；`bench` 的 2,000 组网格 1 进程 vs 8 进程结果 JSON 逐字节相同（05 JSON `bytewise_identical = True`）；`test_optimizer` 的 8 组网格 1 vs 2 进程（逆序提交）vs 8 进程（chunksize=1）形状 + 容差一致，其中 1 vs 8 另加同机逐字节断言；中途 SIGKILL 续跑与一次跑完逐字节相同；黄金文件两次快照逐字节相同（`test_golden.TestGoldenSameMachine`） | 无关（任何机器上都应成立，但只在同一台机器内比较） | 全部通过 | 未复测（旧的 sha256 判据在该机红 3 条，见 §1.1；新判据不依赖 sha256） | ✅ Linux；macOS 未复测 |
| 1b 确定性：跨机形状 + 容差 | 同上，跨机器 | 黄金文件 `tests/golden/golden_v2.json`（JSON，浮点 repr 17 位；生成机器 / 提交写在 `generated_on`）：整数字段精确，资金类浮点相对 1e-9，度量类 5e-7；跨 `PYTHONHASHSEED`（本进程 + 0 + 4242 两个子进程）回测与网格 + walk-forward 结果形状 + 容差一致（`tests/test_determinism.py`，harness 来自 `qdcheck/`，未改）；1-ulp 价格噪声模拟（±2.2e-16 交替）下 6 个策略全部在容差内；放水判据：浮点改错 1e-6 必红、整数字段 ±1 必红 | 浮点量级平台相关，判据按跨机设计 | 全部通过 | 未复测。预期在容差内（题面给出的 CCI 差异 1e-9 量级，远小于容差），但这不是数学保证：某根 bar 上信号恰落在相等边界时整数字段会翻转，判据会红并给出首个差异路径 | ✅ Linux；macOS 未复测 |
| 2 够快 | 1M 根 × 1 策略单核 ≤ 3 s；8 进程 2,000 组网格 ≤ 90 s | `bench/bench.py`：1,000,000 根 × ma_cross 单进程冷缓存（指标缓存清空，数据在内存）；2,000 组 × 20,000 根，进程池 8，`os.cpu_count()` = 4 | **相关** | 历史（04，单次）：冷 1.932 s / 热 0.884 s；网格 8 进程 18.6 s / 1 进程 70.1 s。**本审核复测（best-of-3，`bench/results/05_audit_rerun.json`）**：冷 **1.941 s**（样本 [1.941, 2.058, 2.073]）/ 热 1.039 s；网格 8 进程 **20.1 s**（样本 [22.268, 20.181, 20.081]；实际观察到的子进程数 8）/ 1 进程 77.5 s；复测前 1 分钟负载 0.14 | 题面未提供计时，**未测** | ✅ Linux；macOS 未测 |
| 3a 够硬：覆盖率 | 语句覆盖率 ≥ 90% | `tools/cov.py`（sys.monitoring 语句覆盖，子进程不计入） | 语句计数无关；前提是该平台上测试全绿 | 历史 99.8%（1915/1918，`results/coverage_final.json`）；**本审核复测 100.0%**（1950/1950 语句，`results/coverage_audit.json`；语句数变化来自边界修复新增的代码） | 未复测 | ✅ Linux |
| 3b 够硬：突变 | 抓住率 ≥ 85% | `mutants/run_mutations.py`，8 个模块 × 80 个突变体，seed 0，fail-fast | 整数计数无关；**但抓住率只在测试全绿的平台上有意义**：旧判据在 macOS 红 4 条时，fail-fast 会把每个突变体都算「抓住」，所以此前的 93.1% 只对 Linux 成立 | 历史：640 / 抓住 596 / 漏网 44 / **93.1%**（`mutants/results.json`，原测试集）。本审核：5 个模块源码改过（cli / data / engine / metrics / optimizer）且判据集扩大（contract / boundaries / determinism），8 个模块 × 80 个突变体**全部重抽重跑** → 640 个突变体 / 抓住 608 / 漏网 32 / **95.0%**（`mutants/results_audit.json`；历史 640 / 596 / 44 / 93.1% 在 `mutants/results.json`）（§4 的表与漏网清单给出历史 / 本机两列） | 未复测 | ✅ Linux |
| 3c 够硬：模糊 | 10 万次零未捕获异常 | `fuzz/fuzz.py --iterations 100000 --seed 0 --workers 2` | 无关（全部随机显式 seed） | 历史 100,000 次 / 0 崩溃 / 8.7 s（`fuzz/results.json`）；本审核复测 100,000 次 / 0 崩溃 / 5.0 s（`fuzz/results_audit.json`） | 未复测 | ✅ Linux |
| 4 够诚实 | 数字来自真实运行，结论可复现 | 本报告所有表格由 `bench/results/*.json`、`fuzz/results.json`、`mutants/results.json`（`mutants/history/` 只保存第 1–4 轮与最终合并 run8（= results.json），第 5–7 轮定向重跑的原始 JSON 未保留）、`results/coverage_final.json`、`results/walk_forward.json` 生成，全部入库；提交信息逐阶段记录判据数 / 覆盖率 / 抓住率（例外见 §7.1）；本次审核的每条检查「命令 → 输出 → 结论」在 `REVIEW_QBT_LOB.md` | — | — | — | ✅ |

复现命令：目标 1 `python3.12 -m unittest tests.test_golden tests.test_data tests.test_optimizer tests.test_determinism tests.test_engine`；目标 2 `python3.12 bench/bench.py --repeat 3 --label audit_rerun --out bench/results/05_audit_rerun.json`；目标 3 `python3.12 tools/cov.py --fail-under 90 --json results/coverage_audit.json`、`python3.12 mutants/run_mutations.py --per-module 80 --jobs 3 --out mutants/results_audit.json --md mutants/survivors_audit.md --tests "-f tests.test_golden tests.test_indicators tests.test_broker tests.test_engine tests.test_metrics tests.test_optimizer tests.test_strategies tests.test_data tests.test_report tests.test_cli tests.test_limits tests.test_perf tests.test_contract tests.test_boundaries tests.test_determinism"`、`python3.12 fuzz/fuzz.py --iterations 100000 --seed 0 --workers 2 --out fuzz/results_audit.json`；整套判据 `python3.12 -m qbt.cli test`（冒烟 `--quick`，退出码见 README）。

测试套件（本审核复测，Linux x86_64）：`python3.12 -m qbt.cli test` = `python3.12 -m unittest discover -s tests -p 'test_*.py'` → **263 条判据**（test_boundaries 43、test_broker 31、test_cli 11、test_contract 29、test_data 34、test_determinism 3、test_engine 12、test_golden 9、test_indicators 33、test_limits 3、test_metrics 19、test_optimizer 18、test_perf 3、test_report 6、test_strategies 9），全部通过（OK），26.7 s，退出码 0；`--quick` 126 条 1.2 s。性能判据（`test_perf`）在覆盖率监控 / 突变快速模式（`QBT_FAST_TESTS=1`）下跳过并注明原因（这两种环境下计时无意义），正常模式下真实运行并有阈值（阈值是 PLAN.md 目标按数据量折算：200k 根 ≤ 0.6 s 等，平台相关，慢机器上会红——这是有意的，不放宽）。

覆盖率明细（`python3.12 tools/cov.py`，sys.monitoring 语句覆盖；子进程内执行不计入，优化器因此保留串行路径覆盖同一代码）：

| 文件 | 语句 | 漏 | 覆盖 |
|---|---|---|---|
| `qbt/__init__.py` | 3 | 0 | 100.0% |
| `qbt/broker.py` | 328 | 0 | 100.0% |
| `qbt/cli.py` | 100 | 0 | 100.0% |
| `qbt/data.py` | 197 | 0 | 100.0% |
| `qbt/engine.py` | 212 | 0 | 100.0% |
| `qbt/errors.py` | 4 | 0 | 100.0% |
| `qbt/indicators.py` | 452 | 0 | 100.0% |
| `qbt/metrics.py` | 146 | 0 | 100.0% |
| `qbt/optimizer.py` | 190 | 0 | 100.0% |
| `qbt/report.py` | 97 | 0 | 100.0% |
| `strategies/__init__.py` | 8 | 0 | 100.0% |
| `strategies/bollinger.py` | 27 | 0 | 100.0% |
| `strategies/donchian.py` | 37 | 0 | 100.0% |
| `strategies/grid.py` | 45 | 0 | 100.0% |
| `strategies/ma_cross.py` | 32 | 0 | 100.0% |
| `strategies/rsi_reversion.py` | 29 | 0 | 100.0% |
| `strategies/trend_stop.py` | 43 | 0 | 100.0% |
| **合计** | 1950 | 0 | **100.0%** |

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

- **数据（阶段 0，34 条）**：同 seed 同机逐字节一致（进程内两次 + 子进程）、跨机与黄金 `pinned_data` 相对 1e-9 一致（附放水判据）；长序列前缀 == 短序列（每根固定消耗随机数）；GBM 对数收益均值/方差在 5 个标准误内；标准正态分数位（|z|<1、<2）与峰度；影线半正态矩；成交量与 |收益| 相关；停牌段平价零量、缺口、跳跃方差放大；逐类非法输入拒绝（NaN/inf/负价/零价/负量/high<low/时间戳不递增或重复/长度不一致/非数值/空 symbol）；CSV 往返逐字节；黄金指纹钉住。
- **撮合与账户（阶段 2，31 条）**：每种订单类型的成交价规则与恰好触及边界；冰山分片、参与上限、零成交量不成交；冲击滑点单调；撤单/过期/提交顺序；均价与已实现盈亏（含反手）；资金费多付空收；保证金拒单、杠杆借入、reduce_only；空头开关两处封顶；强平罚金 + 撤单 + 破产；多标的敞口；随机订单流（5 组配置 × 多 seed）**逐 bar** 验证 `equity == cash + Σ qty·close`、`assets − liabilities == equity`、`initial + realized + unrealized − fees − funding == equity`（容差 1e-6，理由：百万笔不同顺序累加）、每笔成交量 ≤ 参与上限 × bar 成交量、maker 成交价 ∈ [low, high]、taker 成交价 ∈ [low·(1−slip), high·(1+slip)]，并从成交流独立重建现金与持仓。
- **引擎与策略（阶段 3，21 条）**：T 收盘下单、T+1 开盘成交；权益曲线与独立盯市逐根一致；**篡改 cut 之后的全部 K 线，6 个策略在 cut 之前的成交序列与权益逐字节不变**；strict 模式 6 个策略零 LookaheadError 且与非 strict 结果相同、偷看策略被拦截；多策略多标的；目标仓位处理部分成交；指标缓存命中/淘汰/不串台；确定性；每个策略的入场成交与指标信号逐笔对拍（按订单创建 bar）。
- **度量（阶段 4，18 条）**：≤ 20 根手算对拍（收益、夏普、索提诺（含 rf 与 ppy）、回撤与恢复、CAGR、Calmar、VaR/CVaR 顺序统计量、按日/月分组、交易统计）；随机权益上回撤与 O(n²) 暴力一致；月度复合 == 总收益。
- **优化器（阶段 5，17 条）**：见目标 1（1 / 2 / 8 进程形状 + 容差 + 放水判据，同机逐字节单列）；另有断点 flush 语义、进度打印、walk-forward 折边界/单折/余数折/断点 id 隔离、CLI。
- **黄金文件（8 条，审核改写）**：格式与来源字段；数据前 200 根容差；6 个策略整数字段精确 / 资金 1e-9 / 度量 5e-7；非平凡性；放水判据（浮点改错 1e-6 必红、整数 ±1 必红）；1-ulp 价格噪声模拟；同机两次快照逐字节相同。
- **跨进程 / 跨哈希种子确定性（3 条，审核新增，`tests/test_determinism.py`，harness 来自 `qdcheck/`）**：回测与网格 + walk-forward 在本进程 + `PYTHONHASHSEED=0 / 4242` 两个子进程中形状 + 容差一致；harness 放水判据（set 迭代顺序泄漏、hash 相关噪声必须被抓住）。
- **接口契约（29 条，审核新增，`tests/test_contract.py`）**：`qbt/CONTRACT.md` 26 张表逐表对照代码（字段 / 默认值 / 状态 × reason / 符号约定 13 条 / 错误契约 / 指标注册表 / 结果 JSON 键 / 边界表编号）；元判据：任一表删一行必红。
- **边界（43 条，审核新增，`tests/test_boundaries.py`）**：每个入口的空数据 / 单根 / 全停牌 / 价格 0・负・NaN・inf / 数量 0・负・NaN / 杠杆 1e6・1e-6 / 空参数空间 / 未知标的 / 未知策略 / 坏 CSV token 等，裸异常即红，与 CONTRACT §9 错误码表 B01–B50 一一对应。
- **工程约束（3 条）**：每个函数 ≤ 60 行、每个文件 ≤ 800 行（AST 检查；代码行数 8877，复现：`cat qbt/*.py strategies/*.py tests/*.py tools/*.py mutants/*.py fuzz/*.py bench/*.py | wc -l`）；无第三方 import；无未显式 seed 的随机调用。

## 3. 性能优化前后对比

**计时口径（审核 2026-10-22 补全）**：所有列都来自 `bench/results/*.json`，每个 JSON 的 `machine` 字段记录平台 / CPU 型号 / `os.cpu_count()` / Python 版本 / git 提交 / 完整命令行 / 起跑时 1 分钟负载（旧文件 01–04 没有 CPU 型号、命令行、负载三项，它们是审核时给 `bench.py` 加的）。

| 项 | 01–04（历史） | 05 审核复测 |
|---|---|---|
| 机器 | Linux-6.18.44-fc-v140-x86_64，Intel Xeon @ 2.10GHz 虚拟机，`os.cpu_count()` = 4，CPython 3.12.3 | 同一台虚拟机（`machine` 字段见 JSON） |
| 命令 | `python3.12 bench/bench.py --out bench/results/0N_*.json`（单次，best-of-1） | `python3.12 bench/bench.py --repeat 3 --label audit_rerun --out bench/results/05_audit_rerun.json`（每个计时 3 次取最小，样本列在 JSON 的 `*_samples`） |
| 冷 / 热 | 冷 = 进程内指标缓存清空、数据 sha256 指纹（`OHLCV._fingerprint`，≈ 0.6 s）未算的第一次回测（数据已在内存，不涉及磁盘页缓存）；热 = 紧接着第二次（指纹与指标全部命中） | 同左；best-of-N 的每个冷样本前都重置指标缓存与指纹（审核发现第 2 次起指纹已记忆、「冷」只剩指标，已修） |
| 进程数 | 网格：进程池上限 8（`ProcessPoolExecutor(max_workers=8)`，fork），4 个 CPU 上跑；实际子进程数未记录 | 进程池上限 8；运行期间采样 `multiprocessing.active_children()` 的最大值记录在 `processes_observed_max` |
| 负载 | 未记录 | 起跑时 1 分钟负载记录在 `load_avg_1m_at_start`（复测在审核代理全部结束、机器空闲后进行） |

| 基准（bench/results/*.json） | 01 优化前 | 02 metrics 日历优化后 | 03 broker 热循环优化后 | 04 验收修复后 | 05 审核复测（best-of-3） | 目标 |
|---|---|---|---|---|---|---|
| 1M 根 × ma_cross 单核，冷缓存（含指标计算） | 2.209s | 2.465s | 2.146s | 1.932s | 1.941s | ≤ 3s |
| 1M 根 × ma_cross 单核，热缓存 | 1.174s | 1.112s | 0.933s | 0.884s | 1.039s |  |
| 1M 根 metrics.compute | 5.41s | 1.81s | 1.58s | 1.39s | 1.17s |  |
| 吞吐（冷） | 452,671 bars/s | 405,720 bars/s | 465,919 bars/s | 517,545 bars/s | 515,215 bars/s |  |
| 2,000 组 × 20,000 根网格，进程池 8（4 CPU） | 74.0s | 23.4s | 21.3s | 18.6s | 20.1s | ≤ 90s |
| 同上，1 进程 | 215.7s | 88.6s | 84.3s | 70.1s | 77.5s |  |
| 加速比（4 CPU） | 2.92x | 3.78x | 3.95x | 3.78x | 3.86x |  |
| 实际观察到的最大子进程数 | — | — | — | — | 8 |  |
| 1 进程 vs 8 进程结果逐字节相同（同机） | True | True | True | True | True | True |
| 1M 根数据生成 / 校验 | 1.89s / 0.38s | 2.00s / 0.53s | 1.83s / 0.32s | 1.73s / 0.36s | 1.84s / 0.38s |  |
| 1M 根 SMA / EMA / ATR / ADX | 0.13 / 0.08 / 0.37 / 1.35s | 0.23 / 0.11 / 0.55 / 1.66s | 0.12 / 0.08 / 0.37 / 1.24s | 0.11 / 0.06 / 0.32 / 1.07s | 0.09 / 0.05 / 0.27 / 1.36s |  |
| best-of-N | 1 | 1 | 1 | 1 | 3 |  |
| 起跑时 1 分钟负载 | — | — | — | — | 0.14 |  |

每次改动的收益：
1. `metrics.period_returns` 去掉每根 bar 的 `datetime.fromtimestamp`，改为整数天序号 + Hinnant 民用历算法（与 datetime 每 7 天一点对拍 1961-10 至 2101-05 零差异）：metrics 1M 根 5.41s → 1.81s；网格每组开销随之下降，8 进程 74.0s → 23.4s，1 进程 215.7s → 88.6s。
2. `Broker.on_bar` 盯市热循环改用预构造的 (symbol, Position, close 列表) 行表，去掉每根 bar 的 `dict.items()` 与 `data[sym].close` 属性链：热回测 1.112s → 0.933s（提交 abe23a1 信息另记有 cProfile 300k 根的数字「broker.on_bar tottime 0.214s；热回测 0.362s → 0.226s」，profile 输出未入库，仅作参考）；网格 8 进程 23.4s → 21.3s。黄金文件逐字节不变。
3. 数据规模自始至终固定（1,000,000 根；2,000 组 × 20,000 根），没有缩小数据换达标；单核基线在优化前就已达标（2.209s），优化主要兑现在网格搜索上。
4. 基准 JSON 里的 `machine.git_commit` 是基准运行时的 HEAD：基准总是在把本轮改动提交之前运行，所以 02/03 记录的提交号是其父提交（被测代码在下一个提交里）。04 是验收修复后的重跑。

## 4. 突变测试

### 4.1 本机复测（审核 2026-10-22）

`qbt/cli.py`、`qbt/data.py`、`qbt/engine.py`、`qbt/metrics.py`、`qbt/optimizer.py` 在审核中改过源码（裸异常修复），判据集加入 `test_contract` / `test_boundaries` / `test_determinism`，因此 8 个模块 × 80 个突变体按同一 seed=0 **全部重抽重跑**（`mutants/results_audit.json`，fail-fast 顺序同历史，`--jobs 3`，机器见 §3；源码改过的模块候选集已变，突变 id 与历史不可逐个对应；`broker` / `indicators` / `report` 源码未变、id 可比）。对 9 个非等价漏网补判据后按 id 定向重跑（`--ids`，结果合并进同一 JSON）。

| 模块 | 历史（原测试集）：总 / 抓住 / 漏网 / 抓住率 | 本机复测：总 / 抓住 / 漏网 / 抓住率 |
|---|---|---|
| `qbt/broker.py` | 80 / 73 / 7 / 91.2% | 80 / 80 / 0 / **100.0%** |
| `qbt/cli.py` | 80 / 76 / 4 / 95.0% | 80 / 75 / 5 / **93.8%** |
| `qbt/data.py` | 80 / 77 / 3 / 96.2% | 80 / 80 / 0 / **100.0%** |
| `qbt/engine.py` | 80 / 77 / 3 / 96.2% | 80 / 79 / 1 / **98.8%** |
| `qbt/indicators.py` | 80 / 78 / 2 / 97.5% | 80 / 78 / 2 / **97.5%** |
| `qbt/metrics.py` | 80 / 73 / 7 / 91.2% | 80 / 72 / 8 / **90.0%** |
| `qbt/optimizer.py` | 80 / 65 / 15 / 81.2% | 80 / 67 / 13 / **83.8%** |
| `qbt/report.py` | 80 / 77 / 3 / 96.2% | 80 / 77 / 3 / **96.2%** |
| **合计** | 640 / 596 / 44 / 93.1% | 640 / 608 / 32 / **95.0%** |

历史漏网中本机被新判据抓住（源码未变的模块，id 可比）：`broker:1461:compare`、`broker:1969:compare`、`broker:2086:compare`、`broker:2594:compare`、`broker:2685:const:1`、`broker:850:binop`、`broker:995:const:1.0`。

本机漏网清单（每条给出“为什么等价”或“补了哪条判据”；标「未补判据」的是 CLI 默认值类、如实列出）：

| id | 行 | 变异 | 结论 |
|---|---|---|---|
| `cli:798:const:0` | 50 | `1` → `0` | 等价（格式）：demo 的 summary.json indent 只改空白 |
| `cli:798:const:2` | 50 | `1` → `2` | 等价（格式）：同上 |
| `cli:1038:const:0` | 80 | `1` → `0` | 等价（输出）：TextTestRunner 的 verbosity 只改 stderr 文本量 |
| `cli:1038:const:2` | 80 | `1` → `2` | 等价（输出）：同上 |
| `cli:881:const:True` | 101 | `False` → `True` | 等价：委托子命令的 add_help 只影响 -h 透传 |
| `engine:1503:const:-1` | 281 | `0` → `-1` | 等价：单策略快路径 on_bars 只有一个元素，[0] 与 [-1] 相同 |
| `indicators:1138:const:0` | 55 | `1` → `0` | 等价：ema 循环从 0 开始时第 0 步 prev + a*(x0 − prev) == x0，输出不变 |
| `indicators:329:delete_stmt` | 567 | `_check_window(n)` → `pass` | 等价（冗余防御）：rolling_max_drawdown 内随后的 rolling_max(dd, n) 仍校验窗口 |
| `metrics:1595:binop` | 65 | `x - m` → `x + m` | 等价（代数恒等式）：Σ(x+m)(x−m) = Σx² − n·m² = Σ(x−m)²，只差浮点舍入（≤ 1e-16 相对） |
| `metrics:1597:binop` | 65 | `x - m` → `x + m` | 等价（代数恒等式）：同上 |
| `metrics:1357:const:True` | 114 | `False` → `True` | 等价：start 在每个新高都重置，连续新高时 i − start = 1 ≤ 已有 longest，under 标志在首个回撤后不再影响结果 |
| `metrics:947:delete_stmt` | 114 | `under = False` → `pass` | 等价：同上（under 复位冗余） |
| `metrics:594:const:-1` | 150 | `0` → `-1` | 等价：alpha 已校验 ∈ (0,1]，ceil(alpha·n) − 1 ≥ 0，max 的下界不起作用 |
| `metrics:1433:const:-1` | 159 | `0` → `-1` | 等价：Python 的 // 已是向下取整，z<0 分支的 −146096 补偿只在 C 截断除法下有意义；z = −1 两种写法同值 |
| `metrics:1444:const:146095` | 161 | `146096` → `146095` | 等价：doe // 146095 与 doe // 146096 只在 doe = 146095（纪元倒数第二天，2000-02-28）不同，此时分子 145998 与 145997 整除 365 都得 399；审核已把 1600 / 2000 / 2400 年 2 月 27–29 日加入 test_civil_from_days_matches_datetime，仍然绿，证实等价 |
| `metrics:1180:compare` | 226 | `len(rets) > 1` → `len(rets) >= 1` | 等价：len(rets) == 1 时 _std(rets, 1) 因 n − ddof ≤ 0 返回 0.0，与 0.0 相同 |
| `optimizer:1320:const:1` | 81 | `0` → `1` | 等价：make_task 里的哑输入只用来取 metrics.compute 的键集合，数值无关 |
| `optimizer:869:const:61` | 81 | `60` → `61` | 等价：同上 |
| `optimizer:96:delete_stmt` | 109 | `load_data(data_spec)` → `pass` | 等价：_worker_init 只是预加载，evaluate 内 load_data 惰性加载同一数据 |
| `optimizer:285:delete_stmt` | 140 | `self.conn.commit()` → `pass` | 等价：sqlite3 在默认 isolation_level 下 CREATE TABLE 不开启事务，commit 为空操作 |
| `optimizer:307:const:2` | 164 | `1` → `2` | 等价（判据本身）：workers 取值不改变结果（并行确定性判据），默认值不可观察 |
| `optimizer:120:delete_stmt` | 166 | `combos = dedupe(combos)` → `pass` | 等价（输出）：结果按 param_key 去重存入 dict，重复组合只多算一遍，输出相同 |
| `optimizer:321:compare` | 170 | `workers <= 1` → `workers < 1` | 等价（输出）：workers == 1 改走进程池路径，结果相同，只多一个子进程 |
| `optimizer:2178:const:59` | 184 | `60` → `59` | 等价（日志）：进度行 key 截断长度 |
| `optimizer:372:delete_stmt` | 223 | `_worker_init(task['data_spec'])` → `pass` | 等价：同 optimizer:96（样本外评估前的预加载） |
| `optimizer:1144:const:4999` | 239 | `5000` → `4999` | 未补判据：optimizer 独立 CLI 的 --n 默认值，所有判据与 qbt.cli 都显式传参，不影响任何结果语义 |
| `optimizer:1148:const:0` | 240 | `1` → `0` | 未补判据：同上（--seed 默认值） |
| `optimizer:1152:const:2` | 241 | `1` → `2` | 未补判据：同上（--workers 默认值；取值不改变结果） |
| `optimizer:1620:const:49` | 259 | `50` → `49` | 等价（日志）：进度打印间隔 |
| `report:1465:const:True` | 47 | `False` → `True` | 等价（JSON 语义）：策略参数行 ensure_ascii 只影响非 ASCII 转义，参数均为 ASCII |
| `report:501:const:1.5` | 93 | `1.0` → `1.5` | 等价：max(dd) or 1.0 只在无回撤时生效，此时所有回撤为 0，分母取值不可观察 |
| `report:1206:const:True` | 134 | `False` → `True` | 等价（JSON 语义）：ensure_ascii 只改变非 ASCII 字符的转义形式，json.load 读回相同 |

### 4.2 历史运行（2026-10-21，原测试集；下文表格与漏网清单是当时的结果，保留不改）

6. **验收评审后（第六、七轮，定向）**：独立验收评审（7 个维度 + 对抗复核，见第 7.0 节）确认的缺陷修复后，broker / indicators / metrics 三个改过源码的模块全量重抽样重跑并合并，`cli:301` 两个此前标为“不可观察”的 id 用 `unittest.mock` 判据定向重跑（全部抓住）；随后删掉 `_margin_ok` 里因修复而变死的 `reduce_only` 分支并补 4 条判据，broker 再次全量重跑合并；最后对补了判据但尚未重跑的 5 个 id 定向重跑（全部抓住）。下表是合并后的最终结果（`mutants/results.json`，`mutants/history/` 只保存第 1–4 轮与最终合并 run8（= results.json），第 5–7 轮定向重跑的原始 JSON 未保留）。

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

总计 100000 次（seed=0，4 类目标轮转），未捕获异常 0 次，耗时 8.72s；复现：`python3.12 fuzz/fuzz.py --iterations 100000 --seed 0 --workers 2`（结果在 `fuzz/results.json`；「验收后另用 seed=5 跑 30,000 次 0 崩溃」仅见于提交 d2481ca 的信息，结果未入库，不作为依据）。

**本机复测（审核 2026-10-22，`fuzz/results_audit.json`）**：同 seed 0 再跑 100,000 次，未捕获异常 0 次，4.97 s；分类计数与上表只差一处——`engine:ConfigError` 4409 → 4414、`engine:ok` 13418 → 13413：`random_params` 抽到的 5 个 grid 参数（levels ≤ 0 或 spacing ≤ 0）此前被静默接受（levels=0 实际是裸 ZeroDivisionError 被 fuzzer 归入 ok 之外的类别？否——它们此前跑完了，因为 `_place_grid` 只在重铺网格时除法，1–120 根数据上未必触发），本审核的 `grid.on_start` 校验把它们改判为受控的 ConfigError；其余 6 个类别逐个相同。
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

- **reduce_only 可反手**：减仓单数量不封顶时，部分成交的入场配上全量止损会把多头打成空头（评审时在 `trend_stop` 默认 10% 参与上限的随机运行里多次出现；当时的计数脚本与结果未入库）。修复：成交时数量封顶在持仓、仓位归零后余量撤销（`cancelled/reduce_only`）、无仓或同向直接拒绝；判据 `tests/test_broker.py::TestReduceOnlyAndMargin`、`tests/test_strategies.py`（默认配置下仓位永不为负）。
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
- **黄金文件与跨机确定性**（审核 2026-10-22 改写）：逐字节 / sha256 判据只在同一台机器上成立（已拆成单独判据，不再当跨机判据用）；跨机判据是「整数精确 + 浮点相对容差」（`tests/_compare.py`，黄金 `golden_v2.json` 生成机器写在 `generated_on`）。仍存在的局限：容差判据不是数学保证——若某根 bar 上信号恰好落在相等边界（例如两条均线在最后一位 ulp 上相等），跨机器可能翻转一笔成交，整数字段的精确判据会红并指出首个差异路径，此时需要人工判断是边界翻转还是真缺陷。macOS arm64 上的新判据本审核没有机器复测（见 §1）。
- **覆盖率工具**只统计主进程；进程池内执行的代码靠串行路径覆盖。
- **性能**：8 进程目标在 4 个 CPU 的虚拟机上实测（进程池 8，CPU 4，加速比见 §3）；单核 1M 根冷缓存在历史 4 次单次计时里 1.93–2.47 s 之间波动，审核复测 best-of-3 的样本为 [1.941, 2.058, 2.073] s（机器噪声量级由样本极差给出）；所有计时只对 §3 写明的机器成立，macOS arm64 未测。
- **突变测试**：每模块抽样 80 个（候选总数 60–1,200+），抽样由 seed 决定；全量跑需数小时。等价突变体按条列出，未做自动等价判定。
- 下一步：Numba/NumPy 不在约束内；可做的是把盯市与撮合按"无挂单快路径"进一步内联、指标按需惰性计算、walk-forward 预热、以及用 `multiprocessing.shared_memory` 共享大数据以降低 worker 初始化成本。

### 7.2 审核（2026-10-22）改了什么、没改什么

- 改了：确定性判据（§1.1–1.3）、CCI 对拍容差与放水判据、`cli test --quick` 与退出码、策略 / 成交 / 订单字段契约 `qbt/CONTRACT.md` 及其判据、各入口的边界判据与错误码表、`bench.py` 的计时口径（best-of-N、CPU 型号、负载、实际进程数）、本报告 §1 / §3 / §4 / §7。每条检查的「命令 → 输出 → 结论」见 `REVIEW_QBT_LOB.md`。
- 对抗复核（REVIEW §1.8）后又补了 5 处：长列表比较改为只报首个差异、成交整数字段类型严格、CCI 显式绝对下限 1e-8、哨兵值不参与量级、绝对容差常量逐处注明理由；判据 262 → 263 条。
- 没改：`qdcheck/`（审核员的提交一行未改，只调用；审核期间 origin 上另一会话合入了两个 qdcheck 提交 dce50d9 / 01917c0，与本审核无关）；没有删除任何已有判据来换绿；放松的唯一一处容差（CCI 对拍：相对 1e-8 + 绝对 1e-12 → 相对 5e-7 + 1e-9·量级的绝对下限）附带了放水判据证明容差仍能抓住 1e-6 的错。
