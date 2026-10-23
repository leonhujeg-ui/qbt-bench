**接库**：给 SQLite 库文件 + 工作负载 JSON → 体检表：`python3.12 -m qdperf.sqlite_doctor your.db --workload your_workload.json --json doctor.json --emit-sql indexes.sql`（原库只读，退出码 0 / 1 / 2）
**接代码**：给可调用 → 基线 JSON → 发版前后比：`python3.12 -m qdperf.profile_gate baseline pkg.mod:func --args '{"n": 1}' --out base.json`，发版后 `... run ... --out new.json`，再 `python3.12 -m qdperf.profile_gate compare base.json new.json`（红 = 退出码 1，首行 `REGRESSION <指标>=<基线>→<本次> (+<百分比>)`）
**接服务**：给 URL 清单 → A/B：`python3.12 -m qdperf.http_load ab --a http://127.0.0.1:8080 --b http://127.0.0.1:8081 --manifest urls.json`（只打本机，退出码 0 / 1 / 2）

```bash
python3.12 -m qdperf.selftest
```

期望输出（任何一行 `FAIL` 都会让退出码变成 1；下面是本仓一次真实运行，耗时与具体数字随机器变化）：

```
qdperf selftest (default)  Linux-6.18.44-fc-v140-x86_64-with-glibc2.39  Python 3.12.3  SQLite 3.45.1  4 CPU
step        scale                                            time  result  key numbers
unittest    172 tests                                      146.5s  OK    OK; skipped 0; tmp dirs left 0
constraints 3 tests (deps / network / writes)                0.0s  OK    stdlib-only ok; no-network ok; writes+connects audit ok
coverage    3006 statements, 172 tests                     148.1s  OK    97.77% (>= 90% required); child processes merged 119
g1_sqlite   synth 399,300 rows, 23 statements               52.8s  OK    synth 1.78s deterministic=True; suggestions 10 verified=True; 3x50/s p99 1.26 ms busy 0; untouched=True
g2_gate     demo_work.spin: pair x3 (12% slower) + rerun    11.4s  OK    noise 1.3%; REGRESSION hot.wall_s=0.01832→0.02062 (+12.5%); rerun OK hot.wall_s=0.01832→0.01835 (+0.1%) tol=10.0%; names equal True
g3_tick     6 configs x 10s, T=0.05s                       179.3s  OK    Q=0 p99 max 1.15T; knee Q=5 p99 8.32T; effective mitigations ['chunked', 'replica', 'wal']
g4_http     1316 req p99 run + ramp 1..32 + 2 A/B           53.8s  OK    p99 injected 120 → 119.0 ms; knee injected 8 → 8; B +30% → REGRESSION; A/A → OK
perf_qbt    qbt 200,000 bars x ma_cross, best-of-8 procs, 3 runs   168.4s  OK    noise 1.2%; 12%: REGRESSION cold.wall_s=0.7862→0.8918 (+13.4%); 5%: OK (expected OK); rerun OK
TOTAL                                                      760.3s  OK    8/8 steps passed
```

---

# qdperf — 量化交易系统性能体检工具包

纯 Python 3.12 标准库（零第三方依赖、零出网、只写 `qdperf/results/` 与 tempfile），给“Python + SQLite（WAL）+ 多进程
（HTTP / 交易引擎 / 通知）”的量化系统找瓶颈、定优化目标、在每次发版前后证明“没变慢”。所有数据都是合成的。

| 工具 | 干什么 | 判据（退出码） |
|---|---|---|
| `qdperf.sqlite_doctor`（G1） | 库 + 工作负载 → 计划判定、最贵 20 条、索引建议（临时副本上建索引 + ANALYZE 后用 EXPLAIN QUERY PLAN 验证）、真进程写争用（busy_timeout 等待分布 / SQLITE_BUSY / 谁饿死谁 / 根因）、WAL 检查点曲线、库文件体检 | 0 无红色发现 / 1 有 / 2 输入错误 |
| `qdperf.synth_db`（G1） | 同 seed 同结果的合成交易库（users 1 万 / bots 5 万 / orders 200 万 / trades 500 万 / settings 300 / events 100 万） | 0 / 2 |
| `qdperf.profile_gate`（G2） | 可调用 → 冷 / 热墙钟、CPU、tracemalloc 峰值、cProfile 前 30（规范化函数名）→ JSON；两份 JSON 的回归闸（容差 = max(手动, 2 × 基线噪声)） | compare：0 绿 / 1 红 / 2 不可比 |
| `qdperf.tick_jitter`（G3） | 引擎 / 面板 / 通知三个真进程：tick 间隔分布、读 / 算 / 写分段、Q × WAL × busy_timeout × 算段位置扫描、饿死拐点、缓解实测 | run：0 p99 ≤ 2T / 1 饿死 |
| `qdperf.http_load` + `qdperf.fake_backend`（G4） | http.client + 线程：每路径 p50 / p95 / p99 / 错误率 / 吞吐、“并发翻倍吞吐不再增长”的拐点、A/B 逐路径判定；假后端可注入已知 p99 / 并发上限 / 错误率 | ab：0 绿 / 1 红；ramp：错误率超限 1 |

- 命令行参数、退出码、stdout 第一行格式、Python 接口与返回 JSON 字段：[CONTRACT.md](CONTRACT.md)
- 目标达成表（要求 / 实测 / 复现命令，每个数字附机器 / Python 版本 / best-of-N / 冷热）：[REPORT.md](REPORT.md)
- 突变漏网逐条判定：[mutants/triage.md](mutants/triage.md)

## 接入示例

```bash
# 1) 体检你的库（工作负载格式见 CONTRACT.md §1.2；examples/workload_trading.json 是一份完整样例）
python3.12 -m qdperf.synth_db --out /tmp/demo.db --scale 0.05            # 没有自己的库时先生成一个合成库
python3.12 -m qdperf.sqlite_doctor /tmp/demo.db --workload qdperf/examples/workload_trading.json --emit-sql /tmp/idx.sql

# 2) 回归闸：嘈杂的机器上用 pair（基线与本次交替测 3 轮，机器的慢漂移同时落在两边）
python3.12 -m qdperf.profile_gate pair qdperf.examples.qbt_case:backtest --args '{"n": 200000}' \
    --env-b QDPERF_QBT_SLOW=0.12 --out /tmp/base.json --out-b /tmp/new.json
python3.12 -m qdperf.profile_gate compare /tmp/base.json /tmp/new.json      # 新旧两个检出目录：--path DIR / --path-b DIR

# 3) tick 抖动：WAL 关 + 面板每秒 5 次长查询
python3.12 -m qdperf.tick_jitter run --wal off --q 5 --seconds 6
python3.12 -m qdperf.tick_jitter scan --quick

# 4) HTTP：先起假后端，再爬坡 / A/B
python3.12 -m qdperf.fake_backend --config backend.json &                   # 打印 FAKE_BACKEND LISTENING http://127.0.0.1:<端口>
python3.12 -m qdperf.http_load ramp --base http://127.0.0.1:<端口> --manifest urls.json --cmax 32
```

## 其他命令

```bash
python3.12 -m qdperf.selftest --quick                       # 更快的自检（跳过覆盖率、缩小规模）
python3.12 -m qdperf.selftest --full                        # 全量：合成库 100%、G3 全网格、qbt 100 万根；写回 qdperf/results/
python3.12 -m unittest discover -s qdperf/tests -t .        # 只跑单元测试
python3.12 qdperf/tools/cov.py --fail-under 90              # 语句覆盖率（复用 tools/cov.py，合并子进程覆盖）
python3.12 qdperf/mutants/run.py --per-module 50 --jobs 4   # 突变测试（复用 mutants/run_mutations.py）
python3.12 -m qdperf.tools.selfcheck g1|g2|g3|g4            # 单独重跑某个目标的实测，写 qdperf/results/
python3.12 -m qdperf.tools.noise_probe                      # 本机噪声探针：进程快慢、进程内 vs 跨进程 best-of-N（REPORT §2.3）
python3.12 qdperf/tools/build_report.py                     # 由 results/*.json 重新生成 REPORT.md
```
