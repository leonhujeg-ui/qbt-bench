# qdcheck — 量化回测系统验证工具包

```bash
python3.12 -m qdcheck.selftest
```

期望输出（任何一行 `FAIL` 都会让退出码变成 1；数字来自本仓一次真实运行，你的机器上耗时会不同）：

```
qdcheck selftest  Linux-6.18.44-fc-v140-x86_64-with-glibc2.39  Python 3.12.3
step        scale                                                    time  result  key numbers
unittest    213 tests                                               87.9s  OK    OK; skipped 0
coverage    2916 statements, 213 tests                              88.1s  OK    100.00% (>= 90% required); tests failed 0
vectors     1260 indicator vectors + 7 ledger vectors, 17 files      4.6s  OK    regenerated: mismatches 0, max_rel_err 0.0e+00; schema: all valid
indicators  5311 property checks                                    19.1s  OK    failures 0; ref vs alt max tol ratio 0.21
fuzz        4 validates x 20000 cases                               15.9s  OK    good: bare 0, 2.17s; caught keyerror x510, zerodiv x103, recursion x430; nan/inf/missing/type 1830/1785/1845/1798
ledger      200 streams x 3000 bars, 94028 records                  21.6s  OK    identity violations 0, alt issues 0, row mismatches 0 (10800000 values); liquidations 812
harness     16 cases (4 fake engines + 12 qbt backtests)             9.5s  OK    as expected 16/16; set-order engine caught: True; qbt identical 12/12
TOTAL                                                              246.7s  OK    7/7 steps passed; written qdcheck/results/selftest.json
```

纯 Python 3.12 标准库，零第三方依赖、零出网、只写 `qdcheck/results/`、`qdcheck/vectors/` 与 tempfile。
所有数据都是合成的，不构成任何交易结论。

---

## 30 分钟接入（四块各一段；字段 / 单位 / 退出码的精确定义在 [CONTRACT.md](CONTRACT.md)）

**1. 指标对拍** —— 你的指标算 `qdcheck/vectors/<指标>.json` 里每条向量的 `input`，把输出交给 compare：

```bash
python3.12 -m qdcheck.compare --vector qdcheck/vectors/kdj.json --actual my_outputs.json   # {vector_id: [..]} 或裸数组 + --id
# 退出码 0 一致 / 1 不一致 / 2 输入不合法；stdout 第一行：OK n=.. max_rel=..  或  MISMATCH idx=.. expected=.. actual=.. rel=.. reason=..
```

口径不一致时按 `variant` 字段选和你一样的那条（全部 61 个口径与预热期见 [REPORT.md 第 3 节](REPORT.md)）；
自己的实现还可以用 `qdcheck.props.check_causality / check_length / expected_warmup / constant_expectation` 过性质判据。

**2. 入参模糊** —— 给 validate 写一个 schema（[CONTRACT.md §3.2](CONTRACT.md)），跑 2 万次：

```python
from qdcheck import fuzz
rep = fuzz.run(my_validate, my_schema, n=20000, seed=0, is_controlled=MyValidationError)
assert rep.ok, rep.to_json("fuzz_report.json")        # 裸异常 = 0；报告里有最小复现、栈顶三行、每个键被乱过几种方式
```

**3. 账本对账** —— 把你引擎的成交流（每条 `{bar, kind, side, qty, price, fee, funding}`，符号约定见 [CONTRACT.md §4.1](CONTRACT.md)）和收盘价喂参考账本：

```python
from qdcheck import ledger
assert ledger.check(records, closes, initial_cash=1e5, margin_mode="cross", leverage=3, mmr=0.05) == []   # 每根恒等式
rep = ledger.check_engine(records, closes, my_engine_equity, initial_cash=1e5, margin_mode="cross", leverage=3, mmr=0.05)
assert rep["ok"], rep["first_mismatch"]               # 你的引擎自报权益 vs 参考账本，逐根
```

也可以反过来用 `qdcheck/vectors/ledger_*.json`（成交流 + 收盘价 + 每根 18 列期望值）测你的账本：`ledger.check_vector(vec, {"equity": my_equity})`。

**4. 跨进程 / 跨哈希种子一致性** —— 一个可 JSON 序列化的任务 + `模块:函数`：

```bash
python3.12 -m qdcheck.determinism mypkg.engine:run_backtest --task '{"strategy": "x", "n": 20000, "seed": 1}'
# 本进程 + PYTHONHASHSEED=0 / 4242 两个子进程，三份结果结构化比较；差异打印路径如 trades[17].price
```

你的模块不在本仓时：`determinism.check(task, "mypkg.engine:run", paths=("/你的/代码根",))`（子进程通过 PYTHONPATH 找模块）。

## 实测过的平台

| 平台 | 命令 | 结果 |
|---|---|---|
| 同机，CPython 3.13.16 | `python3 -m qdcheck.selftest --quick` | 7/7 步通过，97.1 s；向量重新生成逐点一致（mismatches 0） |

向量、合成数据和随机流只用 `random.random()` 与四则运算（不依赖 libm），理论上在任何 IEEE-754 机器上逐位相同；判据只要求 1e-9 内一致。
其他平台未实测——跑 `python3.12 -m qdcheck.selftest`，`vectors` 一行就是“在你的机器上重新生成能否对上”的答案。

## 其他命令

```bash
python3.12 -m qdcheck.selftest --quick          # 约 1 分钟（不跑覆盖率，账本 20 条流）
python3.12 -m qdcheck.selftest --refresh        # 结果写回 qdcheck/results/ 并重建 REPORT.md
python3.12 -m qdcheck.gen_vectors --check       # 在本机重新生成全部向量并与入库的逐点比较
python3.12 -m qdcheck.vector_schema             # 向量文件契约校验（vectors/schema.json）
python3.12 qdcheck/tools/cov.py --fail-under 90 # 语句覆盖率
python3.12 qdcheck/tools/mutate.py --per-module 60 --jobs 4    # 突变测试（约 12 分钟）
python3.12 -m qdcheck.tools.triage --replay     # 历次漏网在最终测试上重跑 -> mutants/triage.md
python3.12 -m qdcheck.tools.perf                # 性能表（规模 / 耗时 / 峰值内存）
```

## 文档

- [CONTRACT.md](CONTRACT.md) —— 接口契约：向量 schema、compare CLI、fuzz API、账本记账数学与恒等式、determinism 约定、一目均衡表下标对齐、预热期公式。
- [REPORT.md](REPORT.md) —— 目标达成表（要求 / 实测 / 复现命令）、变体清单、qbt 真实用例、覆盖率、突变、性能表、修过的缺陷、已知限制（由结果文件生成）。
- [REVIEW.md](REVIEW.md) —— 接入方视角的评审：每条检查命令 → 实际输出 → 改了什么 / 为什么不用改。
- [mutants/triage.md](mutants/triage.md) —— 历次突变漏网逐条去向。
