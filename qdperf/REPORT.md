# qdperf — 量化交易系统性能体检工具包（REPORT）

纯 Python 3.12 标准库，零第三方依赖，零出网。**所有数据都是合成的**；所有判据只用容差和形状 / 倍数，
性能目标写成“相对自身基线”或“相对假后端注入值”，不拿绝对毫秒当达成判据（表里出现的毫秒数只是本机实测，供参考）。

> 本文件由 `python3.12 qdperf/tools/build_report.py` 从 `qdperf/tools/report_template.md` 与 `qdperf/results/*.json`
> 生成，表中每个数字都取自入库的结果文件（`qdperf/tests/test_report.py` 检查本文件与结果文件一致）。

## 0. 机器与口径

| 结果文件 | 生成机器 / Python / SQLite / 提交 / 时间 |
|---|---|
| `results/g1_synth_verify.json` | Linux-6.18.44-fc-v140-x86_64-with-glibc2.39，Python 3.12.3，Intel(R) Xeon(R) Processor @ 2.80GHz × 4，SQLite 3.45.1，提交 441e683，2026-10-22T22:18:06 |
| `results/g1_doctor.json` | Linux-6.18.44-fc-v140-x86_64-with-glibc2.39，Python 3.12.3，Intel(R) Xeon(R) Processor @ 2.80GHz × 4，SQLite 3.45.1，提交 441e683，2026-10-22T22:15:35 |
| `results/g2_profile_gate.json` | Linux-6.18.44-fc-v140-x86_64-with-glibc2.39，Python 3.12.3，Intel(R) Xeon(R) Processor @ 2.80GHz × 4，SQLite 3.45.1，提交 7c602ea，2026-10-22T22:49:13 |
| `results/g2_noise_probe.json` | Linux-6.18.44-fc-v140-x86_64-with-glibc2.39，Python 3.12.3，Intel(R) Xeon(R) Processor @ 2.80GHz × 4，SQLite 3.45.1，提交 ed9b896，2026-10-22T22:02:19 |
| `results/g3_tick_jitter.json` | Linux-6.18.44-fc-v140-x86_64-with-glibc2.39，Python 3.12.3，Intel(R) Xeon(R) Processor @ 2.80GHz × 4，SQLite 3.45.1，提交 5e158a4，2026-10-22T22:33:34 |
| `results/g4_http_load.json` | Linux-6.18.44-fc-v140-x86_64-with-glibc2.39，Python 3.12.3，Intel(R) Xeon(R) Processor @ 2.80GHz × 4，SQLite 3.45.1，提交 5e158a4，2026-10-22T22:33:34 |
| `results/selftest_summary.json` | Linux-6.18.44-fc-v140-x86_64-with-glibc2.39，Python 3.12.3，Intel(R) Xeon(R) Processor @ 2.80GHz × 4，SQLite 3.45.1，提交 7c602ea，2026-10-22T22:49:13 |

- **best-of-N**：回归闸的 N 个样本来自 N 个**全新进程**（每个进程：导入 + 第一次调用 = 冷样本，紧接着的调用 = 热样本），
  冷 / 热各取 N 个进程里最快的；`--runs 3` 时三遍的中位数作基线值、三遍的相对极差作噪声幅度。原因见 §2.3。
- **冷 / 热**：冷 = 新进程里导入目标模块 + 第一次调用（含数据生成、首次分配；不含解释器启动），热 = 同一进程紧接着的调用。
- **分位数**：线性插值（第 7 型）；写争用 / tick / HTTP 的 p99 都是全部样本的分位，不是 best-of-N。

## 1. 目标达成表

| 目标 | 要求 | 实测 | 达成 | 复现命令 |
|---|---|---|---|---|
| G1 | 合成库：users 1 万、bots 5 万、orders 200 万、trades 500 万、settings 300、events 100 万，同 seed 同结果，≤ 3 分钟 | 7,980,300 行（清理掉 8% 旧 events 后），生成 42.1 s；两次生成逻辑指纹一致：True（第二次 41.8 s） | ✅ | `python3.12 -m qdperf.synth_db --scale 1 --fingerprint` |
| G1 | 每条语句 EXPLAIN QUERY PLAN 判定（全表扫描 / 临时 B-tree / 索引 / 覆盖索引），按 频次 × 行数 排前 20 | 23 条语句：全表扫描 11、临时 B-tree 6；前 12 名见 §1.1 | ✅ | `python3.12 -m qdperf.sqlite_doctor <库> --workload qdperf/examples/workload_trading.json` |
| G1 | 索引建议（等值 → 范围 → 排序），临时副本上验证，给大小与写放大 | 10 条建议全部验证通过；**独立复核**（把输出的 SQL 在原库的另一份拷贝上执行再 EXPLAIN）11 项全部不再 SCAN：True；仍扫描的只剩 ['h_email_search']（`LIKE '%..%'`，单列原因）；≥ 1 MB 的索引大小估算误差 ≤ 0.3%；B-tree 写入 132.51 → 299.04 次/秒 | ✅ | 同上 `--emit-sql idx.sql` |
| G1 | 写争用：3 写者 × 50 次/秒（WAL + busy_timeout 5 s）p99 等待 ≤ 200 ms，否则给根因；谁饿死谁 | 真进程实测 p99 0.219 ms、最大 3.99 ms、SQLITE_BUSY 0；无饿死：最差的 notifier p99 等待 0.49 ms ≤ 200 ms，SQLITE_BUSY 0；主要持锁者 engine（占比 1.1%）。根因演示（延迟事务读后写）：SQLITE_BUSY 113 次，根因判定为 BUSY_SNAPSHOT | ✅ | 同上 `--deferred-demo` |
| G1 | WAL 检查点：长读事务把 WAL 撑大的曲线、wal_autocheckpoint 的影响 | 无长读时 WAL 峰值被 autocheckpoint 截住：True；有长读时 WAL 一次也没重置：True（曲线见 §1.3） | ✅ | 同上 |
| G1 | 库文件体检：页大小、碎片率、没主键的表、没索引的外键、ANALYZE / stat1 过期 | 页 4096 B、碎片率 1.0%、无主键 `settings`、无索引外键 6 列、`idx_orders_user` 统计过期（1,000,000 vs 2,000,000 行） | ✅ | 同上 `--sections file` |
| G1 | 工具零写入原库 | 体检前后原库 sha256 / 大小 / mtime / 旁路文件清单一致：True（WAL 库无 -wal 时 `immutable=1` 只读打开，计划与验证都在 tempfile 副本上） | ✅ | `qdperf/tests/test_doctor.py` |
| G2 | 可调用 → 墙钟 / CPU / tracemalloc 峰值 / cProfile 前 30，冷热各一遍，best-of-N（缺省 5），落 JSON | qbt 回测 1,000,000 根 × ma_cross：热 1.359 s、冷 4.299 s、峰值 125,490,348 字节（本实验 N = 8 个进程 × 每进程热调用 2 次） | ✅ | `python3.12 -m qdperf.tools.selfcheck g2` |
| G2 | 回归闸：墙钟 +10% 红、峰值内存 +20% 红、新进前 10 的函数点名；首行 `REGRESSION <指标>=<基线>→<本次> (+<百分比>)`；3 遍基线噪声 × 2 与手动容差取大 | 3 遍基线噪声：热 4.3%、冷 2.2%、内存 0.0% → 热墙钟容差 10.0% | ✅ | `python3.12 -m qdperf.profile_gate compare base.json new.json` |
| G2 | 故意放慢 12% 必红；5% 按噪声幅度决定（写明）；原样重跑不红 | 12%：`REGRESSION cold.wall_s=4.299→4.937 (+14.8%)`，新进前 10：[]；5%：`OK hot.wall_s=1.359→1.415 (+4.1%) tol=10.0%`（容差 10.0% > 5% → 放慢 5% 落在容差内，按设计应判绿；实际 OK）；原样重跑：`OK hot.wall_s=1.359→1.371 (+0.9%) tol=10.0%` | ✅ | 同上 |
| G2 | 两份 JSON 的函数名集合可比（去掉行号 / 临时路径） | 原样重跑前 10 函数集合相同：True；整份代码拷到临时目录再测：前 10 相同 True、前 30 的 Jaccard 1.0；名字里无行号 / 临时路径：True | ✅ | `qdperf/tests/test_profile_gate.py` |
| G3 | 三个真进程（引擎每 T 一个 tick、面板 Q 次/秒长查询、通知偶发写），量 tick 间隔 p50 / p99 / 最大、过 2T 次数、读 / 算 / 写分段 | T = 0.05 s；面板长查询 249,999 行、单线程 147 ms（≈ 2.9T（标定目标 2.5T，行数已封顶为全表））；共 32 个配置 × 20.0 s（全表见 §3） | ✅ | `python3.12 -m qdperf.tick_jitter scan` |
| G3 | Q=0 时 p99 间隔 ≤ 1.2T | 全部 Q=0 配置的最大 p99 = 1.021T | ✅ | 同上 |
| G3 | 证明某个 Q 下 p99 > 2T（饿死点），并给拐点表 | 拐点：WAL 关、busy 5 s、inline：Q = 5.0 时 p99 = 19.64T（表见 §3.1） | ✅ | 同上 |
| G3 | 两条有效缓解各自的实测改善 | 有效（p99 ≤ 2T）：分块读 p99 1.02T（改善 19.2 倍，超过 2T 的 tick 2 个）、读只读副本 p99 1.22T（改善 16.1 倍，超过 2T 的 tick 0 个）、切 WAL p99 1.00T（改善 19.6 倍，超过 2T 的 tick 0 个）；实测无效的也照列：轮询退避 p99 3.96T（改善 5.0 倍，超过 2T 的 tick 7 个）（见 §3.2） | ✅ | `python3.12 -m qdperf.tick_jitter run --wal off --q 5 --mitigation replica`（或 `chunked` / `backoff` / `wal`） |
| G4 | 假后端注入 p99 = 120 ms → 量出 108–132 ms | 119.5 ms（误差 -0.4%，1,306 个请求，C = 8） | ✅ | `python3.12 -m qdperf.tools.selfcheck g4` |
| G4 | 注入并发上限 8 → 拐点报 6–10 | 拐点 8（吞吐见 §4.1） | ✅ | 同上 |
| G4 | A/B 中 B 比 A 慢 30% → 必红 | `REGRESSION a1.p99_ms=60.76→81.17 (+33.6%)`（4/4 项红）；同配置 A/A：`OK a2.p99_ms=12.99→13.26 (+2.0%) tol=10.0%` | ✅ | 同上 |
| G5 | 一条命令自检，任何一项失败非零退出 | `python3.12 -m qdperf.selftest`：169 tests 等 8 步，见 README 第一屏 | ✅ | `python3.12 -m qdperf.selftest` |
| G5 | 语句覆盖率 ≥ 90%（复用 tools/cov.py 做法） | **97.77%**（2938/3005 条，含 119 个子进程的覆盖；表见 §5） | ✅ | `python3.12 qdperf/tools/cov.py --fail-under 90` |
| G5 | 突变抓住率 ≥ 85%，每模块 ≥ 40 个突变体，活下来的逐条判定 | 第 3 轮首跑 80.6%（403/500，抓住须确认），97 条漏网逐条判定、补判据并修 3 个真问题后重跑 → **95.4%**（477/500，每模块 50 个；漏网逐条判定见 `mutants/triage.md`） | ✅ | `python3.12 qdperf/mutants/run.py --per-module 50 --jobs 4` |
| G5 | 零依赖 / 零出网 / 零写仓外各一条测试 | `test_constraints.py`：grep + AST 只许标准库；静态无 socket / urllib.request、http.client 只经本机校验；运行时审计钩子跑一遍四个工具（含全部子进程），写盘只落 tempfile 与 results/，socket 只连 127.0.0.1 | ✅ | `python3.12 -m unittest qdperf.tests.test_constraints` |

### 1.1 最贵的 12 条语句（全量合成库，频次 × 行数）

| # | 语句 | 进程 | 次/秒 | 判定 | 行/次（估） | 频次×行数（行/秒） | 实测 ms（best-of-3） | 占库 ms/秒 |
|---:|---|---|---:|---|---:|---:|---:|---:|
| 1 | `e_open_orders` | engine | 50 | full_scan | 2,000,000 | 100,000,000 | 109.330 | 5,466.5 |
| 2 | `e_last_price` | engine | 10 | full_scan | 5,000,000 | 50,000,000 | 392.798 | 3,928.0 |
| 3 | `h_trades_by_user` | http | 10 | full_scan | 5,000,000 | 50,000,000 | 284.024 | 2,840.2 |
| 4 | `h_dashboard_pnl` | http | 2 | full_scan | 5,000,000 | 10,000,000 | 354.823 | 709.6 |
| 5 | `h_events_feed` | http | 10 | full_scan | 920,000 | 9,200,000 | 46.937 | 469.4 |
| 6 | `n_pending_alerts` | notifier | 1 | full_scan | 920,000 | 920,000 | 67.017 | 67.0 |
| 7 | `h_bots_of_user` | http | 15 | full_scan | 50,000 | 750,000 | 3.144 | 47.2 |
| 8 | `e_running_bots` | engine | 1 | full_scan | 50,000 | 50,000 | 17.948 | 17.9 |
| 9 | `n_purge_events` | notifier | 0.01 | full_scan | 920,000 | 9,200 | 51.732 | 0.5 |
| 10 | `h_setting` | http | 30 | full_scan | 300 | 9,000 | 0.015 | 0.5 |
| 11 | `h_orders_by_user` | http | 20 | temp_btree | 240 | 4,800 | 1.995 | 39.9 |
| 12 | `h_orders_join_bots` | http | 5 | index | 480 | 2,400 | 3.124 | 15.6 |

### 1.2 索引建议（全部在副本上建索引 + ANALYZE 后用 EXPLAIN QUERY PLAN 验证；独立复核见上表）

| 索引（列顺序 = 等值 → 范围 → 排序） | 服务的语句 | 建索引+ANALYZE 后不再扫描 | 估算 MB | 实测 MB | 构建 s | +B-tree 写/秒 |
|---|---|---|---:|---:|---:|---:|
| `CREATE INDEX qdperf_bots_status_updated_at ON bots(status, updated_at)` | e_running_bots | 1/1 | 1.01 | 1.01 | 0.04 | 20.5 |
| `CREATE INDEX qdperf_bots_user_id ON bots(user_id)` | h_bots_of_user | 1/1 | 0.52 | 0.52 | 0.02 | 0 |
| `CREATE INDEX qdperf_events_level_notified_ts ON events(level, notified, ts)` | n_pending_alerts | 1/1 | 17.65 | 17.60 | 0.74 | 12.01 |
| `CREATE INDEX qdperf_events_user_id_ts ON events(user_id, ts)` | h_events_feed | 1/1 | 14.76 | 14.74 | 0.59 | 7.01 |
| `CREATE INDEX qdperf_events_ts ON events(ts)` | n_purge_events | 1/1 | 12.05 | 12.02 | 0.30 | 7.01 |
| `CREATE INDEX qdperf_orders_user_id_status_created_at ON orders(user_id, status, created_at)` | h_orders_by_user | 1/1 | 47.26 | 47.30 | 1.92 | 40 |
| `CREATE INDEX qdperf_orders_bot_id_status ON orders(bot_id, status)` | e_open_orders | 1/1 | 37.79 | 37.80 | 1.52 | 40 |
| `CREATE INDEX qdperf_settings_key ON settings(key)` | h_setting | 1/1 | 0.01 | 0.01 | 0.00 | 0 |
| `CREATE INDEX qdperf_trades_symbol_ts ON trades(symbol, ts)` | h_dashboard_pnl, e_last_price | 1/1 | 107.11 | 107.16 | 4.20 | 20 |
| `CREATE INDEX qdperf_trades_user_id_ts ON trades(user_id, ts)` | h_trades_by_user | 1/1 | 80.23 | 80.04 | 3.45 | 20 |

### 1.3 写争用（真进程，WAL，busy_timeout 5 s）与 WAL 检查点

| 场景 | 写者 | 实际次/秒 | 提交 | 等待 p50 ms | 等待 p99 ms | 最大 ms | SQLITE_BUSY | 持锁 p99 ms |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| workload | http | 2.5 | 10 | 0.074 | 0.105 | 0.106 | 0 | 0.125 |
| workload | engine | 20.0 | 80 | 0.054 | 0.104 | 0.121 | 0 | 0.584 |
| workload | notifier | 10.0 | 40 | 0.076 | 0.189 | 0.191 | 0 | 0.148 |
| 3x50/s | http | 50.0 | 200 | 0.075 | 0.142 | 0.396 | 0 | 0.951 |
| 3x50/s | engine | 50.0 | 200 | 0.060 | 0.129 | 0.492 | 0 | 0.618 |
| 3x50/s | notifier | 50.0 | 200 | 0.058 | 0.493 | 3.992 | 0 | 0.115 |
| 3x50/s deferred+think5ms | http | 49.8 | 199 | 0.081 | 0.168 | 0.501 | 1 | 0.119 |
| 3x50/s deferred+think5ms | engine | 22.2 | 89 | 0.083 | 0.585 | 2.091 | 111 | 0.202 |
| 3x50/s deferred+think5ms | notifier | 49.8 | 199 | 0.074 | 0.143 | 0.369 | 1 | 0.145 |

- **workload**：无饿死：最差的 notifier p99 等待 0.19 ms ≤ 200 ms，SQLITE_BUSY 0；主要持锁者 engine（占比 0.3%）
- **3x50/s**：无饿死：最差的 notifier p99 等待 0.49 ms ≤ 200 ms，SQLITE_BUSY 0；主要持锁者 engine（占比 1.1%）
- **3x50/s deferred+think5ms**：http 持锁占比 0.3%（持锁 p99 0.12 ms），engine 被饿：p99 等待 0.6 ms、SQLITE_BUSY 111 次；根因：延迟事务（BEGIN DEFERRED）先读后写：读快照过期时升级写锁直接返回 SQLITE_BUSY_SNAPSHOT，busy_timeout 不生效 → 写事务改用 BEGIN IMMEDIATE；busy_timeout=5.0 s 内拿不到写锁：同时写者 × 持锁时间超过超时 → 缩短事务 / 合并写 / 加大超时

| autocheckpoint（页） | 长读事务 | WAL 文件峰值 MB | 有效帧峰值 | 重置次数 | 提交 p99 ms | 有效帧随时间（每 0.1 s） |
|---:|---|---:|---:|---:|---:|---|
| 200 | 无 | 0.8 | 200 | 26 | 2.97 | `▄██▄▄▄▄▄▄▄▄▄▄▄▄▄█▄▄▄▄▄▄▅▅▅▅▅▅▅` |
| 200 | 有 | 20.8 | 5044 | 0 | 0.53 | `  ▁▁▁▂▂▂▃▃▃▃▄▄▄▄▅▅▅▅▆▆▆▆▇▇▇▇██` |
| 1000 | 无 | 4.1 | 998 | 4 | 0.42 | `▁▂▃▅▆▇█▂▃▄▆▇█▂▃▄▆▇▇▁▃▄▅▇█▁▃▄▅▆` |
| 1000 | 有 | 19.9 | 4820 | 0 | 0.32 | `  ▁▁▁▁▂▂▂▃▃▃▃▄▄▄▄▅▅▅▆▆▆▆▇▇▇▇██` |
| 4000 | 无 | 16.5 | 3985 | 1 | 0.40 | `  ▁▁▁▂▂▂▃▃▃▄▄▄▅▅▅▆▆▆▇▇▇██ ▁▁▁▂` |
| 4000 | 有 | 19.8 | 4811 | 0 | 0.46 | `  ▁▁▁▁▂▂▂▃▃▃▃▄▄▄▄▅▅▅▆▆▆▆▇▇▇▇██` |

结论：无长读时 WAL 有效帧峰值都被 autocheckpoint 截住（峰值/阈值 200/200, 998/1000, 3985/4000）；有长读时全部场景 WAL 一次也没重置，峰值 20.8, 19.9, 19.8 MB，与 autocheckpoint 取值无关 → 面板读走副本 / 缩短读事务，而不是调 autocheckpoint

## 2. G2 回归闸（qbt 回测真实用例）

### 2.1 放慢 5% / 12% 两档

| 档位 | 无意义循环空转时长 | 实测热墙钟变化（3 遍中位数） | 闸（缺省容差）stdout 首行 | 闸（容差只取 2×噪声） | 新进前 10 的函数 |
|---|---:|---:|---|---|---|
| 放慢 12% | 0.12 × 本次调用时长 | +10.7% | `REGRESSION cold.wall_s=4.299→4.937 (+14.8%)` | `REGRESSION cold.wall_s=4.299→4.937 (+14.8%)` | 无 |
| 放慢 5% | 0.05 × 本次调用时长 | +4.1% | `OK hot.wall_s=1.359→1.415 (+4.1%) tol=10.0%` | `REGRESSION cold.wall_s=4.299→4.566 (+6.2%)` | 无 |

“故意放慢”= 回测之后跑一个无意义循环，空转时长 = 该档比例 × 本次调用已用时长，所以每次调用恰好慢 5% / 12%，与进程本身的快慢无关。
**5% 档按噪声幅度决定**：缺省容差 = max(10%, 2 × 噪声) = 10.0%，5% 低于它 → 绿（按设计）；
若把手动容差设成 0（`--wall-tol 0`，只看噪声），容差 = 2 × 噪声 = 8.6%，此时 5% 档的判定为
`REGRESSION cold.wall_s=4.299→4.566 (+6.2%)`。12% 档每一轮单独拿出来对比基线：3/3 轮判红（`REGRESSION cold.wall_s=4.299→4.955 (+15.3%)`；`REGRESSION cold.wall_s=4.299→4.906 (+14.1%)`；`REGRESSION cold.wall_s=4.299→4.937 (+14.8%)`）。

### 2.2 三遍基线

| 基线第几遍 | 热墙钟 s（8 进程最快） | 冷墙钟 s（8 进程最快） | tracemalloc 峰值字节 |
|---:|---:|---:|---:|
| 1 | 1.3274 | 4.2567 | 125,490,348 |
| 2 | 1.3844 | 4.2986 | 125,490,348 |
| 3 | 1.3591 | 4.3521 | 125,490,348 |

### 2.3 为什么 best-of-N 要跨进程

同一段纯 Python 空转（`qdperf/tools/noise_probe.py`，2,000,000 次循环）在 12 个全新进程里各自取最快，折合 41.3–44.8 ns/次（进程之间相差 8.6%）：每个进程有自己的“快慢”，一个进程内的 N 个样本跳不出它。三遍基线的相对极差（5 次重复的中位数 / 最大）：进程内 best-of-5 为 4.8% / 21.3%，跨进程 best-of-5（每遍 5 个全新进程各测一次取最快）为 2.9% / 4.8%。所以回归闸缺省按进程取样；`pair` 模式（基线与本次交替测）再把分钟级的慢漂移同时落到两边。复现：`python3.12 -m qdperf.tools.noise_probe --trials 5 --n 5 --procs 12`。

## 3. G3 tick 抖动

### 3.1 拐点表（tick 间隔 p99，单位 T；“←拐点”= 该组合里 p99 > 2T 的最小 Q）

```
日志    busy_timeout  算段        Q=0    Q=5           Q=20     Q=50
------  ------------  ----------  -----  ------------  -------  -------
WAL 关  busy 5s       inline      1.00T  19.64T ←拐点  102.07T  197.95T
WAL 关  busy 5s       subprocess  1.00T  16.98T ←拐点  101.54T  100.81T
WAL 关  busy 0.5s     inline      1.00T  14.34T ←拐点  21.11T   21.13T
WAL 关  busy 0.5s     subprocess  1.00T  10.61T ←拐点  20.78T   20.93T
WAL 开  busy 5s       inline      1.01T  1.01T         1.15T    1.39T
WAL 开  busy 5s       subprocess  1.00T  1.03T         1.07T    1.32T
WAL 开  busy 0.5s     inline      1.01T  1.01T         1.16T    1.32T
WAL 开  busy 0.5s     subprocess  1.02T  1.00T         1.33T    1.38T
```

机理：WAL 关（rollback journal）时，面板的长查询持有 SHARED 锁，引擎提交要拿 EXCLUSIVE，必须等当前读完；面板查询 ≈ 2.9T（标定目标 2.5T，行数已封顶为全表），
所以一碰上就把这个 tick 拖过 2T。WAL 开时读不挡写，引擎几乎不受面板影响。

### 3.2 缓解（在拐点配置上）

| 缓解 | tick 间隔 p50 | p99 | 最大 | 超过 2T 的 tick | 面板实际轮询 / 秒 | 面板查询 p50 ms | 有效（p99 ≤ 2T） |
|---|---:|---:|---:|---:|---:|---:|---|
| replica | 1.00T | 1.22T | 1.75T | 0/397 | 5.0 | 160 | 是 |
| chunked | 1.00T | 1.02T | 2.33T | 2/397 | 5.0 | 166 | 是 |
| backoff | 1.00T | 3.96T | 4.99T | 7/379 | 0.3 | 157 | 否 |
| wal | 1.00T | 1.00T | 1.01T | 0/399 | 5.0 | 161 | 是 |

### 3.3 算段放 GIL 内 vs 子进程

| 算段 | 引擎进程里的忙线程 | 读段 p50 ms | 算段 p50 ms | 算段 p99 ms | 写段 p50 ms | tick 间隔 p99 |
|---|---:|---:|---:|---:|---:|---:|
| inline | 0 | 0.1 | 12.9 | 28.4 | 0.9 | 1.01T |
| subprocess | 0 | 0.1 | 13.8 | 32.2 | 0.9 | 1.00T |
| inline | 1 | 138.8 | 23.5 | 54.8 | 37.7 | 7.59T |
| subprocess | 1 | 168.2 | 24.4 | 39.0 | 58.4 | 8.19T |

引擎进程里加 1 个纯 Python 忙线程后，tick 间隔 p99 从 1.01T 变成 7.59T（算段在进程内）/ 8.19T（算段在子进程）。p50 增长最多的是读段：读段 0.1 → 138.8 ms、算段 12.9 → 23.5 ms、写段 0.9 → 37.7 ms。把算段挪到子进程没有让 tick 回到 2T 以内（8.19T）：被拖慢的不只是算段——sqlite3 模块每取一行（sqlite3_step）都先放开 GIL 再拿回，写段的每次 execute / commit 也一样，而纯 Python 忙线程只在切换间隔（sys.getswitchinterval()，缺省 5 ms）到点时才让出 GIL；子进程算完回传后，引擎线程收结果同样要等 GIL。

复现：`python3.12 -m qdperf.tick_jitter run --q 0 --wal on --bg-threads 1 --compute subprocess`（`--compute inline` / `--bg-threads 0` 对照）。

## 4. G4 HTTP

### 4.1 并发爬坡（假后端 `/limited`：延迟 p50 50 ms / p99 60 ms，并发上限 8）

| 并发 C | 成功/秒 | p50 ms | p95 ms | p99 ms | 错误率 |
|---:|---:|---:|---:|---:|---:|
| 1 | 19.3 | 50.8 | 56.7 | 59.3 | 0.00% |
| 2 | 39.3 | 51.1 | 57.0 | 59.5 | 0.00% |
| 4 | 77.7 | 51.3 | 59.0 | 62.4 | 0.00% |
| 8 | 157.0 | 51.1 | 57.6 | 60.9 | 0.00% |
| 16 | 157.7 | 101.3 | 112.2 | 149.3 | 0.00% |
| 32 | 158.7 | 201.7 | 213.4 | 352.9 | 0.00% |

### 4.2 A/B（A、B、A 交替）

| 对比 | 路径 | 分位 | A（两遍均值）ms | B ms | 变化 | 容差 | 判定 |
|---|---|---|---:|---:|---:|---:|---|
| A vs B（B 慢 30%） | a1 | p50 | 20.75 | 26.83 | +29.3% | 10.0% | 红 |
| A vs B（B 慢 30%） | a1 | p99 | 60.76 | 81.17 | +33.6% | 10.0% | 红 |
| A vs B（B 慢 30%） | a2 | p50 | 10.81 | 13.86 | +28.2% | 10.0% | 红 |
| A vs B（B 慢 30%） | a2 | p99 | 13.99 | 16.66 | +19.1% | 10.0% | 红 |
| A vs A'（同配置） | a1 | p50 | 20.70 | 20.75 | +0.3% | 10.0% | 绿 |
| A vs A'（同配置） | a1 | p99 | 60.80 | 60.98 | +0.3% | 10.0% | 绿 |
| A vs A'（同配置） | a2 | p50 | 10.74 | 10.78 | +0.4% | 10.0% | 绿 |
| A vs A'（同配置） | a2 | p99 | 12.99 | 13.26 | +2.0% | 10.0% | 绿 |

## 5. 覆盖率

| 文件 | 语句 | 未覆盖 | 覆盖率 |
|---|---:|---:|---:|
| `qdperf/__init__.py` | 1 | 0 | 100.0% |
| `qdperf/common.py` | 204 | 26 | 87.3% |
| `qdperf/fake_backend.py` | 207 | 1 | 99.5% |
| `qdperf/http_load.py` | 233 | 1 | 99.6% |
| `qdperf/index_advisor.py` | 180 | 2 | 98.9% |
| `qdperf/profile_gate.py` | 313 | 1 | 99.7% |
| `qdperf/sqlite_doctor.py` | 516 | 4 | 99.2% |
| `qdperf/sqlite_load.py` | 242 | 3 | 98.8% |
| `qdperf/sqlplan.py` | 443 | 2 | 99.5% |
| `qdperf/synth_db.py` | 171 | 0 | 100.0% |
| `qdperf/tick_jitter.py` | 495 | 27 | 94.5% |
| **合计（计入判据）** | 3005 | 67 | **97.77%** |

## 6. 突变测试

| 模块 | 候选 | 突变体 | 第 3 轮首跑抓住率 | 最终抓住 | 超时 | 最终漏网（均已判定） | 最终抓住率 |
|---|---:|---:|---:|---:|---:|---:|---:|
| `common.py` | 308 | 50 | 96.0% | 48 | 0 | 2 | 96.0% |
| `fake_backend.py`（改库后重抽样） | 344 | 50 | 76.0% | 48 | 0 | 2 | 96.0% |
| `http_load.py` | 386 | 50 | 98.0% | 49 | 0 | 1 | 98.0% |
| `index_advisor.py` | 293 | 50 | 92.0% | 46 | 0 | 4 | 92.0% |
| `profile_gate.py` | 489 | 50 | 100.0% | 50 | 0 | 0 | 100.0% |
| `sqlite_doctor.py`（改库后重抽样） | 850 | 50 | 72.0% | 48 | 0 | 2 | 96.0% |
| `sqlite_load.py` | 453 | 50 | 64.0% | 42 | 0 | 8 | 84.0% |
| `sqlplan.py` | 1095 | 50 | 84.0% | 46 | 1 | 3 | 94.0% |
| `synth_db.py` | 479 | 50 | 60.0% | 50 | 0 | 0 | 100.0% |
| `tick_jitter.py` | 837 | 50 | 64.0% | 49 | 0 | 1 | 98.0% |
| **合计** | | 500 | 80.6% | 476 | 1 | 23 | **95.4%** |

上表“最终”列是补判据、改库之后重跑的结果（`python3.12 qdperf/mutants/run.py --per-module 50 --jobs 4` 起，重跑命令记在 `meta.reruns`）；“第 3 轮首跑”是补判据之前、抓住须确认、跑的过程中不改任何文件的那一轮（`results/mutation_round3.json`）。第 1、2 轮因测试抖动 / 排版判据误杀而作废（留档在 `results/`）。经过、每条漏网的判定（等价 / 补判据，各带理由）见 [mutants/triage.md](mutants/triage.md)。
