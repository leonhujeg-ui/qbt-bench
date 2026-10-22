# qdcheck 接入评审（REVIEW）

身份：另一个量化系统（Python 3.12 标准库 + SQLite）的维护者，要把 qdcheck 拿过去当判据。目标：只读 README，30 分钟内把四块接进自己的测试。
下面每条 = **检查命令 → 实际输出 → 改了什么 / 为什么不用改**。实际输出都是本机（Linux x86_64，CPython 3.12.3，4 核）真实运行的截取；
最终数字以 `qdcheck/results/*.json` 为准（`python3.12 -m qdcheck.selftest --refresh` 重算）。

---

## A. 可接入性

### A1. 一条命令跑全部

**检查**：`python3.12 -m qdcheck.selftest`（评审前不存在：原来要分别跑 unittest、cov.py、gen_vectors --check、selfcheck g1/g2/g4、stream，7 条命令）。

**实际输出（修后，全量）**：

```
__SELFTEST__
```

**改了什么**：新增 `qdcheck/selftest.py`：7 步（unittest、覆盖率、向量复现 + schema 校验、指标性质、模糊器、账本、harness），
打印总表，任何一步失败（或崩溃）退出码 1；`--quick` 约 1 分钟（unittest 用 QDCHECK_FAST=1、跳过覆盖率、账本 20 条流），
`--refresh` 把结果写回 `qdcheck/results/` 并重建 REPORT.md（REPORT 里的数字就是这么来的）。缺省只写 `qdcheck/results/selftest/`（已 gitignore）
和 `qdcheck/results/selftest.json`。README 第一屏只放这一条命令和它的期望输出。
评审期间它先后抓到了我自己写错的 3 条契约测试（`5e-324` 不是 `%.17g` 写法、一目均衡表例子手算错、正则不含数字），见 B1。

### A2. 零依赖、零出网、零写仓外

**检查 1（原命令的问题）**：清单里给的 `grep -rn "^import\|^from" qdcheck | grep -vE "^(qdcheck|re|...)"` 对每一行都匹配 `^qdcheck`（文件名前缀），
所以**永远为空**，不是有效检查。改成按模块名过滤：

```
$ grep -rn "^import\|^from" qdcheck --include=*.py | grep -vE ":(import|from) (白名单)([ .]|$)"
```

**修前输出**（25 行）：18 处 `from __future__ import annotations`、`importlib`（fuzz / determinism）、`linecache`（fuzz）、
`ast`（tools/mutate.py）、`from tools import cov`（tools/cov.py）、`from mutants import run_mutations`（tools/mutate.py）。

**修后输出**：

```
qdcheck/tools/cov.py:23:from tools import cov as base          # 本仓已有的覆盖率工具（原始要求“复用 tools/cov.py 的做法”）
qdcheck/tools/triage.py:9:import ast
qdcheck/tools/triage.py:20:from mutants import run_mutations as base
qdcheck/tools/mutate.py:14:import ast
qdcheck/tools/mutate.py:29:from mutants import run_mutations as base   # 本仓已有的突变器（原始要求“用本仓 mutants/ 的做法”）
qdcheck/tools/perf.py:42:import json, resource, sys, time              # 误报：这一行在字符串字面量里（子进程包装代码）
```

库模块（`qdcheck/*.py`）与测试：**空**。`qdcheck/tools/`（开发工具，不是接入面）还导入标准库 `ast` 与本仓自己的 `tools` / `mutants` 包。
**改了什么**：删掉全部 `from __future__ import annotations`（无需要）；`importlib.import_module` 换成内建 `__import__` + `sys.modules`；
`linecache` 换成白名单里的 `traceback.extract_tb(tb, limit=-3)`。并把这条策略写成测试 `test_contract.TestDependencyPolicy`：
库模块的模块级导入 ⊆ 白名单；`qbt` / `strategies`（本仓回测引擎，只在“真实用例”适配器里）只许在函数内延迟导入，
`import qdcheck.examples` 不需要 qbt；tests/ 与 tools/ 另许 `ast`、`tools`、`mutants`。

**检查 2**：`grep -rn "socket\|urllib\|http\|requests" qdcheck --include=*.py` → 只命中 test_contract.py 里的黑名单常量本身；**空**（测试 `test_no_network_or_outside_writes_in_library`）。

**检查 3（写文件）**：`grep -rn "open(.*\"w\"" qdcheck --include=*.py | grep -v tests/` → 21 处：CLI 的 `--out` / `--json`（用户自己给的路径）、
`gen_vectors` 写 `vectors/`、selftest / selfcheck / cov / perf / triage 写 `results/`、`build_report` 写 `qdcheck/REPORT.md`、
mutate 写 tempfile 工作目录。**改了什么**：`tools/mutate.py` 原来把突变工作目录放在仓库根的 `mutants/_work/`（仓外），改成 `tempfile.mkdtemp()`。
未改：`build_report` 写 `qdcheck/REPORT.md` 是它的用途；Python 自己的 `__pycache__`（`PYTHONDONTWRITEBYTECODE=1` 可关）。

### A3. 接口契约写死并有判据

新增 `qdcheck/CONTRACT.md` + `qdcheck/tests/test_contract.py`（11 条）。逐项：

**向量 JSON schema** —— 新增 `vectors/schema.json`（指标文件 / 单条向量 / OHLCV / 账本文件 / 账本记录 / manifest 六个定义）与标准库校验器 `qdcheck/vector_schema.py`
（自己写的 JSON-Schema 子集：type / const / enum / pattern / minimum / exclusiveMinimum / minItems / items / required / properties /
additionalProperties / propertyNames / $ref；再加 schema 表达不了的语义：输出长度 = 输入长度、OHLC 顺序、文件头 count / variants / lines 与内容一致、
数值文本恰是 `%.17g`、账本 expected 列长与 records 顺序）。

```
$ python3.12 -m qdcheck.vector_schema
ao.json                                       OK
cci.json                                      OK
... (17 行)
checked 17 files, all valid
```

`test_validator_rejects_tampering` 对 13 种指标文件篡改 + 7 种账本文件篡改 + manifest 容差篡改逐一确认被拒；`test_number_text_rule`：`0.1` 不是 `%.17g`
写法（应为 `0.10000000000000001`）、20 位数字被拒、整数字面量不受约束。

**compare CLI** —— 原来是位置参数 `vectors actual`，没有 stdin，第一行没有固定格式。**改了什么**：

```
$ python3.12 -m qdcheck.compare --vector one.json --actual ok.json ; echo exit=$?
OK n=142 max_rel=0.000e+00
checked 1/1  passed 1  failed 0  missing 0  unknown 0  max_rel_err 0.000e+00
exit=0
$ python3.12 -m qdcheck.compare --vector one.json --actual bad.json ; echo exit=$?
MISMATCH idx=8 expected=98.39736111111114 actual=98.39745950847224 rel=1.000e-06 reason=value
checked 1/1  passed 0  failed 1  missing 0  unknown 0  max_rel_err 0.000e+00
exit=1
$ cat bad.json | python3.12 -m qdcheck.compare --vector one.json --actual - ; echo exit=$?      # stdin
MISMATCH idx=8 expected=98.39736111111114 actual=98.39745950847224 rel=1.000e-06 reason=value
exit=1
$ python3.12 -m qdcheck.compare --vector one.json --actual /nonexistent ; echo exit=$?
error: [Errno 2] No such file or directory: '/nonexistent'
exit=2
$ echo '"str"' | python3.12 -m qdcheck.compare --vector one.json --actual - ; echo exit=$?
error: actual must be a JSON array or an object {vector_id: array}
exit=2
```

第一行格式 `MISMATCH idx=<n> expected=<x> actual=<y> rel=<r> reason=<length|warmup|type|value>`（多条向量时附 ` id=<id>`；
长度不一致时 idx = 较短长度、expected / actual = 两边长度、rel=nan）。`test_cli_first_line_for_each_reason` 把 7 种情形的第一行逐字符钉死，
`test_cli_bare_array_stdin_and_invalid_input` 核 stdin 的 7 种输入与退出码。

**fuzz Python 接口** —— 原来 `run(..., controlled=)` 返回 dict。**改了什么**：`run(validate, schema, n, seed, is_controlled) -> fuzz.Report`
（dict 子类；`.ok`、`.to_json(path=None)`；旧名 `controlled=` 仍接受）。`Report.FIELDS`：

```
('tool', 'format', 'validate', 'seed', 'n', 'elapsed_s', 'per_case_us', 'outcomes', 'bare_by_type', 'categories',
 'field_coverage', 'fields_under_4_kinds', 'schema_valid_cases', 'accepted_invalid', 'rejected_valid', 'bare_unrecorded', 'bare')
```

CONTRACT §3.1 一张表逐字段解释；`test_fuzz_report_fields_match_contract_table` 从 CONTRACT.md 的表里正则抽出字段名，要求 ⊇ `Report.FIELDS`；
`test_report_type_fields_and_json` 核 `to_json()` 是严格 JSON（无 NaN 字面量、ASCII、往返相等）。

**ledger** —— CONTRACT §4.1 一张表：`bar` 根序号、`kind`、`side`（buy/sell/none 或 ±1/0）、`qty`（**多头持仓为正、空头为负**；成交数量必须 > 0）、
`price`（报价货币/单位数量）、`fee`（**正数 = 扣减现金**，负数 = 返佣）、`funding`（**正数 = 账户支付**）。
新增 `ledger.check(records, closes, **config) -> [{"bar", "check", "lhs", "rhs", "detail"}]`（空 = 通过），`ledger.CHECKS` 枚举全部 8 个检查名，
`test_ledger_flag_names_are_enumerated` 用 AST 核代码里每个 `_flag(...)` 的名字都在 `CHECKS` 里且都写进了 CONTRACT。
`test_sign_conventions` 把符号约定钉死（买入现金 −(qty·price+fee)、卖出 +、负 fee 加现金、funding 正数减现金、空头 qty 负）。

**determinism** —— CONTRACT §5：任务必须 `json.dumps` 可序列化（harness 先 JSON 往返再传）；路径 `包.模块:函数`；子进程 `cwd` = 仓库根、
`PYTHONPATH` = 仓库根 + `paths=(...)`；返回值必须可 JSON 序列化（set / bytes 直接报错，不替你排序）；差异路径 `trades[17].price` / `["非标识符键"]` / `<root>`。
新测试 `test_child_finds_modules_via_pythonpath_paths`：把模块放在临时目录，`paths=(dir,)` 能找到、不给则 `ModuleNotFoundError`。

### A4. 可移植

**实测过的平台**：见 README“实测过的平台”表（本机 CPython 3.12.3；同机 CPython 3.13.16 跑 `--quick`：__PY313__）。
其他平台未实测，README 明说；向量 / 合成数据 / 随机流不依赖 libm，`selftest` 的 `vectors` 一行就是“在你的机器上能否重新生成对上”的答案。

**等价突变的判定理由集中**：新增 `qdcheck/mutants/triage.md`，由 `python3.12 -m qdcheck.tools.triage --replay` 生成：
收集 `results/mutation_history/*.json` **每一轮**的全部漏网（去重后 __UNIQ__ 处），在当前源码里找同一处突变（模块 + 算子 + 改动前后文本），
用最终测试重跑一次，每条写明：抓住（被哪个测试）/ 仍漏网 → 等价理由（来自 `results/mutation_triage.json`，不许只写“等价”）/ 代码已不存在。见 C1。

---

## B. 正确性再核

### B1. 指标参考实现

**Decimal 复算**：新增 `tests/test_decimal.py`——9 个指标各挑 1 个变体，按 CONTRACT 的公式用 `decimal.Decimal`（28 位）在 ≤ 5 根上重算，与 ref 与 alt 两份浮点实现逐点对拍（容差 1e-12）：

```
test_ao_sma_hl2 ... ok            test_cci_hlc3 ... ok              test_dema_ema_seed_sma ... ok
test_force_ema_seed_sma ... ok    test_ichimoku_forward_shift_1 ... ok   test_kdj_rma_init50 ... ok
test_stochrsi_wilder_talib ... ok test_tema_rma_seed_first ... ok   test_trix_sma ... ok
Ran 9 tests  OK
```

**Ichimoku 位移**：CONTRACT §1.5 画了 5 根的例子（tenkan=2, kijun=3, senkou_b=4, displacement=1）：`senkou_a[t] = base_a[t−off]`（在 t−off 算出、画在 t），
`chikou[t] = close[t+off]`，前移后数组长度不变、末尾 off 个“未来云”不输出。`test_ichimoku_example_in_contract_matches_code` 核例子里的每个数。
**它抓到了我自己的错**：手算 tenkan[1] 写成 2.0，Decimal 复算与代码都是 1.75（(3 + 0.5)/2），已改文档。

**预热期公式**：CONTRACT §1.4 每个指标每个变体一行公式（`ℓ(n) = n−1` 当平滑有 n−1 的预热，否则 0；trix +1；ichimoku + off），
判据 `test_warmup_positions_match_formula`（每变体 × 两组参数 × 两条序列核前导 null 个数）早已存在，本次把公式写进文档。

### B2. 模糊器

**缩小是真的**：三个坏例的最小复现（`results/g2_selfcheck.json`，`test_minimal_repros_are_human_readable` 钉死）：

```
validate_keyerror   -> KeyError            minimal {}                 size 2 / 88        top: examples.py:123 out = {"period": _int(cfg["period"] ...
validate_zerodiv    -> ZeroDivisionError   minimal {'period': 0}      size 12 / 143      top: examples.py:134 bars_per_period = 10000 // period
validate_recursion  -> RecursionError      minimal {'symbol': 'abab…'} size 1106 / 1000098 top: examples.py:144 in _all_printable
```

空配置 → 缺必填键；只剩 `period=0` → 除零；只剩一个 1106 字符的 symbol → 递归逐字符检查爆栈。人眼看得出问题。

**同 seed 两次运行逐字节相同**：`test_same_seed_gives_byte_identical_case_sequence`——3000 个用例（类别、路径、编码后的输入）的 JSON 字节相同；
两次 `run()` 的报告去掉计时字段后 JSON 相同。另有跨 PYTHONHASHSEED=0/4242 子进程的用例序列比对（原有）。

**每个键至少被乱过 4 种方式**：报告新增 `field_coverage` / `fields_under_4_kinds`（按 schema 路径归档：乱出来的结构 / 改名的键算在包含它的字段上）：

```
fields_under_4_kinds = []
$                    kinds=15  total= 1491  extra_key=1330, type_wrong=112, ...
$.fee_rate           kinds=14  total= 2480  numeric_string=314, neg_zero=307, nan=297, negative=285, huge=281
$.leverage           kinds=15  total= 2444
$.period             kinds=15  total= 4846
$.risk               kinds=15  total= 2180  nested_garbage=655, extra_key=555, empty=338, missing_key=135, key_case=129
$.risk.max_drawdown  kinds=15  total= 2019
$.risk.stop_loss     kinds=14  total= 1166
$.risk.window        kinds=15  total= 1128
$.side               kinds=15  total= 2744  unicode=642, long_string=605, key_case=266, boundary=259, missing_key=258
$.symbol             kinds=15  total= 3439
$.symbols            kinds=15  total= 1709
$.symbols[]          kinds=15  total= 1920
$.use_stop           kinds=15  total=  817
```

判据 `test_every_schema_key_is_mutated_in_at_least_4_ways`（13 个路径全部 ≥ 4 种，实际 14–15 种）；selftest 的 fuzz 一步也要求 `fields_under_4_kinds` 为空。
第一版把乱出来的结构路径（`$.period.k.k.k`）原样记录，噪声太大，改成按 schema 归档。

### B3. 账本

**恒等式一页数学**：CONTRACT §4.4–4.5（成交 / 资金费 / 结算 / 盯市 / 逐仓保证金 / 有效保证金 / 强平线 / 强平价 / 穿仓补偿的公式，8 条检查的不等式与容差 τ）。

**五个边界用例**（`tests/test_ledger.py::TestEdgeCases`，ref 与 alt 都跑、逐根对拍）：

| 用例 | 结果 |
|---|---|
| 多次反手（4 次连续反手，数量 10→−15→25→−30→40→10） | 持仓 / 均价 / 已实现逐根等于手算 |
| 同根开平（买 10 卖 10 同一根；卖 5 买 5 同一根） | 收盘持仓 0、均价 0、浮动 0、现金 == 权益，已实现 15 |
| 数量为 0 的成交 | **拒绝**（`LedgerError`），不是默默跳过——引擎产出零量成交是要暴露的 bug；写进 CONTRACT §4.1 |
| 手续费为 0（含资金费 0） | fees 恒 0，权益 == 现金 == 初始 + 已实现 |
| 强平恰好在边界（PE == MM：多头 100 @ 100、10 倍、mmr 0.1，强平价恰为 100） | 强平与不强平都通过；高 1e-3 强平 → `premature_liquidation`；低 1e-3 不强平 → `I4_missed_liquidation`；alt 同判 |

**第二份实现不许 import 第一份**：`test_second_implementation_does_not_import_the_first` 用 AST 核 `ledger_alt.py` 只导入 `math`；
`indicators_alt.py` 只从 `indicators_ref` 拿 `resolve / LINES / apply_warmup`（口径字符串与参数解析，不含计算）。

### B4. determinism harness

**子进程的 PYTHONHASHSEED 真的不同**：子进程把自己看到的 `PYTHONHASHSEED` 和 `hash("qdcheck-hash-probe")` 一并返回；
**改了什么**：`run_child` 现在对不符的 seed 直接 `HarnessError`（原来只记录）。测试 `test_children_report_the_seed_we_set_and_hash_differently`
（两个子进程报 "0" / "4242"，探针 hash 不同）、`test_child_with_wrong_seed_is_rejected`（mock 子进程把 seed 改成 77 → HarnessError）。

**第三份结果：无关随机性不干扰**：新增 `examples.seeded_engine`——每次调用 `random.seed(f"{pid}:{perf_counter_ns}")` 并做无关的 shuffle，
结果只由 `task["seed"]` 决定；`test_unrelated_process_randomness_does_not_trip_the_harness`：harness 通过，且 seed=5 与 seed=6 的结果确实不同。
证明 harness 只对“哈希顺序 / 结果”敏感。

---

## C. 够硬、够诚实

### C1. 覆盖率 / 突变 / 漏网去向

**覆盖率**：`python3.12 qdcheck/tools/cov.py --fail-under 90` → __COV__（`results/coverage.json`）。`selftest.py` 单列不计入（它只调度各步、在子进程里跑测试），
`vector_schema.py` 计入。

**突变**（最终代码 + 最终测试，13 个模块 × 60 = 780 个，`python3.12 qdcheck/tools/mutate.py --per-module 60 --jobs 4`）：

```
__MUT__
```

**历次漏网逐条去向**（`python3.12 -m qdcheck.tools.triage --replay` → `mutants/triage.md`）：__TRIAGE__

### C2. 性能表

`python3.12 -m qdcheck.tools.perf` → `results/perf.json`（每块独立子进程，峰值内存 = `ru_maxrss`）：

```
__PERF__
```

两个有目标的块（fuzz 2 万次 ≤ 20 s、随机流 200 × 3000 ≤ 3 分钟）都在目标内；没有超目标的项。

### C3. 这套验法抓到过什么

REPORT.md 第 6b 节：G1–G5 与评审期间修过的 14 处缺陷（发现方式 / 现象 / 修法），其中 7 处由突变漏网暴露、3 处由随机流 / 对拍暴露、
3 处由单元测试暴露、1 处由代码走查；评审期间新增 1 处（文档里的手算错，被 Decimal 复算抓到）。

### C4. REPORT 的每个数字可重算

REPORT.md 由 `tools/build_report.py` 从模板 + `results/*.json` 生成，`test_report.test_report_is_generated_from_results` 要求入库的 REPORT.md
与重新渲染逐字节相同；第 0 节列出每个结果文件对应的命令；`python3.12 -m qdcheck.selftest --refresh` 一次重算除突变 / 性能外的全部结果并重建报告。

---

## D. 交付

提交（前缀 `review:`）：__COMMITS__

未改 `qbt/`、`lob/`；无新增依赖；未联网。
