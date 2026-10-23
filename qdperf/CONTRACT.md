# qdperf 接口契约（CONTRACT）

四个工具的命令行（参数 / 退出码 / stdout 第一行格式）与 Python 接口（函数签名 / 返回 JSON 字段表）。
`qdperf/tests/test_contract.py` 逐条核对本文与代码一致（参数名、退出码、第一行正则、字段名）。

## 0. 通用约定

| 约定 | 取值 |
|---|---|
| 运行环境 | Python 3.12 标准库，零第三方依赖；Linux / macOS / Windows（`tick_jitter` 的 GIL 对照、`sqlite_load` 的 `-shm` 读取在任何平台都可跑，判据只看倍数 / 形状） |
| 分位数 | 线性插值（Hyndman-Fan 第 7 型，numpy 缺省）：排序后位置 h = (n−1)·q，取相邻两点线性插值；样本为空时为 `null` |
| 相对变化 | `(本次 − 基线) / 基线`；基线为 0 时本次也为 0 记 0，否则记 `+inf` |
| 噪声幅度 | 同一台机器连跑的几遍结果的相对极差 `(最大 − 最小) / 最小` |
| 容差 | `max(手动容差, 2 × 噪声幅度)`；`变化 > 容差 + 1e-9` 判红（恰好等于容差不算红） |
| 时间单位 | JSON 里 `*_s` 为秒，`*_ms` 为毫秒，`*_T` 为 tick 周期 T 的倍数 |
| 写盘 | 只写 `qdperf/results/`、`tempfile` 目录、以及调用方用 `--json` / `--out` / `--emit-sql` 明确指定的文件 |
| 网络 | 只连 127.0.0.1 / localhost（`http_load` 遇到其他主机直接退出码 2，除非显式 `--allow-remote`） |
| 子进程 | 一律 `multiprocessing` 的 spawn 方式或 `python -m` 子进程；子进程入口调 `common.child_init()` |
| 退出码通则 | 0 = 判据通过；1 = 判据不通过（发现问题 / 回归 / 饿死）；2 = 输入错误（参数、文件、格式、不可比） |

---

## 1. SQLite 体检器 `qdperf.sqlite_doctor`（G1）

### 1.1 命令行

```
python3.12 -m qdperf.sqlite_doctor DB --workload WL.json [选项]
```

| 参数 | 缺省 | 含义 |
|---|---|---|
| `DB` | 必填 | 被体检的库文件（只读打开；WAL 库且没有 `-wal` 文件时用 `immutable=1`） |
| `--workload` | 必填 | 工作负载 JSON（§1.2） |
| `--sections` | `plans,indexes,contention,wal,file` | 要跑的节（逗号分隔）；`plans` 总会跑 |
| `--duration` | 4 | 每个写争用场景的秒数 |
| `--wal-duration` | 6 | 每个 WAL 场景的秒数 |
| `--writers` / `--rate` | 3 / 50 | 判据场景：N 个写者各 w 次/秒 |
| `--busy-timeout` | 取工作负载 `connection.busy_timeout_ms` | 秒 |
| `--budget-ms` | 200 | 写争用 p99 等待预算 |
| `--scan-cost-red` | 10000 | 全表扫描语句 `频次 × 行数 ≥` 此值（行/秒）记红 |
| `--deferred-demo` | 关 | 另跑一个 `BEGIN DEFERRED` 读后写（读完思考 5 ms）的根因演示场景（不计入判定） |
| `--quick` | 关 | 写争用 2 s、WAL 3 s、只测 autocheckpoint=1000 |
| `--json` | — | 报告 JSON 写到此文件 |
| `--emit-sql` | — | 通过验证的 `CREATE INDEX ...;` 与末尾的 `ANALYZE;` 写到此文件 |

**退出码**：0 无红色发现；1 有红色发现；2 输入错误（库不存在 / 不是 SQLite / 工作负载不合法 / 未知节）。

**stdout 第一行**（正则）：

```
^SQLITE_DOCTOR (OK|FAIL) statements=\d+ full_scan=\d+ temp_btree=\d+ suggestions=(\d+/\d+|-) contention_p99_ms=([\d.]+|-) busy=(\d+|-) wal_max_mb=([\d.]+|-) red=\d+ yellow=\d+$
```

`suggestions=已验证/候选`；`contention_p99_ms` 与 `busy` 取判据场景（`gate=true`）里最差的；输入错误时第一行为 `SQLITE_DOCTOR ERROR <原因>`（退出码 2）——包括工作负载里有语句在原库上编译不过（语法错、表 / 列不存在、参数个数不对）：体检开始前每条语句先在只读连接上 EXPLAIN 一遍，报 `SQLITE_DOCTOR ERROR statement <id>: <原因>`。

**红色发现**：`HOT_FULL_SCAN`（全表扫描且 `频次×行数 ≥ --scan-cost-red`）、`CONTENTION`（判据场景 p99 > 预算或出现 SQLITE_BUSY，附根因）、
`ORIGINAL_MODIFIED`（原库或其旁路文件前后不一致）。**黄色**：`FULL_SCAN`、`TEMP_BTREE`、`CONTENTION_DEMO`、`WAL_GROWTH`、`FRAGMENTED`（freelist ≥ 5%）、
`NO_PRIMARY_KEY`、`FK_WITHOUT_INDEX`、`NEVER_ANALYZED`、`STATS_STALE`（统计行数与实际差 > 10%）、`STATS_MISSING`。**info**：`SUGGESTION_REJECTED`。

### 1.2 工作负载 JSON（`qdperf.workload.v1`）

```json
{"format": "qdperf.workload.v1", "name": "...",
 "connection": {"synchronous": "NORMAL", "busy_timeout_ms": 5000, "begin": "immediate"},
 "processes": {"http": {"description": "..."}, "engine": {}, "notifier": {}},
 "statements": [{"id": "e_open_orders", "process": "engine",
                 "sql": "SELECT ... WHERE bot_id = ? AND status = 'open'",
                 "params": [42], "param_samples": [[42], [43]], "per_second": 50, "txn": "tick"}]}
```

| 字段 | 必填 | 规则 |
|---|---|---|
| `format` | 是 | 固定 `qdperf.workload.v1` |
| `statements[].id` | 是 | 非空字符串，唯一 |
| `statements[].process` | 是 | 来自哪个进程（写争用按它分组成写者） |
| `statements[].sql` | 是 | 一条 SQL；位置参数 `?` 或命名参数 `:name` |
| `statements[].per_second` | 是 | 有限实数 ≥ 0（bool 不算） |
| `statements[].params` | 否 | 列表或对象，缺省 `[]` |
| `statements[].param_samples` | 否 | 非空的参数列表，缺省 `[params]`；计划 / 实测用第一个，写争用用第一个 |
| `statements[].txn` | 否 | 同进程同 `txn` 名的写语句合成一个事务（速率取组内最大） |
| `connection.begin` | 否 | `immediate`（缺省）或 `deferred`：工作负载场景里写事务的开法 |

### 1.3 Python 接口

```python
from qdperf import sqlite_doctor
rep = sqlite_doctor.diagnose(db_path, workload, sections=("plans", "indexes", "contention", "wal", "file"),
                             duration=4.0, wal_duration=6.0, writers=3, rate=50.0, busy_timeout=None,
                             budget_ms=200.0, deferred_demo=False, measure_timeout=5.0, scan_cost_red=10000.0,
                             wal_checkpoints=(200, 1000, 4000), top=20)        # 抛 sqlite_doctor.DoctorError = 输入错误
sqlite_doctor.first_line(rep); sqlite_doctor.format_text(rep); sqlite_doctor.load_workload(path_or_dict)
```

返回字段（`format = "qdperf.sqlite_doctor.v1"`）：

| 字段 | 含义 |
|---|---|
| `db.{path, size_bytes, sha256_before, sha256_after, untouched, siblings_before, siblings_after}` | 原库前后指纹；`untouched` 为真 = sha256、大小、mtime、旁路文件清单全部不变 |
| `statements[]` | 每条语句：`id, process, sql, per_second, kind(select/insert/update/delete/other), plan[]（EXPLAIN QUERY PLAN 原文）, verdict, flags[], scan_tables[], temp_btree[], rows_est, cost, measured{ms_best, vm_kilo_steps, rows_returned, timed_out}, db_ms_per_second, verdict_after_analyze` |
| `verdict` 取值 | `full_scan` / `temp_btree` / `automatic_index` / `index` / `covering_index` / `rowid` / `none`（多项时取最坏） |
| `rows_est` | 每次执行触达行数估算：SCAN = 表行数；SEARCH = 新鲜 `sqlite_stat1` 的每键平均行数（无统计时等值按 10 行），范围项 ÷4（两个 ÷64）；嵌套循环按 Σ_k Π_{j≤k} r_j |
| `cost` | `per_second × rows_est`（行/秒），`top` 按它降序取前 20 |
| `indexes.suggestions[]` | `table, columns, kind(scan/sort), name, sql, statements[], serves[], check_as_is{}, check_analyzed{}, verified, needs_analyze, size_est_bytes, size_bytes, size_source, build_seconds`（`covering` 为覆盖索引变体） |
| `indexes.{rejected[], unsolvable[], already_indexed[], write_amplification{}, write_us_before{}, write_us_after{}, verdict_after_all{}, sql[]}` | 未采用的候选（含原因）、索引解决不了的语句（含原因）、写放大模型、写语句实测单次耗时（µs）、全部建议 + ANALYZE 后每条语句的判定、可执行 SQL |
| `contention.{busy_timeout, scenarios[]}` | 场景 `name, gate, ok, config{}, writers[], overall{wait_ms, busy, committed, attempts, crashed}, starvation{starver, victim, starved, text}, root_cause[]` |
| `contention.scenarios[].writers[]` | `name, planned, attempts, committed, achieved_rate, wait_ms{n,min,p50,p95,p99,max,mean}, hold_ms{}, late_ms{}, errors{名: 次数}, busy, lock_share, crash` |
| `wal.scenarios[]` | `config{autocheckpoint, long_reader, duration, rate, rows_per_txn, hold, synchronous}, curve_fields, curve[[t_s, wal_file_bytes, wal_live_frames, backfilled_frames]], max_bytes, final_bytes, max_live_frames, final_live_frames, resets, commit_ms{}, read_txns, crashed[]` |
| `file` | `pragmas{page_size, page_count, freelist_count, auto_vacuum, encoding, user_version}, journal_mode_header, opened_immutable, fragmentation, no_primary_key[], fk_without_index[], analyzed, stat1_entries, stats_stale[], stats_missing[], largest_objects[[名, 字节]]` |
| `findings[]` | `level(red/yellow/info), code, message` |
| `ok` | 没有红色发现 |

### 1.4 合成库 `qdperf.synth_db`

```
python3.12 -m qdperf.synth_db [--out PATH] [--seed 20261022] [--scale 1.0] [--force] [--fingerprint] [--json OUT]
```

退出码 0 / 2（文件已存在且没给 `--force`、比例不在 (0, 1]）。第一行：
`^SYNTH_DB rows=\d+ seconds=[\d.]+ size_mb=[\d.]+ fingerprint=([0-9a-f]{16}|-) path=.+$`。
Python：`synth_db.generate(path, seed, scale, force) -> {path, seed, scale, rows{}, rows_after_housekeeping{}, seconds, phases_seconds{}, size_bytes, sqlite_version}`；
`synth_db.fingerprint(path) -> {表: {rows, sha256}, all}`（每张表按 rowid 顺序逐行 `repr` 的 sha256）。

---

## 2. 热路径剖析 + 回归闸 `qdperf.profile_gate`（G2）

### 2.1 命令行

```
python3.12 -m qdperf.profile_gate run      TARGET [--args JSON] [--repeat 5] [--inner 1] [--no-cold] [--top 30] [--path DIR]... [--env K=V]... --out new.json
python3.12 -m qdperf.profile_gate baseline TARGET [同上] [--runs 3] --out base.json
python3.12 -m qdperf.profile_gate pair     TARGET [同上] [--runs 3] [--path-b DIR]... [--env-b K=V]... --out base.json --out-b new.json
python3.12 -m qdperf.profile_gate compare  BASE NEW [--wall-tol 0.10] [--mem-tol 0.20] [--gate hot,cold,mem] [--json OUT]
```

| 参数 | 含义 |
|---|---|
| `TARGET` | `包.模块:函数` 或 `包.模块:类.方法` |
| `--args` | JSON 对象，作为关键字参数 |
| `--repeat` | best-of-N 的 N：N 个全新进程，各测一次冷调用（导入 + 第一次调用）与一次热调用 |
| `--inner` | 每个进程里热调用的次数（取最快；目标很短时调大） |
| `--runs` | baseline / pair：连跑几遍（中位数作基线值，相对极差作噪声幅度） |
| `--path` / `--path-b` | 额外加入测量子进程 `sys.path` 的目录（例如旧 / 新版本的检出目录） |
| `--env` / `--env-b` | 传给测量子进程的环境变量（功能开关等；不进入 target/args，不影响可比性） |
| `--wall-tol` / `--mem-tol` | 手动容差（墙钟 / 峰值内存），实际容差 = max(手动, 2 × 基线噪声) |
| `--gate` | 参与判定的指标：`hot`（hot.wall_s）、`cold`（cold.wall_s）、`mem`（memory.peak_bytes） |

**退出码**：run / baseline / pair：0 成功，2 输入错误或目标导入 / 调用失败；compare：0 绿、1 红、2 两份 JSON 不可比（format / target / args 不同）或输入错误。

**stdout 第一行**：

```
run / baseline:  ^PROFILE target=\S+ hot_wall_s=\S+ cold_wall_s=(\S+|-) peak_bytes=\d+ runs=\d+ noise_hot=([\d.]+%|-)$
pair:            ^PROFILE_B target=...（B 的那份；A 写到 --out，B 写到 --out-b）
compare 红:      ^REGRESSION (hot\.wall_s|cold\.wall_s|memory\.peak_bytes)=[\d.e+-]+→[\d.e+-]+ \(\+([\d.]+|inf)%\)$
compare 绿:      ^OK hot\.wall_s=[\d.e+-]+→[\d.e+-]+ \([+-][\d.]+%\) tol=[\d.]+%$
```

即 `REGRESSION <指标>=<基线>→<本次> (+<百分比>)` / `OK hot.wall_s=<基线>→<本次> (<±百分比>) tol=<容差>%`；
有多个红时第一行取“超出容差最多”的那个；其后每个指标一行（`RED` / `ok `），再后每个“新进前 10 的函数”一行
`NEW_TOP10 <函数名> cum=<秒>s`。输入错误时第一行为 `PROFILE_GATE ERROR <异常类>: <原因>`。

### 2.2 Python 接口

```python
from qdperf import profile_gate as G
doc  = G.measure(target, args=None, repeat=5, cold=True, top=30, paths=(), timeout=3600, env=None, inner=1)
base = G.baseline(target, args=None, repeat=5, runs=3, cold=True, top=30, paths=(), env=None, inner=1)
a, b = G.pair(target, args=None, runs=3, repeat=5, top=30, paths_a=(), paths_b=(), env_a=None, env_b=None, inner=1)
agg  = G.aggregate([doc1, doc2, doc3])
res  = G.compare(base, new, wall_tol=0.10, mem_tol=0.20, gate=("hot", "cold", "mem"), top_new=10)
G.function_names(doc)          # 规范化后的函数名集合
```

### 2.3 剖析 JSON 字段（`format = "qdperf.profile.v1"`）

| 字段 | 含义 |
|---|---|
| `target, args, repeat, inner, runs, machine{}` | 测什么、怎么测、在哪台机器 |
| `hot.{wall_s, cpu_s, wall_samples[], cpu_samples[], processes}` | 热：N 个进程各一个样本，`wall_s` = 最小值（runs > 1 时为各遍最小值的中位数） |
| `cold.{wall_s, cpu_s, wall_samples[], cpu_samples[], process_wall_samples[]}` | 冷：导入 + 第一次调用；`process_wall_samples` 含解释器启动 |
| `memory.{peak_bytes, current_bytes, mode}` | 预热后一次调用期间 tracemalloc 的峰值 / 结束时字节 |
| `profile.{top[], functions, sort}` | cProfile 按累计时间前 `top` 个：`{name, ncalls, primitive_calls, tottime, cumtime}`；`name` = `相对路径:限定名`（标准库为 `<stdlib>/...`，内建为 `<builtin>:...`），无行号、无临时目录 |
| `noise` | 单遍为 `null`；多遍为 `{hot_wall_rel, cold_wall_rel, peak_rel, per_run[]}` |

`compare` 返回 `{verdict: "OK"|"REGRESSION", lines[], metrics[{metric, name, base, new, change, tolerance, manual_tolerance, noise, red}], new_top[]}`。

---

## 3. 多进程 tick 抖动判据 `qdperf.tick_jitter`（G3）

### 3.1 命令行

```
python3.12 -m qdperf.tick_jitter run  [--T 0.05] [--seconds 8] [--q 20] [--wal on|off] [--busy 5] [--compute inline|subprocess]
                                      [--mitigation none|replica|chunked|backoff|wal] [--panel-ratio 2.5] [--compute-ratio 0.3]
                                      [--panel-threads 4] [--notifier-rate 2] [--bg-threads 0] [--db PATH | --scale 0.05] [--json OUT]
python3.12 -m qdperf.tick_jitter scan [--quick] [同上的模型参数] [--json OUT]
```

| 参数 | 含义 |
|---|---|
| `--T` | 引擎 tick 周期（秒） |
| `--q` | 面板每秒轮询次数（开环，线程池 `--panel-threads` 个线程执行） |
| `--panel-ratio` | 面板长查询目标耗时 / T（单线程无争用时；按“行数 × 遍数”迭代校准到目标 ±20%：整张 trades 不够长就让同一区间多扫几遍，行数不设上限） |
| `--compute-ratio` | 引擎算段目标耗时 / T（纯 Python 循环，按本进程速度校准） |
| `--compute` | `inline`：算段在引擎进程里（GIL 内）；`subprocess`：发给常驻子进程算完回传 |
| `chunk_ratio` / `chunk_pause`（Python 接口 cfg） | 分块读每块目标耗时 / T（缺省 0.2）与块间让出秒数（缺省 0） |
| `replica_every` / `backoff_k`（Python 接口 cfg） | 副本刷新间隔秒数（缺省 1）/ 退避倍数（缺省 19） |
| `--mitigation` | `replica`（面板读双缓冲只读副本，每 1 s 刷新：主库 → 内存 → 副本文件）/ `chunked`（长查询按 id 切块，每块 ≈ 0.2T）/ `backoff`（下一次轮询至少等 19 × 上次耗时）/ `wal`（把库切到 WAL） |
| `--db` | 用你的库（只读拷到 tempfile）；缺省按 `--scale` 生成合成库 |

**退出码**：run：0 = p99 间隔 ≤ 2T，1 = 被饿死（p99 > 2T）或进程崩溃，2 = 参数错误；scan：0 = 所有配置都没饿死，1 = 至少一个配置 p99 > 2T（找到了拐点），2 = 参数错误。
两者在面板长查询标定达不到目标 ±20% 时（例如 trades 表是空的）都不跑实验、退出码 2，第一行 `TICK_JITTER ERROR panel calibration: <原因>`。

**stdout 第一行**：

```
run:  ^TICK_JITTER (OK|STARVED) T=[\d.]+ ticks=\d+ p50=[\d.]+T p99=[\d.]+T max=[\d.]+T over2T=\d+ read_p99_ms=[\d.]+ compute_p99_ms=[\d.]+ write_p99_ms=[\d.]+$
scan: ^TICK_SCAN knee=(none|wal=(on|off),busy=[\d.]+,compute=(inline|subprocess):Q=[\d.]+) q0_p99_max=[\d.]+T mitigations=(-|\w+:[\d.]+T(,\w+:[\d.]+T)*)$
```

### 3.2 Python 接口与字段

```python
from qdperf import tick_jitter as J
work, master = J.prepare(db=None, scale=0.05, seed=20261022)   # tempfile 里的母库
cal = J.calibrate_panel(master, target_s, probe_rows=20000, tolerance=0.2)
      # {rows, passes, range[lo, hi], seconds, per_row_us, target_seconds, tolerance, error, capped, reason, rounds[{rows, passes, seconds}]}
      # capped=True 只在 ±tolerance 达不到时（reason 写明原因）；scan 遇到它抛 J.CalibrationError（.calibration = 这份记录），不往下跑
r   = J.run_once(master, cfg, work, cal)                  # cfg 键见 J.DEFAULTS
s   = J.scan(quick=False, base=None, db=None, scale=0.05, mitigations=("replica", "chunked", "backoff", "wal"), log=None)
J.find_knees(results); J.pick_knee(knees); J.first_line_run(r); J.first_line_scan(s); J.format_scan(s)
```

tick 间隔 = 相邻 tick 开始时刻之差，最后一个 tick 记 `max(T, 它自己的耗时)`（引擎卡死在最后一个 tick 里也进分布）。
`run_once` 返回：`config{}, ticks, expected_ticks, missed_ticks, crashed[], interval_ms{n,min,p50,p95,p99,max,mean}, interval_T{p50,p99,max}, over_2T,
segments_ms{read{}, compute{}, write{}}, tick_errors{read{}, write{}}, wake_late_ms{}（引擎按时睡下后实际开始比计划晚多少 = 操作系统 / 宿主机调度延迟，与锁和算段无关）, compute_rate,
panel{polls, achieved_q, query_ms{}, errors{}, dropped, replica_refreshes},
notifier{writes, wait_ms{}, errors{}}, starved`。
面板长查询 `J.PANEL_QUERY` 的参数是 `(遍数, lo, hi)`：把 trades 的 id 区间 `(lo, hi]` 扫“遍数”遍、按 user_id 汇总（一条语句、一个读事务）；
连接顺序用 `CROSS JOIN` 固定为 遍数 → trades 的 rowid 区间 → orders 主键，耗时与 行数 × 遍数 成正比、不随 SQLite 版本的计划选择变。
`scan` 返回（`format = "qdperf.tick_jitter.v1"`）：`base{}, panel_calibration{}, results[], knees[{wal, busy, compute, knee_q, knee_p99_T, last_ok_q, p99_T_by_q{}}], knee, mitigations[], gil[], quick, machine{}`。

---

## 4. HTTP 压测 `qdperf.http_load` 与假后端 `qdperf.fake_backend`（G4）

### 4.1 命令行

```
python3.12 -m qdperf.http_load ramp --base URL --manifest urls.json [--cmax 32] [--steps 1,2,4] [--seconds 3] [--warmup 0.3]
                                    [--growth 0.10] [--max-error-rate 0.01] [--allow-remote] [--json OUT]
python3.12 -m qdperf.http_load ab   --a URL --b URL --manifest urls.json [--c 8] [--seconds 3] [--warmup 0.3] [--tol 0.10]
                                    [--allow-remote] [--json OUT]
python3.12 -m qdperf.fake_backend --config backend.json [--port 0] [--ready-file F] [--seconds 0]
```

**退出码**：ramp：0 / 1（任何路径错误率 > `--max-error-rate`）；ab：0 绿 / 1 红；2 = 输入错误（清单不合法、非本机地址且没给 `--allow-remote`、并发档不合法）。
fake_backend：0 正常结束 / 2 配置不合法。

**stdout 第一行**：

```
ramp:          ^HTTP_LOAD knee=(\d+|none) max_rps=[\d.]+ at_c=\d+ p99_ms=([\d.]+|-) errors=[\d.]+$
ab 红:         ^REGRESSION \S+\.(p50|p99)_ms=([\d.]+|-)→([\d.]+|-) \((\+[\d.]+%|no successful requests)\)$
ab 绿:         ^OK \S+\.p99_ms=[\d.]+→[\d.]+ \([+-][\d.]+%\) tol=[\d.]+%$
fake_backend:  ^FAKE_BACKEND LISTENING http://127\.0\.0\.1:\d+$
```

拐点 = 第一个“下一档成功吞吐增长 < `growth`”的并发数（缺省并发档 1, 2, 4, …, cmax，即“并发翻倍吞吐不再增长”）。
A/B 按 A、B、A 交替各跑一档；每个路径的 p50 / p99 与 A 两遍均值比，容差 = max(`--tol`, 2 × A 两遍的相对差)。

### 4.2 清单与假后端配置

```json
{"format": "qdperf.urls.v1",
 "targets": [{"name": "orders", "method": "GET", "path": "/api/orders?user=7", "headers": {}, "body": null, "weight": 3,
              "expect": [200]}]}
```

| 字段 | 规则 |
|---|---|
| `method` | GET（缺省）/ POST / PUT / DELETE / PATCH / HEAD |
| `path` | 以 `/` 开头 |
| `name` | 缺省 = path，唯一 |
| `weight` | 有限实数 > 0，缺省 1 |
| `body` | `null` / 字符串 / 对象或数组（按 JSON 发送并补 `Content-Type: application/json`） |
| `expect` | 视为成功的状态码列表；缺省 2xx / 3xx |

```json
{"format": "qdperf.fake_backend.v1", "seed": 1, "max_concurrency": 0,
 "routes": {"/api/orders": {"latency": {"kind": "lognormal", "p50_ms": 30, "p99_ms": 120}, "error_rate": 0.01,
                            "max_concurrency": 8, "body_bytes": 256, "schedule": "stratified"}},
 "default": {"latency": {"kind": "fixed", "ms": 1}}}
```

延迟 `kind`：`fixed{ms}` / `uniform{lo_ms, hi_ms}` / `lognormal{p50_ms, p99_ms}`；`schedule`：`stratified`（缺省，每 1000 个请求恰好是分布的 1000 个分层点）
或 `random`；`max_concurrency`：0 = 不限，超出排队；`error_rate` ∈ [0, 1]；延迟参数 ∈ [0, 1e6) ms；`body_bytes`（非负整数，缺省 64）：响应体用 JSON 的 `pad` 字段补到恰好这么多字节，差不到 9 字节（`,"pad":""` 的最小开销）时不补——体只会比它短、不会比它长。以上任何一项不合法都报配置错误（`FAKE_BACKEND ERROR …`，退出码 2）。路径按“完全相同 → 最长前缀 → default”匹配。特殊路径 `/__health`、`/__stats`。

### 4.3 Python 接口与字段

```python
from qdperf import http_load as H, fake_backend as B
proc, url = B.start(config, timeout=20.0); B.stop(proc)       # 子进程假后端；B.serve(config) 为进程内线程版
                                                              # 起不来（配置非法等）或 timeout 秒内没就绪 → 杀掉子进程、删 scratch 目录、
                                                              # RuntimeError，消息里带子进程报的原因
step = H.run_step(base, manifest, c, seconds=3.0, warmup=0.3, seed=0, timeout=10.0, allow_remote=False)
r    = H.ramp(base, manifest, cmax=32, seconds=3.0, steps=None, growth=0.10, warmup=0.3, seed=0, allow_remote=False)
res  = H.ab(base_a, base_b, manifest, c=8, seconds=3.0, tol=0.10, warmup=0.3, seed=0, allow_remote=False)
H.judge(a1, a2, b, tol); H.find_knee(steps, growth); H.first_line_ramp(r)
```

`run_step` 返回 `{c, seconds, warmup, per_target{名: 统计}, total: 统计}`，统计 = `{requests, ok, errors, error_rate, rps, ok_rps,
latency_ms{n,min,p50,p95,p99,max,mean}（只算成功请求）, statuses{状态码或 "error": 次数}}`。
`ramp` 返回 `{format: "qdperf.http_load.v1", mode: "ramp", base, steps[], growth, knee, max_rps, max_rps_c, machine}`；
`ab` 返回 `{format, mode: "ab", a, b, c, seconds, verdict, first_line, rows[{target, q, a, b, change, noise, tolerance, red}], runs{a1, b, a2}, machine}`。

---

## 5. 一键自检 `qdperf.selftest`（G5）

```
python3.12 -m qdperf.selftest [--quick | --full]
```

退出码 0 = 全部步骤通过，1 = 任一步骤失败。第一行 `qdperf selftest (<模式>) <平台> Python <版本> SQLite <版本> <核数> CPU`，
其后每步一行 `<步骤> <规模> <秒>s OK|FAIL <关键数字>`，最后一行 `TOTAL ... <通过>/<总数> steps passed`。
步骤：`unittest`（另查测试跑完后 tempfile 下没有新留下的 `qdperf-*` 目录）、`constraints`（零依赖 / 零出网 / 零写仓外三条测试）、`coverage`、`g1_sqlite`、`g2_gate`、`g3_tick`、`g4_http`、`perf_qbt`。
`g2_gate` 在演示小负载上验回归闸：放慢 20% 必红、原样重跑必绿（基线 / 放慢 / 重跑三条臂按进程轮转交替取样）；需求里的“放慢 12% 必红”由 `perf_qbt` 在 qbt 真实用例上判（12% 离 10% 的容差下限只有 2 个点，演示负载太小，进程快慢会把它量到 10% 以下）。
`g3_tick` 只按形状判（拐点在哪个 Q、哪几条缓解有效随平台变，不卡具体值）：面板长查询标定到目标 ±20%（达不到 → 红，关键数字栏写 `panel calibration FAILED: <原因>`）；Q=0 时 p99 ≤ 1.2T；存在某个 Q 使 p99 > 2T；至少两条缓解把拐点配置的 p99 降回 ≤ 2T。
零写仓外的运行时审计：带目录 fd 的删除 / 改名按 `/proc/self/fd`（Linux）或 `fcntl(F_GETPATH)`（macOS）还原成全路径；两种都拿不到时记成 `<unresolved dir_fd=N>/名字`，写入判据把它当仓外写判红（失效时默认拒绝）。
