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
