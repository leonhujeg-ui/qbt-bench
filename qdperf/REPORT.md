# qdperf — 量化交易系统性能体检工具包（REPORT）

纯 Python 3.12 标准库，零第三方依赖，零出网。**所有数据都是合成的**；所有判据只用容差和形状 / 倍数，
性能目标写成“相对自身基线”或“相对假后端注入值”，不拿绝对毫秒当达成判据（表里出现的毫秒数只是本机实测，供参考）。

> 本文件由 `python3.12 qdperf/tools/build_report.py` 从 `qdperf/tools/report_template.md` 与 `qdperf/results/*.json`
> 生成，表中每个数字都取自入库的结果文件（`qdperf/tests/test_report.py` 检查本文件与结果文件一致）。

## 0. 机器与口径

| 结果文件 | 生成机器 / Python / SQLite / 提交 / 时间 |
|---|---|
| `results/g1_synth_verify.json` | Linux-6.18.44-fc-v153-x86_64-with-glibc2.39，Python 3.12.3，Intel(R) Xeon(R) Processor @ 2.10GHz × 4，SQLite 3.45.1，提交 45d917a，2026-10-23T11:04:39 |
| `results/g1_doctor.json` | Linux-6.18.44-fc-v153-x86_64-with-glibc2.39，Python 3.12.3，Intel(R) Xeon(R) Processor @ 2.10GHz × 4，SQLite 3.45.1，提交 45d917a，2026-10-23T11:02:12 |
| `results/g2_profile_gate.json` | Linux-6.18.44-fc-v153-x86_64-with-glibc2.39，Python 3.12.3，Intel(R) Xeon(R) Processor @ 2.10GHz × 4，SQLite 3.45.1，提交 f3efae4，2026-10-23T11:33:59 |
| `results/g2_noise_probe.json` | Linux-6.18.44-fc-v140-x86_64-with-glibc2.39，Python 3.12.3，Intel(R) Xeon(R) Processor @ 2.80GHz × 4，SQLite 3.45.1，提交 ed9b896，2026-10-22T22:02:19 |
| `results/g3_tick_jitter.json` | Linux-6.18.44-fc-v153-x86_64-with-glibc2.39，Python 3.12.3，Intel(R) Xeon(R) Processor @ 2.10GHz × 4，SQLite 3.45.1，提交 2e98df8，2026-10-23T11:20:02 |
| `results/g4_http_load.json` | Linux-6.18.44-fc-v153-x86_64-with-glibc2.39，Python 3.12.3，Intel(R) Xeon(R) Processor @ 2.10GHz × 4，SQLite 3.45.1，提交 2e98df8，2026-10-23T11:20:02 |
| `results/selftest_summary.json` | Linux-6.18.44-fc-v153-x86_64-with-glibc2.39，Python 3.12.3，Intel(R) Xeon(R) Processor @ 2.10GHz × 4，SQLite 3.45.1，提交 f3efae4，2026-10-23T11:33:59 |

- **best-of-N**：回归闸的 N 个样本来自 N 个**全新进程**（每个进程：导入 + 第一次调用 = 冷样本，紧接着的调用 = 热样本），
  冷 / 热各取 N 个进程里最快的；`--runs 3` 时三遍的中位数作基线值、三遍的相对极差作噪声幅度。原因见 §2.3。
- **冷 / 热**：冷 = 新进程里导入目标模块 + 第一次调用（含数据生成、首次分配；不含解释器启动），热 = 同一进程紧接着的调用。
- **分位数**：线性插值（第 7 型）；写争用 / tick / HTTP 的 p99 都是全部样本的分位，不是 best-of-N。

## 1. 目标达成表

| 目标 | 要求 | 实测 | 达成 | 复现命令 |
|---|---|---|---|---|
| G1 | 合成库：users 1 万、bots 5 万、orders 200 万、trades 500 万、settings 300、events 100 万，同 seed 同结果，≤ 3 分钟 | 7,980,300 行（清理掉 8% 旧 events 后），生成 36.5 s；两次生成逻辑指纹一致：True（第二次 40.1 s） | ✅ | `python3.12 -m qdperf.synth_db --scale 1 --fingerprint` |
| G1 | 每条语句 EXPLAIN QUERY PLAN 判定（全表扫描 / 临时 B-tree / 索引 / 覆盖索引），按 频次 × 行数 排前 20 | 23 条语句：全表扫描 11、临时 B-tree 6；前 12 名见 §1.1 | ✅ | `python3.12 -m qdperf.sqlite_doctor <库> --workload qdperf/examples/workload_trading.json` |
| G1 | 索引建议（等值 → 范围 → 排序），临时副本上验证，给大小与写放大 | 10 条建议全部验证通过；**独立复核**（把输出的 SQL 在原库的另一份拷贝上执行再 EXPLAIN）11 项全部不再 SCAN：True；仍扫描的只剩 ['h_email_search']（`LIKE '%..%'`，单列原因）；≥ 1 MB 的索引大小估算误差 ≤ 0.3%；B-tree 写入 132.51 → 299.04 次/秒 | ✅ | 同上 `--emit-sql idx.sql` |
| G1 | 写争用：3 写者 × 50 次/秒（WAL + busy_timeout 5 s）p99 等待 ≤ 200 ms，否则给根因；谁饿死谁 | 真进程实测 p99 0.104 ms、最大 0.18 ms、SQLITE_BUSY 0；无饿死：最差的 http p99 等待 0.11 ms ≤ 200 ms，SQLITE_BUSY 0；主要持锁者 engine（占比 1.1%）。根因演示（延迟事务读后写）：SQLITE_BUSY 111 次，根因判定为 BUSY_SNAPSHOT | ✅ | 同上 `--deferred-demo` |
| G1 | WAL 检查点：长读事务把 WAL 撑大的曲线、wal_autocheckpoint 的影响 | 无长读时 WAL 峰值被 autocheckpoint 截住：True；有长读时 WAL 一次也没重置：True（曲线见 §1.3） | ✅ | 同上 |
| G1 | 库文件体检：页大小、碎片率、没主键的表、没索引的外键、ANALYZE / stat1 过期 | 页 4096 B、碎片率 1.0%、无主键 `settings`、无索引外键 6 列、`idx_orders_user` 统计过期（1,000,000 vs 2,000,000 行） | ✅ | 同上 `--sections file` |
| G1 | 工具零写入原库 | 体检前后原库 sha256 / 大小 / mtime / 旁路文件清单一致：True（WAL 库无 -wal 时 `immutable=1` 只读打开，计划与验证都在 tempfile 副本上） | ✅ | `qdperf/tests/test_doctor.py` |
| G2 | 可调用 → 墙钟 / CPU / tracemalloc 峰值 / cProfile 前 30，冷热各一遍，best-of-N（缺省 5），落 JSON | qbt 回测 1,000,000 根 × ma_cross：热 1.111 s、冷 3.395 s、峰值 125,490,348 字节（本实验 N = 8 个进程 × 每进程热调用 2 次） | ✅ | `python3.12 -m qdperf.tools.selfcheck g2` |
| G2 | 回归闸：墙钟 +10% 红、峰值内存 +20% 红、新进前 10 的函数点名；首行 `REGRESSION <指标>=<基线>→<本次> (+<百分比>)`；3 遍基线噪声 × 2 与手动容差取大 | 3 遍基线噪声：热 0.2%、冷 8.7%、内存 0.0% → 热墙钟容差 10.0% | ✅ | `python3.12 -m qdperf.profile_gate compare base.json new.json` |
| G2 | 故意放慢 12% 必红；5% 按噪声幅度决定（写明）；原样重跑不红 | 12%：`REGRESSION hot.wall_s=1.111→1.277 (+14.9%)`，新进前 10：['qdperf/examples/qbt_case.py:meaningless_loop']；5%：`OK hot.wall_s=1.111→1.163 (+4.6%) tol=10.0%`（容差 10.0% > 5% → 放慢 5% 落在容差内，按设计应判绿；实际 OK）；原样重跑：`OK hot.wall_s=1.111→1.179 (+6.1%) tol=10.0%` | ✅ | 同上 |
| G2 | 两份 JSON 的函数名集合可比（去掉行号 / 临时路径） | 原样重跑、整份代码拷到临时目录再测，两份前 10 的名字都在基线前 30 里：True / True；名字里无行号 / 临时路径：True；前 30 的 Jaccard 1.00 / 1.00；前 10 排位完全相同（只供参考：第 10 名上下的函数耗时接近，排位随噪声互换）：True / True | ✅ | `qdperf/tests/test_profile_gate.py` |
| G3 | 三个真进程（引擎每 T 一个 tick、面板 Q 次/秒长查询、通知偶发写），量 tick 间隔 p50 / p99 / 最大、过 2T 次数、读 / 算 / 写分段 | T = 0.05 s；面板长查询单线程 127 ms ≈ 2.54T（标定目标 2.5T ± 20%，偏差 +1.5%；扫描 243,275 行 × 1 遍）；共 32 个配置 × 20.0 s（全表见 §3） | ✅ | `python3.12 -m qdperf.tick_jitter scan` |
| G3 | Q=0 时 p99 间隔 ≤ 1.2T | 全部 Q=0 配置的最大 p99 = 1.011T | ✅ | 同上 |
| G3 | 证明某个 Q 下 p99 > 2T（饿死点），并给拐点表（判据：存在这样的 Q，不卡 Q 的具体值） | 拐点（本机实测，平台相关）：WAL 关、busy 5 s、inline：Q = 5.0 时 p99 = 8.55T（表见 §3.1） | ✅ | 同上 |
| G3 | 两条有效缓解各自的实测改善（判据：至少两条把拐点配置的 p99 降回 ≤ 2T，不限哪两条） | 本机实测，平台相关：有效（p99 ≤ 2T）：分块读 p99 1.18T（改善 7.3 倍，超过 2T 的 tick 0 个）、读只读副本 p99 1.03T（改善 8.3 倍，超过 2T 的 tick 0 个）、切 WAL p99 1.01T（改善 8.5 倍，超过 2T 的 tick 0 个）；实测无效的也照列：轮询退避 p99 2.89T（改善 3.0 倍，超过 2T 的 tick 7 个）（见 §3.2） | ✅ | `python3.12 -m qdperf.tick_jitter run --wal off --busy 5 --compute inline --q 5 --mitigation replica`（或 `chunked` / `backoff` / `wal`） |
| G4 | 假后端注入 p99 = 120 ms → 量出 108–132 ms | 119.0 ms（误差 -0.9%，1,315 个请求，C = 8） | ✅ | `python3.12 -m qdperf.tools.selfcheck g4` |
| G4 | 注入并发上限 8 → 拐点报 6–10 | 拐点 8（吞吐见 §4.1） | ✅ | 同上 |
| G4 | A/B 中 B 比 A 慢 30% → 必红 | `REGRESSION a1.p99_ms=60.63→79.80 (+31.6%)`（4/4 项红）；同配置 A/A：`OK a2.p99_ms=13.22→12.72 (-3.8%) tol=10.0%` | ✅ | 同上 |
| G5 | 一条命令自检，任何一项失败非零退出 | `python3.12 -m qdperf.selftest`：179 tests 等 8 步，见 README 第一屏 | ✅ | `python3.12 -m qdperf.selftest` |
| G5 | 语句覆盖率 ≥ 90%（复用 tools/cov.py 做法） | **97.85%**（2998/3064 条，含 119 个子进程的覆盖；表见 §5） | ✅ | `python3.12 qdperf/tools/cov.py --fail-under 90` |
| G5 | 突变抓住率 ≥ 85%，每模块 ≥ 40 个突变体，活下来的逐条判定 | 第 3 轮首跑 80.6%（403/500，抓住须确认），97 条漏网逐条判定、补判据、修 4 个真问题（3 个由漏网追查发现、1 个由自检的临时目录检查发现）、改了库的模块重抽样后重跑 → **94.8%**（474/500，每模块 50 个；漏网逐条判定见 `mutants/triage.md`） | ✅ | `python3.12 qdperf/mutants/run.py --per-module 50 --jobs 4` |
| G5 | 零依赖 / 零出网 / 零写仓外各一条测试 | `test_constraints.py`：grep + AST 只许标准库；静态无 socket / urllib.request、http.client 只经本机校验；运行时审计钩子跑一遍四个工具（含全部子进程），写盘只落 tempfile 与 results/，socket 只连 127.0.0.1 | ✅ | `python3.12 -m unittest qdperf.tests.test_constraints` |

### 1.1 最贵的 12 条语句（全量合成库，频次 × 行数）

| # | 语句 | 进程 | 次/秒 | 判定 | 行/次（估） | 频次×行数（行/秒） | 实测 ms（best-of-3） | 占库 ms/秒 |
|---:|---|---|---:|---|---:|---:|---:|---:|
| 1 | `e_open_orders` | engine | 50 | full_scan | 2,000,000 | 100,000,000 | 95.840 | 4,792.0 |
| 2 | `e_last_price` | engine | 10 | full_scan | 5,000,000 | 50,000,000 | 331.170 | 3,311.7 |
| 3 | `h_trades_by_user` | http | 10 | full_scan | 5,000,000 | 50,000,000 | 215.255 | 2,152.6 |
| 4 | `h_dashboard_pnl` | http | 2 | full_scan | 5,000,000 | 10,000,000 | 291.827 | 583.7 |
| 5 | `h_events_feed` | http | 10 | full_scan | 920,000 | 9,200,000 | 56.004 | 560.0 |
| 6 | `n_pending_alerts` | notifier | 1 | full_scan | 920,000 | 920,000 | 56.032 | 56.0 |
| 7 | `h_bots_of_user` | http | 15 | full_scan | 50,000 | 750,000 | 2.382 | 35.7 |
| 8 | `e_running_bots` | engine | 1 | full_scan | 50,000 | 50,000 | 15.961 | 16.0 |
| 9 | `n_purge_events` | notifier | 0.01 | full_scan | 920,000 | 9,200 | 45.593 | 0.5 |
| 10 | `h_setting` | http | 30 | full_scan | 300 | 9,000 | 0.014 | 0.4 |
| 11 | `h_orders_by_user` | http | 20 | temp_btree | 240 | 4,800 | 1.828 | 36.6 |
| 12 | `h_orders_join_bots` | http | 5 | index | 480 | 2,400 | 2.041 | 10.2 |

### 1.2 索引建议（全部在副本上建索引 + ANALYZE 后用 EXPLAIN QUERY PLAN 验证；独立复核见上表）

| 索引（列顺序 = 等值 → 范围 → 排序） | 服务的语句 | 建索引+ANALYZE 后不再扫描 | 估算 MB | 实测 MB | 构建 s | +B-tree 写/秒 |
|---|---|---|---:|---:|---:|---:|
| `CREATE INDEX qdperf_bots_status_updated_at ON bots(status, updated_at)` | e_running_bots | 1/1 | 1.01 | 1.01 | 0.03 | 20.5 |
| `CREATE INDEX qdperf_bots_user_id ON bots(user_id)` | h_bots_of_user | 1/1 | 0.52 | 0.52 | 0.02 | 0 |
| `CREATE INDEX qdperf_events_level_notified_ts ON events(level, notified, ts)` | n_pending_alerts | 1/1 | 17.65 | 17.60 | 0.69 | 12.01 |
| `CREATE INDEX qdperf_events_user_id_ts ON events(user_id, ts)` | h_events_feed | 1/1 | 14.76 | 14.74 | 0.59 | 7.01 |
| `CREATE INDEX qdperf_events_ts ON events(ts)` | n_purge_events | 1/1 | 12.05 | 12.02 | 0.40 | 7.01 |
| `CREATE INDEX qdperf_orders_user_id_status_created_at ON orders(user_id, status, created_at)` | h_orders_by_user | 1/1 | 47.26 | 47.30 | 2.48 | 40 |
| `CREATE INDEX qdperf_orders_bot_id_status ON orders(bot_id, status)` | e_open_orders | 1/1 | 37.79 | 37.80 | 1.74 | 40 |
| `CREATE INDEX qdperf_settings_key ON settings(key)` | h_setting | 1/1 | 0.01 | 0.01 | 0.00 | 0 |
| `CREATE INDEX qdperf_trades_symbol_ts ON trades(symbol, ts)` | h_dashboard_pnl, e_last_price | 1/1 | 107.11 | 107.16 | 4.55 | 20 |
| `CREATE INDEX qdperf_trades_user_id_ts ON trades(user_id, ts)` | h_trades_by_user | 1/1 | 80.23 | 80.04 | 3.21 | 20 |

### 1.3 写争用（真进程，WAL，busy_timeout 5 s）与 WAL 检查点

| 场景 | 写者 | 实际次/秒 | 提交 | 等待 p50 ms | 等待 p99 ms | 最大 ms | SQLITE_BUSY | 持锁 p99 ms |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| workload | http | 2.5 | 10 | 0.053 | 0.100 | 0.102 | 0 | 0.143 |
| workload | engine | 20.0 | 80 | 0.040 | 0.247 | 0.754 | 0 | 0.621 |
| workload | notifier | 10.0 | 40 | 0.048 | 0.125 | 0.127 | 0 | 0.217 |
| 3x50/s | http | 50.0 | 200 | 0.035 | 0.108 | 0.141 | 0 | 0.316 |
| 3x50/s | engine | 50.0 | 200 | 0.036 | 0.086 | 0.180 | 0 | 0.447 |
| 3x50/s | notifier | 50.0 | 200 | 0.037 | 0.096 | 0.108 | 0 | 0.165 |
| 3x50/s deferred+think5ms | http | 50.0 | 200 | 0.031 | 0.065 | 0.096 | 0 | 0.079 |
| 3x50/s deferred+think5ms | engine | 22.2 | 89 | 0.049 | 0.113 | 0.116 | 111 | 0.248 |
| 3x50/s deferred+think5ms | notifier | 50.0 | 200 | 0.033 | 0.088 | 0.147 | 0 | 0.151 |

- **workload**：无饿死：最差的 engine p99 等待 0.25 ms ≤ 200 ms，SQLITE_BUSY 0；主要持锁者 engine（占比 0.4%）
- **3x50/s**：无饿死：最差的 http p99 等待 0.11 ms ≤ 200 ms，SQLITE_BUSY 0；主要持锁者 engine（占比 1.1%）
- **3x50/s deferred+think5ms**：engine 持锁占比 0.3%（持锁 p99 0.25 ms），engine 被饿：p99 等待 0.1 ms、SQLITE_BUSY 111 次；根因：延迟事务（BEGIN DEFERRED）先读后写：读快照过期时升级写锁直接返回 SQLITE_BUSY_SNAPSHOT，busy_timeout 不生效 → 写事务改用 BEGIN IMMEDIATE

| autocheckpoint（页） | 长读事务 | WAL 文件峰值 MB | 有效帧峰值 | 重置次数 | 提交 p99 ms | 有效帧随时间（每 0.1 s） |
|---:|---|---:|---:|---:|---:|---|
| 200 | 无 | 0.8 | 204 | 18 | 2.84 | `▄███████▄▄▄▄█████▄▄▄▄▄▄▄▅▅▅▅▅▅` |
| 200 | 有 | 20.8 | 5044 | 0 | 0.84 | `  ▁▁▁▂▂▂▃▃▃▃▄▄▄▄▅▅▅▅▆▆▆▆▇▇▇▇██` |
| 1000 | 无 | 4.1 | 998 | 4 | 0.49 | `▁▂▃▅▆▇█▂▃▄▆▇█▂▃▄▆▇▇▁▃▄▅▇█▁▃▄▅▆` |
| 1000 | 有 | 19.8 | 4816 | 0 | 0.43 | `  ▁▁▁▁▂▂▂▃▃▃▃▄▄▄▄▅▅▅▆▆▆▆▇▇▇▇██` |
| 4000 | 无 | 16.5 | 3985 | 1 | 0.27 | `  ▁▁▁▂▂▂▃▃▃▄▄▄▅▅▅▆▆▆▇▇▇██ ▁▁▁▂` |
| 4000 | 有 | 19.8 | 4811 | 0 | 0.44 | `  ▁▁▁▁▂▂▂▃▃▃▃▄▄▄▄▅▅▅▆▆▆▆▇▇▇▇██` |

结论：无长读时 WAL 有效帧峰值都被 autocheckpoint 截住（峰值/阈值 204/200, 998/1000, 3985/4000）；有长读时全部场景 WAL 一次也没重置，峰值 20.8, 19.8, 19.8 MB，与 autocheckpoint 取值无关 → 面板读走副本 / 缩短读事务，而不是调 autocheckpoint

## 2. G2 回归闸（qbt 回测真实用例）

### 2.1 放慢 5% / 12% 两档

| 档位 | 无意义循环空转时长 | 实测热墙钟变化（3 遍中位数） | 闸（缺省容差）stdout 首行 | 闸（容差只取 2×噪声） | 新进前 10 的函数 |
|---|---:|---:|---|---|---|
| 放慢 12% | 0.12 × 本次调用时长 | +14.9% | `REGRESSION hot.wall_s=1.111→1.277 (+14.9%)` | `REGRESSION hot.wall_s=1.111→1.277 (+14.9%)` | `qdperf/examples/qbt_case.py:meaningless_loop` |
| 放慢 5% | 0.05 × 本次调用时长 | +4.6% | `OK hot.wall_s=1.111→1.163 (+4.6%) tol=10.0%` | `REGRESSION hot.wall_s=1.111→1.163 (+4.6%)` | `qbt/engine.py:Context.target` |

“故意放慢”= 回测之后跑一个无意义循环，空转时长 = 该档比例 × 本次调用已用时长，所以每次调用恰好慢 5% / 12%，与进程本身的快慢无关。
**5% 档按噪声幅度决定**：缺省容差 = max(10%, 2 × 噪声) = 10.0%，5% 低于它 → 绿（按设计）；
若把手动容差设成 0（`--wall-tol 0`，只看噪声），容差 = 2 × 噪声 = 0.3%，此时 5% 档的判定为
`REGRESSION hot.wall_s=1.111→1.163 (+4.6%)`。12% 档每一轮单独拿出来对比基线：3/3 轮判红（`REGRESSION hot.wall_s=1.111→1.277 (+14.9%)`；`REGRESSION hot.wall_s=1.111→1.332 (+19.9%)`；`REGRESSION cold.wall_s=3.395→4.035 (+18.9%)`）。

### 2.2 三遍基线

| 基线第几遍 | 热墙钟 s（8 进程最快） | 冷墙钟 s（8 进程最快） | tracemalloc 峰值字节 |
|---:|---:|---:|---:|
| 1 | 1.1106 | 3.3950 | 125,490,348 |
| 2 | 1.1125 | 3.3677 | 125,490,348 |
| 3 | 1.1111 | 3.6623 | 125,490,348 |

### 2.3 为什么 best-of-N 要跨进程

同一段纯 Python 空转（`qdperf/tools/noise_probe.py`，2,000,000 次循环）在 12 个全新进程里各自取最快，折合 41.3–44.8 ns/次（进程之间相差 8.6%）：每个进程有自己的“快慢”，一个进程内的 N 个样本跳不出它。三遍基线的相对极差（5 次重复的中位数 / 最大）：进程内 best-of-5 为 4.8% / 21.3%，跨进程 best-of-5（每遍 5 个全新进程各测一次取最快）为 2.9% / 4.8%。所以回归闸缺省按进程取样；`pair` 模式（基线与本次交替测）再把分钟级的慢漂移同时落到两边。复现：`python3.12 -m qdperf.tools.noise_probe --trials 5 --n 5 --procs 12`。

## 3. G3 tick 抖动

判据只按形状：面板长查询先标定到目标 ±20%（达不到 → 判红，不往下跑）；Q=0 时 p99 ≤ 1.2T；存在某个 Q 使 p99 > 2T；至少两条缓解把拐点配置的 p99 降回 ≤ 2T。拐点落在哪个 Q、哪几条缓解有效取决于平台（每行扫描耗时、文件锁的实现、线程调度），§3.1 / §3.2 的具体 Q 与缓解名单是**本机实测，平台相关**，换一台机器会变。

### 3.1 拐点表（本机实测，平台相关；tick 间隔 p99，单位 T；“←拐点”= 该组合里 p99 > 2T 的最小 Q）

```
日志    busy_timeout  算段        Q=0    Q=5          Q=20     Q=50
------  ------------  ----------  -----  -----------  -------  -------
WAL 关  busy 5s       inline      1.01T  8.55T ←拐点  101.67T  107.37T
WAL 关  busy 5s       subprocess  1.01T  7.17T ←拐点  197.96T  197.74T
WAL 关  busy 0.5s     inline      1.01T  6.96T ←拐点  21.01T   21.32T
WAL 关  busy 0.5s     subprocess  1.00T  7.21T ←拐点  22.65T   21.01T
WAL 开  busy 5s       inline      1.01T  1.02T        1.07T    1.07T
WAL 开  busy 5s       subprocess  1.01T  1.01T        1.06T    1.07T
WAL 开  busy 0.5s     inline      1.00T  1.01T        1.08T    1.29T
WAL 开  busy 0.5s     subprocess  1.00T  1.01T        1.16T    1.30T
```

机理：WAL 关（rollback journal）时，面板的长查询持有 SHARED 锁，引擎提交要拿 EXCLUSIVE，必须等当前读完；面板查询 ≈ 2.54T（标定目标 2.5T ± 20%，偏差 +1.5%；扫描 243,275 行 × 1 遍），
所以一碰上就把这个 tick 拖过 2T。WAL 开时读不挡写，引擎几乎不受面板影响。

### 3.2 缓解（在拐点配置上；本机实测，平台相关）

| 缓解 | tick 间隔 p50 | p99 | 最大 | 超过 2T 的 tick | 面板实际轮询 / 秒 | 面板查询 p50 ms | 有效（p99 ≤ 2T） |
|---|---:|---:|---:|---:|---:|---:|---|
| replica | 1.00T | 1.03T | 1.22T | 0/399 | 5.0 | 141 | 是 |
| chunked | 1.00T | 1.18T | 1.44T | 0/398 | 5.0 | 157 | 是 |
| backoff | 1.00T | 2.89T | 4.90T | 7/383 | 0.3 | 149 | 否 |
| wal | 1.00T | 1.01T | 1.17T | 0/399 | 5.0 | 142 | 是 |

### 3.3 算段放 GIL 内 vs 子进程

| 算段 | 引擎进程里的忙线程 | 读段 p50 ms | 算段 p50 ms | 算段 p99 ms | 写段 p50 ms | tick 间隔 p99 |
|---|---:|---:|---:|---:|---:|---:|
| inline | 0 | 0.1 | 11.4 | 23.2 | 0.9 | 1.00T |
| subprocess | 0 | 0.1 | 14.8 | 27.2 | 1.0 | 1.01T |
| inline | 1 | 99.5 | 15.1 | 34.5 | 18.3 | 6.04T |
| subprocess | 1 | 140.5 | 20.7 | 28.3 | 32.3 | 6.43T |

引擎进程里加 1 个纯 Python 忙线程后，tick 间隔 p99 从 1.00T 变成 6.04T（算段在进程内）/ 6.43T（算段在子进程）。p50 增长最多的是读段：读段 0.1 → 99.5 ms、算段 11.4 → 15.1 ms、写段 0.9 → 18.3 ms。把算段挪到子进程没有让 tick 回到 2T 以内（6.43T）：被拖慢的不只是算段——sqlite3 模块每取一行（sqlite3_step）都先放开 GIL 再拿回，写段的每次 execute / commit 也一样，而纯 Python 忙线程只在切换间隔（sys.getswitchinterval()，缺省 5 ms）到点时才让出 GIL；子进程算完回传后，引擎线程收结果同样要等 GIL。

复现：`python3.12 -m qdperf.tick_jitter run --q 0 --wal on --bg-threads 1 --compute subprocess`（`--compute inline` / `--bg-threads 0` 对照）。

## 4. G4 HTTP

### 4.1 并发爬坡（假后端 `/limited`：延迟 p50 50 ms / p99 60 ms，并发上限 8）

| 并发 C | 成功/秒 | p50 ms | p95 ms | p99 ms | 错误率 |
|---:|---:|---:|---:|---:|---:|
| 1 | 19.7 | 50.8 | 56.7 | 58.6 | 0.00% |
| 2 | 39.3 | 50.9 | 56.7 | 59.3 | 0.00% |
| 4 | 78.3 | 50.6 | 58.7 | 61.1 | 0.00% |
| 8 | 158.7 | 50.6 | 57.1 | 60.5 | 0.00% |
| 16 | 158.3 | 100.8 | 110.6 | 114.2 | 0.00% |
| 32 | 159.0 | 201.9 | 213.0 | 346.3 | 0.00% |

### 4.2 A/B（A、B、A 交替）

| 对比 | 路径 | 分位 | A（两遍均值）ms | B ms | 变化 | 容差 | 判定 |
|---|---|---|---:|---:|---:|---:|---|
| A vs B（B 慢 30%） | a1 | p50 | 20.63 | 26.46 | +28.3% | 10.0% | 红 |
| A vs B（B 慢 30%） | a1 | p99 | 60.63 | 79.80 | +31.6% | 10.0% | 红 |
| A vs B（B 慢 30%） | a2 | p50 | 10.65 | 13.62 | +27.8% | 10.0% | 红 |
| A vs B（B 慢 30%） | a2 | p99 | 12.69 | 16.21 | +27.7% | 10.0% | 红 |
| A vs A'（同配置） | a1 | p50 | 20.70 | 20.57 | -0.6% | 10.0% | 绿 |
| A vs A'（同配置） | a1 | p99 | 63.79 | 60.63 | -5.0% | 18.2% | 绿 |
| A vs A'（同配置） | a2 | p50 | 10.68 | 10.65 | -0.4% | 10.0% | 绿 |
| A vs A'（同配置） | a2 | p99 | 13.22 | 12.72 | -3.8% | 10.0% | 绿 |

## 5. 覆盖率

| 文件 | 语句 | 未覆盖 | 覆盖率 |
|---|---:|---:|---:|
| `qdperf/__init__.py` | 1 | 0 | 100.0% |
| `qdperf/common.py` | 219 | 26 | 88.1% |
| `qdperf/fake_backend.py` | 208 | 1 | 99.5% |
| `qdperf/http_load.py` | 233 | 1 | 99.6% |
| `qdperf/index_advisor.py` | 180 | 2 | 98.9% |
| `qdperf/profile_gate.py` | 313 | 1 | 99.7% |
| `qdperf/sqlite_doctor.py` | 516 | 4 | 99.2% |
| `qdperf/sqlite_load.py` | 242 | 3 | 98.8% |
| `qdperf/sqlplan.py` | 443 | 2 | 99.5% |
| `qdperf/synth_db.py` | 171 | 0 | 100.0% |
| `qdperf/tick_jitter.py` | 538 | 26 | 95.2% |
| **合计（计入判据）** | 3064 | 66 | **97.85%** |

## 6. 突变测试

| 模块 | 候选 | 突变体 | 第 3 轮首跑抓住率 | 最终抓住 | 超时 | 最终漏网（均已判定） | 最终抓住率 |
|---|---:|---:|---:|---:|---:|---:|---:|
| `common.py` | 308 | 50 | 96.0% | 48 | 0 | 2 | 96.0% |
| `fake_backend.py`（改库后重抽样） | 346 | 50 | 76.0% | 45 | 0 | 5 | 90.0% |
| `http_load.py` | 386 | 50 | 98.0% | 49 | 0 | 1 | 98.0% |
| `index_advisor.py` | 293 | 50 | 92.0% | 46 | 0 | 4 | 92.0% |
| `profile_gate.py` | 489 | 50 | 100.0% | 50 | 0 | 0 | 100.0% |
| `sqlite_doctor.py`（改库后重抽样） | 850 | 50 | 72.0% | 48 | 0 | 2 | 96.0% |
| `sqlite_load.py` | 453 | 50 | 64.0% | 42 | 0 | 8 | 84.0% |
| `sqlplan.py` | 1095 | 50 | 84.0% | 46 | 1 | 3 | 94.0% |
| `synth_db.py` | 479 | 50 | 60.0% | 50 | 0 | 0 | 100.0% |
| `tick_jitter.py` | 837 | 50 | 64.0% | 49 | 0 | 1 | 98.0% |
| **合计** | | 500 | 80.6% | 473 | 1 | 26 | **94.8%** |

上表“最终”列是补判据、改库之后重跑的结果（`python3.12 qdperf/mutants/run.py --per-module 50 --jobs 4` 起，重跑命令记在 `meta.reruns`）；“第 3 轮首跑”是补判据之前、抓住须确认、跑的过程中不改任何文件的那一轮（`results/mutation_round3.json`）。第 1、2 轮因测试抖动 / 排版判据误杀而作废（留档在 `results/`）。经过、每条漏网的判定（等价 / 补判据，各带理由）见 [mutants/triage.md](mutants/triage.md)。
