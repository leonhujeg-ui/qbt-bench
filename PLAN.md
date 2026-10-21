# PLAN — qbt: 纯标准库 Python 回测与参数优化引擎（Python 3.12，零第三方依赖，零网络）

1. 阶段 0：仓库骨架、`qbt/data.py` 合成 K 线（GBM + 跳跃/停牌/缺口，Box-Muller 自实现，显式 seed）、`tools/cov.py`（sys.monitoring 语句覆盖率）、`mutants/run_mutations.py`（AST 突变器）骨架、可复现性测试。
2. 阶段 1：`qbt/indicators.py` 30+ 指标，全部单遍/滚动窗口（单调队列、滚动和）；每个指标一条数学判据（暴力对拍/闭式解/取值范围/对称性）。
3. 阶段 2：`qbt/broker.py` 订单（市价/限价/止损/止盈/冰山）、maker/taker、滑点、成交量参与上限、保证金/爆仓、资金费率；判据：逐 bar 会计恒等式、成交量上限、成交价在 bar 区间内。
4. 阶段 3：`qbt/engine.py` 事件驱动逐 bar 主循环（T 收盘下单、T+1 撮合，多策略多标的）+ `strategies/` 6 策略（参数空间 ≥ 4 维）；判据：篡改未来 K 线历史交易序列不变。
5. 阶段 4：`qbt/metrics.py`（收益/年化/夏普/索提诺/回撤及天数/胜率/盈亏比/Calmar/VaR/CVaR/月度表）+ 黄金文件；≤ 20 根手算对拍。
6. 阶段 5：`qbt/optimizer.py` 网格/随机/walk-forward，进程池 + sqlite 断点；判据：1 进程 vs 8 进程逐字节一致、kill 后续跑一致、去重。
7. 阶段 6：性能。固定基准：单核 1,000,000 根 × 1 策略 ≤ 3 s；8 进程 × 2,000 组参数 × 20,000 根网格 ≤ 90 s。每次优化前后 `bench/results/*.json` 入库，禁止缩数据。
8. 阶段 7：突变测试（每模块 ≥ 60 个）抓住率 ≥ 85%，模糊测试 100,000 次零未捕获异常；漏网逐个补判据，崩溃逐个修。
9. 阶段 8：`REPORT.md`、`README.md`、`python -m qbt.cli demo` 一键演示（文本报告 + SVG 权益曲线）。
10. 约束：每函数 ≤ 60 行、每文件 ≤ 800 行（由 `tests/test_limits.py` 自动检查）；浮点比较显式容差并注明理由；每阶段一次提交，提交信息含判据数/覆盖率/突变抓住率。
11. 机器：Linux x86_64，4 核 Xeon 2.1GHz，16 GB，Python 3.12.3；8 进程目标在 4 物理核上实测并如实写入报告。
