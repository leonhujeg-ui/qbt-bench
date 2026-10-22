# lob — 限价订单簿撮合引擎、回放、排队成交模型

**怎么把我接到你的系统（三行）**：
1. 数据：把你的订单流写成 56 字节定长记录（`lob.events.pack(seq, ts_ns, symbol, kind, side, flags, trader, oid, price, qty)`，字节布局 / 枚举 / 状态码见 [CONTRACT.md](CONTRACT.md) §1–2；价格 = 整数 tick，数量 = 整数 lot），旁边放一个 `<stream>.meta.json`（§1.5），或先用 `python3.12 -m lob.cli gen` 生成合成流；
2. 引擎：`lob.book.Book(lob.common.BookConfig(...))` 对 `lob.events.iter_records(path)` 解出的每条 12 元组调用 `process(rec)`，返回 1 字节状态码（0–6 成功、10–20 拒绝，§2），成交落在 `book.trades`；或者直接 `python3.12 -m lob.cli replay --stream <stream> --out <dir> --json <stats.json>`；
3. 结果：`<dir>/stats.json`（键见 §6.2：events / trades / volume / 状态码计数…）、`trades.bin`（48 字节成交记录，§4.4）、`status.bin`（每事件 1 字节状态码，下标 = seq − 1）、`snapshots.jsonl`（每品种每秒 Top-10）。

纯 Python 3.12 标准库，无第三方依赖。价格 = 整数 tick，数量 = 整数 lot。

## 一键演示（约 1–2 分钟）

```bash
cd <repo root>
python3.12 -m lob.cli demo            # 生成 20 万事件 → 对拍 → 回放 → 压测 → 排队模型 → 模糊
```

## 常用命令

```bash
python3.12 -m lob.cli test                      # 全量：discover 整个 lob/tests/（standard: 1 万 + 100 万对拍）
python3.12 -m lob.cli test --quick              # 冒烟：LOB_TEST_SCALE=quick，只跑 test_book / test_events / test_cli / test_boundaries / test_contract
python3.12 -m lob.cli test --scale full         # 含 1,000 万对拍（约 10 分钟，4 进程）；等价于 LOB_TEST_SCALE=full python3.12 -m lob.cli test
python3.12 -m lob.cli gen --n 10000000 --symbols 8 --seed 12345 --out lob/data/events_10m.bin
python3.12 -m lob.cli compare --stream lob/data/events_10m.bin --processes 4 --json lob/results/compare_10m.json
python3.12 -m lob.cli replay  --stream lob/data/events_10m.bin --out lob/data/replay --check 10000
python3.12 -m lob.cli replay  --stream lob/data/events_10m.bin --out lob/data/replay4 --processes 4   # 与单进程逐字节一致
python3.12 -m lob.cli stress  --stream lob/data/events_10m.bin --out lob/data/stress --label rN --json lob/results/stress_rN.json
python3.12 -m lob.cli queue-collect --stream lob/data/events_10m.bin --out lob/data/probes_10m.csv --sample-every 50
python3.12 -m lob.cli queue-model --probes lob/data/probes_10m.csv --out lob/results/queue_model --horizons 1 2 5 10
python3.12 -m lob.cli fuzz --n 200000 --json lob/results/fuzz_200k.json
python3.12 -m lob.cli mutate --target book --json lob/results/mutations_book.json
python3.12 -m lob.cli mutate --target queue_model --json lob/results/mutations_queue_model.json
python3.12 -m lob.cli coverage --json lob/results/coverage.json
python3.12 -m lob.cli bench --json lob/results/bench_structures.json
```

### 退出码

| 命令 | 0 | 1 | 2 |
|---|---|---|---|
| `test` / `test --quick` | 跑到的判据全部通过 | 任一失败或错误（一个判据都没跑到也算 1） | 用法错误：argparse 拒绝的参数、`--quick` 与非 quick 的 `--scale` 同用、`LOB_TEST_SCALE` 取值非法 |
| `compare` | 两套引擎无分歧（`first_divergence` 为 None） | 有分歧 | 用法错误 |
| 其他子命令 | 正常结束 | 未捕获异常（Python 解释器默认退出码） | 用法错误 |

`test` 不带参数跑的是 `lob/tests/` 的**全量** discover（standard 规模，本机约 90 s）；`--quick` 只跑五个模块（约 10 s），用于冒烟，不替代全量。
接口契约（事件二进制格式、状态码表、字段表、错误表）见 `CONTRACT.md`，每张表都由 `tests/test_contract.py` 对照代码核对。

## 目录

| 文件 | 内容 |
|---|---|
| `book.py` | 正式订单簿：有序价格数组 + 二分，每档 list+head FIFO，惰性删除 + 压实，限价/市价/IOC/FOK/post-only/冰山/撤单/改量，自成交两策略，tick/lot/价格带 |
| `reference.py` | 暴力参考撮合器（扁平列表，O(n) 逐单扫描） |
| `SPEC.md` | 两套实现共同遵守的撮合语义 |
| `events.py` | 56 字节定长事件记录 + seed 可复现的多品种合成生成器（regime: normal / withdraw / shock，畸形事件） |
| `replay.py` | 回放器：成交流、状态流、每秒 Top-10 快照、统计；单进程与按 symbol 分片多进程逐字节一致 |
| `compare.py` | 逐事件 lockstep 对拍（状态码 + 成交 + 周期档位/计数器/订单集合） |
| `invariants.py` | 运行时不变量检查器（每事件 O(1) + 周期全扫描，可开关） |
| `stress.py` | 吞吐 / 延迟分位 / 快照成本 / 内存峰值 → JSON |
| `queue_collect.py` / `queue_model.py` | 排队成交模型的真值采集与校准、Brier、可靠性图 SVG |
| `fuzz/fuzz.py` | 随机 + 畸形事件模糊测试（常开不变量，参考实现 lockstep） |
| `mutants/run_mutations.py` | AST 突变器 + 子进程判定 |
| `tools/coverage.py` | `sys.monitoring` 语句覆盖率 |
| `tools/lint_limits.py` | 文件 ≤ 800 行、函数 ≤ 60 行 |
| `tools/bench_structures.py` | 价格档索引结构备选对比 |
| `tests/` | unittest：手写用例（两引擎）、检查器自检、对拍三级、回放确定性、模糊、排队模型、规则 |
| `results/` | 所有报告数字的来源 JSON / SVG |
| `REPORT.md` | 最终报告 |

## 数据文件

`lob/data/` 被 git 忽略；`events_1m.bin` / `events_10m.bin` 由 `gen` 用固定 seed 生成（sha256 见 `results/*.json` 与 `*.meta.json`），任何机器上重新生成都逐字节相同。
