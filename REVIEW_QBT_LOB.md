# REVIEW_QBT_LOB — 第三方复现审核（qbt + lob）

审核员身份：第一次拿到 `qbt/` 与 `lob/` 两个包，目标是在另一台机器上原样复现两份 REPORT 里的每个数字；先复现、再修、再把报告改成实话。
每条记录格式：**检查命令 → 实际输出（原样摘录）→ 结论（通过 / 修了什么 / 报告改了什么）**。

**审核机器（本文件里所有“实际输出”的来源）**：Linux 6.18 x86_64，Intel Xeon @ 2.10GHz，`os.cpu_count()` = 4，16 GB，CPython 3.12.3。
**另一台机器（题面给出的已知事实，本审核无法直接登录）**：macOS / Apple Silicon（arm64），CPython 3.12.15。凡引用该机器的数字均标注“题面事实”。

## 0. 已知事实的处理

| # | 题面事实 | 本审核的处理 |
|---|---|---|
| 0.1 | macOS arm64 上 qbt 177 条里 4 条红：`test_pinned_fingerprints`、`test_golden_file_sanity`、`test_snapshot_matches_golden_bytewise`（三条都是 sha256 钉死在 x86 Linux 的浮点结果上）、`test_cci_vs_bruteforce`（index 1895 相对差 1.1e-8，容差 1e-8 太紧） | 四条全部重写为“形状 + 容差”判据并附放水判据（§1.1–1.3）；在本机用“每个价格 ±1 ulp 交替扰动”模拟另一台机器的 libm 差异，6 个策略仍在容差内且整数字段不变（§1.1 第 4 条） |
| 0.2 | lob/tests 在 macOS 上 384 OK（skipped 1） | 本机同样 384 OK（skipped 1）；契约 / 边界判据落地后 442 OK（skipped 1），见 §2.3；442 条的树未在 macOS 复跑 |
| 0.3 | lob 报告把“排队模型 Brier ≤ 0.08”在整体 0.1472 未达成的情况下标成了“部分达成” | 改为“未达成”（整体 T=1/2/5/10 为 0.1726 / 0.1771 / 0.1472 / 0.1170，全部 > 0.08），达成子集（d=0 四个 T 与 d=1,T=10 共 5 格）与原因单列，不再整体 / 子集混写（§2.6） |
| 0.4 | qbt 报告“1 进程 vs 8 进程结果 JSON 逐字节一致”只在同一台机器成立 | 判据改为形状 + 容差；同机逐字节另列为单独断言；报告 §1 按“平台相关 / 平台无关”逐行重写（§1.3、§1.4） |

## 1. 跨平台与确定性（qbt）

### 1.1 黄金文件 / 指纹 → 形状 + 容差

**检查命令**（原判据，本机 Linux）：`python3.12 -m unittest tests.test_golden tests.test_data -v 2>&1 | tail -3`
**实际输出**：`Ran 35 tests … OK`（本机绿；题面事实：macOS 上 3 条 sha256 判据红）。
**结论**：修了。原判据比较 sha256(原始 double) 与 `repr` 字符串，任何机器的 libm 最后 1 ulp 差异都会让它红，判据本身不可跨机。改动：

- `tests/golden/golden_v2.json`（由 `python3.12 tools/make_golden.py --update` 生成，文件内记录生成命令、GenSpec（含 seed=20240601）、BrokerConfig）：浮点用 JSON 数值（Python `repr`，最短往返表示，≤ 17 位有效数字，读回逐位相同），存 6 个策略的完整权益曲线（3000 点）、全部成交记录、订单顺序、11 项度量、数据前 200 根；文件内另有 `generated_on`（平台 / 架构 / Python / git 提交，无时间戳，同机两次快照仍逐字节相同），`test_format_and_provenance` 要求四个来源字段齐全且非空（缺失 → 红），跨机对比时能知道黄金来自哪台机器；删除 sha256 版 `golden_v1.json`。
- `tests/_compare.py`：形状 + 容差比较器——整数 / 布尔 / 字符串 / None / 列表顺序 / dict 键集合精确（int 与 float 不互换）；浮点 `|a−b| ≤ rel·max(|a|,|b|) + 1e-9·序列量级`；金额 `rel=1e-9`，指标 / 度量 `rel=5e-7`（规格预算 1e-6 的一半，使“改错 1e-6”必红）。
- `tests/test_golden.py`：成交笔数 / 订单数 / 往返数 / 每笔成交的 order_id、bar、side、liquidity、tag / 订单顺序 完全相等；权益、价格、数量、费用 1e-9；度量 5e-7。

**放水判据**（证明容差不是放水）：`python3.12 -m unittest tests.test_golden.TestGoldenTolerance -v`
**实际输出**：`test_float_error_1e6_is_red … ok`、`test_integer_fields_exact … ok`（权益 / 度量 / 单笔价格人为改错 1e-6 → 红；成交笔数 ±1、bar ±1、方向取反、订单顺序互换、maker↔taker → 红；1e-12 噪声 → 绿）。

**同机 vs 跨机两条判据**：
- 同机逐字节：`tests.test_golden.TestGoldenSameMachine.test_two_snapshots_bytewise_identical`（两次快照 JSON 逐字节相同）与 `tests.test_data.TestGeneratorInternals.test_pinned_same_machine_bytewise`（进程内两次 + 一个子进程，sha256 指纹相同）；
- 跨机容差：`tests.test_data.TestGeneratorInternals.test_pinned_cross_machine_within_tolerance`（seed=123 / universe seed=9 的前 200 根与黄金 JSON 对比，时间戳精确、价格 1e-9，附改错 1e-6 必红）；
- 跨机模拟（本机可执行）：`tests.test_golden.TestGoldenCrossMachineSimulation.test_robust_to_one_ulp_price_noise`——把每个价格乘以 (1 ± 2.2e-16) 交替扰动后重跑 6 个策略。
  **实际输出**（直接脚本版）：`bollinger_reversion OK within tolerance … trend_stop OK within tolerance / ulp-noise simulation: PASS`。
  这说明在 1 ulp 量级的输入差异下，本数据集上没有任何比较恰好处在平局点，整数字段不变；但这不是数学保证——若某台机器的差异恰好翻转一个 `f > s` 的平局，整数字段会变，届时判据会如实变红（见 §4 局限）。

### 1.2 「vs 暴力实现」对拍统一相对容差 + 放水判据

**检查命令**：`python3.12 -m unittest tests.test_indicators.TestOscillators.test_cci_vs_bruteforce`
**实际输出**：`OK`
**结论**：修了。CCI = (tp − SMA)/(0.015·MAD) 是抵消型量：SMA 滚动和在 3000 根上的累积舍入 ~n·eps·|tp| ≈ 1e-11，被 0.015·MAD ≈ 0.005 放大到 ~1e-9 绝对，正是 macOS 上 index 1895 看到的 1.1e-8 相对差的来源；原容差 1e-8 相对（无绝对下限）在该点恰好不够。改为指标档 `rel=5e-7` + `1e-9·序列量级` 绝对下限，并加两条放水判据：整体改错 1e-6（相对）必红、单个元素改错 1e-6 必红（`with self.assertRaises(ShapeMismatch)`）。其余 30 余条暴力对拍本来就是 `rel·max(|x|,|y|)` 形式（`tests/_helpers.assertCloseList`，rel=1e-9，滚动和 / 递推的求和顺序差异远小于此），在 macOS 上绿，不动。

### 1.3 「1 进程 vs 8 进程」→ 形状 + 容差；跨 PYTHONHASHSEED

**检查命令**：`python3.12 -m unittest tests.test_optimizer.TestSearch.test_serial_vs_parallel_shape_and_tolerance tests.test_determinism -v`
**实际输出**：`Ran 4 tests … OK`。
**结论**：修了。1 vs 2（逆序提交）vs 8 进程（chunksize=1）改为 `tests/_compare.assert_close`（整数 / 键 / 顺序精确，浮点 1e-9）+ 放水判据（改错 1e-6 必红）；同机逐字节断言单独保留并注明“跨机不要求”。新增 `tests/test_determinism.py`，直接用 `qdcheck.determinism.check`（未改 `qdcheck/`）：本进程 + 两个子进程（`PYTHONHASHSEED=0` / `4242`）各跑一遍 `qdcheck.examples:qbt_backtest`（trend_stop，默认参与上限 0.5）与 qbt 侧的网格搜索 + walk-forward 目标，三份结果形状 + 容差（1e-9）一致；放水判据用 qdcheck 自带的 `set_order_engine`（set 迭代顺序泄漏）与 `noisy_engine(noise=1e-6)`（hash 相关噪声）证明 harness 能抓住。

### 1.4 报告 §1 目标表逐行重写（平台相关性）与性能复测

**检查命令**

```
python3.12 bench/bench.py --repeat 3 --label audit_rerun --out bench/results/05_audit_rerun.json
```

**实际输出**（机器 Intel(R) Xeon(R) Processor @ 2.10GHz，`os.cpu_count()` = 4，CPython 3.12.3，Linux-6.18.44-fc-v140-x86_64-with-glibc2.39，git 71658d9，起跑时 1 分钟负载 0.14，审核代理与其他任务全部结束后；JSON 的 `git_commit` 是起跑时的 HEAD 71658d9，被测代码是当时的工作树 = 之后提交的 7d64152 的 qbt/ 内容，与本报告 §3 第 4 条的既有口径一致）

```
1M 根 × ma_cross 单进程  冷缓存 best-of-3 = 1.941 s（样本 1.941 / 2.058 / 2.073）   热缓存 = 1.039 s（样本 1.297 / 1.211 / 1.039）
                         metrics.compute = 1.17 s   吞吐（冷）515,215 bars/s   同机两次 equity 逐字节相同 = True
2,000 组 × 20,000 根网格 进程池 8 / CPU 4：best-of-3 = 20.1 s（样本 22.268 / 20.181 / 20.081）  实际观察到的最大子进程数 = 8
                         1 进程 best-of-3 = 77.5 s（样本 77.529 / 77.956 / 82.549）  加速比 3.86x  1 vs 8 进程 JSON 同机逐字节相同 = True
1M 根 生成 / 校验 = 1.84 / 0.38 s   SMA / EMA / ATR / ADX = 0.09 / 0.05 / 0.27 / 1.36 s
pass = {'grid': True, 'single_core': True}
历史（04，单次，同一 VM）：冷 1.932 s / 热 0.884 s；网格 8 进程 18.6 s / 1 进程 70.1 s
```

**审核中发现并修掉的口径问题**：第一次 best-of-3 复测得到冷 1.124 s（样本 1.863 / 1.200 / 1.124）≈ 热 1.103 s——第 2 次起「冷」已经不冷：`OHLCV.fingerprint()` 的 sha256 指纹记忆在数据对象上（`_fingerprint`），`INDICATOR_CACHE.clear()` 清不掉它，而 01–04 的单次「冷」数字包含这 ≈ 0.6 s 的指纹计算。已改 `bench.py` 的 cold()：每个冷样本前同时清指标缓存与 `_fingerprint`，`cache_semantics` 写明「冷 = 指纹 + 指标都重算」，那份口径不一致的 JSON 不入库（留在审核草稿目录），下面的数字来自修正后的重跑。

**结论**：通过（目标 2 两项在本机仍达标），报告改了。`bench/bench.py` 补了审核要求的口径：`--repeat N`（best-of-N，样本全部进 JSON），`machine` 记录 CPU 型号 / `os.cpu_count()` / Python 实现与版本 / 完整命令行 / 起跑负载，网格运行期间采样 `multiprocessing.active_children()` 记录实际子进程数；冷 / 热缓存的定义写进 JSON（`cache_semantics`）与报告 §3。`REPORT.md` §1 目标表逐行重写为「判据 / 平台相关？/ Linux x86_64 本机实测 / macOS arm64 / 达成」：确定性拆成「同机逐字节」（平台无关，只在同机比较）与「跨机形状 + 容差」两行；全部计时标「平台相关」，给本机 best-of-3 与历史单次两组数字；macOS 列如实写「未复测 / 未测」。§3 性能表由 01–05 五份 JSON 生成，新增「05 审核复测」列，旧列保留；口径表（机器 / 命令 / 冷热 / 进程数 / 负载）放在表前。


### 1.5 `python3.12 -m qbt.cli test` / `--quick` / 退出码

**检查命令 / 实际输出**

```
python3.12 -m qbt.cli test                 # Ran 262 tests in 23.2s  OK  exit=0（最终树；= unittest discover -s tests -p 'test_*.py'，全量，含 test_perf；契约落地时 258，突变补判据后 262）
python3.12 -m qbt.cli test --quick         # Ran 126 tests in 1.2s   OK  exit=0（limits / golden / broker / engine / boundaries / contract，QBT_FAST_TESTS=1）
python3.12 -m qbt.cli test --nope          # exit=2（修前：parse_known_args 静默吞掉未知参数，退出码 0）
python3.12 -m qbt.cli                      # exit=2
```

**结论**：修了。默认 `test` 跑 `tests/` 全量 discover；`--quick` 跑冒烟子集；退出码 0 / 1 / 2（1 = unittest 汇总有失败或错误，由 `tests/test_cli.py` 用 `unittest.mock` 判据覆盖）写进 README「退出码」，README 的 `--quick` 时长改为本机实测。另外 `tests/test_limits.py` 的第三方 import 白名单补上仓库内的 `qdcheck`、`lob`（原来会把 `import qdcheck` 当第三方）。

### 1.6 策略 / 成交 / 订单字段契约、边界判据、错误码表（qbt）

**检查命令**（契约与判据由审核代理起草，审核员复核、修代码、重跑）

```
python3.12 -I <scratch>/probe.py        # 对 generate/validate、from_csv、Broker.submit/on_bar、Engine/run_backtest、
                                        # metrics.compute、optimizer.*、report.*、indicators.REGISTRY 逐入口灌
                                        # 空数据 / 单根 / 全停牌 / 价格 0・负・NaN・inf / 数量 0・负・NaN / 杠杆 1e6・1e-6 /
                                        # 空参数空间 / 零长度权益，按 QBTError=受控、其他异常=裸 分类
python3.12 -m unittest tests.test_contract tests.test_boundaries tests.test_limits
```

**实际输出**（探针，修复前）：题面列出的边界类别里 4 处裸异常——B10 `OHLCV.from_csv` 非数值 token（裸 ValueError / OverflowError）、B25 `strategies.grid` levels=0（裸 ZeroDivisionError）、B31 `metrics.compute` / `report.write_report` 的 bar_seconds=None / 0（裸 TypeError / ZeroDivisionError）、B35 `optimizer.walk_forward` 空组合（best 返回 None 后被下标，裸 TypeError）；类别之外另有 `random_search(n<0)`（裸 ValueError）、`make_task` 未知策略 / 目标（evaluate 里裸 KeyError）、`ctx.position / target / indicator(symbol=…)` 未知标的（裸 KeyError）。撮合引擎（Broker.submit / on_bar）与 36 个指标在全部边界输入下零裸异常。

**结论**：修了。

- 代码（`qbt/data.py`、`strategies/grid.py`、`qbt/metrics.py`、`qbt/optimizer.py`、`qbt/engine.py`）：上述 7 处全部改为 `QBTError` 子类（DataError / ConfigError / OrderError），错误信息带行号或候选集合；没有改任何既有判据的期望值。
- 契约：`qbt/CONTRACT.md` 9 节 26 张表，每张表前有 `<!-- contract-table: NAME -->` 标记——§1 策略接口（何时被调、能看到的历史、不能看到的未来、`ctx` 公开方法签名），§2 Order 14 字段 / Fill 9 字段 / Position 4 / Trade 9 / Result 18 / summary 12，§3 订单类型 × 状态 × reason 12 行与费用 / 滑点 / 资金费符号约定 13 条，§4 错误契约（5 个异常类、17 个入口 → 异常 / 状态 → 判据位置），§6 数据格式，§7 结果 JSON 键，§8 指标 36 个，§9 边界 → 判据 50 行（B01–B50）。
- 判据：`tests/test_contract.py` 29 条，每张表一条与代码对照（`__slots__` 顺序、dataclass 字段与默认值、`_reject` / `CANCELLED` 字面量、`validate` 里 8 条 `raise DataError` 逐条对应、指标注册表与输出列数、metrics 29 个键、sqlite 表结构）；符号约定 13 条各用数值场景核过（市价买 + 滑点 / 卖 − 滑点、止损 max(open,p)×(1+slip)、maker/taker 费率、多头付正资金费、负现金 = 负债、三条恒等式）。`tests/test_boundaries.py` 43 条，一入口一条，带 `# contract: Bxx` 标注，裸异常即红；B48–B50 是审核员补的（random_search 负 n、make_task 未知策略 / 目标、ctx 未知标的）。
- 放水判据：`TestMeta.test_deleting_any_row_turns_red` 对 26 张表各删首行 / 末行写临时副本，在进程内重跑对应判据并断言变红（常驻）；审核代理另在临时副本上做了 4 种破坏（删 Order 表 remaining 行；Fill 表加幻影字段 venue = 代码少字段；删 §9 末行；把 reason「short not allowed」改成「short_not_allowed」），各自只让对应那条判据红。`test_contract` 还核对 §9 编号连续、行数 = `N_BOUNDARY_ROWS`、每个引用可 import、Bxx 标注集合与表一一对应、「待修」行 ⇔ `expectedFailure`——修完代码后 §9 已无「待修」行、判据上已无 `expectedFailure`（修前 4 条 expectedFailure 若不删会以 unexpected success 变红）。
- 复跑：`python3.12 -m unittest tests.test_contract tests.test_boundaries` → `Ran 72 tests … OK`；受影响模块 `tests.test_data tests.test_metrics tests.test_strategies tests.test_engine tests.test_optimizer tests.test_limits tests.test_golden` 全绿；`python3.12 -m qbt.cli test` → 契约落地时 **258 条 OK**，收尾最终树 **262 条 OK，23.2 s**（见 §3）。
- 未来函数判据：题面要求的「篡改 i ≥ cut 的 K 线，[0,cut) 的信号 / 成交逐条不变」已有 `tests.test_engine.test_future_tamper_leaves_history_unchanged`（6 个策略）与 `tests.test_indicators.test_causality_future_tamper`，CONTRACT §1.3 直接引用，未重复。契约如实写明 `ctx.indicator` 返回全序列预计算 list，strict 模式只守卫六列不守卫它，由上述两条判据兜底。
- README：顶部「怎么把我接到你的系统（三行）」（数据格式 → 策略接口 → 读结果 JSON），「退出码」小节，`--quick` 时长改为本机实测。


### 1.7 突变 / 覆盖率 / 模糊的本机复测（qbt）

**检查命令 / 实际输出**

```
python3.12 mutants/run_mutations.py --per-module 80 --jobs 3 --out mutants/results_audit.json --md mutants/survivors_audit.md \
    --tests "-f tests.test_golden tests.test_indicators tests.test_broker tests.test_engine tests.test_metrics tests.test_optimizer \
             tests.test_strategies tests.test_data tests.test_report tests.test_cli tests.test_limits tests.test_perf \
             tests.test_contract tests.test_boundaries tests.test_determinism"          # 8 模块 × 80，seed 0，real 9m19s
  第一轮：640 / 抓住 600 / 漏网 40 / 93.8%（broker 80/80、cli 72/80、data 80/80、engine 79/80、indicators 78/80、metrics 70/80、optimizer 64/80、report 77/80）
  40 条漏网逐条阅读：31 条等价（格式 / 日志 / 默认值不可观察 / 冗余守卫 / 代数恒等式 Σ(x+m)(x−m)=Σ(x−m)² / sqlite DDL 自动提交等）、
  9 条判定为真缺口 → 补 4 条判据（test_cli --quick 装载集合、test_metrics periods_per_year(1) 与 var_cvar 默认 alpha、
  test_metrics 纪元边界 1600/2000/2400-02-27..29、test_optimizer 文档默认值）
python3.12 mutants/run_mutations.py --ids cli:1129:binop cli:183:delete_stmt cli:184:delete_stmt metrics:784:const:1 \
    metrics:263:const:0.07500000000000001 metrics:1444:const:146095 optimizer:346:const:2 optimizer:348:const:0 optimizer:348:const:2 \
    --per-module 80 --jobs 2 --timeout 600 --out mutants/results_audit.json ... （同一测试串；结果合并进同一 JSON）
  8 条抓住，metrics:1444:const:146095 仍漏网 → 重新推导：doe//146095 与 doe//146096 只在 doe=146095 不同，此时 (145998 vs 145997)//365 都是 399，等价（判据保留）
  合并后：640 / 抓住 608 / 漏网 32 / **95.0%**（`mutants/results_audit.json`；历史 640 / 596 / 44 / 93.1% 在 `mutants/results.json`）
python3.12 tools/cov.py --fail-under 90 --json results/coverage_audit.json
  Ran 262 tests in 23.1s  OK (skipped=3)   TOTAL 1950 语句 / 漏 0 / 100.0%（历史 1915/1918 = 99.8%，177 条）
python3.12 fuzz/fuzz.py --iterations 100000 --seed 0 --workers 2 --out fuzz/results_audit.json
  100,000 次 / 0 崩溃 / 4.97 s；分类计数与历史只差 engine:ConfigError 4409→4414、engine:ok 13418→13413（grid 参数校验把 5 个输入改判为受控拒绝）
python3.12 -m unittest discover -s qdcheck/tests -t .      # Ran 164 tests in 34.8s  OK（qdcheck/ 未改；origin 上另一会话推送的 qdcheck G5 收尾已合入）
```

**结论**：通过（≥ 85% / ≥ 90% / 零崩溃），报告改了。源码改过的 5 个模块候选集已变，突变 id 与历史不可逐个对应（`broker` / `indicators` / `report` 可比：历史 broker 的 7 条漏网全部被新判据抓住）；REPORT.md §4 新增「4.1 本机复测」（历史 / 本机两列的模块表 + 32 条漏网逐条表：id、行、变异、为什么等价 / 补了哪条判据；3 条 optimizer 独立 CLI 默认值如实标「未补判据」），历史表与漏网清单整体保留为 4.2；§1 3a/3b/3c 的实测列写两组数字（历史 / 本机）；§5 加本机复测段。没有删除或放松任何判据；所有新判据都是增加的。

## 2. lob

### 2.1 事件流可复现（平台无关）

**检查命令**：`python3.12 -m lob.cli gen --n 10000000 --symbols 8 --seed 12345 --out lob/data/events_10m.bin`
**实际输出**：`wrote lob/data/events_10m.bin: 10000000 events in 54.8s, sha256=fc064598e6320a01fdebe3136f972fa835fc5d78c6c3ac0d35920dd0d0e6919d`；`counts: {'new': 5298812, 'cancel': 3988498, 'modify': 692731, 'bad': 19959} regimes: 2294`（文件 560,000,000 字节 = 10,000,000 × 56）。
**结论**：通过。与 lob/REPORT.md §4.2 引用的流 sha256 `fc064598…` 完全一致——生成器只用整数与显式 seed 的 `random.Random`，在 Linux x86 上逐字节复现；这是 lob 里唯一可以跨平台“逐字节”核对的数字，其余吞吐 / 延迟 / RSS 全是平台相关。

### 2.2 1,000 万事件 lockstep 对拍（平台无关的整数结果）

**检查命令**

```
python3.12 -m lob.cli compare --stream lob/data/events_10m.bin --processes 4 \
    --json <scratch>/compare_10m_rerun.json      # --check 默认 1000
```

**实际输出**（`lob/data/events_10m.bin` 为 §2.1 复现的流，sha256 与报告一致）

```
events = 10000000   trades = 1820774   trades_checked = 1820774   volume = 14206029
first_divergence = null   resting_orders = 6632   full_checks = 10004
seconds = 219.9   shard_seconds = [196.0, 219.9, 182.7, 196.2]   shards = [[0,4],[1,5],[2,6],[3,7]]
```

**结论**：通过。成交笔数 1,820,774、成交量 14,206,029 lot、首次分歧「无」与 `lob/REPORT.md` §1 行 1 / §4.1 第三行逐字相同（整数，平台无关）。两处差异都是口径而非结果：全扫描次数 10,004（CLI 默认 `--check 1000`）vs 报告的 5,006（`Compare10MTest` 用 `check_every=2000`）；耗时 220 s（本机 Xeon 2.10GHz，4 进程，另有两个审核代理在跑测试）vs 报告的 645 s（原 VM，不变量常开 + 更密的扫描）——耗时是平台相关数字，`lob/REPORT.md` §4.1 的「秒」列已注明来源机器并加本机复测值（见 2.5）。

### 2.3 测试套件（standard 规模）与 `lob.cli test` / `--quick` / 退出码

**检查命令 / 实际输出**

```
python3.12 -m lob.cli test                 # 审核代理（改契约前）：Ran 384 tests in 84.8s  OK (skipped=1)  exit=0
python3.12 -m lob.cli test                 # 审核员（契约 / 边界判据落地后）：Ran 442 tests in 85.4s  OK (skipped=1)  exit=0；最终树（+1 条 queue_model 判据）：Ran 443 tests in 83.8s  OK (skipped=1)
python3.12 -m lob.cli test --quick         # Ran 87 tests in 8.4s  OK  exit=0（LOB_TEST_SCALE=quick，只跑 test_book/test_events/test_cli/test_boundaries/test_contract）
python3.12 -m lob.cli test --nope          # exit=2        python3.12 -m lob.cli test --quick --scale full   # exit=2
LOB_TEST_SCALE=bogus python3.12 -m lob.cli test   # exit=2   python3.12 -m lob.cli   # exit=2
```

**结论**：通过 + 修了两处。384 OK / skipped 1 与报告 §8 一致（被跳过的唯一测试是 `Compare10MTest.test_10m_sharded`，需 `LOB_TEST_SCALE=full`）；现树 443 条 = 384 + 58 新判据（`test_boundaries` 35 + `test_contract` 23）+ 1（`test_queue_model` 突变补判据）。修了：(1) 旧 `cmd_test` 用 `--scale` 默认值 `standard` 覆盖了环境变量，README 写的 `LOB_TEST_SCALE=full python3.12 -m lob.cli test` 过去实际跑的是 standard——现在 `--scale` 缺省时尊重 `$LOB_TEST_SCALE`，README 改为 `--scale full` 并注明等价写法；(2) 新增 `--quick`，非法组合 / 非法环境变量 → 2，unittest 非零（含一个都没跑到的 5）→ 1，写进 `lob/README.md`「退出码」。注意：standard 套件每次都会用 `test_compare.save()` 改写 `lob/results/compare_10k.json` / `compare_1m.json` 的 `seconds` / `shard_seconds`（只有计时字段），审核结束前按 git 恢复（见 §3）。

### 2.4 模糊器（两轮 20 万次）

**检查命令 / 实际输出**

```
python3.12 -m lob.cli fuzz --n 200000 --json <tmp>/lob_fuzz.json                      # seed 2024, bad-rate 0.3（CLI 默认）
  events 200000  full_checks 2001  crashes []  invariant_violations []  n_divergences 0  trades_checked 38359  rejects 63383  seconds 2.06
python3.12 -m lob.cli fuzz --n 200000 --seed 2025 --bad-rate 0.6 --json <tmp>/lob_fuzz_bad60.json
  events 200000  full_checks 2001  crashes []  invariant_violations []  n_divergences 0  trades_checked 43148  rejects 92072  seconds 18.08
```

**结论**：通过。与 `lob/results/fuzz_200k.json` / `fuzz_200k_bad60.json` 除 `seconds`（3.53 → 2.06、32.9 → 18.08，平台相关）外**逐字段相同**（含 `status_counts` 全部 18 个状态码计数）；报告 §6 表两行逐项复现；「11 种拒绝码全部出现」核实为 `lob/common.py` 的 REJ_KIND…REJ_UNKNOWN 共 11 个键在两份 `status_counts` 中均非零；§1「400000 次（2 轮）」= 两份 JSON 的 events 之和。

### 2.5 突变测试（book / queue_model）

**检查命令 / 实际输出**（审核代理脚本 `mut_summary.py`：核对 `mutations_book.json` / `mutations_queue_model.json` / `*_run1.json` / `mutation_triage.json` / 报告漏网表，并用项目自身的 `run_mutations.make_mutants + sample_mutants(…, 120, 1)` 重算候选与抽样）

```
book:        candidates 484  run 120  killed 110 (= 95 killed + 15 timeout)  survived 10  kill_rate 91.7   recomputed killed = 110
queue_model: candidates 292  run 120  killed 115  survived 5  timeouts 0  kill_rate 95.8
recomputed candidates = 484 / 292 | sampled ids == JSON ids: True / True | desc/line mismatches: 0
triage classes: {'补判据': 45, '等价': 16}；report survivor rows: 15 = 10 book + 5 queue_model
按 id 复跑（run_mutations.make_mutants + run_one）：book 169/263/458 仍漏网、22/27/34 仍抓住；queue_model 124/169 仍漏网（8/12 条完成，与记录逐条相同）
```

**结论**：通过 + 修了两处 + 重跑。报告 §6.x 的 484/120/110/10/15/91.7% 与 292/120/115/5/0/95.8% 与 JSON 一致，抓住数可由状态计数重算；15 条漏网 id 在报告表与 triage 中一一对应；seed=1 抽样的 120 个 id / 改动 / 行号在本机重算后逐个相同（突变器确定性成立）；triage 里 `queue_model:235` 标「等价」但最终状态是 killed（复跑后被抓住），报告漏网表不含它，无数字影响。修了：(1) `lob.cli mutate` 此前没有按 id 复跑的入口（审核代理只能直接调用 `run_one`），现加 `--ids`（`run_mutations.py` / `cli.py`），JSON 里记录 `seed` / `ids` / `tests`；(2) `TARGETS['book']` 的判定集补入 `test_boundaries` / `test_contract`。因 `queue_model.py` 在边界修复中多了一个条件表达式（候选集变化）、book 的判定集扩大、突变 `seconds` 本来就是原 VM 的历史数字，两个模块在本机**全部重跑**（见 2.9），旧结果保留在「历史」列。

### 2.6 排队模型「Brier ≤ 0.08」

**检查命令 / 实际输出**（审核代理脚本 `qm_check.py`：用 `lob/results/queue_model/queue_model_eval.json` 重算 §5.x 全部表并与 REPORT.md、`queue_model_summary.json` 比对）

```
probes 192918 = train 135042 + test 57876（True）；各 T 的 n = 57876 − censored_test（182/278/574/898）精确成立
| T=1  | n 57694 | 整体 Brier 0.1726 | d=0 0.0751 | d=1 0.1655 | … |   normal 0.1752 / shock 0.1942 / withdraw 0.1670
| T=2  | n 57598 | 0.1771 | d=0 0.0644 | …
| T=5  | n 57302 | 0.1472 | d=0 0.0504 | …
| T=10 | n 56978 | 0.1170 | d=0 0.0411 | d=1 0.0732 | …
summary.json == eval.json for h=1/2/5/10: True
```

**结论**：**目标未达成**，报告改了。整体 Brier 在 T=1/2/5/10 为 0.1726 / 0.1771 / 0.1472 / 0.1170，全部 > 0.08；达标的只有 5 格：d=0 的 T=1/2/5/10（0.0751 / 0.0644 / 0.0504 / 0.0411）与 (d=1, T=10)=0.0732。原报告 §1 写「部分达成：触价档 ≤ 0.08，整体未达」把子集和整体写在一句里，`lob/REPORT.md` 现改为：§1 第 3 行结论「未达成」，实测列分三句（整体四个 T 均 > 0.08；达成子集 5 格逐格列出；未达成 11 格逐格列出），§5 结论同样改写并给原因 4 条（特征只见 t0 的队列位置与强度、shock 段比 normal 差 0.02–0.06、删失不是原因——影子单只有流结束删失 1.6%、合成数据）。顺带修正：手抄的 by_d / regime 表 4 格与 JSON 的 5 位值舍入不一致（0.2309 vs 0.23085 等），改为由 JSON 生成；§5「校准方法」的桶键描述是旧版本（(min(d,3), u 桶 0.25…16)），改为与代码一致的 (min(dist,8), 13 个 u 桶 0.1…16, 失衡 3 桶, 价差 3 桶)；§7 第 3 条「探针是真实挂单」改为影子单；原文「常数预测在 d≥1 子集 > 0.2」「8 组变体把 T=1 从 0.183 降到 0.173」在 `results/` 里没有 JSON（探针 CSV 未入库），标为「本次审核未能复核，不作为依据」。没有改模型代码，没有改判据阈值。

### 2.7 事件二进制布局 / 状态码表 / 字段表契约、边界判据（lob）

**检查命令 / 实际输出**

```
PYTHONPATH=. python3.12 <scratch>/probe.py           # 每个入口灌空 / 单条 / 全拒 / 0 / 负 / 2^63-1 / 重复 oid / 未知 symbol-kind-side /
                                                     # 越带 / 未知 oid 改撤 / FOK 不可成 / POST_ONLY 交叉 / 冰山 display 0 / 空流回放对拍 / 零探针排队模型 / BookConfig 组合
python3.12 -m unittest lob.tests.test_limits lob.tests.test_contract lob.tests.test_boundaries   # OK（23 + 35 + 2）
grep -v '^| 19 | REJ_FOK ' lob/CONTRACT.md > <red>/c.md && LOB_CONTRACT_PATH=<red>/c.md python3.12 -m unittest lob.tests.test_contract   # 红（同法删 E27 行、F_AUX 行各验一次）
LOB_MUTANT_MODULE=lob.common LOB_MUTANT_SRC=<red>/common_no19.py python3.12 -m lob.mutants.loader lob.tests.test_contract   # 红（同法 events.RECORD 改 '<QQHBBBBHQqqi'、book.COMPACT_MIN_DEAD 改值）
```

**结论**：修了。撮合引擎本身零裸异常（所有畸形 NEW / CANCEL / MODIFY 都是 REJ_*）；裸异常全在外围入口，各做最小修复：`events.pack` 字段越界 → ValueError；`iter_chunks` 负 start / stop → ValueError；`GenParams.validate()`（n_events ≥ 0、1 ≤ n_symbols ≤ 65536、rate > 0、depth_scale > 0、tick / lot ≥ 1、1 ≤ min_qty ≤ max_qty ≤ (2^63−1)//1000 且 lot 倍、0 ≤ band ≤ 2^60、1 ≤ base_price ≤ 2^60、1 ≤ n_traders ≤ 65536、target_resting ≥ 1、整数字段必须 int）→ ValueError；`replay._check_stream` 校验文件记录数 == meta.n_events 且长度为 56 倍、`Core.run` 把 KeyError / IndexError 定位到首个坏记录后改抛 ValueError（热路径零开销，出错后才慢扫一次）、`replay_sharded` 处理数 ≠ n 时删分片临时文件并抛 ValueError；`queue_model.run` 测试集为空时 SVG 标题写 'Brier n/a'；`BookConfig.from_dict` 缺键 → ValueError。`lob/CONTRACT.md` 24 张带标记的表：56 字节记录布局（`'<QQHBBBBHQqqq'`，逐字段偏移 / 长度）、kind / side / flags 枚举、17 行标志组合穷举、meta 键、18 行状态码表（0–6 成功、10–20 拒绝，含触发输入与计数器效应）、NEW 校验顺序、`O_*` / `Q_*` / `COMPACT_*` / `T_*` 槽位、`trades.bin` 48 字节布局、BookConfig 字段 / 默认值 / 8 条 ValueError 示例、replay / compare 输出文件与 JSON 键、分歧字典、E01–E35 错误契约表（含两类按原类型保留的：文件不存在 → FileNotFoundError，GenParams 未知参数名 → KeyError，既有判据不许弱化）。`test_contract.py` 23 条逐表对照代码（struct 格式 / 偏移 / 长度、`dir()` 前缀常量集合相等、`flags_valid` 逐行、校验顺序用两两违反构造在两引擎上核对、输出键集合用真实 replay / compare 结果核对、错误表编号与 `test_boundaries` 的 `# contract: Exx` 标注双向一一对应）；`test_boundaries.py` 35 条一入口一条，裸异常即红。放水判据：文档少一行必红（三张不同的表各验一次）、代码少一个常量 / 改一个值必红（用既有 `lob.mutants.loader` 装进草稿副本，真源码未动）。契约如实写明两处不检测：纯模式 replay 按记录位置写 status 不读 seq、seq=0 任何模式都不检测；compare 对未知 symbol 的记录静默过滤。既有判据一条未删未改。

### 2.8 覆盖率 97.40% 的复现

**检查命令 / 实际输出**

```
python3.12 -m lob.cli coverage --json <tmp>/lob_cov.json        # 审核代理，修改前
  Ran 384 tests in 101.2s  OK (skipped=1)   TOTAL core 95.49  (报告 97.40)   TOTAL extras 54.04
  逐模块与报告逐行一致，唯一差异 events.py 258/308 = 83.8%（报告 305/308 = 99.0%）
  events.py 本机额外未覆盖行：14–42（模块级语句）、60…410（生成器路径）
```

**根因与修复**（审核员）：两条推测都成立，并已实验验证。(1) `lob/cli.py` 顶部 `from lob import events as E`——经 `python3.12 -m lob.cli coverage` 入口时 `events.py` 的模块级语句（14–42 行）在采集器启动前就执行完了，`sys.monitoring` 看不到；报告的 97.40% 是用 `python3.12 -m lob.tools.coverage` 直接入口量的，两种入口给出不同数字。(2) 生成器路径（60…410 行）只在套件内重新生成 `lob/data/events_1m.bin` 时执行，本机该文件已存在，`test_compare.stream()` 不再调用 `E.generate`——覆盖率取决于缓存状态。修复 `lob/tools/coverage.py`：启动采集器前把已 import 的被测模块从 `sys.modules` 移除（`_forget_measured_modules`，测试的 import 在监控下重新执行）；并把流文件目录指到临时目录（新增 `LOB_DATA_DIR` 环境变量，`lob/cli.py` 与 `lob/tests/test_compare.py` 尊重它），每次覆盖率运行都重新生成 1 万 / 100 万流，生成器路径稳定计入，跑完删除。

```
python3.12 -m lob.cli coverage --json lob/results/coverage_audit.json      # 修复后，本机（收尾最终树）
  Ran 443 tests in 93.2s  OK (skipped=1)   TOTAL core 96.48   TOTAL extras 43.19   real 1m33s
  events.py 323/326 = 99.1%（漏 [65, 243, 424] 三行，与报告的 305/308 同为 3 行）
  与 results/coverage.json 的逐模块差异全部来自本审核新增的代码：cli.py 201/222（报告 191/204；新增的 mutate --ids/--merge/--timeout、coverage、test 子命令分支在进程内套件里不执行），
  replay.py 215/220（189/192）、stress.py 120/126（119/121；macOS 分支），其余模块逐行相同或 100%
```

**结论**：修了，报告改了。修复后 `events.py` 与报告一致；核心合计 96.48%（≥ 90% 达成）低于报告的 97.40% 是因为边界修复新增的防御分支（`replay._first_bad_record` 的兜底 `raise`、`stress.machine_info` 的 macOS 分支等）没有全部被判据执行到，不是测量口径问题。`lob/REPORT.md` 6.y 覆盖率表保留原 VM 的 97.40%（标「历史」）并加「本机复测（采集器修复后）」列 96.48%，注明两种入口此前给出不同数字的原因。



### 2.9 本机复测：压测、突变、覆盖率（平台相关数字的「历史 / 本机」两列）

**检查命令 / 实际输出**（压测，平台相关；机器 Intel(R) Xeon(R) Processor @ 2.10GHz，os.cpu_count()=4，CPython 3.12.3，Linux-6.18.44-fc-v140-x86_64-with-glibc2.39，起跑负载 1.07，审核代理全部结束后，1 进程）

```
python3.12 -m lob.cli stress --stream lob/data/events_10m.bin --out lob/data/stress --label audit_rerun --json lob/results/stress_audit_rerun.json
engine-only      best-of-3 = 720,901 ev/s   样本 637171 / 718887 / 720901        （历史 r4 原 VM：424,678，样本 333193 / 424678 / 372537）
engine+checker   best-of-3 = 539,614 ev/s   样本 523647 / 539614 / 478932        （历史：309,381）
端到端 replay     328,583 ev/s  单次  trades 1,820,774  volume 14,206,029  RSS 46 MB      （历史：213,568，46 MB）
Top-10 快照       0.91 µs/事件                                      （历史：1.95）
延迟 p50/p99/p999 1.2 / 7.5 / 33.7 µs                         （历史：2.0 / 13.5 / 53.7）
tracemalloc 峰值  12.5 MB                                                 （历史：12.5）
流 sha256 fc064598e6320a01… == 历史 fc064598e6320a01… : True   e2e trades/volume == 历史: True
```

**结论（压测）**：只记录，不与历史比较。本机是另一台 VM（CPU 标称 2.10GHz 却比原 VM 快 1.5–1.7 倍，说明 CPU 代际 / 负载不同），所以「本机复测」列只回答「在这台机器上是否达标」：纯引擎 720,901 ev/s ≥ 30 万（达成），engine+checker 539,614（达成），端到端 328,583 ≥ 30 万（**本机达成；原 VM 213,568 未达成，历史结论保留**）。平台无关的量全部一致：同一份流（sha256 相同）、端到端成交 1,820,774 笔 / 14,206,029 lot 与历史和 2.2 的对拍一致。`lob/REPORT.md` 改了什么：开头「机器」行与 4.2「历史数字与本机复测」表的复测列由 `results/stress_audit_rerun.json` 生成（机器 / Python / 命令 / best-of-N 样本 / 冷热 / 进程数 / 起跑负载），§1 2a/2b/2c 的实测列分「历史·原 VM」与「本机」两句，结论按两台机器分别写。

**检查命令 / 实际输出**（突变本机重跑；`python3.12 -m lob.cli mutate --target <book|queue_model> --max 120 --seed 1 --workers 4 --json lob/results/mutations_<t>_audit.json`，判定集 = 历史 + `test_boundaries` / `test_contract`，与 qbt 突变 / 覆盖率等并行时负载 2–4）

```
book: candidates 484（历史 484）  run 120  killed 110（timeouts 15）  survived 10  kill_rate 91.7%（历史 91.7%）  seconds 1532.3
  本机漏网 id [169, 210, 263, 276, 284, 297, 307, 431, 458, 459]；历史漏网 id [169, 210, 263, 276, 284, 297, 307, 431, 458, 459]；历史漏网中本机被抓住 []；本机新增漏网 []（候选集相同时 id 可比；queue_model 源码已改，id 不可比）
queue_model: candidates 295（历史 292）  run 120  killed 118（timeouts 0）  survived 2  kill_rate 98.3%（历史 95.8%）  seconds 587.2
  本机漏网 id [125, 189]；历史漏网 id [124, 169, 176, 188, 211]；历史漏网中本机被抓住 [124, 169, 176, 188, 211]；本机新增漏网 [125, 189]（候选集相同时 id 可比；queue_model 源码已改，id 不可比）
book    real 25m46s（超时 15 条的 id 与历史完全相同——都是循环条件被改成死循环；本机非超时最慢 108.4 s < 300 s 超时阈，历史最慢 192.9 s，故 300 s 不会造成假抓住；10 条漏网 id 与历史完全相同，历史判定（等价）逐条沿用，见 mutation_triage_audit.json）
queue_model  第一轮 real 9m56s：295 候选 / 120 / 114 / 6 / 95.0%；6 条逐条判定：125（SVG 刻度 range 上界）、189（空 rel 的 nmax 默认值）等价，
        195（SVG 标题 Brier 文本）、247（by_regime base_rate 四位舍入）、267（parametric_params 键集合）、272（_horizon 最近表）为真缺口 → 补 4 条判据后
        python3.12 -m lob.cli mutate --target queue_model --ids 195 247 267 272 --merge --workers 2 --json lob/results/mutations_queue_model_audit.json  → 4 条全部抓住，合并后 118 / 2 / 98.3%
```

**结论**：通过（book 91.7% 与历史逐条相同，queue_model 98.3% ≥ 85%），报告改了。`lob/REPORT.md` 6.x 突变表与漏网表分「历史（原 VM）」「本机复测」两块（复测块的逐条判定读 `mutation_triage_audit.json`），§6 结果段加本机复测段落（机器 / workers / 命令 / 判定集 / 为什么两列不可按 id 直接比较）。修了：`lob.cli mutate` 新增 `--ids` / `--merge` / `--timeout`（此前无法按 id 复跑、也无法把复跑结果合并进原 JSON）。没有删除或放松任何判据。



### 2.10 「382 tests OK，788.5 s」与「日志摘要见提交 9fd7864」

**检查命令 / 实际输出**

```
git archive 0be998f~1 / 0be998f / 9fd7864 / 3500a1c → unittest.TestLoader().discover('lob/tests').countTestCases()
  0be998f~1: 382    0be998f（+2 条 queue_model 判据，21:13:34 提交）: 384    之后一直 384
git show 9fd7864 → 提交信息只有标题行
```

**结论**：解释清楚，报告改了。788.5 s ≈ 13 分钟的全量运行必然始于 ≈21:02，即 0be998f 之前的 382 条树；现树（契约前）384 条，standard 下 383 跑 + 1 skip。`lob/REPORT.md` §8 改为：382 OK / 788.5 s 是原 VM 的 full 规模与阶段 7 时的树；full（含 1,000 万对拍）本次审核未重跑；新增本审核 VM 的 standard 结果（442 OK / skipped 1 / 85.4 s）与审核员 macOS arm64 的 384 OK（题面事实，契约前的树）；「日志摘要见提交 9fd7864 说明」不成立，已删。

### 2.11 报告可追溯性与工程约束

```
python3.12 -m lob.tools.lint_limits            # 37 个文件（阶段 7）/ 审核时 39 个，0 个问题
python3.12 -m unittest lob.tests.test_limits    # OK
(cd <old 3500a1c 导出> && python3.12 -m lob.tools.build_report) | diff - lob/REPORT.md     # 逐字节相同（改写前）
python3.12 -m lob.tools.build_report | cmp - lob/REPORT.md     # 逐字节相同（改写后，二次生成）
```

**结论**：通过。`lob/REPORT.md` 是 `python3.12 -m lob.tools.build_report > lob/REPORT.md` 的生成产物，§1、§2.2、§4.1、§4.2、§5.x、§6.x、漏网表、模糊表、覆盖率表全部可追溯到 `results/*.json`；此前手写的 by_d / regime 两张表改为 JSON 生成；§8 引用的阶段提交哈希全部存在。本审核把两个工具文件（`build_report.py` / `report_tables.py`）拆成 ≤ 60 行的辅助函数，目标 3 的结论改为由 JSON 计算（整体任一 T 的 Brier > 0.08 即「未达成」），`lint_limits` 与 `test_limits` 均通过。


## 3. 本机全量复跑记录

机器：Intel Xeon @ 2.10GHz 虚拟机，`os.cpu_count()` = 4，Linux 6.18.44-fc-v140 x86_64，CPython 3.12.3，git 40b5089（审核提交前的 HEAD；审核提交见 git log）。所有命令在仓库根执行，日志在 `out/`（已 gitignore）。

| 命令 | 实际输出 | 结论 |
|---|---|---|
| `python3.12 -m qbt.cli test` | Ran 262 tests in 23.189s，OK，exit=0 | 通过（退出码 0） |
| `python3.12 -m qbt.cli test --quick` | Ran 126 tests in 1.200s，OK，exit=0 | 通过 |
| `python3.12 -m lob.cli test`（standard） | Ran 443 tests in 83.769s，OK (skipped=1)，exit=0 | 通过（退出码 0；`LOB_TEST_SCALE=full` 含 1,000 万对拍未在本机重跑，10M 对拍另由 2.2 的 CLI 命令复现） |
| `python3.12 -m lob.cli test --quick` | Ran 87 tests in 8.4s，OK，exit=0 | 通过 |
| `python3.12 -m unittest discover -s qdcheck/tests -t .` | Ran 164 tests in 34.754s，OK，exit=0 | 通过（`qdcheck/` 未改） |
| `python3.12 -m unittest tests.test_limits` / `python3.12 -m lob.tools.lint_limits` | Ran 3 tests OK；39 files checked, 0 problems | 通过：函数 ≤ 60 行、文件 ≤ 800 行、仅标准库、随机显式 seed |
| `python3.12 bench/bench.py --repeat 3 --label audit_rerun --out bench/results/05_audit_rerun.json` | 冷 1.941 s（样本 1.941/2.058/2.073）/ 热 1.039 s；网格 8 进程 20.1 s（样本 22.3/20.2/20.1，实际子进程 8）/ 1 进程 77.5 s；pass={'grid': True, 'single_core': True}；real 5m35.553s | 通过（≤ 3 s / ≤ 90 s），见 1.4 |
| `python3.12 tools/cov.py --fail-under 90 --json results/coverage_audit.json` | 262 tests，100.0%（1950/1950），real 0m23.263s | 通过（≥ 90%） |
| `python3.12 mutants/run_mutations.py --per-module 80 --jobs 3 --out mutants/results_audit.json …` | 640 突变体 / 抓住 608 / 漏网 32 / 95.0%，real 9m19.010s | 通过（≥ 85%）；漏网逐条判定见 REPORT.md §4 |
| `python3.12 fuzz/fuzz.py --iterations 100000 --seed 0 --workers 2 --out fuzz/results_audit.json` | 100,000 次 / 0 崩溃 / 5.0 s | 通过（0 次未捕获异常） |
| `python3.12 -m lob.cli stress --stream lob/data/events_10m.bin --out lob/data/stress --label audit_rerun --json lob/results/stress_audit_rerun.json` | engine-only 720,901 / engine+checker 539,614 / 端到端 328,583 ev/s，real 1m24.221s | 见 2.9（平台相关，只记录不判达标） |
| `python3.12 -m lob.cli mutate --target queue_model --max 120 --seed 1 --workers 4 --json lob/results/mutations_queue_model_audit.json` | 候选 295 / 抽样 120 / 抓住 118 / 漏网 2 / 98.3%，real 9m55.782s | 通过（≥ 85%） |
| `python3.12 -m lob.cli mutate --target book --max 120 --seed 1 --workers 4 --json lob/results/mutations_book_audit.json` | 候选 484 / 抽样 120 / 抓住 110（含超时 15）/ 漏网 10 / 91.7%，real 25m46.263s | 通过（≥ 85%） |
| `python3.12 -m lob.cli coverage --json lob/results/coverage_audit.json` | 443 tests OK (skipped=1)，核心 96.48%，93.2 s | 通过（≥ 90%），见 2.8 |
| `python3.12 -m lob.tools.build_report \| cmp - lob/REPORT.md` | identical | 通过：报告是生成产物 |
| 占位符检查：`grep -c` 填充 token（双角括号）/ HTML 注释占位 / 「待本机复测」 | REPORT.md 0 / 0；REVIEW_QBT_LOB.md 0 / 0（1.6 提到的 `contract-table` 标记是契约文件自身的表格标记，不是占位）；lob/REPORT.md 0 | 通过：无遗留占位符 |

提交信息更正：审核中间提交 71658d9 的信息写「判据数 182」，实际加载计数是 186（177 + test_data 1 + test_golden 5 + test_determinism 3）；历史已推送不改写，在此更正。

`lob/results/compare_10k.json` / `compare_1m.json`：standard 套件每次运行都重写其 `seconds` / `shard_seconds`（只有计时字段），审核收尾时按 `git checkout` 恢复为已提交版本后再生成 `lob/REPORT.md`，因此 4.1 表的「秒」列仍是原 VM 的历史数字（本机值见 2.2 / 2.5 的对拍命令输出）。

## 4. 审核员认为仍然存在的局限

- **macOS arm64 没有复测**：题面给出的 4 条红判据已按「形状 + 容差」改写，并用 1-ulp 噪声模拟与跨哈希种子 harness 在 Linux 上验证，但审核员没有 macOS 机器，新判据在该平台的实际结果是「未复测」而不是「通过」。容差判据也不是数学保证：若某根 bar 的信号恰落在相等边界，整数字段会翻转并使判据变红，届时需要人工判断。
- **lob 流文件 sha256 的跨平台性未验证**：生成器经 `random.gauss / expovariate / uniform`（libm），同 seed 在 arm64 上重新生成是否逐字节相同未知；跨平台复现应复制流文件或先核对 sha256（已写进 lob/REPORT.md 7.1）。
- **lob full 规模（`LOB_TEST_SCALE=full`，含套件内 1,000 万对拍）未在本机重跑**；1,000 万对拍改用 CLI 单独复现（2.2），整数结果一致。
- **lob 排队模型目标 3 未达成**（整体 Brier 0.117–0.177 > 0.08，仅 d=0 与 (d=1,T=10) 5 格达标）；原报告 §5 的两个数字（常数预测 > 0.2、0.183→0.173）没有入库 JSON，标为「未复核、不作为依据」；要复核需重新采集探针（`queue-collect`，未做）。
- **性能数字只对写明的机器成立**：qbt 的 01–04 与 05 在同一台 Linux VM；lob 的历史压测在另一台 VM（Xeon 2.80GHz），本机复测列不可与历史列直接比较（同一张表里已分列标明机器）。
- **突变测试的分母是抽样**（qbt 每模块 80 / lob 每模块 120），等价判定为人工阅读；qbt 的 `strategies/`、`tools/`、`bench/` 不在突变范围内。
- **覆盖率工具只统计主进程**：进程池 / 子进程内执行的代码靠串行路径覆盖；lob 的覆盖率现以临时目录重新生成流文件为准（每次 +≈ 15 s）。
- **lob 的两处不检测**（CONTRACT 如实写明）：纯模式 replay 按记录位置写 status 不读 seq、seq=0 任何模式都不检测；compare 对未知 symbol 的记录静默过滤。
- **审核代理的部分检查是只读推断后由审核员实验证实的**（覆盖率根因两条均已实验证实），突变按 id 复跑在代理阶段只完成 8/12 条，审核员改为两个模块全部重跑（2.9），不再依赖那 12 条。
