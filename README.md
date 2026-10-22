# qbt — 纯标准库的事件驱动回测与参数优化引擎

**怎么把我接到你的系统（三行）**：
1. 数据：把你的 K 线转成 `qbt.data.OHLCV(symbol, ts, open, high, low, close, volume)`（列式 list，ts 严格递增的秒级整数，价格 > 0，volume ≥ 0）或 `OHLCV.from_csv(path)`（表头 `ts,open,high,low,close,volume`），然后 `.validate()`。
2. 策略：继承 `qbt.engine.Strategy`，实现 `on_start(ctx)` / `on_bar(ctx, i)`，只读下标 ≤ i 的数据，用 `ctx.order(...)` / `ctx.target(...)` 下单（订单最早在 i+1 成交）；字段、符号约定与错误码见 `qbt/CONTRACT.md`。
3. 结果：`run_backtest(data, strategy, BrokerConfig(...))` 返回 `Result`（`equity`、`fills`、`trades`），`qbt.report.write_report(result, out_dir, name)` 写出 `<name>.json`（`summary` + `metrics`，字段表见 `qbt/CONTRACT.md`）。

Python 3.12，零第三方依赖，零网络。目标：**正确（确定性）、够快、够硬（测试/突变/模糊）、够诚实（所有数字可复现）**。
详细结论见 [REPORT.md](REPORT.md)，实施计划见 [PLAN.md](PLAN.md)。

## 一键演示

```bash
python3.12 -m qbt.cli demo                 # 合成数据 + 6 个策略 + 文本报告 + SVG 权益曲线 -> out/demo/
python3.12 -m qbt.cli demo --n 100000 --seed 7 --out out/demo7
```

## 其他命令

```bash
python3.12 -m qbt.cli backtest --strategy donchian_breakout --params '{"entry_n": 55}' --n 50000 --out out/bt
python3.12 -m qbt.cli optimize --strategy rsi_reversion --n 20000 --random 200 --workers 8 --checkpoint out/ck.db --out out/opt.json
python3.12 -m qbt.cli optimize --strategy ma_cross --n 60000 --walk-forward 3 --workers 4 --out out/wf.json
python3.12 -m qbt.cli test                                   # 全量 = python -m unittest discover -s tests（约 25 s）
python3.12 -m qbt.cli test --quick                           # 冒烟子集（limits/golden/broker/engine/boundaries/contract，约 5 s）
python3.12 tools/cov.py --fail-under 90                      # 语句覆盖率（sys.monitoring，纯标准库）
python3.12 mutants/run_mutations.py --per-module 80 --jobs 3 --tests "-f tests.test_golden tests.test_indicators tests.test_broker tests.test_engine tests.test_metrics tests.test_optimizer tests.test_strategies tests.test_data tests.test_report tests.test_cli tests.test_limits tests.test_perf"   # 突变测试（失败即停 + 先跑最能抓的模块）-> mutants/results.json, survivors.md
python3.12 fuzz/fuzz.py --iterations 100000 --workers 4      # 模糊测试 -> fuzz/results.json
python3.12 bench/bench.py --out bench/results/xx.json        # 性能基准 -> JSON
```

## 退出码

`python3.12 -m qbt.cli test [--quick]`：**0** 全部通过；**1** 有失败或错误（unittest 的汇总）；**2** 命令行参数错误（argparse）。
其他子命令：`demo` / `backtest` / `optimize` / `bench` 正常结束返回 0；`fuzz` 有未捕获异常返回 1；内部受控错误抛 `qbt.errors.QBTError` 子类（错误码表见 `qbt/CONTRACT.md`）。

## 目录

| 路径 | 内容 |
|---|---|
| `qbt/data.py` | 合成 K 线（GBM + 跳跃 / 停牌 / 缺口），同 seed 同机逐字节可复现、跨机相对 1e-9 内一致，前缀一致 |
| `qbt/indicators.py` | 36 个指标，全部单遍 / 滚动窗口（单调队列、Wilder 递推、West 滑窗方差） |
| `qbt/broker.py` | 市价 / 限价 / 止损 / 止盈 / 冰山，maker/taker 费，固定 + 冲击滑点，成交量参与上限，杠杆 / 维持保证金 / 强平 / 破产，资金费率 |
| `qbt/engine.py` | 事件驱动主循环（T 收盘下单、T+1 撮合），多策略多标的，严格模式守卫列拦截偷看未来 |
| `qbt/metrics.py` | 总收益、CAGR、夏普、索提诺、最大回撤与时长、胜率、盈亏比、Calmar、VaR/CVaR、月度表 |
| `qbt/optimizer.py` | 网格 / 随机 / walk-forward，进程池，去重，sqlite 断点续跑，结果与进程数无关 |
| `qbt/report.py` | 纯文本报告 + 标准库拼接的 SVG 权益曲线（含回撤阴影） |
| `strategies/` | 均线交叉、布林回归、Donchian 突破、RSI 均值回归、网格、带止损趋势跟随（参数空间 ≥ 4 维） |
| `tests/` | unittest：随机性质测试、黄金文件、并发确定性、性能阈值、工程约束（函数 ≤ 60 行 / 文件 ≤ 800 行） |
| `mutants/run_mutations.py` | 自写 AST 突变器（9 类算子），子进程隔离运行 |
| `fuzz/fuzz.py` | 10 万次随机 / 畸形输入打 engine / broker / indicators / metrics |
| `bench/bench.py` | 计时脚本，输出 JSON（`bench/results/`） |
| `tools/cov.py` | 语句覆盖率工具（sys.monitoring, PEP 669） |
| `results/`, `mutants/history/`, `bench/results/` | 报告引用的全部原始 JSON（覆盖率、walk-forward、各轮突变结果、基准） |

## 时间语义（不偷看未来）

第 i 根 bar：先用 bar i 撮合上一根收盘后提交的订单并以 close[i] 盯市，再把 bar i 交给策略；策略下的单最早在 i+1 成交。
指标全部因果（输出 i 只依赖输入 [0, i]）；`Engine(strict=True)` 下访问未来下标直接抛 `LookaheadError`。

## 免责声明

所有数据均为合成数据，所有策略结果不构成任何投资结论。
