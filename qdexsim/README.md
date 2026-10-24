```bash
python3.12 -m qdexsim.selftest
```

最近一次真实输出（提交 `da6272a`，任何一行 `FAIL` 都会让退出码变成 1；耗时随机器变化）：

```
qdexsim selftest (default)  Linux-6.18.44-fc-v153-x86_64-with-glibc2.39  Python 3.12.3  SQLite 3.45.1  4 CPU  commit da6272a
step         scale                                                                     time  result  key numbers
unittest     158 tests                                                                20.1s  OK      OK; tmp dirs left 0
constraints  3 tests (deps / network / writes)                                         0.0s  OK      stdlib-only ok; no-network ok; writes+binds+connects audit ok
coverage     3002 statements, 158 tests                                               20.4s  OK      98.03% (>= 90% required); child processes merged 3
g1_dialects  5 dialects x (bad sign, expired ts, place, partial fill, cancel) + pro    0.4s  OK      sign/ts codes binance:-1022/-1021, bybit:10004/10002, okx:50113/50102, bitget:40009/40008, bitunix:10007/10002; partial 0.05/0.06 x5; restart orders 5 fills 5 equal=True dup-after-restart=DUP_CID
g2_chaos     11 injection kinds, same seed x2 + other seed                             3.0s  OK      events 20 identical=True; other seed differs=True; kinds logged 11/11
g3_judges    60 scenarios x 2 clients                                                 62.6s  OK      correct green 60/60; naive red 60/60; naive must-red 20 ok=True
g4_recon     synthetic 200 orders, 50 planted diffs                                    0.0s  OK      found 50/50, missed 0, false positives 0, tz offset 28800000 ms
knives       3 hand-made mutants                                                       1.3s  OK      remove_cid_idempotency red=True; unknown_result_not_executed red=True; recon_skips_status red=True
TOTAL                                                                                107.9s  OK      8/8 steps passed
```

---

# qdexsim — 五方言 USDT 永续仿真交易所 + 混沌注入 + 客户端判据套件 + 对账

纯 Python 3.12 标准库（零第三方依赖、零出网、只听 / 只连 127.0.0.1、只写 `qdexsim/results/` 与 tempfile）。
给交易客户端一个**会出事**的假交易所：同一进程同时说 Binance / Bybit / OKX / Bitget / Bitunix 五种 REST 方言，可脚本化注入“结果未知”“限频”
“撤单竞态”“持仓旧值”“时钟漂移”“交易对下架”等故障，场景结束后以服务端真实状态判定客户端有没有重复下单、丢单、持仓对不上、撤单没撤掉、急停
不生效、限频不守规矩。

| 目标 | 模块 | 一句话 |
|---|---|---|
| G1 仿真交易所 | `server.py` `engine.py` `store.py` `feed.py` `dialects/` | 五方言 13 个接口 × 各家签名 / 时间窗 / 错误码形状；内存簿 + 行情源撮合、部分成交、持仓 / 强平 / 手续费 / 资金费；SQLite 写穿，重启不丢；clientOrderId 幂等（内存 + 库 UNIQUE 两道） |
| G2 混沌注入 | `chaos.py` | 11 种注入，JSON 场景 / `/__chaos` 运行时改，全部按种子复现，每次注入一行事件日志（时间、接口、类型、真实执行了什么） |
| G3 判据套件 | `judges.py` `harness.py` `clients.py` `scenarios/` | 10 个判据、12 模板 × 5 方言 = 60 个场景；天真客户端 60/60 红（结果未知 / 重启模板必红），正确客户端 60/60 绿 |
| G4 对账 | `recon.py` | 字段可映射的本地表 vs 服务端订单；漏记 / 幻单 / 不一致三类 + 建议动作；拆单聚合、时区偏移识别；50 条埋点全找出零误报 |
| G5 够硬 | `selftest.py` `tests/` `tools/` `mutants/` | 一条命令全自检；覆盖率 ≥ 90%、突变抓住率 ≥ 85%、三刀必红、零依赖 / 零出网 / 零写仓外各一条判据 |

- 五家报文逐字段可信度（**全部凭记忆，文档站在开发环境不可达**）：[SHAPES.md](SHAPES.md)
- 每个目标的实测数字、待核清单、没仿到的行为、搬运建议：[REPORT.md](REPORT.md)
- 命令行 / 配置 / 管理口 / 场景文件 / 客户端接口 / 适配器 / 对账的字段契约：[CONTRACT.md](CONTRACT.md)
- 突变漏网逐条判定：[mutants/triage.md](mutants/triage.md)

## 接你的客户端

```python
from qdexsim import harness, scenarios

class MyClient:                       # 最小接口见 CONTRACT.md §7；参考实现 clients.CorrectClient
    NAME = "mine"
    def __init__(self, dialect, base_url, state_dir, creds=None, timeout_s=0.3, log=None): ...
    def place(self, intent): ...      # intent = {"intent","symbol","side","type","qty","price","reduce_only"} → local_id
    def cancel(self, local_id): ...
    def query(self, key): ...         # local_id / 服务端订单号 / clientOrderId → 记录 | None
    def positions(self): ...          # {symbol: 带符号数量}
    def balance(self): ...            # float | None
    # 可选：restart() / kill_switch() / settle() / open_orders()

res = harness.run_suite(scenarios.load_all(dialect="okx"), client_names=())      # 或逐个：
r = harness.run_scenario(scenarios.load("okx/unknown_timeout"), MyClient)
print(r["ok"], [v for v in r["verdicts"] if not v["ok"]])
```

或者只用仿真器：

```bash
python3.12 -m qdexsim.server --port 18080 --db /tmp/sim.db --chaos my_chaos.json      # 打印 QDEXSIM LISTENING http://127.0.0.1:18080
# 你的客户端把 base_url 指到它；凭据缺省 <dialect>-key / <dialect>-secret / <dialect>-pass（可用 --config 的 creds 改）
curl -s http://127.0.0.1:18080/__admin/state | python3.12 -m json.tool | head           # 服务端真相
curl -s -X POST http://127.0.0.1:18080/__chaos -d '{"add": {"kind": "unknown_result", "op": "place", "p": 0.3, "mode": "timeout"}}'
```

对账：

```bash
python3.12 -m qdexsim.recon --local my_orders.csv --remote state.json --map my_map.json --out report.json   # 映射格式见 recon.py 顶部
```

## 其他命令

```bash
python3.12 -m qdexsim.selftest --quick                       # 跳过覆盖率与三刀
python3.12 -m qdexsim.selftest --full                        # 各步结果写回 qdexsim/results/*.json（REPORT.md 引用的快照）
python3.12 -W ignore::ResourceWarning -m unittest discover -s qdexsim/tests -t .    # 只跑单元测试（约 20 s）
python3.12 qdexsim/tools/cov.py --fail-under 90              # 语句覆盖率（复用 tools/cov.py，合并子进程）
python3.12 qdexsim/mutants/run.py --per-module 30 --jobs 4   # 突变测试（复用 mutants/run_mutations.py）
python3.12 qdexsim/mutants/triage.py --check                 # 漏网判定与结果文件一致
python3.12 -m qdexsim.tools.selfcheck g1|g2|g3|g4|knives     # 单独重跑某一步
python3.12 qdexsim/scenarios/gen.py --check                  # 场景文件与生成器一致
```

## 目录

| 路径 | 内容 |
|---|---|
| `engine.py` / `store.py` / `feed.py` | 撮合与账户（规范模型）、SQLite 写穿、行情源（随机游走 / 回放 CSV / 跳空）与 K 线聚合 |
| `dialects/{base,binance,bybit,okx,bitget,bitunix}.py` | 各家路由、鉴权、错误码、报文翻译；每个 handler 注释里写官方文档章节名 |
| `server.py` | `http.server` 进程：前缀分流、chaos 注入顺序、管理口、重启恢复 |
| `chaos.py` | 规则匹配（种子）、Decision、事件日志、限频违规记录 |
| `adapters.py` | 客户端侧五家签名 / 翻译 / 错误归一（UnknownResult / RateLimited / ExchangeError） |
| `clients.py` | 天真客户端与正确客户端 |
| `judges.py` / `harness.py` / `scenarios/` | 10 个判据、场景驾驭器、60 个场景文件与生成器 |
| `recon.py` | 对账与合成数据自检 |
| `tests/` | 136 个 unittest（含零依赖 / 零出网 / 零写仓外的静态 + 运行时审计判据，函数 ≤ 60 行 / 文件 ≤ 800 行） |
| `tools/cov.py` `tools/selfcheck.py` `mutants/` | 覆盖率、各目标实测、突变测试与漏网判定 |
| `results/` | REPORT.md 引用的全部原始 JSON（覆盖率、突变、g1–g4、三刀、selftest 总表） |
