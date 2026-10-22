# qdperf — 量化交易系统性能体检工具包（REPORT）

纯 Python 3.12 标准库，零第三方依赖，零出网。**所有数据都是合成的**；所有判据只用容差和形状 / 倍数，
性能目标写成“相对自身基线”或“相对假后端注入值”，不拿绝对毫秒当达成判据（表里出现的毫秒数只是本机实测，供参考）。

> 本文件由 `python3.12 qdperf/tools/build_report.py` 从 `qdperf/tools/report_template.md` 与 `qdperf/results/*.json`
> 生成，表中每个数字都取自入库的结果文件（`qdperf/tests/test_report.py` 检查本文件与结果文件一致）。

## 0. 机器与口径

{{@machines}}

- **best-of-N**：回归闸的 N 个样本来自 N 个**全新进程**（每个进程：导入 + 第一次调用 = 冷样本，紧接着的调用 = 热样本），
  冷 / 热各取 N 个进程里最快的；`--runs 3` 时三遍的中位数作基线值、三遍的相对极差作噪声幅度。原因见 §2.3。
- **冷 / 热**：冷 = 新进程里导入目标模块 + 第一次调用（含数据生成、首次分配；不含解释器启动），热 = 同一进程紧接着的调用。
- **分位数**：线性插值（第 7 型）；写争用 / tick / HTTP 的 p99 都是全部样本的分位，不是 best-of-N。

## 1. 目标达成表

| 目标 | 要求 | 实测 | 达成 | 复现命令 |
|---|---|---|---|---|
| G1 | 合成库：users 1 万、bots 5 万、orders 200 万、trades 500 万、settings 300、events 100 万，同 seed 同结果，≤ 3 分钟 | {{g1_synth_verify.json:summary.rows|,}} 行（清理掉 8% 旧 events 后），生成 {{g1_synth_verify.json:synth.seconds|.1f}} s；两次生成逻辑指纹一致：{{g1_synth_verify.json:summary.synth_deterministic}}（第二次 {{g1_synth_verify.json:synth.determinism.second_run_seconds|.1f}} s） | {{?g1_synth}} | `python3.12 -m qdperf.synth_db --scale 1 --fingerprint` |
| G1 | 每条语句 EXPLAIN QUERY PLAN 判定（全表扫描 / 临时 B-tree / 索引 / 覆盖索引），按 频次 × 行数 排前 20 | {{g1_synth_verify.json:summary.statements}} 条语句：全表扫描 {{g1_synth_verify.json:summary.full_scan}}、临时 B-tree {{g1_synth_verify.json:summary.temp_btree}}；前 12 名见 §1.1 | {{?g1_plans}} | `python3.12 -m qdperf.sqlite_doctor <库> --workload qdperf/examples/workload_trading.json` |
| G1 | 索引建议（等值 → 范围 → 排序），临时副本上验证，给大小与写放大 | {{g1_synth_verify.json:summary.suggestions}} 条建议全部验证通过；**独立复核**（把输出的 SQL 在原库的另一份拷贝上执行再 EXPLAIN）{{g1_synth_verify.json:summary.verify_checks}} 项全部不再 SCAN：{{g1_synth_verify.json:summary.suggestions_verified_independently}}；仍扫描的只剩 {{g1_synth_verify.json:summary.still_scanning_after_apply}}（`LIKE '%..%'`，单列原因）；≥ 1 MB 的索引大小估算误差 ≤ {{g1_synth_verify.json:summary.size_est_error_max_ge_1mb|%}}；B-tree 写入 {{g1_doctor.json:indexes.write_amplification.btree_writes_per_second_before}} → {{g1_doctor.json:indexes.write_amplification.btree_writes_per_second_after}} 次/秒 | {{?g1_index}} | 同上 `--emit-sql idx.sql` |
| G1 | 写争用：3 写者 × 50 次/秒（WAL + busy_timeout 5 s）p99 等待 ≤ 200 ms，否则给根因；谁饿死谁 | 真进程实测 p99 {{g1_synth_verify.json:summary.contention_3x50.p99_ms|.3f}} ms、最大 {{g1_synth_verify.json:summary.contention_3x50.max_ms|.2f}} ms、SQLITE_BUSY {{g1_synth_verify.json:summary.contention_3x50.busy}}；{{g1_synth_verify.json:summary.contention_3x50.starvation}}。根因演示（延迟事务读后写）：SQLITE_BUSY {{g1_synth_verify.json:summary.deferred_demo_busy}} 次，根因判定为 BUSY_SNAPSHOT | {{?g1_contention}} | 同上 `--deferred-demo` |
| G1 | WAL 检查点：长读事务把 WAL 撑大的曲线、wal_autocheckpoint 的影响 | 无长读时 WAL 峰值被 autocheckpoint 截住：{{g1_synth_verify.json:summary.wal_free_capped}}；有长读时 WAL 一次也没重置：{{g1_synth_verify.json:summary.wal_long_reader_never_reset}}（曲线见 §1.3） | {{?g1_wal}} | 同上 |
| G1 | 库文件体检：页大小、碎片率、没主键的表、没索引的外键、ANALYZE / stat1 过期 | 页 {{g1_doctor.json:file.pragmas.page_size}} B、碎片率 {{g1_doctor.json:file.fragmentation|%}}、无主键 {{g1_doctor.json:file.no_primary_key|list}}、无索引外键 {{g1_doctor.json:file.fk_without_index|len}} 列、`idx_orders_user` 统计过期（{{g1_doctor.json:file.stats_stale.0.stat_rows|,}} vs {{g1_doctor.json:file.stats_stale.0.actual_rows|,}} 行） | {{?g1_file}} | 同上 `--sections file` |
| G1 | 工具零写入原库 | 体检前后原库 sha256 / 大小 / mtime / 旁路文件清单一致：{{g1_synth_verify.json:summary.original_untouched}}（WAL 库无 -wal 时 `immutable=1` 只读打开，计划与验证都在 tempfile 副本上） | {{?g1_untouched}} | `qdperf/tests/test_doctor.py` |
| G2 | 可调用 → 墙钟 / CPU / tracemalloc 峰值 / cProfile 前 30，冷热各一遍，best-of-N（缺省 5），落 JSON | qbt 回测 {{g2_profile_gate.json:summary.n|,}} 根 × ma_cross：热 {{g2_profile_gate.json:summary.hot_wall_s|.3f}} s、冷 {{g2_profile_gate.json:summary.cold_wall_s|.3f}} s、峰值 {{g2_profile_gate.json:summary.peak_bytes|,}} 字节（本实验 N = {{g2_profile_gate.json:summary.repeat}} 个进程 × 每进程热调用 {{g2_profile_gate.json:summary.inner}} 次） | {{?g2_measure}} | `python3.12 -m qdperf.tools.selfcheck g2` |
| G2 | 回归闸：墙钟 +10% 红、峰值内存 +20% 红、新进前 10 的函数点名；首行 `REGRESSION <指标>=<基线>→<本次> (+<百分比>)`；3 遍基线噪声 × 2 与手动容差取大 | 3 遍基线噪声：热 {{g2_profile_gate.json:summary.noise.hot_wall_rel|%}}、冷 {{g2_profile_gate.json:summary.noise.cold_wall_rel|%}}、内存 {{g2_profile_gate.json:summary.noise.peak_rel|%}} → 热墙钟容差 {{g2_profile_gate.json:summary.tolerance_hot_default|%}} | {{?g2_gate}} | `python3.12 -m qdperf.profile_gate compare base.json new.json` |
| G2 | 故意放慢 12% 必红；5% 按噪声幅度决定（写明）；原样重跑不红 | 12%：`{{g2_profile_gate.json:summary.slow.12pct.first_line}}`，新进前 10：{{g2_profile_gate.json:summary.slow.12pct.new_top10}}；5%：`{{g2_profile_gate.json:summary.slow.5pct.first_line}}`（{{@g2_slow5_note}}）；原样重跑：`{{g2_profile_gate.json:summary.rerun_first_line}}` | {{?g2_slow}} | 同上 |
| G2 | 两份 JSON 的函数名集合可比（去掉行号 / 临时路径） | 原样重跑前 10 函数集合相同：{{g2_profile_gate.json:summary.names_identical_rerun_top10}}；整份代码拷到临时目录再测：前 10 相同 {{g2_profile_gate.json:summary.names_moved_copy_top10_equal}}、前 30 的 Jaccard {{g2_profile_gate.json:summary.names_moved_copy_jaccard}}；名字里无行号 / 临时路径：{{g2_profile_gate.json:summary.names_have_no_lineno_or_tmp}} | {{?g2_names}} | `qdperf/tests/test_profile_gate.py` |
| G3 | 三个真进程（引擎每 T 一个 tick、面板 Q 次/秒长查询、通知偶发写），量 tick 间隔 p50 / p99 / 最大、过 2T 次数、读 / 算 / 写分段 | T = {{g3_tick_jitter.json:summary.T}} s；面板长查询 {{g3_tick_jitter.json:summary.panel_rows|,}} 行、单线程 {{g3_tick_jitter.json:summary.panel_query_ms|.0f}} ms（≈ {{@g3_panel_T}}）；共 {{g3_tick_jitter.json:summary.configs}} 个配置 × {{g3_tick_jitter.json:summary.seconds_per_config}} s（全表见 §3） | {{?g3_scan}} | `python3.12 -m qdperf.tick_jitter scan` |
| G3 | Q=0 时 p99 间隔 ≤ 1.2T | 全部 Q=0 配置的最大 p99 = {{g3_tick_jitter.json:summary.q0_p99_T_max|.3f}}T | {{?g3_q0}} | 同上 |
| G3 | 证明某个 Q 下 p99 > 2T（饿死点），并给拐点表 | 拐点：{{@g3_knee_cfg}}：Q = {{g3_tick_jitter.json:summary.knee.knee_q}} 时 p99 = {{g3_tick_jitter.json:summary.knee.knee_p99_T|.2f}}T（表见 §3.1） | {{?g3_knee}} | 同上 |
| G3 | 两条有效缓解各自的实测改善 | 有效（p99 ≤ 2T）：{{g3_tick_jitter.json:summary.effective_mitigations}}；读只读副本 p99 {{g3_tick_jitter.json:summary.mitigations.replica.p99_T|.2f}}T（改善 {{g3_tick_jitter.json:summary.mitigations.replica.improvement_x|.1f}} 倍）、切 WAL {{g3_tick_jitter.json:summary.mitigations.wal.p99_T|.2f}}T（改善 {{g3_tick_jitter.json:summary.mitigations.wal.improvement_x|.1f}} 倍）；实测无效的也照列：分块读 {{g3_tick_jitter.json:summary.mitigations.chunked.p99_T|.2f}}T、轮询退避 {{g3_tick_jitter.json:summary.mitigations.backoff.p99_T|.2f}}T（见 §3.2） | {{?g3_mitig}} | `python3.12 -m qdperf.tick_jitter run --wal off --q 5 --mitigation replica`（或 `wal`） |
| G4 | 假后端注入 p99 = 120 ms → 量出 108–132 ms | {{g4_http_load.json:summary.p99_measured_ms|.1f}} ms（误差 {{g4_http_load.json:summary.p99_error|%}}，{{g4_http_load.json:summary.p99_requests|,}} 个请求，C = 8） | {{?g4_p99}} | `python3.12 -m qdperf.tools.selfcheck g4` |
| G4 | 注入并发上限 8 → 拐点报 6–10 | 拐点 {{g4_http_load.json:summary.knee_measured}}（吞吐见 §4.1） | {{?g4_knee}} | 同上 |
| G4 | A/B 中 B 比 A 慢 30% → 必红 | `{{g4_http_load.json:summary.ab_slow_first_line}}`（{{g4_http_load.json:summary.ab_slow_red_rows}}/{{g4_http_load.json:summary.ab_rows}} 项红）；同配置 A/A：`{{g4_http_load.json:summary.ab_same_first_line}}` | {{?g4_ab}} | 同上 |
| G5 | 一条命令自检，任何一项失败非零退出 | `python3.12 -m qdperf.selftest`：{{selftest_summary.json:steps.0.scale}} 等 8 步，见 README 第一屏 | {{?g5_selftest}} | `python3.12 -m qdperf.selftest` |
| G5 | 语句覆盖率 ≥ 90%（复用 tools/cov.py 做法） | **{{coverage.json:library.total_percent|.2f}}%**（{{coverage.json:library.total_covered}}/{{coverage.json:library.total_statements}} 条，含 {{coverage.json:child_processes_merged}} 个子进程的覆盖；表见 §5） | {{?g5_coverage}} | `python3.12 qdperf/tools/cov.py --fail-under 90` |
| G5 | 突变抓住率 ≥ 85%，每模块 ≥ 40 个突变体，活下来的逐条判定 | {{@mutation_first}}**{{mutation.json:summary.kill_rate|.1f}}%**（{{mutation.json:summary.caught}}/{{mutation.json:summary.total}}，每模块 {{mutation.json:meta.per_module}} 个；漏网逐条判定见 `mutants/triage.md`） | {{?g5_mutation}} | `python3.12 qdperf/mutants/run.py --per-module 50 --jobs 4` |
| G5 | 零依赖 / 零出网 / 零写仓外各一条测试 | `test_constraints.py`：grep + AST 只许标准库；静态无 socket / urllib.request、http.client 只经本机校验；运行时审计钩子跑一遍四个工具（含全部子进程），写盘只落 tempfile 与 results/，socket 只连 127.0.0.1 | {{?g5_constraints}} | `python3.12 -m unittest qdperf.tests.test_constraints` |

### 1.1 最贵的 12 条语句（全量合成库，频次 × 行数）

{{@g1_top}}

### 1.2 索引建议（全部在副本上建索引 + ANALYZE 后用 EXPLAIN QUERY PLAN 验证；独立复核见上表）

{{@g1_suggestions}}

### 1.3 写争用（真进程，WAL，busy_timeout 5 s）与 WAL 检查点

{{@g1_contention}}

{{@g1_wal}}

## 2. G2 回归闸（qbt 回测真实用例）

### 2.1 放慢 5% / 12% 两档

{{@g2_slow}}

“故意放慢”= 回测之后跑一个无意义循环，空转时长 = 该档比例 × 本次调用已用时长，所以每次调用恰好慢 5% / 12%，与进程本身的快慢无关。
**5% 档按噪声幅度决定**：缺省容差 = max(10%, 2 × 噪声) = {{g2_profile_gate.json:summary.tolerance_hot_default|%}}，5% 低于它 → 绿（按设计）；
若把手动容差设成 0（`--wall-tol 0`，只看噪声），容差 = 2 × 噪声 = {{g2_profile_gate.json:summary.tolerance_hot_noise_only|%}}，此时 5% 档的判定为
`{{g2_profile_gate.json:summary.slow.5pct.first_line_noise_only}}`。12% 档每一轮单独拿出来对比基线：{{@g2_per_round}}。

### 2.2 三遍基线

{{@g2_noise}}

### 2.3 为什么 best-of-N 要跨进程

{{@g2_noise_probe}}

## 3. G3 tick 抖动

### 3.1 拐点表（tick 间隔 p99，单位 T；“←拐点”= 该组合里 p99 > 2T 的最小 Q）

{{@g3_table}}

机理：WAL 关（rollback journal）时，面板的长查询持有 SHARED 锁，引擎提交要拿 EXCLUSIVE，必须等当前读完；面板查询 ≈ {{@g3_panel_T}}，
所以一碰上就把这个 tick 拖过 2T。WAL 开时读不挡写，引擎几乎不受面板影响。

### 3.2 缓解（在拐点配置上）

{{@g3_mitigations}}

### 3.3 算段放 GIL 内 vs 子进程

{{@g3_gil}}

引擎进程里只有 tick 一个 Python 线程时，算段放子进程只多出 IPC 往返；引擎进程里还有别的纯 Python 忙线程（bg=1）时，
GIL 让进程内算段变慢，放到子进程算则不受影响。

## 4. G4 HTTP

### 4.1 并发爬坡（假后端 `/limited`：延迟 p50 50 ms / p99 60 ms，并发上限 8）

{{@g4_knee}}

### 4.2 A/B（A、B、A 交替）

{{@g4_ab}}

## 5. 覆盖率

{{@coverage}}

## 6. 突变测试

{{@mutation}}

上表是第 2 轮（`{{mutation.json:meta.command}}`，抓住须确认：报失败的测试在同一突变体上单独重跑仍失败才算）。第 1 轮（留档于 `results/mutation_round1.json`）的抓住未经确认，事后发现其中一批是并行满载下的测试抖动误杀，整轮作废；经过、两轮对照与每条漏网的判定（等价 / 补判据，各带理由）见 [mutants/triage.md](mutants/triage.md)。
