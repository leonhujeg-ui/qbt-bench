# 实施计划（lob/）

1. 阶段 0：`events.py` 固定 56 字节记录（seq/ts_ns/symbol/kind/side/flags/trader/oid/price/qty/aux，int64 价格与数量），带 seed 的多品种合成生成器（regime：normal / withdraw / shock），同 seed 两次生成逐字节一致；`cli.py gen`。
2. 阶段 1：`reference.py` 扁平列表 O(n) 扫描撮合，语义文档 `SPEC.md`；`tests/test_cases.py` ≥ 100 条手写用例（每种订单类型 ≥ 8 条）。
3. 阶段 2：`book.py` 有序价格数组 + 二分（best 在尾部，pop O(1)），每档 list+head 指针 FIFO、惰性删除+压实；`invariants.py` 每事件 O(1) 检查 + 周期全扫描；对拍 1 万 → 100 万。
4. 阶段 3：`replay.py` 单进程 / 按 symbol 分片多进程 → 按 seq 归并，与单进程逐字节一致；每秒快照、状态流、摘要；1,000 万对拍（参考实现并行分片）。
5. 阶段 4：`stress.py` 吞吐 / p50 / p99 / p999 / ru_maxrss + tracemalloc；基线 JSON → 逐轮优化（避免对象分配、减少调用、快照脏标记缓存），每轮 JSON 入库；数据结构备选对比（数组+二分 / 跳表 / 堆+惰性删除）。
6. 阶段 5：`queue_model.py` 用回放中真实挂单作为探针（随机取样，撤单前未成交视为删失），特征：距触价档数、前方量、档总量、到达/撤单/成交强度；分桶经验概率 + 拉普拉斯平滑，时间切分训练/测试，Brier + 可靠性图 SVG；按 regime 分段报告失效场景。
7. 阶段 6：`mutants/run_mutations.py` 基于 ast 的突变器（比较/算术/常量/布尔/分支），每文件 ≥ 80 个，子进程跑快速测试集；`fuzz/fuzz.py` 20 万次随机+畸形事件常开不变量；漏网逐条补判据或标注等价。
8. 阶段 7：`tools/coverage.py`（sys.monitoring 语句覆盖）、`tools/lint_limits.py`（≤60 行函数 / ≤800 行文件）、`README.md`（`python -m lob.cli demo`）、`REPORT.md`。
9. 规则：所有随机显式 seed；价格/数量一律整数；每阶段至少一次提交并合回 main 推送；数字只来自 `lob/results/*.json` 的真实运行。
