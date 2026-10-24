# qdexsim 突变漏网逐条判定（由 mutants/triage.py 生成）

命令 `python3.12 qdexsim/mutants/run.py --per-module 30 --jobs 4`；每模块 30 个、共 510 个突变体，抓住 476（超时 0），漏网 34，抓住率 93.3%；基线 19.7 s，总耗时 905.3 s。

| 模块 | 总数 | 抓住 | 漏网 | 抓住率 |
|---|---|---|---|---|
| adapters | 30 | 25 | 5 | 83.3% |
| chaos | 30 | 29 | 1 | 96.7% |
| clients | 30 | 28 | 2 | 93.3% |
| common | 30 | 30 | 0 | 100.0% |
| dialects/base | 30 | 29 | 1 | 96.7% |
| dialects/binance | 30 | 30 | 0 | 100.0% |
| dialects/bitget | 30 | 29 | 1 | 96.7% |
| dialects/bitunix | 30 | 30 | 0 | 100.0% |
| dialects/bybit | 30 | 29 | 1 | 96.7% |
| dialects/okx | 30 | 29 | 1 | 96.7% |
| engine | 30 | 25 | 5 | 83.3% |
| feed | 30 | 30 | 0 | 100.0% |
| harness | 30 | 26 | 4 | 86.7% |
| judges | 30 | 26 | 4 | 86.7% |
| recon | 30 | 27 | 3 | 90.0% |
| server | 30 | 28 | 2 | 93.3% |
| store | 30 | 26 | 4 | 86.7% |

## 漏网逐条

| id | 行 | 变化 | 判定 | 理由 |
|---|---|---|---|---|
| `adapters:666:const:0.44999999999999996` | 76 | `0.3` → `0.44999999999999996` | 等价 | Transport(timeout_s=0.3) 的缺省值从未被用到：Adapter 总是显式传 timeout_s |
| `adapters:4846:const:-1` | 418 | `0` → `-1` | 等价 | OKX /public/time 的 data 恒 1 个元素，[0] 与 [-1] 相同 |
| `adapters:4875:const:-1` | 430 | `0` → `-1` | 等价 | OKX /market/ticker 按 instId 查恒 1 个元素 |
| `adapters:3428:const:True` | 433 | `False` → `True` | 等价 | 公开接口多带一套签名头，服务端对公开路由不验签，报文相同 |
| `adapters:3519:const:-1` | 470 | `0` → `-1` | 等价 | OKX /trade/order 按 ordId/clOrdId 查恒 1 个元素 |
| `chaos:1635:const:2` | 171 | `1` → `2` | 等价 | at_req 缺省 -1 改 -2：没有 at_req 的规则两种缺省都永远不等于请求序号 |
| `clients:2059:binop` | 210 | `e.retry_after_ms / 1000.0 + 0.01` → `e.retry_after_ms / 1000.0 - 0.01` | 等价 | 退避时长 ±10 ms 的抖动，判据只看退避窗口内有没有再打（窗口 ≥ 30 ms），不可观测 |
| `clients:1361:const:0.03` | 318 | `0.02` → `0.03` | 等价 | 撤单重试之间 sleep 20 → 30 ms，只影响耗时 |
| `dialects/base:259:delete_stmt` | 179 | `raise NotImplementedError` → `pass` | 等价 | 基类抽象方法桩（pragma: no cover），子类全部覆写 |
| `dialects/bitget:1534:const:999` | 87 | `1000` → `999` | 等价 | error() 里 retry_after_ms or 1000 的兜底：服务端注入限频时总是显式传 retry_after_ms |
| `dialects/bybit:1302:const:1001` | 148 | `1000` → `1001` | 等价 | error() 里 retry_after_ms or 1000 的兜底：服务端注入限频时总是显式传 retry_after_ms |
| `dialects/okx:2326:const:0` | 185 | `1` → `0` | 等价 | posId 占位常量（真实为服务端生成的 id），SHAPES.md 标占位 |
| `engine:1611:const:1.0` | 151 | `0.0` → `1.0` | 等价 | 逐仓强平价 max(0.0, x) → max(1.0, x)：x 在任何可达状态下远大于 1 |
| `engine:2976:compare` | 348 | `old > 0` → `old >= 0` | 等价 | old > 0 → old >= 0 只在 old == 0 时不同，而 old == 0 已走上一分支 |
| `engine:3022:delete_stmt` | 370 | `break` → `pass` | 等价 | 流动性用完后不 break 继续循环：_fill 对 qty ≤ 0 直接返回 None，结果相同 |
| `engine:4186:binop` | 371 | `o['qty'] - o['filled']` → `o['qty'] + o['filled']` | 等价 | min(liq, qty + filled) 比 min(liq, qty − filled) 更大，但 _fill 内部再按剩余量截断 |
| `engine:2202:compare` | 398 | `abs(p['qty']) * p['entry'] / p['leverage'] + upl <= mmr * notional` → `abs(p['qty']) * p['entry'] / p['leverage'] + upl < mmr * notional` | 等价 | 逐仓强平 ≤ 改 <：浮点相等的边界状态不可达（标记价按 tick 归整后恰好等于阈值的概率可忽略） |
| `harness:690:const:1` | 131 | `2` → `1` | 等价 | 耗时 round 到 2 位改 1 位，只是报告精度 |
| `harness:1144:const:49` | 133 | `50` → `49` | 等价 | 服务端重启间隔 sleep 50 → 49 ms，只影响耗时 |
| `harness:1144:const:51` | 133 | `50` → `51` | 等价 | 同上 |
| `harness:977:const:False` | 146 | `True` → `False` | 等价 | rmtree(ignore_errors) 只在工作目录已不存在时有区别，驾驭器自己创建的目录总在 |
| `judges:937:const:1.0` | 97 | `0.0` → `1.0` | 等价 | 只影响 detail 文本里的元组，判定用的是下一行的比较 |
| `judges:940:const:1.0` | 97 | `0.0` → `1.0` | 等价 | 同上 |
| `judges:593:compare` | 98 | `abs(mine.get(s, 0.0) - server.get(s, 0.0)) > 1e-09` → `abs(mine.get(s, 0.0) - server.get(s, 0.0)) >= 1e-09` | 等价 | 持仓差 > 1e-9 改 >=：恰好 1e-9 的差不可达（数量按 1e-10 归整） |
| `judges:339:compare` | 104 | `abs(float(mine) - server) < 1e-06` → `abs(float(mine) - server) <= 1e-06` | 等价 | 余额差 < 1e-6 改 <=：同上，边界不可达 |
| `recon:287:const:1799999` | 123 | `1800000` → `1799999` | 等价 | 时区偏移识别阈值 30 分钟 − 1 ms，中位差恰好落在这 1 ms 内不可达 |
| `recon:296:compare` | 129 | `abs(med) < min_ms` → `abs(med) <= min_ms` | 等价 | abs(med) < min_ms 改 <=：同上 |
| `recon:669:compare` | 142 | `abs(l['ts'] - ts_offset_ms - r['ts']) > ts_tol_ms` → `abs(l['ts'] - ts_offset_ms - r['ts']) >= ts_tol_ms` | 等价 | ts 容差 > 改 >=：差恰好等于 5000 ms 不可达（合成数据与真实时间戳都是毫秒随机） |
| `server:184:delete_stmt` | 266 | `self.httpd.daemon_threads = True` → `pass` | 等价 | daemon_threads 只影响进程退出时是否等待处理线程，所有测试都显式 stop() |
| `server:221:const:1` | 311 | `0` → `1` | 等价 | server.main 的 return 0 只在 KeyboardInterrupt 后到达，测试用 SIGTERM 退出（sys.exit(0)） |
| `store:406:const:-1` | 93 | `0` → `-1` | 等价 | 单列查询 row[0] 改 row[-1] |
| `store:426:const:-1` | 103 | `0` → `-1` | 等价 | 单列查询 row[0] 改 row[-1] |
| `store:144:const:-1` | 105 | `0` → `-1` | 等价 | 单列查询 row[0] 改 row[-1] |
| `store:447:const:-1` | 117 | `0` → `-1` | 等价 | COUNT(*) 单列 fetchone()[0] 改 [-1] |

等价 34，已补判据 0，待补 0。
