# LOB 工程报告

> 全部数据为合成数据，**不构成任何交易结论**。所有数字来自 `lob/results/*.json`，表格由 `python -m lob.tools.report_tables` / `build_report` 生成。性能数字分「历史·原 VM」与「本机复测」两列；复测列由审核员在空闲 CPU 上重跑得到的 `results/stress_audit_rerun.json` 生成（机器 / Python / 命令 / N / 冷热 / 进程数 / 起跑负载一并记录）；两台 VM 不同，历史列与复测列不可直接比较。

机器（**历史·原 VM**，取自 `results/stress_r*.json` 的 `machine` 字段）：Intel(R) Xeon(R) Processor @ 2.80GHz，os.cpu_count()=4（压测 1 进程 / 1 核；对拍 / 分片回放 4 进程 = 全部核），CPython 3.12.3，Linux-6.18.44-fc-v139-x86_64-with-glibc2.39；流：events_10m.bin（sha256 fc064598e6320a01…，`python3.12 -m lob.cli gen --n 10000000 --symbols 8 --seed 12345 --out lob/data/events_10m.bin`）。本机复测：Intel(R) Xeon(R) Processor @ 2.10GHz，os.cpu_count()=4，CPython 3.12.3，Linux-6.18.44-fc-v140-x86_64-with-glibc2.39，流 sha256 fc064598e6320a01…（与历史相同：True）（机器 / Python / 命令 / N / 冷热缓存见 4.2 的复测表；平台相关性见 7.1）

## 1. 目标达成情况

| 目标 | 实测 | 结论 | 复现命令 |
|---|---|---|---|
| 1 正确：1,000 万事件与参考实现逐笔一致 | 事件 10000000 / 成交 1820774 笔 / 成交量 14206029 lot / 首次分歧：无（4 进程 lockstep = 原 VM 的 os.cpu_count()=4，不变量常开）。耗时 645 s 为历史·原 VM 的单次数字（平台相关）；事件 / 成交 / 成交量 / 分歧为整数结果，平台无关（见 7.1） | **达成** | `LOB_TEST_SCALE=full python3.12 -m unittest lob.tests.test_compare.Compare10MTest` |
| 2a 快：撮合吞吐 ≥ 300k/s（单核） | engine-only 424678/s（best-of-3，三次 333193/424678/372537；热：流文件已在页缓存，每次重新解码前 1000000 条、新建簿）；engine+每事件不变量检查 309381/s（best-of-3，三次 258986/276302/309381，同上）；端到端回放（解码+撮合+检查+落盘）213568/s（单次 N=1，独立子进程，harness 中第一个运行的项目，运行前页缓存状态未记录）。历史·原 VM（Intel(R) Xeon(R) Processor @ 2.80GHz，os.cpu_count()=4，压测用 1 进程；CPython 3.12.3；`python3.12 -m lob.cli stress --stream lob/data/events_10m.bin --out lob/data/stress --label r4_status_chunks`）。本机复测：engine-only 720901/s（best-of-3，三次 637171/718887/720901）；engine+检查器 539614/s（best-of-3，三次 523647/539614/478932）；端到端 328583/s（单次）（Intel(R) Xeon(R) Processor @ 2.10GHz，os.cpu_count()=4，1 进程，CPython 3.12.3，起跑负载 1.07，`python3.12 -m lob.cli stress --stream lob/data/events_10m.bin --out lob/data/stress --label audit_rerun --json lob/results/stress_audit_rerun.json`，同一份流 sha256 fc064598…） | **历史：引擎 达成；引擎+检查器 临界（三次 258986–309381）；端到端 未达成；本机：引擎 达成；引擎+检查器 达成（三次 478932–539614）；端到端 达成** | `python3.12 -m lob.cli stress --stream lob/data/events_10m.bin --out lob/data/stress --label rX` |
| 2b 快：Top-10 快照 ≤ 2 µs（amortized） | 每事件后调用 1.95 µs（单次，500000 事件有/无快照两遍差分）；缓存命中 93 ns；强制重建 1.94 µs（各 20 万次重复调用取均值，单次；热）。历史·原 VM（Intel(R) Xeon(R) Processor @ 2.80GHz，os.cpu_count()=4，压测用 1 进程；CPython 3.12.3；`python3.12 -m lob.cli stress --stream lob/data/events_10m.bin --out lob/data/stress --label r4_status_chunks`）。本机复测：每事件后调用 0.91 µs（单次） | **历史：达成；本机：达成** | `同上` |
| 2c 内存：1,000 万回放峰值 ≤ 1.5 GB | ru_maxrss 46 MB（端到端独立子进程，单次）；tracemalloc 峰值 12.5 MB（1000000 事件，单次）。历史·原 VM（Intel(R) Xeon(R) Processor @ 2.80GHz，os.cpu_count()=4，压测用 1 进程；CPython 3.12.3；`python3.12 -m lob.cli stress --stream lob/data/events_10m.bin --out lob/data/stress --label r4_status_chunks`）。本机复测：ru_maxrss 46 MB；tracemalloc 峰值 12.5 MB | **历史：达成；本机：达成** | `同上` |
| 3 真实：排队模型 Brier ≤ 0.08 | 整体（前 5 档均匀取样的测试集）Brier T=1/2/5/10 = 0.17264/0.1771/0.1472/0.11699，均 > 0.08（常数预测 0.24718/0.22312/0.16644/0.12674）。达成的子集（`by_d` 中 ≤ 0.08 的格，共 5 格）：d=0 T=1s 0.07514；d=0 T=2s 0.06442；d=0 T=5s 0.05039；d=0 T=10s 0.04108；d=1 T=10s 0.0732。未达成的格（共 11 格）：d=1 T=1s 0.18011；d=1 T=2s 0.13843；d=1 T=5s 0.09394；d=2 T=1s 0.23085；d=2 T=2s 0.21238；d=2 T=5s 0.15901；d=2 T=10s 0.12227；d≥3 T=1s 0.18917；d≥3 T=2s 0.23615；d≥3 T=5s 0.21755；d≥3 T=10s 0.17547（原因见第 5 节） | **未达成** | `python3.12 -m lob.cli queue-collect … && python3.12 -m lob.cli queue-model …` |
| 4a 硬：语句覆盖率 ≥ 90% | 核心模块 97.40%（384 个测试） | **达成** | `python3.12 -m lob.cli coverage --json lob/results/coverage.json` |
| 4b 硬：突变抓住率 ≥ 85%（分母 = 抽样的 120 个突变体） | book.py 110/120 = 91.7%；queue_model.py 115/120 = 95.8%（候选 484 / 292，seed=1 分层抽样） | **达成** | `python3.12 -m lob.cli mutate --target book|queue_model --max 120 --seed 1` |
| 4c 硬：模糊 20 万次零崩溃零不变量违背 | 400000 次（2 轮），崩溃 0 / 违背 0 / 分歧 0 | **达成** | `python3.12 -m lob.cli fuzz --n 200000 --json lob/results/fuzz_200k.json` |
| 5 诚实：数字来自真实运行、附复现命令 | 本报告全部表格由 tools/report_tables.py / build_report.py 从 results/*.json 生成；性能数字分「历史·原 VM」与「本机复测」两列，复测列由 results/stress_audit_rerun.json 生成 | **达成** | `python3.12 -m lob.tools.build_report` |

## 2. 订单簿数据结构选型与备选方案实测

### 2.1 选型

| 组件 | 选择 | 理由 |
|---|---|---|
| 价格档索引（每侧） | 有序 Python list，**最优价放在尾部**（买侧按价格升序、卖侧按「负价格」升序） | 读最优价 / 弹出最优档为 O(1) 且是 C 级操作；插入是 `bisect.insort`（二分 + memmove），几百个档位时 ≈ 0.2 µs；禁止每次排序 |
| 档内队列 | `[head, dead, o1, o2, …]` 的 list + 头指针；撤单置 `None`（惰性删除），`dead`/`head` 超阈值时压实 | 撤单 O(1)（订单自带 slot 下标），队首消费 O(1)，压实摊还 O(1)；避免 deque 的中间删除 O(n) |
| 档量 | 与队列分离的 dict（按排序键） | 快照只需 10 次 dict 取值；每次成交/撤单只改一个整数 |
| 订单 | 9 元素 list（无类、无 `__slots__` 对象） | 创建 ≈ 0.1 µs，字段按常量下标访问；避免属性查找 |
| 成交 | 10 元组追加到共享 list，按块刷盘 | 不在热路径打包 |
| 快照 | 脏标记缓存；价格切片与数量分开缓存（只在档位增删时重建价格切片） | 「amortized ≤ 2 µs」的来源 |

### 2.2 备选方案实测（`python -m lob.cli bench`，`tools/bench_structures.py`）

四种价格档索引在同一工作负载（插入 40% / 删除 40% / 弹出最优 10% / 读最优 10%，seed=5，30 万次操作）下的每操作耗时：

| 档数 | SortedList | LazyHeap | SkipList | DictScan |
|---|---|---|---|---|
| 50 | 170 | 386 | 1964 | 175 |
| 200 | 263 | 378 | 1472 | 182 |
| 1000 | 198 | 310 | 1500 | - |

(ns / 操作，insert 40% / remove 40% / pop_best 10% / best 10%)

结论：纯 Python 跳表每个指针跳转都是解释器操作，比有序数组慢 7–8 倍；堆 + 惰性删除慢 1.7 倍且读最优价需要清理堆顶；`dict + max()` 在 ≤ 200 档时与有序数组相当，但每次读最优都是 O(n)，且真实订单簿每个事件至少读一次最优价。有序数组胜出，并在真实回放中由 r0→r4 的压测复核。

## 3. 撮合语义摘要

详见 `lob/SPEC.md`。要点：价格优先、时间优先（簿内全局 `prio` 计数器）；冰山露出后排到该档队尾并获得新 `prio`；改量减量保位、增量排尾；自成交 `cancel_new`（撤新单剩余并停止撮合）/ `cancel_old`（撤旧单继续），按 symbol 奇偶交替配置以便两种策略都在每次对拍中覆盖；FOK 先做**纯模拟**再撮合；市价单只吃 `ref ± band` 内档位（`ref` = 最近成交价）；守恒式 `submitted = filled(两腿) + resting + cancelled + rejected` 每事件校验。

## 4. 对拍与压测

### 4.1 对拍（lockstep：状态码 + 逐笔成交 + 周期档位/计数器/订单集合，不变量常开）

| 规模 | 事件 | 成交笔数 | 成交量 (lot) | 首次分歧 | 全扫描次数 | 秒 |
|---|---|---|---|---|---|---|
| 1 万 | 10000 | 2338 | 16970 | 无 | 10004 | 5 |
| 100 万 | 1000000 | 160378 | 1240035 | 无 | 1005 | 55 |
| 1,000 万 | 10000000 | 1820774 | 14206029 | 无 | 5006 | 645 |

（「秒」列为平台相关的单次耗时，取自生成该 JSON 的机器——compare JSON 没有 machine 字段，机器见提交记录；其余列为整数结果，给定同一份流文件时平台无关，见 7.1。）

### 4.2 压测：优化轮次（每轮前后 JSON 均已入库）

**数字出处**：本节与第 1 节的全部吞吐 / 延迟 / 内存数字都是**历史数字（原 VM）**：机器、Python 版本见本报告开头的「机器」行（`results/stress_r*.json` 的 `machine` 字段：Intel Xeon @ 2.80GHz，os.cpu_count()=4，CPython 3.12.3，Linux x86_64）；压测始终是 **1 个进程**（对拍 / 分片回放才用 4 进程）。每个数字的命令、best-of-N 与冷热缓存如下（N 与顺序来自 `stress.py` 的 `run()`，`repeats=3`）：

| 项目 | 命令 | N（best-of） | 冷 / 热缓存 | 进程数 / os.cpu_count() |
|---|---|---|---|---|
| engine-only、engine+checker | `python3.12 -m lob.cli stress --stream lob/data/events_10m.bin --out lob/data/stress --label rX` | best-of-3（`stress.run(repeats=3)`；三次原始值在 JSON 的 `runs`，报告取最大值） | 热：三次都在端到端回放之后，流文件已在页缓存；每次重新解码前 100 万条并新建簿 | 1 / 4 |
| 端到端回放 | 同上（harness 内部启动 `python3.12 -m lob.cli replay --stream … --check 10000` 子进程） | 单次（N=1） | harness 中第一个运行的项目；运行前页缓存状态 JSON 未记录（流 560 MB，若生成后立即压测则为热） | 1 / 4 |
| Top-10 快照 | 同上 | 单次：50 万事件有 / 无快照两遍差分；缓存命中与强制重建各 20 万次重复调用取均值 | 热（engine 测量之后） | 1 / 4 |
| 延迟分位 | 同上 | 单次：1,000 万事件逐条 `perf_counter_ns` | 热 | 1 / 4 |
| RSS / tracemalloc | 同上 | 单次：RSS = 端到端子进程 `ru_maxrss`；tracemalloc 另跑 100 万事件 | — | 1 / 4 |

**本机复测**（审核员，Intel(R) Xeon(R) Processor @ 2.10GHz，os.cpu_count()=4，CPython 3.12.3，Linux-6.18.44-fc-v140-x86_64-with-glibc2.39，起跑时 1 分钟负载 1.07，命令 `python3.12 -m lob.cli stress --stream lob/data/events_10m.bin --out lob/data/stress --label audit_rerun --json lob/results/stress_audit_rerun.json`，同一份流 sha256 fc064598e6320a01…，压测 1 进程）：engine-only best-of-3 = **720,901 ev/s**（样本 637171 / 718887 / 720901），engine+checker best-of-3 = **539,614 ev/s**（样本 523647 / 539614 / 478932），端到端 **328,583 ev/s**（单次，RSS 46 MB），Top-10 快照 0.91 µs，延迟 p50 / p99 / p999 = 1.2 / 7.5 / 33.7 µs，tracemalloc 峰值 12.5 MB。下方「历史数字与本机复测」表两列并排；两台 VM 不同（历史 Intel(R) Xeon(R) Processor @ 2.80GHz vs 本机 Intel(R) Xeon(R) Processor @ 2.10GHz），数字不可直接比较，只用于说明本机是否达标：纯引擎 ≥ 30 万/秒，端到端 ≥ 30 万/秒。

**测量方法**（`stress.py`，每轮前后 JSON 均在 `results/stress_r*.json`）：

- *engine-only*：100 万条预解码记录，只计 `Book.process`（无检查器、无 I/O），best-of-3；
- *engine+checker*：同上，加每事件 O(1) 不变量检查（`Checker.after_event`，全扫描关闭），best-of-3；
- *端到端*：独立子进程对 1,000 万条做单进程回放（解码 + 撮合 + 每事件 O(1) 检查 + 每 1 万事件全扫描 + 状态流 / 成交流 / 每秒快照落盘），`ru_maxrss` 取自该子进程，单次；
- *快照*：每个事件后调用一次 `top(10)`（最坏情形：每次都脏）的增量成本 = (有快照总时长 − 无快照总时长)/事件数；另给缓存命中与强制重建成本；
- *延迟*：对 1,000 万事件逐条 `perf_counter_ns` 包裹 `Book.process`，nearest-rank 分位；
- *harness v1 → v2*：v2 在预解码输入上 `gc.freeze()`，消除循环 GC 反复遍历 100 万元组对引擎计时的污染（r3 在 v1/v2 下 engine+checker 从 252k 变为 302k，说明 v1 的该项数字被 GC 压低）。v1 行之间可比、v2 行之间可比；端到端与延迟两列不受此影响。
- 噪声：同一代码 best-of-3 的三次结果相差可达 10–20%（云 VM，`runs` 字段保留了全部三次），因此 ±5% 内的差异不作为结论。

**各轮改动**：

| 轮次 | 改动 | 效果 |
|---|---|---|
| r0 | 基线（阶段 2 的簿 + 阶段 3 的回放） | — |
| r1 | 卖侧两个字典改按排序键（热路径不再取负）；快照价格切片单独缓存（只在档位增删时重建）；校验链内联进 `_new` | 快照 3.68 → 1.48 µs；e2e +6% |
| r2 | 检查器直接读键数组（不经方法）；回放按块迭代去掉每事件取模 | 噪声内无变化 |
| r3 | `process` 一次元组解包；价格带上下界在 ref 变化时预计算；撤单路径内联 `_unlink`；检查器按状态码跳过无成交事件的成交扫描 | e2e +5%；engine-only 最佳 410k |
| r4 | 无过滤单进程快路径按块写状态流（list → bytes 切片）；检查器前态改为属性 | e2e 213.6k；engine+checker 最佳 309k；快照 1.95 µs |

**禁止事项核对**：所有轮次都在同一份 1,000 万流（sha256 `fc064598…`）上测量，检查器始终开启（端到端：每事件 O(1) + 每 1 万事件全扫描；engine+checker：每事件 O(1)），未缩小数据、未放宽阈值。

**v2 下 r1 → r4 的净效果**（同一 harness、同一流、原 VM）：端到端 187,018 → 213,568（+14%），engine+checker best 271,767 → 309,381（+14%），engine-only best 331,048 → 424,678（+28%），快照 1.92 → 1.95 µs（r1 已引入缓存，之后持平）。

**结论（历史·原 VM）**：在该 VM 上，纯引擎吞吐 ≈ 40 万事件/秒（达标），加上每事件不变量检查 ≈ 26–31 万/秒（三次 258,986–309,381，临界），端到端回放 ≈ 21 万/秒（**未达 30 万**）。端到端每事件 ≈ 4.7 µs 的构成（profile）：撮合 ≈ 2.4 µs、检查器 ≈ 0.7 µs、解码 ≈ 0.35 µs、循环 / 状态流 / 快照边界 ≈ 0.5 µs、成交落盘 ≈ 0.2 µs。进一步的 Python 级微优化收益已低于测量噪声；要跨过 30 万/秒需要 C 扩展（约束排除）。Top-10 快照 1.95 µs（每事件后都调用的最坏情形）与缓存命中 93 ns 达标；内存 46 MB 远低于 1.5 GB。这些结论在其他机器上是否成立，以复测列为准。

**历史数字与本机复测**（历史列由 `results/stress_r4_status_chunks.json` 生成；复测列由 `results/stress_audit_rerun.json` 生成）：

| 项目 | 历史（原 VM，r4_status_chunks） | 本机复测（audit_rerun） |
|---|---|---|
| engine-only ev/s（best-of-N，括号内为全部样本） | 424678 （best-of-3：333193 / 424678 / 372537） | 720901 （best-of-3：637171 / 718887 / 720901） |
| engine+checker ev/s（best-of-N，括号内为全部样本） | 309381 （best-of-3：258986 / 276302 / 309381） | 539614 （best-of-3：523647 / 539614 / 478932） |
| 端到端 ev/s（单次，独立子进程） | 213568 | 328583 |
| Top-10 快照 µs（每事件后调用，单次） | 1.95 | 0.91 |
| 延迟 p50 / p99 / p999 µs（单次，10000000 事件） | 2.0 / 13.5 / 53.7 | 1.2 / 7.5 / 33.7 |
| RSS 峰值 MB（端到端子进程 ru_maxrss，单次） | 46 | 46 |
| tracemalloc 峰值 MB（1000000 事件，单次） | 12.5 | 12.5 |
| 机器 / os.cpu_count() / 进程数 | Intel(R) Xeon(R) Processor @ 2.80GHz / 4 / 1 | Intel(R) Xeon(R) Processor @ 2.10GHz / 4 / 1 |
| Python / 平台 | CPython 3.12.3 / Linux-6.18.44-fc-v139-x86_64-with-glibc2.39 | CPython 3.12.3 / Linux-6.18.44-fc-v140-x86_64-with-glibc2.39 |
| 起跑时 1 分钟负载 | 未记录 | 1.07421875 |
| 命令 | `python3.12 -m lob.cli stress --stream lob/data/events_10m.bin --out lob/data/stress --label r4_status_chunks` | `python3.12 -m lob.cli stress --stream lob/data/events_10m.bin --out lob/data/stress --label audit_rerun --json lob/results/stress_audit_rerun.json` |
| 冷 / 热缓存 | engine、快照、延迟：热（端到端回放之后）；端到端：冷（独立子进程，流文件已在页缓存） | engine、快照、延迟：热（端到端回放之后）；端到端：冷（独立子进程，流文件已在页缓存） |

**各轮压测结果（全部为历史·原 VM）**：

| harness | 轮次 | engine-only ev/s | engine+checker ev/s | 端到端 ev/s | Top-10 快照 µs | p50 µs | p99 µs | p999 µs | RSS 峰值 MB |
|---|---|---|---|---|---|---|---|---|---|
| v1 | r0_baseline | 399111 | 260590 | 186641 | 3.68 | 2.2 | 13.9 | 52.8 | 44 |
| v1 | r1_keys_snapshot_inline | 365185 | 285758 | 197440 | 1.48 | 2.2 | 14.9 | 56.6 | 44 |
| v1 | r2_checker_chunks | 359188 | 285047 | 194775 | 1.53 | 2.2 | 14.6 | 58.9 | 47 |
| v1 | r3_unpack_band_cancel | 409868 | 252735 | 207761 | 2.29 | 2.1 | 14.0 | 56.6 | 46 |
| v2 | r1_v2 | 331048 | 271767 | 187018 | 1.92 | 2.2 | 14.4 | 53.5 | 43 |
| v2 | r3_v2 | 399043 | 302235 | 208125 | 2.36 | 2.1 | 14.2 | 56.8 | 51 |
| v2 | r4_status_chunks | 424678 | 309381 | 213568 | 1.95 | 2.0 | 13.5 | 53.7 | 46 |

## 5. 排队成交模型（方法部分）

**问题**：给定我方挂单在某价位的队列位置（前方可见量 `ahead`）、该档总量、档距触价 `d`（及 tick 距离 `dist`）、到达 / 撤单 / 成交强度估计、价差，输出 T 秒内首次成交概率，并给出条件成交时间估计。

**真值**（`queue_collect.py`，`python -m lob.cli queue-collect`）：**影子单（虚拟单）**。在 1,000 万事件回放中，每个 symbol 每 50 个事件放一张我方虚拟单：随机一侧、随机前 5 档之一（d = 0..4）、随机排在该档 j 张存活单之后（j = 0..n，j = n 即「现在下单排队尾」）。虚拟单不改变市场（零冲击假设）、永不撤单，因此**没有撤单删失，只有流结束删失**。跟踪规则逐笔精确：我方价位上吃掉前方单的成交消耗 `ahead`；吃到排在我方之后的单、或扫穿到比我方更差的价位、或对侧新单的限价越过我方价且仍有剩余，都视为我方成交；前方单的撤单 / 减量减少 `ahead`，增量移到我方之后；前方单因自成交而消失在事件后检出。（第一版用回放中的真实挂单作探针：生成器的随机撤单删失了 54% 样本、剩余样本成交率 92%、常数预测即达 Brier 0.078，该版本已废弃，见第 6 节 bug 6。）

**特征**：`d`（档位序号，0 = 触价档）与 `dist`（距触价的 tick 数），`ahead`（队列前方可见 lot），`level_total`，`rate_trd` / `rate_trd_opp`（滑窗 2 s 内本侧 / 对侧成交 lot/s），`rate_arr` / `rate_cxl`（同侧挂入 / 撤单 lot/s），`spread`，regime 标签（来自生成器 sidecar，仅用于分段评估，不作输入）。

**校准方法（选择：分桶经验概率）**：桶键 = (`min(dist, 8)`, `u` 桶, 成交失衡桶, 价差桶)，其中 `u = ahead / (rate_trd·T + 1)` 是「前方量 / 期望在 T 内成交的量」的归一化队列位置，`u` 桶边界 0.1/0.2/0.35/0.5/0.75/1/1.5/2/3/5/8/16（13 桶）；成交失衡 `(rate_trd − rate_trd_opp)/(rate_trd + rate_trd_opp + 1)` 按 ±0.3 分 3 桶；价差按 ≤ 2 / ≤ 5 / > 5 tick 分 3 桶；每桶概率 = `(k + α·g)/(n + α)`（α = 2，g = 全局成交率，拉普拉斯平滑）；成交时间估计 = 桶内已成交探针首次成交时间的中位数。特征集与桶边界在训练集内部的验证切分上选择，未碰测试集（`queue_model.py` 模块说明；以上常量与 `results/queue_model/queue_model_eval.json` 的 `u_edges` / `dist_max` / `alpha` 一致）。选择理由：无分布假设、单调性由数据决定、每个数字可直接核对；对照组为最大似然拟合的参数模型 `p = 1 − exp(−e^{a_d}·(rate_trd·T + b_d)/(ahead+1))`（按 `min(d,3)` 分别网格搜索 (a, b)）。

**评估协议**：按时间切分，前 70%（按探针时刻）训练、后 30% 测试；Brier = 测试集上 `mean((p − y)²)`；同时给出常数预测（全局成交率）的 Brier 作为底线、训练集 Brier 作为过拟合参照、按 regime 与按 d 的分段 Brier，以及 10 桶可靠性图（SVG，`results/queue_model/reliability_h*.svg`）。

### 5.x 结果

| T (s) | 测试样本 | 成交率 | Brier（分桶） | Brier（常数） | Brier（参数） | Brier（训练） | 按 regime |
|---|---|---|---|---|---|---|---|
| 1 | 57694 | 0.553 | 0.1726 | 0.2472 | 0.2539 | 0.1655 | normal 0.1752 (n=22347) / shock 0.1942 (n=5125) / withdraw 0.1670 (n=30222) |
| 2 | 57598 | 0.664 | 0.1771 | 0.2231 | 0.2760 | 0.1673 | normal 0.1840 (n=22306) / shock 0.1968 (n=5118) / withdraw 0.1687 (n=30174) |
| 5 | 57302 | 0.789 | 0.1472 | 0.1664 | 0.2599 | 0.1389 | normal 0.1527 (n=22213) / shock 0.1943 (n=5022) / withdraw 0.1353 (n=30067) |
| 10 | 56978 | 0.851 | 0.1170 | 0.1267 | 0.1749 | 0.1099 | normal 0.1230 (n=22074) / shock 0.1798 (n=4953) / withdraw 0.1022 (n=29951) |

**真值规模**：1,000 万事件流上 192,918 个影子单探针（每 symbol 每 50 个事件一个；8 个 symbol），按时间前 70% 训练（135,042）、后 30% 测试（57,876）；流结束删失极少（T=10 s 时 898 个，占测试集 1.6%）。

**读法**：`Brier（常数）` 是用训练集全局成交率预测所有样本的底线；`Brier（训练）` 用于判断过拟合（与测试集差距 ≤ 0.01，无明显过拟合）；参数模型是最大似然拟合的指数形式，在该数据上劣于分桶模型（函数形式不对：成交概率对 `ahead/(μT)` 应是 S 形而非 1−e^{−x}），仅作对照。

**按档距分段（测试集）**：

| T (s) | d=0（触价档） | d=1 | d=2 | d≥3 |
|---|---|---|---|---|
| 1 | 0.07514 | 0.18011 | 0.23085 | 0.18917 |
| 2 | 0.06442 | 0.13843 | 0.21238 | 0.23615 |
| 5 | 0.05039 | 0.09394 | 0.15901 | 0.21755 |
| 10 | 0.04108 | 0.0732 | 0.12227 | 0.17547 |

（每档约 11.5k 个测试样本，d≥3 约 23k；数字按 `results/queue_model/queue_model_eval.json` 的 `by_d` 原样抄录，JSON 已舍入到 5 位小数。）

**结论（目标 3「排队模型 Brier ≤ 0.08」）**：**未达成**。目标的度量对象是「前 5 档均匀取样」的整体测试集；整体 Brier T=1/2/5/10 = 0.17264/0.1771/0.1472/0.11699（`results/queue_model_summary.json`，原样抄录），四个期限全部 > 0.08。

达成的子集（上表中 ≤ 0.08 的格，共 5 格）：d=0（触价档）T=1 s 0.07514、T=2 s 0.06442、T=5 s 0.05039、T=10 s 0.04108；d=1 T=10 s 0.0732。

未达成的格（共 11 格）：d=1 T=1/2/5 s（0.18011/0.13843/0.09394）；d=2 全部 T（0.23085/0.21238/0.15901/0.12227）；d≥3 全部 T（0.18917/0.23615/0.21755/0.17547）。

未达成的原因（只用本节已有材料，未做新实验）：

1. **特征只看得到 t0 时刻的队列位置与强度**：d≥1 的挂单要先等价格走到自己的档位，这取决于合成流随机游走（漂移 + regime 切换）的未来路径，`ahead` / `rate_*` / `spread` 对此几乎无预测力——上表中 d 每增加一档，Brier 上升约 0.05–0.10；而 d=0 不需要等价格，只需等前方量被吃掉，强度特征直接可用。（原报告另称「常数预测在 d≥1 子集上 Brier 也 > 0.2」「8 组特征 / 分桶变体只把 T=1 整体 Brier 从 0.183 降到 0.173」：这两个数字来自阶段 5 的作者记录，`results/` 中没有对应 JSON（探针 CSV 未入库），本次审核未能复核，不作为依据。）
2. **shock regime**：t0 处于 shock 段的样本 Brier 比 normal 段差 0.02–0.06（下表），突发大单一次扫穿多档、价格跳变，t0 的队列位置与强度估计很快失效。
3. **删失不是原因**：影子单永不撤单，只有流结束删失（T=10 s 时 898 个，1.6%），对 Brier 的影响可忽略；第一版真实挂单探针的 54% 撤单删失正是被弃用的原因。
4. **合成数据**：事件流由带 regime 的随机过程生成，不含真实市场的自相关与隐藏流动性，本节数字只描述这份合成流，不能外推。

我们没有通过改变探针分布（例如只采触价档）来「制造」达标数字；表中的分段数字即是对不同使用场景的回答。

**失效场景（按 t0 所在 regime，测试集 Brier，按 `queue_model_summary.json` 的 `by_regime` 原样抄录）**：

| T (s) | normal | withdraw（流动性抽走） | shock（突发大单/急行情） |
|---|---|---|---|
| 1 | 0.17525 | 0.16704 | 0.19423 |
| 5 | 0.15268 | 0.13528 | 0.19431 |
| 10 | 0.12296 | 0.10222 | 0.17976 |

shock 段比 normal 段差 0.02–0.06：突发大单把多档一次扫穿、价格跳变，t0 的队列位置与强度估计很快失效；withdraw 段反而略好——撤单潮使前方量快速减少、成交率更高且更可预测（withdraw 段 T=5 s 成交率 0.81 对 normal 0.79、shock 0.65）。可靠性图：`results/queue_model/reliability_h{1,2,5,10}.svg`（预测概率 10 等宽桶的观测频率；点大小 ∝ 样本数）。

**成交时间估计**：`QueueModel.time_to_fill(...)` 返回所在桶内已成交探针首次成交时间的中位数（秒），无样本时返回 None；它只在该桶成交概率较高时有意义，报告不把它作为达标项。

## 6. 突变与模糊（方法部分）

**突变器**（`mutants/run_mutations.py`，基于 `ast`）：比较运算互换（< ↔ <=，> ↔ >=，== ↔ !=）、算术互换（+ ↔ −，* ↔ //，% → //）、增量赋值互换（+= ↔ −=）、整数常量 +1、布尔翻转、and ↔ or、条件取反、break ↔ continue、去掉一元负号、删除语句（赋值 / 增量赋值 / 调用表达式）。只对函数体内的位点生成；每个文件取 seed 固定的分层样本（每种算子等额）至多 120 个。判定：子进程跑快速测试集（`LOB_TEST_SCALE=quick`：手写用例、检查器自检、1 万对拍、2 万模糊、10 万回放、簿内部测试），失败 / 异常 / 超时 = 抓住。

**模糊器**（`fuzz/fuzz.py`）：30% 畸形事件（负价 / 零价 / int64 极值价、零量 / 负量 / 超大量 / 非 lot 整数倍、tick 不对齐、带外价格、撤销不存在或已死的单、改成负量、重复 id、非法 side / kind / 标志组合、时间戳回退 / 极值、垃圾字段），70% 合法事件（含全部订单类型、6 个 trader 以触发自成交）；每事件 O(1) 不变量 + 每 100 事件全扫描，并与参考实现 lockstep 比对状态码与成交。

### 6.x 结果（抓住率分母 = 抽样运行的突变体数）

| 运行 | 文件 | 候选位点 | 抽样运行（= 抓住率分母） | 抓住 | 漏网 | 超时（计入抓住） | 抓住率 = 抓住 / 抽样 |
|---|---|---|---|---|---|---|---|
| 历史（原 VM） | lob/book.py | 484 | 120 | 110 | 10 | 15 | 91.7% |
| 历史（原 VM） | lob/queue_model.py | 292 | 120 | 115 | 5 | 0 | 95.8% |

模块 → 候选位点 → 抽样 → 第一轮 / 复跑 / 最终（复跑只重跑漏网者，已抓住的保留）：

| 模块 | 候选位点 | 抽样运行（分母） | 第一轮 抓住 / 漏网 / 超时 | 第一次复跑后漏网 | 最终 抓住 / 漏网 / 超时 | 最终抓住率 |
|---|---|---|---|---|---|---|
| lob/book.py | 484 | 120 | 100 / 20 / 15 | 13 | 110 / 10 / 15 | 110/120 = 91.7% |
| lob/queue_model.py | 292 | 120 | 79 / 41 / 0 | 7 | 115 / 5 / 0 | 115/120 = 95.8% |

按算子的抽样分布（每格：抽样运行 / 第一轮抓住 → 最终抓住）：

| 算子 | lob/book.py | lob/queue_model.py |
|---|---|---|
| arith | 13 / 10 → 11 | 25 / 15 → 25 |
| aug | 12 / 12 → 12 | 5 / 5 → 5 |
| bool | 12 / 8 → 10 | 3 / 0 → 3 |
| boolop | 12 / 11 → 12 | 3 / 3 → 3 |
| brkcont | 6 / 3 → 3 | 2 / 2 → 2 |
| cmp | 12 / 9 → 11 | 13 / 8 → 11 |
| const | 13 / 9 → 12 | 30 / 11 → 27 |
| delete | 15 / 14 → 14 | 23 / 22 → 23 |
| negate | 13 / 12 → 13 | 12 / 10 → 12 |
| usub | 12 / 12 → 12 | 4 / 3 → 4 |
| **合计** | **120 / 100 → 110** | **120 / 79 → 115** |

漏网清单（逐条判定）：

| 运行 | 文件 | id | 突变 | 行 | 代码 | 判定 | 说明 |
|---|---|---|---|---|---|---|---|
| 历史 | book | 169 | bool -> True | 219 | `stopped = False` | 等价 | _match 中 stopped 初值：_match 只在价格交叉时被调用，循环体至少执行一次并覆盖该变量 |
| 历史 | book | 210 | delete statement | 337 | `self.keys_dirty = True` | 等价 | _unlink 删档后漏设 keys_dirty：_unlink 现仅被增量改单调用，随后 _rest 以同价位重建档并置 keys_dirty，价格切片不变 |
| 历史 | book | 263 | const 0 -> 1 | 191 | `o = [oid, side, price, rem, vis, disp, trader, self.prio, 0]` | 等价 | 订单列表 O_SLOT 占位 0→1：紧接着被 Q_FIRST 或 len(queue) 覆盖 |
| 历史 | book | 276 | break<->continue | 252 | `continue` | 等价 | _match_level 遇 None 槽 continue→break：外层 while 会以更新后的 head 重新进入同一档，效果相同 |
| 历史 | book | 284 | break<->continue | 263 | `continue` | 等价 | cancel_old 撤旧单后 continue→break：同上，外层循环重入同一档 |
| 历史 | book | 297 | break<->continue | 306 | `break` | 等价 | _fillable 中 break→continue：后续档位同样不交叉或 avail 已够，只多做无效扫描 |
| 历史 | book | 307 | bool -> False | 337 | `self.keys_dirty = True` | 等价 | 同 book:210（keys_dirty 布尔翻转） |
| 历史 | book | 431 | Sub -> Add | 318 | `fill = want - avail if want - avail < vis else vis` | 等价 | _fillable 中 fill 值 want-avail→want+avail：仅使 avail 超过 want，而调用方只比较 avail<qty，判定不变 |
| 历史 | book | 458 | Lt -> LtE | 281 | `o[O_VIS] = o[O_DISP] if o[O_DISP] < o[O_REM] else o[O_REM]` | 等价 | 冰山露出 disp<rem 改 <=：disp==rem 时两分支取值相同 |
| 历史 | book | 459 | Sub -> Add | 318 | `fill = want - avail if want - avail < vis else vis` | 等价 | _fillable 条件 want-avail<vis→want+avail<vis：只改变 fill 取 vis 的时机，avail 一旦 >= want 即退出，判定不变 |
| 历史 | queue_model | 124 | const 11 -> 12 | 256 | `for t in range(0, 11, 2):` | 等价 | 刻度 range(0,11,2) 改 range(0,12,2)：步长 2 下两者都是 0..10，集合不变（首次误判为可补判据，复跑仍漏网后改判等价） |
| 历史 | queue_model | 169 | const 61 -> 62 | 168 | `for a10 in range(-60, 61, 5):` | 等价 | 粗网格 range(-60,61,5) 改 62：步长 5 下网格点集合不变 |
| 历史 | queue_model | 176 | Gt -> GtE | 176 | `if ll > best_ll:` | 等价 | 细网格 > 改 >=：仅在对数似然恰好相等时取后者，浮点对数似然相等概率为零且 fit 不会对空数据调用 _grid |
| 历史 | queue_model | 188 | const 1 -> 2 | 266 | `nmax = max((b['n'] for b in rel), default=1)` | 等价 | max(..., default=1) 改 default=2：default 只在无桶时使用，此时也不画圆点 |
| 历史 | queue_model | 211 | Gt -> GtE | 171 | `if ll > best_ll:` | 等价 | 粗网格 > 改 >=：同 176 |

**抽样与分母**：两个模块各运行 `python3.12 -m lob.cli mutate --target <book|queue_model> --max 120 --seed 1 --workers 4`（`--seed 1` 是 CLI 默认值）。结果 JSON 没有 `seed` 字段，审核时用 `run_mutations.make_mutants` + `sample_mutants(…, 120, seed=1)` 对 HEAD 源码重新枚举：候选数 484 / 292 与 JSON 一致，抽中的 120 个 id 与 JSON 的 `results` 逐一相同（seed=0 / 2 分别只重合 43 / 44 与 62 / 66 个），即表中样本确由 seed=1 产生。**抓住率的分母始终是抽样运行的 120 个突变体，不是候选总数**（book 484 / queue_model 292）；上面的「模块 → 候选位点 → 抽样 → 结果」表与「按算子」表给出模块、算子与突变体数量的对应关系。

**book.py**：484 个候选位点，按算子分层抽样 120 个（seed=1）。第一轮抓住 100/120 = 83.3%（其中 15 个超时：循环条件被改成死循环，按「抓住」计）。20 条漏网逐条人工判定：10 条等价（见上表「等价」行：初值随后被覆盖、`continue→break` 后外层循环重入同一档、FOK 纯模拟只用布尔结论等），10 条补判据后单独复跑被抓住（已抓住的突变不重跑——只增加了测试，不会让已失败的突变变为存活；第一次复跑后仍漏网的 13 个 id 记录在 `mutations_book.json` 的 `rerun.survivor_ids`，第一轮原始结果保留在 `mutations_book_run1.json`）。补判据时发现的一个真实问题：三条「压实阈值」漏网（id 315 / 398 / 461）落在 `_cancel` 内联的条件上而非 `_unlink` 的同形条件——第一版补的判据打错了位置，复跑仍漏网，随后补了撤单路径的判据才抓住。最终 **110/120 = 91.7%**，剩余 10 条全部为等价突变。

**queue_model.py**：292 个候选位点，抽样 120 个（seed=1）。第一轮抓住 79/120 = 65.8%：漏网集中在可靠性图的绘图常量（坐标、刻度、半径）和参数模型网格搜索的细节，因为当时的判据只检查「SVG 里有圆点和标签」「参数模型优于常数」。41 条漏网逐条判定后补判据：可靠性图几何按公式精确核对（坐标轴、对角线、刻度位置、圆点坐标与半径、折线）、网格搜索结果必须等于独立重算的粗/细网格 argmax、构造最优点落在粗网格点之间的数据检验细网格窗口、失衡阈值 ±0.3 的精确边界、默认价差、桶宽/桶数、base_rate 舍入位数、分桶计数守恒、输出 JSON 规范字节、已存在目录重复运行。第一次复跑后剩 7 个漏网（`rerun.survivor_ids`），第二次复跑后 **115/120 = 95.8%**，剩余 5 条均为等价突变（刻度 range 上界 11→12 与粗网格上界 61→62 在步长下集合不变；`>`→`>=` 只在浮点对数似然恰好相等时不同；`max(default=1)` 的默认值只在无桶时使用且此时不画点）。第一轮原始结果保留在 `mutations_queue_model_run1.json`。

**诚实说明**：抓住率的分母是抽样的 120 个突变而非全部候选（book 484 / queue_model 292）；超时（死循环）计为抓住；「等价」判定为人工阅读所得，逐条理由在上表（id、改动、为什么等价）；复跑只针对漏网者（增加测试不会使已失败的突变存活）；突变运行的秒数（`seconds` 字段）是原 VM 的历史数字。

| 运行 | 事件 | 畸形比例 | 拒绝数 | 崩溃 | 不变量违背 | 与参考分歧 | 全扫描 | 校验成交 |
|---|---|---|---|---|---|---|---|---|
| fuzz_200k.json | 200000 | 0.30 | 63383 | 0 | 0 | 0 | 2001 | 38359 |
| fuzz_200k_bad60.json | 200000 | 0.60 | 92072 | 0 | 0 | 0 | 2001 | 43148 |

两轮各 20 万次（seed 2024 畸形比例 0.3；seed 2025 畸形比例 0.6），每事件 O(1) 不变量 + 每 100 事件全扫描（各 2,001 次）+ 与参考实现 lockstep：**崩溃 0、不变量违背 0、分歧 0**。异常事件的分布见各 JSON 的 `status_counts`（11 种拒绝码全部出现）。

模糊与对拍过程中修过的 bug（均在提交历史中可查）：
1. 惰性删除的 `dead` 计数语义：队首越过 `None` 槽后计数不减，全扫描报「dead count mismatch」→ 改为「列表中 `None` 槽总数」并补队首压实触发条件；
2. 检查器直接读 `bid_keys` 在包裹参考实现时抛 `AttributeError` → 按引擎类型分路；
3. 守恒式中成交量只计一腿导致 `submitted ≠ …` → `filled` 计两腿并写入 SPEC；
4. 回放分片归并需要每事件内的成交序号而非全局序号 → `idx` 改为事件内计数（两套引擎同改）；
5. 子进程测 `ru_maxrss` 继承父进程 exec 前的高水位 → 端到端回放改在加载任何大对象之前启动；
6. 排队模型首版用真实挂单作探针：生成器的随机撤单删失 54% 样本，剩余样本成交率 92%，常数预测已达 Brier 0.078 → 改为影子单真值（见第 5 节）。

### 6.y 覆盖率

| 文件（语句 / 执行 / 覆盖） | 历史（原 VM，`coverage.json`） | 本机复测（采集器修复后，`coverage_audit.json`） |
|---|---|---|
| book.py | 323 / 323 / 100.0% | 323 / 323 / 100.0% |
| cli.py | 204 / 191 / 93.6% | 220 / 199 / 90.5% |
| common.py | 55 / 55 / 100.0% | 58 / 58 / 100.0% |
| compare.py | 89 / 81 / 91.0% | 89 / 81 / 91.0% |
| events.py | 308 / 305 / 99.0% | 326 / 323 / 99.1% |
| fuzz/fuzz.py | 170 / 158 / 92.9% | 170 / 158 / 92.9% |
| invariants.py | 142 / 130 / 91.5% | 142 / 130 / 91.5% |
| mutants/run_mutations.py | 189 / 184 / 97.4% | 196 / 186 / 94.9% |
| queue_collect.py | 253 / 249 / 98.4% | 253 / 249 / 98.4% |
| queue_model.py | 196 / 196 / 100.0% | 197 / 197 / 100.0% |
| reference.py | 221 / 219 / 99.1% | 221 / 220 / 99.5% |
| replay.py | 192 / 189 / 98.4% | 220 / 215 / 97.7% |
| stress.py | 121 / 119 / 98.3% | 126 / 120 / 95.2% |
| **核心合计** | **97.40%**（384 tests） | **96.77%**（442 tests） |
| 其余（tools / tests 之外）| 58.86% | 47.07% |

## 7. 已知局限与下一步

- **纯 Python 单核**：引擎本身 ≈ 2.5 µs/事件（原 VM）；端到端回放还包含解码、状态流、每秒快照与不变量检查，无法在该 VM（Xeon 2.8 GHz）上达到 30 万/秒；进一步提升需要 C 扩展或 PyPy，本项目的约束（仅标准库）排除了这条路。
- **合成数据**：事件流由带 regime 的随机过程生成，不含真实市场的自相关、日内季节性、隐藏流动性策略；排队模型的所有数字只描述这份合成流。**本报告不构成任何交易结论。**
- **排队模型假设**：探针是影子单（虚拟单：零冲击、永不撤单），不是回放中的真实挂单；因此没有撤单删失，但也没有建模我方挂单对后续订单流的影响；特征使用 2 秒滑窗强度；未建模价格穿越后的重新排队。目标 3 整体未达成（第 5 节）。
- **参考实现与正式实现同作者**：对拍能发现数据结构层面的错误，不能发现对语义的共同误解；手写用例（期望值按 SPEC 手推）是对此的部分补救。
- **突变等价判定由人工完成**（见第 6 节逐条说明）。
- 下一步：影子单 vs 真实探针对照；按 symbol 并行的多核回放已具备（分片 + 归并逐字节一致），可扩展到多品种大规模回放；把 `Book` 热路径用 C 重写并保留本测试基础设施作为判据。

### 7.1 平台相关性

- **平台相关（换机器必然变化）**：第 1 节与 4.2 的全部吞吐（engine-only / engine+checker / 端到端 ev/s）、延迟分位（p50 / p99 / p999）、Top-10 快照 µs、RSS 与 tracemalloc 峰值；4.1 对拍表的「秒」列；第 6 节突变 / 模糊 JSON 里的 `seconds`。这些数字在本报告中一律标为历史（原 VM），复测值填入 4.2 的复测列。
- **平台无关（给定同一份 `events_10m.bin` 文件）**：对拍表的事件数 / 成交笔数 / 成交量 / 首次分歧 / 全扫描次数、状态码及其计数、模糊的拒绝数 / 崩溃 / 违背 / 分歧、分片回放的归并逐字节一致——撮合全程整数运算、无浮点，任何平台上对同一流文件应得到相同结果。
- **流文件的 sha256 本身**（`fc064598…`）：流的字节只含整数字段，但生成器用 `random.gauss` / `expovariate` / `uniform`（经 libm 的 log / cos）产生这些整数，同一 seed 在另一平台重新生成能否逐字节相同**未验证**（`lob/tests/test_events.py` 只断言同机两次生成的 sha256 相同）。跨平台复现对拍 / 压测时应复制流文件，或先核对重新生成（命令见报告开头）的 sha256 是否等于 `fc064598…`。
- **排队模型的 Brier 等统计量**为浮点，报告按 4–5 位小数记录；跨平台重算是否逐位相同未验证（探针 CSV 未入库，需先 `queue-collect` 重新采集）。

## 8. 验证运行记录

- `LOB_TEST_SCALE=full python3.12 -m unittest discover -s lob/tests -t .` → **382 tests OK，788.5 s**（**原 VM**，阶段 7 时的测试数；含 1,000 万条 4 进程 lockstep 对拍、100 万对拍、分片回放逐字节一致、模糊 2 万、排队模型、CLI 端到端）；提交 9fd7864 的信息只有标题行，并没有日志摘要（审核核实，原文「日志摘要见提交 9fd7864 说明」不成立）。full 规模（含 1,000 万对拍）**只在原 VM 上跑过，本次审核未重跑**。
- 审核复跑（standard 规模，10M 级在 standard 下 skip；测试数 382 → 384 是阶段 7 之后新增的判据，384 → 442 是审核新增的 `test_contract`（23）与 `test_boundaries`（35））：
  - 本审核 VM（Intel Xeon @ 2.10GHz，os.cpu_count()=4，Linux 6.18.44-fc-v140 x86_64，CPython 3.12.3）：`python3.12 -m lob.cli test`（= `unittest discover -s lob/tests -t .`，standard）→ `Ran 442 tests in 85.4s` / `OK (skipped=1)`，退出码 0；`python3.12 -m lob.cli test --quick` → `Ran 87 tests in 8.4s` / `OK`。
  - 审核员另一台机器（macOS arm64，Python 3.12.15，契约 / 边界判据落地前的树）同一 discover 命令 → 384 OK（skipped 1）；442 条的树未在该机复跑。
- 覆盖率：`python3.12 -m lob.cli coverage --json lob/results/coverage.json`（standard 规模，原 VM）→ 384 tests OK，核心 97.40%（**历史**）。审核复现时同一命令只得 95.49%，差异全在 `events.py`（258/308）：`lob/cli.py` 入口在采集器启动前已 import `events`，模块级语句计不到；生成器路径只在套件内重新生成 `events_1m.bin` 时执行。采集器已修（启动前移除已 import 的被测模块；流文件在临时目录重新生成，`LOB_DATA_DIR`），修后 `python3.12 -m lob.cli coverage --json lob/results/coverage_audit.json` → 442 tests OK，`events.py` 323/326，核心 **96.77%**（本机复测列见 6.y；低于 97.40% 的部分是审核新增的防御分支未全部被判据执行到）。
- `python3.12 -m lob.tools.lint_limits` → 37 个文件，0 个问题（≤ 800 行 / 函数 ≤ 60 行；阶段 7 时的文件数，审核收尾时为 39 个文件、0 个问题），`tests/test_limits.py` 同时断言只用标准库；审核复跑 `python3.12 -m unittest lob.tests.test_limits` → `Ran 2 tests` / `OK`。
- 所有随机过程显式 seed；价格 / 数量全程整数；无浮点比较（排队模型的 Brier 等统计量用浮点计算并按 4–5 位小数记录）。
- 每阶段提交并合回 `main`：阶段 0 babadfd、阶段 1 7318d87、阶段 2 2aaa147、阶段 3/4 16547bb 起多次、阶段 5/6 2f9aebc 起多次、阶段 7 本提交。

