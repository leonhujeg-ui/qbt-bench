# qdperf 突变漏网逐条判定

判据：突变抓住率 ≥ 85%，每模块 ≥ 40 个突变体（这里每模块 50 个、seed 0，10 个库模块共 500 个），活下来的逐条判“等价”或补判据。跑法与口径见 `qdperf/mutants/run.py` 文档串。

**结论：第 3 轮（抓住须确认、跑的过程中不改任何文件）首跑抓住率 80.6%（403/500），漏网 97 条，逐条判定为等价 26、补判据 71；补判据并修掉追查中发现的 3 个真问题后重跑，最终 95.4%（477/500，`results/mutation.json`），剩下的 23 条漏网全部判为等价（见表 A / B）。**

## 经过

1. **第 1 轮**（`results/mutation_round1.json`）：500 个中“抓住” 460（92.0%）。事后查明其中 101 个不可信：57 个死在“函数 ≤ 60 行”判据上（我在跑到一半时改长了 `qdperf/mutants/run.py:main`，之后开跑的突变体复制到的都是超长版本）、25 个死在按源码文本、按双引号匹配的判据上（突变器写回的是 `ast.unparse` 的单引号输出）、19 个死在与被突变模块毫无关系的测试上（4 个突变体并行、机器满载时的偶发失败）。整轮作废；它的 40 条漏网已逐条补判据 / 判等价（附录 C）。
2. **修正一**：三个负载敏感的测试改为不依赖负载的推断；跑器加“抓住须确认”（报失败的测试在同一突变体上单独重跑仍失败才算，否则记 `unconfirmed`、整套再跑）；跑的过程中不再改仓库里任何文件。
3. **第 2 轮**（`results/mutation_round2_partial.json`）：跑到 320/500 时因后台时限中断；暴露了 26 个仍按引号文本判死的误杀，以及第 1 轮被误杀遮住的一批漏网（附录 B）。**修正二**：源码文本判据改成与排版无关（AST 字符串常量 / `ast.unparse` 规范化文本）；修掉 `sqlite_load` 的 tempfile 目录泄漏（测试与突变跑了几千次后 /tmp 堆了 1.5 GB）。
4. **第 3 轮**（`results/mutation_round3.json`，`python3.12 qdperf/mutants/run.py --per-module 50 --jobs 4`）：同 seed、500 个突变体一次跑完，耗时 71 分钟，首跑抓住率 **80.6%**，漏网 97；未确认的抖动失败 2 次（均未算抓住，列在文末）。
5. **逐条判定这 97 条**：等价 26、补判据 71（表 A）。追查漏网时发现并修了 3 个真问题：(a) 体检工具遇到写错的 SQL 会跑到一半抛 Traceback（违反“输入错误退出码 2”的契约）→ 先在原库只读连接上把每条语句 EXPLAIN 一遍；(b) `fake_backend.start()` 起不来时异常里没有原因（原因打在子进程 stdout、却被丢进 DEVNULL）→ 收集子进程输出；(c) `fake_backend` 补齐响应体时差 1～8 字节会补出负数个 x、体反而比 `body_bytes` 长（缺省 64 字节时 POST 的第 1000 个请求起就会），`body_bytes` / `schedule` 也不校验 → 差不到 9 字节就不补、两项都校验。
6. **重跑**：`python3.12 qdperf/mutants/run.py --module fake_backend sqlite_doctor --jobs 4`（100 个，14 分钟）；`python3.12 qdperf/mutants/run.py --ids <71 个 id> --jobs 4`（71 个，14 分钟）；`python3.12 qdperf/mutants/run.py --module fake_backend --jobs 4`（50 个，7 分钟）；`python3.12 qdperf/mutants/run.py --ids <10 个 id> --jobs 4`（10 个，2 分钟）；`python3.12 qdperf/mutants/run.py --ids <9 个 id> --jobs 4`（9 个，3 分钟）。改了库代码的模块（fake_backend 改了两次、sqlite_doctor 一次）源码变了、突变体按新源码重新抽样，所以整模块重跑，它们的漏网按“同一处突变”（算子 + 前后文本 + 所在源码行）对回之前的判定；其余模块只按 id 重跑第 3 轮的漏网（记 `first_run`）。

| 模块 | 突变体 | 第 1 轮“抓住”（作废） | 第 3 轮首跑抓住 | 第 3 轮首跑抓住率 | 最终抓住（含超时） | 最终漏网 | 最终抓住率 |
|---|---:|---:|---:|---:|---:|---:|---:|
| common | 50 | 41 | 48 | 96% | 48 | 2 | 96% |
| fake_backend（重抽样） | 50 | 50 | 38 | 76% | 48 | 2 | 96% |
| http_load | 50 | 47 | 49 | 98% | 49 | 1 | 98% |
| index_advisor | 50 | 33 | 46 | 92% | 46 | 4 | 92% |
| profile_gate | 50 | 39 | 50 | 100% | 50 | 0 | 100% |
| sqlite_doctor（重抽样） | 50 | 50 | 36 | 72% | 48 | 2 | 96% |
| sqlite_load | 50 | 50 | 32 | 64% | 42 | 8 | 84% |
| sqlplan | 50 | 50 | 42 | 84% | 47 | 3 | 94% |
| synth_db | 50 | 50 | 30 | 60% | 50 | 0 | 100% |
| tick_jitter | 50 | 50 | 32 | 64% | 49 | 1 | 98% |
| **合计** | 500 | 460 | 403 | 80.6% | 477 | 23 | **95.4%** |

复现：`python3.12 qdperf/mutants/run.py --per-module 50 --jobs 4`（约 70 分钟，4 核）；只看某几条：`--ids <id>...`；某模块改了库代码后：`--module <模块>`（都并入结果文件，原 meta 保留、重跑记进 `reruns`）。


## A. 第 3 轮的 97 条漏网（最终判定）

“等价”给出为什么任何合法输入下都观测不到差别（或差别只落在没有消费者的字段 / 不可达的缺省值上）；“补判据”写明新断言在哪个测试、断言什么。最后一栏是最终结果文件里的状态。


### common

| id | 行 | 突变 | 判定 | 理由 | 最终 |
|---|---|---|---|---|---|
| `common:1191:const:9` | 57 | `10` → `9` | **等价** | `git rev-parse` 的超时 10 s → 9 s：本地 git 毫秒级返回，只有 git 卡在 9～10 s 之间才有差别；超时值不进入任何输出或判据，外部不可观测。 | 漏网 |
| `common:866:const:1` | 92 | `2` → `1` | **等价** | `rel_spread` 的 `len(values) < 2` → `< 1`：只剩 1 个值时，原代码提前返回 0.0，突变后走 `(max-min)/min = 0/v = 0.0`（v ≤ 0 时被同一行的 `min(values) <= 0` 拦下返回 0.0）；空列表仍被 `< 1` 拦下。对所有输入返回值相同。 | 漏网 |

### fake_backend

| id | 行 | 突变 | 判定 | 理由 | 最终 |
|---|---|---|---|---|---|
| `fake_backend:1981:const:1` | 82 | `0` → `1` | **补判据** | 全局并发上限缺省 `max_concurrency` 0（不限）→ 1：所有请求被串行化，只有计时类断言能看出来（突变测试下跳过）；`test_config_validation` 新增确定性断言：缺省 seed 1、全局上限 0、Backend 不建信号量，路由缺省错误率 0、上限 0、体 64 字节、分层抽样。 | （重抽样后为 `fake_backend:2067:const:1`）抓住：FAIL: test_config_validation |
| `fake_backend:1484:const:1` | 85 | `0` → `1` | **等价** | `load_config` 里传给 `check_latency(...)` 的缺省延迟 `ms: 0` → `ms: 1`：这个字典只用来做合法性校验（固定 0 ms 与 1 ms 都合法），真正存进路由的是下一行自己的缺省 `{'kind': 'fixed', 'ms': 0}`；若改的是 `r.get('latency', …)` 里的缺省则更是死代码（该分支只在 'latency' 在 r 里时才走）。任何配置下输出都相同。 | 模块改库后重抽样，这一处没再抽中（或该行已改写） |
| `fake_backend:1840:const:1.5000000000000002e-09` | 120 | `1e-09` → `1.5000000000000002e-09` | **等价** | 分位截断 `min(max(u, 1e-9), 1 - 1e-9)` 的下界 1e-9 → 1.5e-9：分层抽样的 u = (perm + 0.5)/1000 ∈ [0.0005, 0.9995]，碰不到截断；random 模式下 u 落进 [1e-9, 1.5e-9) 的概率每个请求 5×10⁻¹⁰，即使落进去，对数正态左尾 z 从 −6.00 变 −5.93，延迟差在纳秒级。截断只为避免 quantile(0) = −∞，任何可行的实验里都观测不到。 | 模块改库后重抽样，这一处没再抽中（或该行已改写） |
| `fake_backend:793:const:0` | 144 | `1` → `0` | **等价** | `wbufsize = 1 << 16` → `0 << 16`（不缓冲，头和体分两次写）：同一个 Handler 已开 `disable_nagle_algorithm`（TCP_NODELAY），小包不再等对端的延迟 ACK，两者是双保险，去掉一个不改变字节流也不增加时延——开计时跑 `test_latency_matches_injection` 对突变体 3/3 通过（p50 仍 ≈ 21 ms，没有 +40 ms）。 | 模块改库后重抽样，这一处没再抽中（或该行已改写） |
| `fake_backend:793:const:2` | 144 | `1` → `2` | **等价** | 响应写缓冲 `wbufsize = 1 << 16` → `2 << 16`（128 KB）：假后端的响应都远小于 64 KB，头和体照样攒在一个缓冲里一次写出，字节流相同；开计时跑 `test_http_load.TestLoad.test_latency_matches_injection`（p50 < 30 ms）对突变体 3/3 通过。 | 模块改库后重抽样，这一处没再抽中（或该行已改写） |
| `fake_backend:1915:const:True` | 180 | `False` → `True` | **补判据** | 500 响应体 `{'ok': False, …}` → `{'ok': True, …}`（压测端只看状态码，所以没被抓住）；`test_in_process_server` 新增断言：错误响应体为 `{'ok': false, 'route': '/err', 'n': 0}`。 | 模块改库后重抽样，这一处没再抽中（或该行已改写） |
| `fake_backend:1939:boolop` | 212 | `proc.stderr.read() or ''` → `proc.stderr.read() and ''` | **补判据** | 起不来时错误信息里的 `(stderr or '')` → `(stderr and '')`，丢掉子进程报的原因。追查时发现真问题：子进程把 `FAKE_BACKEND ERROR <原因>` 打在 stdout，而 start() 把 stdout 丢进 DEVNULL——原因本来就带不出来。修库：start() 收集子进程 stdout + stderr 拼进异常；新增断言异常里含 `config.format must be`。 | 模块改库后重抽样，这一处没再抽中（或该行已改写） |
| `fake_backend:2106:const:501` | 212 | `500` → `501` | **等价** | 错误信息保留子进程输出的最后 500 → 501 个字符：只在输出超过 500 字符时差一个字符，不进任何判定（这一行随后因修库重写）。 | 模块改库后重抽样，这一处没再抽中（或该行已改写） |
| `fake_backend:507:delete_stmt` | 212 | `raise RuntimeError('fake backend failed to start: ' + (proc.` → `pass` | **补判据** | `start()` 里子进程起不来时的 `raise` 被删 → 一直轮询下去；新增 `test_subprocess_backend_start_failure`：非法配置时 30 s 内必须抛出带 stderr 的 RuntimeError（在线程里调用，突变体不会把测试挂死）。 | 模块改库后重抽样，这一处没再抽中（或该行已改写） |
| `fake_backend:902:const:11` | 222 | `10` → `11` | **补判据** | `stop()` 里terminate 之后的等待上限 `wait(10)` → `wait(11)`。第 3 轮时判为等价（假后端收到 SIGTERM 毫秒级退出，上限不可观测）；之后为抓 “SIGKILL 后不回收”（fake_backend:531）补了 `test_stop_escalation`，用不理 SIGTERM 的假进程把停止协议 terminate → wait(10) → kill → wait(10) 整条钉住，这一条也随之被抓住，改判补判据。 | 模块改库后重抽样，这一处没再抽中（或该行已改写） |
| `fake_backend:1337:const:11` | 225 | `10` → `11` | **补判据** | `stop()` 里kill 之后的等待上限 `wait(10)` → `wait(11)`。第 3 轮时判为等价（假后端收到 SIGTERM 毫秒级退出，上限不可观测）；之后为抓 “SIGKILL 后不回收”（fake_backend:531）补了 `test_stop_escalation`，用不理 SIGTERM 的假进程把停止协议 terminate → wait(10) → kill → wait(10) 整条钉住，这一条也随之被抓住，改判补判据。 | 模块改库后重抽样，这一处没再抽中（或该行已改写） |
| `fake_backend:108:negate_cond` | 226 | `if proc.stderr: proc.stderr.close()` → `if not proc.stderr: proc.stderr.close()` | **补判据** | `stop()` 收尾 `if proc.stderr: proc.stderr.close()` 取反 → stderr 管道不关（文件句柄泄漏）；`test_subprocess_backend_and_main_errors` 新增断言：stop 之后 stderr 已关闭、临时目录已删除。 | 模块改库后重抽样，这一处没再抽中（或该行已改写） |

### http_load

| id | 行 | 突变 | 判定 | 理由 | 最终 |
|---|---|---|---|---|---|
| `http_load:1226:delete_stmt` | 136 | `conn.close()` → `pass` | **等价** | 删掉 `if resp.will_close: conn.close()`：CPython `HTTPConnection.getresponse()` 在 `response.will_close` 时自己已经 `self.close()`（把 socket 交给 response），下一次 `request()` 自动重连；我们这行是冗余的保险，删除后行为完全相同。 | 漏网 |

### index_advisor

| id | 行 | 突变 | 判定 | 理由 | 最终 |
|---|---|---|---|---|---|
| `index_advisor:407:delete_stmt` | 29 | `nd['alias'] = nd['table']` → `pass` | **等价** | 删掉 `nd['alias'] = nd['table']`：resolve_aliases 的输入只来自 `parse_eqp`（3 处调用都是 `resolve_aliases(explain(...))`），而 parse_eqp 给每个带 table 的节点都已设好 alias（= EQP 里出现的名字）；SQLite ≥ 3.36 的格式（`SCAN x`）下这句写入的是同一个值。旧格式（`SCAN TABLE t AS x`）下两者才不同，但节点的 alias 字段没有任何消费者（doctor / advisor / classify 只读 table、index、detail），不进输出。 | 漏网 |
| `index_advisor:209:compare` | 97 | `hi - lo + 1 <= k` → `hi - lo + 1 < k` | **等价** | `sample_rowids` 的 `hi - lo + 1 <= k` → `< k`：恰好 k 行时突变走等距公式 `lo + (hi-lo)·i // (k-1) = lo + (k-1)·i // (k-1) = lo + i`，与 `range(lo, hi+1)` 逐个相同；k = 1 的除零分支只在直接传 k = 1 时可达，库内唯一调用用常量 SAMPLE_ROWS = 2000。 | 漏网 |
| `index_advisor:618:const:2` | 157 | `3` → `2` | **等价** | `round(build_seconds, 3)` → `2`：只改报告里“建索引耗时”这个计时值的小数位；它不进任何判定，契约也不约定位数，且是不可复现的计时，无法写出确定性断言。 | 漏网 |
| `index_advisor:322:const:3` | 181 | `4` → `3` | **等价** | `verify` 的最多轮数 4 → 3：第 1 轮可能删 / 换覆盖，第 2 轮只可能删（覆盖变体不再派生，被删的是没人用的或仍扫描的索引，删它不改变其他语句的计划），第 3 轮必然无变化而退出——有变化的轮最多 2 轮、总轮数 ≤ 3，上限 3 与 4 走完全相同的路径。 | 漏网 |

### sqlite_doctor

| id | 行 | 突变 | 判定 | 理由 | 最终 |
|---|---|---|---|---|---|
| `sqlite_doctor:5146:const:1` | 223 | `0` → `1` | **补判据** | 自动索引的“建索引扫描”按 `counts.get(表, 0)` 计 → 缺省 1（第 2 轮被引号排版误杀遮住，下同）；`test_statement_rows_nested` 新增：表行数未知的自动索引节点只计 10 行。 | 模块改库后重抽样，这一处没再抽中（或该行已改写） |
| `sqlite_doctor:3876:delete_stmt` | 248 | `timed_out = True` → `pass` | **补判据** | 测量被进度回调打断时不再记 `timed_out = True`；新增 `test_measure_timeout_and_harvest_sample`：3000×3000 交叉连接、超时 0 s → timed_out 为真。 | 模块改库后重抽样，这一处没再抽中（或该行已改写） |
| `sqlite_doctor:637:const:21` | 291 | `20` → `21` | **补判据** | `top_by_cost` 缺省前 20 → 21；`test_statement_rows_nested` 新增：25 条语句缺省只取前 20、按成本降序。 | （重抽样后为 `sqlite_doctor:648:const:21`）抓住：FAIL: test_statement_rows_nested |
| `sqlite_doctor:5257:const:-1` | 305 | `0` → `-1` | **补判据** | ANALYZE 之后的计划判定用最后一个样例参数（`[-1]`）而不是第一个；同 `test_measure_timeout_and_harvest_sample`：mock 掉 explain，断言用的是 `param_samples[0]`（与 ANALYZE 前的判定同一个参数）。 | 模块改库后重抽样，这一处没再抽中（或该行已改写） |
| `sqlite_doctor:7676:const:1` | 353 | `0` → `1` | **补判据** | 统计过期的漂移率 `(n - st[0])` → `st[1]`（stat1 第二个数是每键平均行数）；`test_file_checkup` 新增：stat_rows ≈ 实际行数的一半（ANALYZE 做在订单生成一半时）、drift = (实际 − 统计)/统计。 | 模块改库后重抽样，这一处没再抽中（或该行已改写） |
| `sqlite_doctor:223:delete_stmt` | 354 | `sizes = []` → `pass` | **补判据** | 删掉 `sizes = []`：dbstat 不可用时 `sizes` 未定义 → 体检崩溃；新增 `test_file_checkup_without_dbstat`：让副本连接上的 dbstat 查询抛 OperationalError，体检照常出结果、largest_objects 为空。 | （重抽样后为 `sqlite_doctor:227:delete_stmt`）抓住：ERROR: test_file_checkup_without_dbstat |
| `sqlite_doctor:6313:const:-1` | 380 | `0` → `-1` | **等价** | `writer_mix` 里事务的 read_table 取 `parsed['tables'][0]` → `[-1]`：writer_mix 只收写语句（INSERT / UPDATE / DELETE），而 `parse_statement` 对写语句只给出目标表一张（子查询里的表不进 tables），`[0]` 与 `[-1]` 是同一个元素。 | 模块改库后重抽样，这一处没再抽中（或该行已改写） |
| `sqlite_doctor:7000:const:7.5` | 427 | `5.0` → `7.5` | **补判据** | 延迟事务演示场景的 think 5 ms → 7.5 ms（场景名仍写 think5ms）；`test_load.test_contention_and_wal_sections` 新增：该场景 config 的 think_ms 为 5.0。 | 模块改库后重抽样，这一处没再抽中（或该行已改写） |
| `sqlite_doctor:794:const:15000.0` | 439 | `10000.0` → `15000.0` | **补判据** | 热点全表扫描的缺省红线 1 万行/秒 → 1.5 万；`test_levels` 新增：成本恰好 10000 → HOT_FULL_SCAN（含边界），9999 → 不是。 | 模块改库后重抽样，这一处没再抽中（或该行已改写） |
| `sqlite_doctor:822:delete_stmt` | 464 | `ro.close()` → `pass` | **等价** | `diagnose` 的 finally 里删掉 `ro.close()`：`ro` 是局部变量，diagnose 返回时引用计数归零，CPython 立即析构并关闭连接；这条只读连接（immutable 或 mode=ro）从不写库，关闭早晚不影响前后文件指纹（`untouched` 判据照样成立），也不进任何输出。 | （重抽样后为 `sqlite_doctor:835:delete_stmt`）漏网 |
| `sqlite_doctor:6510:const:99` | 557 | `100` → `99` | **补判据** | 碎片发现文字里的百分比 ×100 → ×99；0.05 × 99 仍印成 5.0%，所以 `test_levels` 用 0.2 断言消息以“碎片率 20.0%”开头。 | 模块改库后重抽样，这一处没再抽中（或该行已改写） |
| `sqlite_doctor:6644:const:1500000.0` | 624 | `1000000.0` → `1500000.0` | **补判据** | 索引建议表里“估算MB”按 1.5e6 换算；估算很准时估算与实测印成同一个数，包含式断言抓不到 → 改为按列位置断言该行的“估算MB / 实测MB”两列。 | 模块改库后重抽样，这一处没再抽中（或该行已改写） |
| `sqlite_doctor:3391:compare` | 670 | `w['max_live_frames'] <= 1.1 * w['config']['autocheckpoint'] ` → `w['max_live_frames'] < 1.1 * w['config']['autocheckpoint'] +` | **补判据** | WAL 结论里“被 autocheckpoint 截住”的判定 `<=` → `<`；新增 `test_wal_conclusion_boundary`：峰值恰好 1.1 × 100 + 50 = 160 帧仍算截住、161 不算。 | 模块改库后重抽样，这一处没再抽中（或该行已改写） |
| `sqlite_doctor:5967:const:99` | 683 | `100` → `99` | **补判据** | 库文件体检文字里的碎片率 ×100 → ×99；`test_file_checkup` 新增：文本含按 ×100、两位小数印出的碎片率。 | 模块改库后重抽样，这一处没再抽中（或该行已改写） |

### sqlite_load

| id | 行 | 突变 | 判定 | 理由 | 最终 |
|---|---|---|---|---|---|
| `sqlite_load:268:const:1.0` | 95 | `0.0` → `1.0` | **等价** | 私有函数 `_one_txn(…, think=0.0)` 的缺省 → 1.0：唯一调用方 `run_writer` 总是显式传 `spec.get('think', 0.0)`，缺省值不可达。 | 漏网 |
| `sqlite_load:665:const:1.5` | 137 | `1.0` → `1.5` | **等价** | `summarize_writer` 里 `dur = duration or 1.0` 的兜底 → 1.5：只在“有记录却没有 duration（或为 0）”时才影响速率与持锁占比，而这种输入不存在——写者正常结束总带 duration；崩溃 / 无输出时只有 {name, error}、没有记录（速率、占比都是 0，与兜底值无关）；duration = 0 时调度表为空、也没有记录。 | 漏网 |
| `sqlite_load:1217:const:1500.0` | 140 | `1000.0` → `1500.0` | **补判据** | `late_ms`（相对计划时刻的迟到）按 1500 而不是 1000 换算成毫秒；`test_summarize_writer` 新增：迟到统计 n = 5（含失败的尝试）、最大 1.0 ms。 | 抓住：FAIL: test_summarize_writer |
| `sqlite_load:2440:const:0` | 140 | `1` → `0` | **补判据** | `late_ms` 取 `r[0]`（计划时刻）而不是 `r[1]`（迟到）；同 `test_summarize_writer` 新增的断言：迟到最大 1.0 ms（计划时刻最大是 400 ms）。 | 抓住：FAIL: test_summarize_writer |
| `sqlite_load:333:compare` | 176 | `hold99 > cfg['budget_ms'] / 4` → `hold99 >= cfg['budget_ms'] / 4` | **补判据** | 根因“持锁过长”的判定 `hold99 > 预算/4` → `>=`；原测试在 50 ms（= 200/4）处只数了根因条数——突变后那一条变成“持锁过长”、条数仍是 1；改为断言恰好 50 ms 时唯一的根因是“退避粒度”。 | 抓住：FAIL: test_root_cause |
| `sqlite_load:1321:binop` | 191 | `0.2 * len(writers)` → `0.2 / len(writers)` | **等价** | 写者统一起跑时刻的预留 `1.5 + 0.2 × 写者数` → `1.5 + 0.2 / 写者数`（3 个写者时 2.1 s → 1.57 s）：预留只为让 spawn 的子进程都赶在同一时刻起跑；每个写者的记录（计划时刻、迟到、等待、持锁）都相对自己的起跑时刻，没有任何输出字段记录“起跑偏差”。只有 spawn 慢到 1.57 s 以上的极端负载才会让起跑不齐，这时任何固定预留都保证不了，属于外部不可观测（并非数学等价，单列说明）。 | 漏网 |
| `sqlite_load:2013:binop` | 196 | `seed * 1000 + k` → `seed * 1000 - k` | **等价** | 各写者的种子 `seed × 1000 + k` → `seed × 1000 − k`（规格层面等价）：规格只要求“同 seed 结果可复现、同一实验里各写者的随机流互不相同”，两种写法都满足（k < 1000，不同 seed / 写者之间不会撞号）；具体抽到的那组时刻与事务会换成另一组同分布的随机数，不存在“正确的”那组数，测试也不该钉死它。 | 漏网 |
| `sqlite_load:2546:binop` | 196 | `seed * 1000` → `seed / 1000` | **等价** | 各写者的种子 `seed × 1000 + k` → `seed / 1000 + k`（规格层面等价，同 sqlite_load:2013）：种子变成浮点数，`random.Random` 在 3.12 接受浮点种子；仍然同 seed 可复现、同一实验里各写者互不相同，只是换了一组同分布的随机数。 | 漏网 |
| `sqlite_load:2895:const:999` | 196 | `1000` → `999` | **等价** | 各写者的种子 `seed × 1000 + k` → `seed × 999 + k`（规格层面等价，同 sqlite_load:2013）：同一实验里写者 k 互不相同、同 seed 可复现；只有单次实验写者数 ≥ 999 时才可能与相邻 seed 撞号，工具里写者数是个位数。 | 漏网 |
| `sqlite_load:2575:const:59` | 200 | `60` → `59` | **等价** | 等写者进程结束的上限 `duration + 60` → `+ 59` 秒：写者按调度表跑完 duration 就退出（毫秒级），上限只在写者卡死 59～60 s 时才有差别，不进任何输出。 | 漏网 |
| `sqlite_load:1374:const:1500.0` | 210 | `1000.0` → `1500.0` | **补判据** | 总体等待分布按 1500 而不是 1000 换算成毫秒（p99 ≤ 200 ms 判据就看它）；`test_contention_immediate_ok` 新增：总体等待的样本数 = 各写者之和、最大值 = 各写者最大值中的最大（同一单位）。 | 抓住：FAIL: test_contention_immediate_ok |
| `sqlite_load:2147:const:3` | 252 | `2` → `3` | **等价** | 长读者进程开 `range(2)` → `range(3)` 条连接：只用 `conns[k % 2]` 两条轮转，第 3 条从不开事务（不占 WAL 读标记、不挡检查点），最后与其他连接一起关闭；读事务数与 WAL 行为都不变。 | 漏网 |
| `sqlite_load:2172:const:1` | 267 | `2` → `1` | **补判据** | 长读者两条连接的错开间隔 `hold / 2` → `hold / 1`（每个读事务持有 2×hold）；原测试只要求读事务数 > 1；`test_wal_growth_shapes` 新增：读事务数 ≥ (duration + 0.5) / (hold/2) − 1。 | 抓住：FAIL: test_wal_growth_shapes |
| `sqlite_load:1630:const:1` | 306 | `0` → `1` | **补判据** | WAL 曲线为空时的 `max_bytes / final_bytes` 缺省 0 → 1；`test_defaults_and_scratch_cleanup`（mock 掉采样，曲线为空）新增：峰值 / 末值字节、有效帧、重置次数全为 0。 | 抓住：FAIL: test_defaults_and_scratch_cleanup |
| `sqlite_load:2721:const:2` | 306 | `1` → `2` | **补判据** | WAL 文件末值 `sizes[-1]` → `sizes[-2]`；`test_defaults_and_scratch_cleanup` 新增：给定 4 个采样点的曲线，峰值 / 末值字节、峰值 / 末值有效帧、重置次数逐一精确断言。 | 抓住：FAIL: test_defaults_and_scratch_cleanup |
| `sqlite_load:1644:const:1500.0` | 309 | `1000.0` → `1500.0` | **补判据** | WAL 实验的提交耗时按 1500 而不是 1000 换算成毫秒；`test_defaults_and_scratch_cleanup` 让假写者写出两笔 2 ms / 4 ms 的提交，断言 commit_ms 为 n = 2、最大 4.0 ms。 | 抓住：FAIL: test_defaults_and_scratch_cleanup |
| `sqlite_load:1647:const:-1` | 309 | `0` → `-1` | **补判据** | 没有长读者输出时 `read_txns` 缺省 0 → -1；同 `test_defaults_and_scratch_cleanup`：无长读者时 read_txns 为 0。 | 抓住：FAIL: test_defaults_and_scratch_cleanup |
| `sqlite_load:2770:const:4` | 341 | `3` → `4` | **补判据** | WAL 曲线的时刻 `round(t, 3)` → `round(t, 4)`；`test_wal_growth_shapes` 新增：曲线时刻都是 3 位小数（报告与 JSON 的格式约定）。 | 抓住：FAIL: test_wal_growth_shapes |

### sqlplan

| id | 行 | 突变 | 判定 | 理由 | 最终 |
|---|---|---|---|---|---|
| `sqlplan:1974:delete_stmt` | 234 | `j = i + 1` → `pass` | **补判据** | FROM 里遇到 ON 时删掉 `j = i + 1`：j 沿用上一张表留下的值；`orders JOIN (SELECT …) ON …`（合法 SQL：JOIN 一个不带别名的子查询）时 j 指回 JOIN 之前 → 解析死循环；新增 `test_join_unaliased_subquery_on`（在线程里跑、10 s 内必须返回）：子查询记不可用、ON 条件照常切出、WHERE 等值照常识别。 | 抓住：FAIL: test_join_unaliased_subquery_on |
| `sqlplan:1180:compare` | 270 | `i < len(toks)` → `i <= len(toks)` | **等价** | `_parse_update` 跳过 `OR <冲突子句>` 的循环条件 `i < len(toks)` → `<=`：只有 i 恰好等于记号数（语句在 `UPDATE` 或 `UPDATE OR x` 处就结束了）时才不同，而这时原代码下一行取表名 `toks[i]` 同样抛 IndexError——两者抛同一个异常；合法的 UPDATE 后面总有表名，i 永远小于记号数（实测 `UPDATE`、`UPDATE OR IGNORE`、`UPDATE OR REPLACE t SET …` 三种输入两者完全一致）。 | 漏网 |
| `sqlplan:1242:const:0` | 298 | `1` → `0` | **等价** | `_parse_delete` 里没有 FROM 时表名位置 `else 1` → `else 0`：只影响不带 FROM 的 DELETE（以及光秃秃的 `DELETE`），而 SQLite 语法要求 `DELETE FROM`，这两种都编译不过（实测 syntax error / incomplete input）；合法 SQL 上两者完全一致。追查时发现真问题：工作负载里写错的 SQL 会让体检跑到一半抛 Traceback（违反“输入错误退出码 2”的契约）→ 修库：先在原库只读连接上把每条语句 EXPLAIN 一遍，编译不过即报 `SQLITE_DOCTOR ERROR statement <id>: <原因>`、退出码 2（`test_main_exit_codes` 新增语法错、表不存在、参数个数不对三种）。修复后非法语句根本到不了解析器之后的任何输出，这个突变体更无从观测。 | 漏网 |
| `sqlplan:3042:const:2` | 309 | `1` → `2` | **等价** | `_parse_insert` 取目标表的条件 `i + 1 < len(toks)` → `i + 2 < len(toks)`：只在语句恰好停在表名处（`INSERT INTO orders`）时不同，而这不是合法 SQL（实测 EXPLAIN 报 incomplete input）；合法的 INSERT 在表名后总还有 `(列…)` / VALUES / SELECT / DEFAULT VALUES，两者结果一致，且非法语句已被体检的预编译检查挡在门外（见 sqlplan:1242）。 | 漏网 |
| `sqlplan:1329:delete_stmt` | 351 | `pending_between = True` → `pass` | **补判据** | 顶层 AND 切分时不再记 `pending_between`：`ts BETWEEN ? AND ?` 被切成 `ts BETWEEN ?` 和孤零零的 `?`；原测试只比了可用条件（ts 照样是 range），没看不可用清单；`test_range_between_in_is` 新增：不可用清单为空、ts 那一项的原文是完整的 `ts BETWEEN ? AND ?`。 | 抓住：FAIL: test_range_between_in_is |
| `sqlplan:658:boolop` | 384 | `term[0] == ('op', '(') and _skip_paren(term, 0) == len(term)` → `term[0] == ('op', '(') or _skip_paren(term, 0) == len(term)` | **补判据** | “整项被一对括号包住才剥括号”的 `and` → `or`：单个记号的条件（裸列 `WHERE status`、字面量 `1`）会被当成整项括号剥成空表 → IndexError 崩溃；新增 `test_top_level_or_and_bare_terms`：这类条件记为不可用、其余条件照常解析。 | 抓住：ERROR: test_top_level_or_and_bare_terms |
| `sqlplan:691:negate_cond` | 421 | `if depth == 0: yield t` → `if not depth == 0: yield t` | **补判据** | `_top_level` 只产出括号深度 0 的记号 → 取反后只产出括号里的：原测试的 OR 恰好写在括号里（两种写法都能“看到”它），所以没抓住；新增：不带括号的顶层 OR 必须报“顶层 OR”，子查询里的 OR 不算顶层（IN 子查询照常记等值）。 | 抓住：FAIL: test_top_level_or_and_bare_terms |
| `sqlplan:3558:const:0` | 596 | `1` → `0` | **补判据** | 记录头长 `types + varint_len(types + 1)` → `varint_len(types + 0)`：头长字段要把自己也算进去，只在 serial type 总长 127 处（127 → 头长 129 需 2 字节）才有差别；`test_matches_sqlite_payload` 新增 126 列 / 127 列两张表，与 dbstat 的负载字节逐字节对账。 | 抓住：FAIL: test_matches_sqlite_payload |

### synth_db

| id | 行 | 突变 | 判定 | 理由 | 最终 |
|---|---|---|---|---|---|
| `synth_db:213:const:870.0` | 38 | `580.0` → `870.0` | **补判据** | BNBUSDT 基准价 580 → 870：合成数据的内容常数（第 1 轮被行数判据误杀遮住，下同）；原测试只验“同一份代码两次生成相同”，抓不到“换了一份数据”。新增 `test_synth_db.test_golden_fingerprint`：缺省 seed、0.4% 规模（20 个交易对、6 种策略都出现）的逻辑指纹钉死（生成器只用 random() 与四则运算 / round()、不用 libm，跨平台可钉）——数据一变就红，提醒同时重算 results/ 的 G1 实测。 | 抓住：FAIL: test_golden_fingerprint |
| `synth_db:214:const:0.8999999999999999` | 38 | `0.6` → `0.8999999999999999` | **补判据** | XRPUSDT 基准价 0.6 → 0.9：合成数据的内容常数（第 1 轮被行数判据误杀遮住，下同）；原测试只验“同一份代码两次生成相同”，抓不到“换了一份数据”。新增 `test_synth_db.test_golden_fingerprint`：缺省 seed、0.4% 规模（20 个交易对、6 种策略都出现）的逻辑指纹钉死（生成器只用 random() 与四则运算 / round()、不用 libm，跨平台可钉）——数据一变就红，提醒同时重算 results/ 的 G1 实测。 | 抓住：FAIL: test_golden_fingerprint |
| `synth_db:215:const:0.675` | 38 | `0.45` → `0.675` | **补判据** | ADAUSDT 基准价 0.45 → 0.675：合成数据的内容常数（第 1 轮被行数判据误杀遮住，下同）；原测试只验“同一份代码两次生成相同”，抓不到“换了一份数据”。新增 `test_synth_db.test_golden_fingerprint`：缺省 seed、0.4% 规模（20 个交易对、6 种策略都出现）的逻辑指纹钉死（生成器只用 random() 与四则运算 / round()、不用 libm，跨平台可钉）——数据一变就红，提醒同时重算 results/ 的 G1 实测。 | 抓住：FAIL: test_golden_fingerprint |
| `synth_db:219:const:22.5` | 38 | `15.0` → `22.5` | **补判据** | LINKUSDT 基准价 15 → 22.5：合成数据的内容常数（第 1 轮被行数判据误杀遮住，下同）；原测试只验“同一份代码两次生成相同”，抓不到“换了一份数据”。新增 `test_synth_db.test_golden_fingerprint`：缺省 seed、0.4% 规模（20 个交易对、6 种策略都出现）的逻辑指纹钉死（生成器只用 random() 与四则运算 / round()、不用 libm，跨平台可钉）——数据一变就红，提醒同时重算 results/ 的 G1 实测。 | 抓住：FAIL: test_golden_fingerprint |
| `synth_db:229:const:3.5999999999999996` | 39 | `2.4` → `3.5999999999999996` | **补判据** | OPUSDT 基准价 2.4 → 3.6：合成数据的内容常数（第 1 轮被行数判据误杀遮住，下同）；原测试只验“同一份代码两次生成相同”，抓不到“换了一份数据”。新增 `test_synth_db.test_golden_fingerprint`：缺省 seed、0.4% 规模（20 个交易对、6 种策略都出现）的逻辑指纹钉死（生成器只用 random() 与四则运算 / round()、不用 libm，跨平台可钉）——数据一变就红，提醒同时重算 results/ 的 G1 实测。 | 抓住：FAIL: test_golden_fingerprint |
| `synth_db:1288:binop` | 109 | `i * 300 * DAY` → `i * 300 / DAY` | **补判据** | bot 创建时间 `i * 300 * DAY` → `i * 300 / DAY`：合成数据的内容常数（第 1 轮被行数判据误杀遮住，下同）；原测试只验“同一份代码两次生成相同”，抓不到“换了一份数据”。新增 `test_synth_db.test_golden_fingerprint`：缺省 seed、0.4% 规模（20 个交易对、6 种策略都出现）的逻辑指纹钉死（生成器只用 random() 与四则运算 / round()、不用 libm，跨平台可钉）——数据一变就红，提醒同时重算 results/ 的 G1 实测。 | 抓住：FAIL: test_golden_fingerprint |
| `synth_db:907:binop` | 109 | `i * 300 * DAY // n` → `i * 300 * DAY / n` | **补判据** | bot 创建时间 `(i × 300 天) // n` → `/ n`：除不尽时 created_at 变成浮点（REAL）；钉死的指纹规模下 n = 200 恰好整除、INTEGER 亲和把整值浮点存回整数，看不出来——新增 `test_integer_columns_and_notified_rule`：bots = 65（除不尽）的规模下，所有声明为 INTEGER 的列都只存整数。 | 抓住：FAIL: test_integer_columns_and_notified_rule |
| `synth_db:2022:binop` | 111 | `rnd() * 50` → `rnd() / 50` | **补判据** | bot 配置 JSON 里 grid 档数 `1 + int(rnd() * 50)` → `rnd() / 50`：合成数据的内容常数（第 1 轮被行数判据误杀遮住，下同）；原测试只验“同一份代码两次生成相同”，抓不到“换了一份数据”。新增 `test_synth_db.test_golden_fingerprint`：缺省 seed、0.4% 规模（20 个交易对、6 种策略都出现）的逻辑指纹钉死（生成器只用 random() 与四则运算 / round()、不用 libm，跨平台可钉）——数据一变就红，提醒同时重算 results/ 的 G1 实测。 | 抓住：FAIL: test_golden_fingerprint |
| `synth_db:2267:const:49` | 111 | `50` → `49` | **补判据** | bot 配置 grid 档数上限 `rnd() * 50` → `* 49`：合成数据的内容常数（第 1 轮被行数判据误杀遮住，下同）；原测试只验“同一份代码两次生成相同”，抓不到“换了一份数据”。新增 `test_synth_db.test_golden_fingerprint`：缺省 seed、0.4% 规模（20 个交易对、6 种策略都出现）的逻辑指纹钉死（生成器只用 random() 与四则运算 / round()、不用 libm，跨平台可钉）——数据一变就红，提醒同时重算 results/ 的 G1 实测。 | 抓住：FAIL: test_golden_fingerprint |
| `synth_db:2043:const:4` | 120 | `3` → `4` | **补判据** | settings 值的保留位数 `round(rnd() * 1000, 3)` → 4 位：合成数据的内容常数（第 1 轮被行数判据误杀遮住，下同）；原测试只验“同一份代码两次生成相同”，抓不到“换了一份数据”。新增 `test_synth_db.test_golden_fingerprint`：缺省 seed、0.4% 规模（20 个交易对、6 种策略都出现）的逻辑指纹钉死（生成器只用 random() 与四则运算 / round()、不用 libm，跨平台可钉）——数据一变就红，提醒同时重算 results/ 的 G1 实测。 | 抓住：FAIL: test_golden_fingerprint |
| `synth_db:1744:const:3` | 135 | `4` → `3` | **补判据** | 部分成交量保留位数 `round(qty * rnd(), 4)` → 3 位：合成数据的内容常数（第 1 轮被行数判据误杀遮住，下同）；原测试只验“同一份代码两次生成相同”，抓不到“换了一份数据”。新增 `test_synth_db.test_golden_fingerprint`：缺省 seed、0.4% 规模（20 个交易对、6 种策略都出现）的逻辑指纹钉死（生成器只用 random() 与四则运算 / round()、不用 libm，跨平台可钉）——数据一变就红，提醒同时重算 results/ 的 G1 实测。 | 抓住：FAIL: test_golden_fingerprint |
| `synth_db:1352:binop` | 136 | `rnd() * step` → `rnd() / step` | **补判据** | 订单创建时间抖动 `int(rnd() * step)` → `int(rnd() / step)`：合成数据的内容常数（第 1 轮被行数判据误杀遮住，下同）；原测试只验“同一份代码两次生成相同”，抓不到“换了一份数据”。新增 `test_synth_db.test_golden_fingerprint`：缺省 seed、0.4% 规模（20 个交易对、6 种策略都出现）的逻辑指纹钉死（生成器只用 random() 与四则运算 / round()、不用 libm，跨平台可钉）——数据一变就红，提醒同时重算 results/ 的 G1 实测。 | 抓住：FAIL: test_golden_fingerprint |
| `synth_db:997:const:5` | 150 | `4` → `5` | **补判据** | 成交数量保留位数 `round(0.001 + rnd(), 4)` → 5 位：合成数据的内容常数（第 1 轮被行数判据误杀遮住，下同）；原测试只验“同一份代码两次生成相同”，抓不到“换了一份数据”。新增 `test_synth_db.test_golden_fingerprint`：缺省 seed、0.4% 规模（20 个交易对、6 种策略都出现）的逻辑指纹钉死（生成器只用 random() 与四则运算 / round()、不用 libm，跨平台可钉）——数据一变就红，提醒同时重算 results/ 的 G1 实测。 | 抓住：FAIL: test_golden_fingerprint |
| `synth_db:2116:binop` | 152 | `T0 + 120 * DAY` → `T0 - 120 * DAY` | **补判据** | 成交时间基准 `T0 + 120 * DAY` → `T0 - 120 * DAY`：合成数据的内容常数（第 1 轮被行数判据误杀遮住，下同）；原测试只验“同一份代码两次生成相同”，抓不到“换了一份数据”。新增 `test_synth_db.test_golden_fingerprint`：缺省 seed、0.4% 规模（20 个交易对、6 种策略都出现）的逻辑指纹钉死（生成器只用 random() 与四则运算 / round()、不用 libm，跨平台可钉）——数据一变就红，提醒同时重算 results/ 的 G1 实测。 | 抓住：FAIL: test_golden_fingerprint |
| `synth_db:1419:compare` | 162 | `i > recent` → `i >= recent` | **补判据** | “最后 2% 的 alert 未通知”的 `i > recent` → `>=`：只差恰好第 recent 条且它是 alert 时；同 `test_integer_columns_and_notified_rule`：40 个 seed × 1000 条事件逐条核对 notified 规则（其中 3 个 seed 的第 980 条恰是 alert）。 | 抓住：FAIL: test_integer_columns_and_notified_rule |
| `synth_db:2135:const:9` | 163 | `8` → `9` | **补判据** | 事件 payload 的服务编号 `svc{int(rnd() * 8)}` → `* 9`：合成数据的内容常数（第 1 轮被行数判据误杀遮住，下同）；原测试只验“同一份代码两次生成相同”，抓不到“换了一份数据”。新增 `test_synth_db.test_golden_fingerprint`：缺省 seed、0.4% 规模（20 个交易对、6 种策略都出现）的逻辑指纹钉死（生成器只用 random() 与四则运算 / round()、不用 libm，跨平台可钉）——数据一变就红，提醒同时重算 results/ 的 G1 实测。 | 抓住：FAIL: test_golden_fingerprint |
| `synth_db:1833:const:2` | 164 | `1` → `2` | **补判据** | 事件的 user_id 下界 `1 + int(…)` → `2 + int(…)`：合成数据的内容常数（第 1 轮被行数判据误杀遮住，下同）；原测试只验“同一份代码两次生成相同”，抓不到“换了一份数据”。新增 `test_synth_db.test_golden_fingerprint`：缺省 seed、0.4% 规模（20 个交易对、6 种策略都出现）的逻辑指纹钉死（生成器只用 random() 与四则运算 / round()、不用 libm，跨平台可钉）——数据一变就红，提醒同时重算 results/ 的 G1 实测。 | 抓住：FAIL: test_golden_fingerprint |
| `synth_db:2137:binop` | 164 | `n_users * rnd() * rnd()` → `n_users * rnd() / rnd()` | **补判据** | 事件 user_id 偏斜 `n_users * rnd() * rnd()` → `n_users * rnd() / rnd()`：合成数据的内容常数（第 1 轮被行数判据误杀遮住，下同）；原测试只验“同一份代码两次生成相同”，抓不到“换了一份数据”。新增 `test_synth_db.test_golden_fingerprint`：缺省 seed、0.4% 规模（20 个交易对、6 种策略都出现）的逻辑指纹钉死（生成器只用 random() 与四则运算 / round()、不用 libm，跨平台可钉）——数据一变就红，提醒同时重算 results/ 的 G1 实测。 | 抓住：FAIL: test_golden_fingerprint |
| `synth_db:2330:binop` | 164 | `n_users * rnd()` → `n_users / rnd()` | **补判据** | 事件 user_id `n_users * rnd()` → `n_users / rnd()`：合成数据的内容常数（第 1 轮被行数判据误杀遮住，下同）；原测试只验“同一份代码两次生成相同”，抓不到“换了一份数据”。新增 `test_synth_db.test_golden_fingerprint`：缺省 seed、0.4% 规模（20 个交易对、6 种策略都出现）的逻辑指纹钉死（生成器只用 random() 与四则运算 / round()、不用 libm，跨平台可钉）——数据一变就红，提醒同时重算 results/ 的 G1 实测。 | 抓住：FAIL: test_golden_fingerprint |
| `synth_db:1430:binop` | 165 | `T0 + 100 * DAY + int(i * step) + int(rnd() * step)` → `T0 + 100 * DAY + int(i * step) - int(rnd() * step)` | **补判据** | 事件时间抖动 `+ int(rnd() * step)` → `- int(rnd() * step)`：合成数据的内容常数（第 1 轮被行数判据误杀遮住，下同）；原测试只验“同一份代码两次生成相同”，抓不到“换了一份数据”。新增 `test_synth_db.test_golden_fingerprint`：缺省 seed、0.4% 规模（20 个交易对、6 种策略都出现）的逻辑指纹钉死（生成器只用 random() 与四则运算 / round()、不用 libm，跨平台可钉）——数据一变就红，提醒同时重算 results/ 的 G1 实测。 | 抓住：FAIL: test_golden_fingerprint |

### tick_jitter

| id | 行 | 突变 | 判定 | 理由 | 最终 |
|---|---|---|---|---|---|
| `tick_jitter:555:delete_stmt` | 174 | `conn.execute('COMMIT')` → `pass` | **补判据** | 引擎写事务不 COMMIT：下一个 tick 的 BEGIN IMMEDIATE 撞上未结束的事务报错并回滚，写入全丢；原测试没看写错误；`test_q0_no_jitter_and_wal_off_starves` 新增：安静配置下引擎写错误为空。 | 抓住：FAIL: test_q0_no_jitter_and_wal_off_starves |
| `tick_jitter:1158:binop` | 180 | `start - t0` → `start + t0` | **补判据** | tick 起点 `start − t0` → `start + t0`：间隔是差值、不变，但“唤醒迟到”（起点 − 计划时刻）变成以小时计；新增：安静配置下唤醒迟到要有样本、且最大值 < 10 s（第一次只断言了最大值——突变后每个 tick 都被“上一个 tick 结束晚于计划时刻”的条件筛掉，样本为空、`None or 0` 照样小于 10 s，重跑时仍漏网，于是补上“有样本”）。 | 抓住：FAIL: test_q0_no_jitter_and_wal_off_starves |
| `tick_jitter:581:delete_stmt` | 227 | `self._copy(self.paths[other])` → `pass` | **补判据** | 只读副本刷新时不再拷贝（只切换“当前份”）→ 面板读到的永远是开跑时的旧数据，计时上看不出；新增 `test_replica_refresh_sees_new_rows`：主库插入新行、refresh 后从当前份读到新行，refreshes = 1、当前份切到另一份。 | 抓住：FAIL: test_replica_refresh_sees_new_rows |
| `tick_jitter:1355:const:-1` | 325 | `0` → `-1` | **补判据** | 面板开环轮询的在途计数初值 0 → −1：在途上限从 2 × 线程数悄悄变成 2 × 线程数 + 1；新增 `test_open_loop_caps_inflight`：假查询卡住时，单线程只放进 2 个、其余全部记丢弃。 | 抓住：FAIL: test_open_loop_caps_inflight |
| `tick_jitter:3675:binop` | 365 | `spec['backoff_k'] * res[0]` → `spec['backoff_k'] / res[0]` | **补判据** | 轮询退避的等待 `k × 上次耗时` → `k ÷ 上次耗时`（变成几十秒）；原测试只要求实际 Q 低于目标；新增 `test_backoff_sleep`（mock 掉查询与 sleep）：上次耗时 50 ms / 10 ms 时依次等 0.2 s / 0.1 s。 | 抓住：FAIL: test_backoff_sleep |
| `tick_jitter:2606:const:2` | 430 | `1` → `2` | **等价** | 标定时探测行数 `min(probe_rows, hi − 1)` → `hi − 2`：只在表小于 2 万行时少探 1 行，探测只用来估“每行耗时”（本身是有噪声的计时），随后按估计放大并复测，小表上行数还会被整表封顶——少 1 行的差别淹没在计时噪声里，没有确定性的输出可观测。 | 漏网 |
| `tick_jitter:3780:const:2` | 436 | `1` → `2` | **补判据** | 标定结果的“已封顶”标记 `rows == hi − 1` → `hi − 2`；`test_calibration` 新增：目标远超整表耗时时 capped 为真、行数 = 整表，正常标定 capped 为假。 | 抓住：FAIL: test_calibration |
| `tick_jitter:1568:const:1` | 467 | `0` → `1` | **补判据** | 起通知进程的条件 `notifier_rate > 0` → `> 1`：速率 ≤ 1 次/秒时通知进程不起；缺省 2 次/秒所以没被抓住；`test_replica_backoff_subprocess_bg` 的退避配置改用 0.5 次/秒并断言结果里有 notifier。 | 抓住：FAIL: test_replica_backoff_subprocess_bg |
| `tick_jitter:5553:const:3` | 486 | `2` → `3` | **补判据** | 分段统计里“算”取第 3 列（写）而不是第 2 列；`test_summarize` 新增：读 / 算各段的最大值分别等于各自那一列（1.0 / 30.0 ms）。 | 抓住：FAIL: test_summarize |
| `tick_jitter:6040:const:0` | 541 | `1` → `0` | **补判据** | 拐点表的排序键 `-busy` → `-wal`：busy 不再从大到小；原测试的组合恰好按 compute 排也同序；新增两组（busy 0.5 inline / 5 subprocess）断言 busy 大的在前。 | 抓住：FAIL: test_knees_and_pick |
| `tick_jitter:5054:const:True` | 578 | `False` → `True` | **补判据** | 精简网格里子进程那两组的 WAL 关 → 开；原测试只数了 6 个；新增：逐项断言精简网格的 6 个配置。 | 抓住：FAIL: test_knees_and_pick |
| `tick_jitter:5120:const:-1` | 608 | `0` → `-1` | **补判据** | 明细行里没有样本时 `or 0` → `or -1`（印成 −1.0）；新增：分段 p99 全为 None 时明细行印 `0.0/0.0/0.0 ms`、唤醒迟到印 0。 | 抓住：FAIL: test_knees_and_pick |
| `tick_jitter:5124:const:1` | 609 | `0` → `1` | **补判据** | 明细行里唤醒迟到没有样本时 `or 0` → `or 1`；同上。 | 抓住：FAIL: test_knees_and_pick |
| `tick_jitter:4194:const:1` | 630 | `0` → `1` | **补判据** | 首行里读段 p99 没有样本时 `or 0` → `or 1`；新增：分段 p99 全为 None 时首行印 `read_p99_ms=0.00 compute_p99_ms=0.00 write_p99_ms=0.00`。 | 抓住：FAIL: test_knees_and_pick |
| `tick_jitter:4198:const:-1` | 631 | `0` → `-1` | **补判据** | 首行里算段 p99 没有样本时 `or 0` → `or -1`；同上。 | 抓住：FAIL: test_knees_and_pick |
| `tick_jitter:5228:const:999` | 648 | `1000` → `999` | **补判据** | 扫描报告里面板查询耗时 ×1000 → ×999（0.1 s 时两者都印 100 ms）；新增：标定 2.0 s 时印“单线程 2000 ms”。 | 抓住：FAIL: test_knees_and_pick |
| `tick_jitter:5306:const:False` | 702 | `True` → `False` | **补判据** | CLI run 收尾 `rmtree(work, ignore_errors=True)` → `False`；`test_cli` 让 rmtree 在 ignore_errors 为假时抛错，断言 run 照常退出并写出结果。 | 抓住：ERROR: test_cli |
| `tick_jitter:1001:delete_stmt` | 704 | `print(row_text(res))` → `pass` | **补判据** | CLI run 不再打印明细行；`test_cli` 新增：首行之后第二行是 `WAL …` 开头的明细。 | 抓住：ERROR: test_cli |

## A2. 重抽样模块里新出现的 3 条漏网

| id | 行 | 突变 | 判定 | 理由 | 最终 |
|---|---|---|---|---|---|
| `fake_backend:2194:const:501` | 218 | `500` → `501` | **等价** | 起不来时异常信息保留子进程输出的最后 500 → 501 个字符：只在输出超过 500 字符时差一个字符，不进任何判定（测试只要求原因的关键字在里面）。 | 漏网 |
| `fake_backend:292:delete_stmt` | 272 | `common.child_finish()` → `pass` | **等价** | 假后端子进程收尾的显式 `child_finish()` 被删：`child_init` 已用 atexit 注册了同一个（幂等的）`child_finish`，假后端是普通的 `python -m` 进程、经 sys.exit 正常退出，atexit 照样写出覆盖率——显式调用只对用 os._exit 结束的 multiprocessing 子进程才是必需的；`test_stop_is_graceful_and_child_flushes` 实测突变体下覆盖率文件照样写出。 | 漏网 |
| `sqlite_doctor:7069:const:4` | 436 | `3` → `4` | **等价** | 延迟事务演示场景的 `seed=3` → `4`（规格层面等价，同 sqlite_load:2013）：三个争用场景的种子 1 / 2 / 3 只要求彼此不同、同 seed 可复现，4 与 1、2 仍不同；换的只是同分布的另一组随机时刻。 | 漏网 |

## B. 第 2 轮显形的漏网（第 1 轮被误杀遮住；第 3 轮前已补判据）

| id | 行 | 突变 | 判定 | 理由 | 第 3 轮 |
|---|---|---|---|---|---|
| `http_load:420:const:True` | 212 | `False` → `True` | **补判据** | `ab()` 的缺省 `allow_remote=False` → `True`（第 1 轮被 test_profile_gate 的抖动“抓住”）；新增 `test_ab_loopback_only_by_default`：mock 掉 run_step，断言 ab / ramp 缺省都以 allow_remote=False 调用（只许打本机；测试本身不开 socket）。 | 抓住 |
| `http_load:1537:const:False` | 280 | `True` → `False` | **补判据** | CLI `ramp --base` 由必填改成可选（第 1 轮被 test_load 的 WAL 测试抖动“抓住”）；新增 `test_cli_required_args`：缺 --base 时 argparse 以退出码 2 拒绝、提示 required。 | 抓住 |
| `http_load:1558:const:False` | 286 | `True` → `False` | **补判据** | CLI `ab --a` 由必填改成可选（第 1 轮同上被抖动“抓住”）；同 `test_cli_required_args`（缺 --a / 缺 --b 都必须被 argparse 拒绝）。 | 抓住 |
| `http_load:2526:compare` | 303 | `st['error_rate'] > a.max_error_rate` → `st['error_rate'] >= a.max_error_rate` | **补判据** | CLI ramp 的超限判定 `error_rate > 上限` → `>=`（第 1 轮被 WAL 测试抖动“抓住”）；新增断言：注入 100% 错误的路径配 `--max-error-rate 1.0` 退出码为 0（恰好等于上限不算超限；错误是注入的，与负载无关）。 | 抓住 |
| `index_advisor:30:const:1.4925` | 19 | `0.995` → `1.4925` | **补判据** | 估算用的叶页填充率 FILL 0.995 → 1.4925（估算缩小 1/3；第 1 轮被 WAL 测试抖动“抓住”）。原有的手算断言按 `A.FILL` 符号计算，抓不到常数本身；新增 `test_estimate_close_to_built_index`：2 万行表上估算与真建出来的索引（dbstat）相差 ≤ 10%（实测 ≤ 2%）。 | 抓住 |
| `index_advisor:407:delete_stmt` | 29 | `nd['alias'] = nd['table']` → `pass` | **等价** | 删掉 `nd['alias'] = nd['table']`：resolve_aliases 的输入只来自 `parse_eqp`（3 处调用都是 `resolve_aliases(explain(...))`），而 parse_eqp 给每个带 table 的节点都已设好 alias（= EQP 里出现的名字）；SQLite ≥ 3.36 的格式（`SCAN x`）下这句写入的是同一个值。旧格式（`SCAN TABLE t AS x`）下两者才不同，但节点的 alias 字段没有任何消费者（doctor / advisor / classify 只读 table、index、detail），不进输出。 | 漏网 |
| `index_advisor:428:boolop` | 45 | `bool(rec['temp_btree']) and (not scan)` → `bool(rec['temp_btree']) or not scan` | **补判据** | `sort = temp_btree and not scan` → `or`（第 1 轮被 WAL 测试抖动“抓住”）：既不扫描、也没有临时 B-tree（排序已被现有索引消掉）的语句会被误出“排序”建议；`test_propose_already_and_unsolvable` 加入这样一条语句 q7，断言建议清单不变。 | 抓住 |
| `index_advisor:196:negate_cond` | 85 | `if c['kind'] == 'scan': host['kind'] = 'scan'` → `if not c['kind'] == 'scan': host['kind'] = 'scan'` | **补判据** | 合并候选时“scan 优先”的条件取反（第 1 轮被 WAL 测试抖动“抓住”）；新增 `test_merge_kind_scan_wins`：sort+sort → sort，sort 宿主并入 scan → scan，scan 宿主并入 sort → scan。 | 抓住 |
| `index_advisor:483:delete_stmt` | 86 | `host['kind'] = 'scan'` → `pass` | **补判据** | 合并时删掉 `host['kind'] = 'scan'`（第 1 轮被 WAL 测试抖动“抓住”）；同 `test_merge_kind_scan_wins`（sort 宿主并入 scan 候选必须变 scan）。 | 抓住 |
| `index_advisor:498:binop` | 97 | `hi - lo + 1` → `hi - lo - 1` | **补判据** | `hi - lo + 1 <= k` → `hi - lo - 1 <= k`（第 1 轮被 WAL 测试抖动“抓住”）：行数比 k 多 1、2 时会误取全部行；`test_sample_rowids` 新增 100 行取 k = 99 必须恰好 99 个。 | 抓住 |
| `index_advisor:517:const:499` | 107 | `500` → `499` | **补判据** | 抽样行按 500 一块查询，步长 500 → 499：相邻两块重叠一行，被重复计入平均单元字节（第 1 轮被 WAL 测试抖动“抓住”）；新增 `test_estimate_counts_each_sampled_row_once`：1200 行（跨块边界），mock 掉单元字节计算，断言每个抽样 rowid 恰好计一次。 | 抓住 |
| `index_advisor:1857:const:501` | 108 | `500` → `501` | **补判据** | 块切片 `ids[i:i+500]` → `ids[i:i+501]`，同样让相邻块重叠一行；同上 `test_estimate_counts_each_sampled_row_once`。 | 抓住 |
| `profile_gate:857:delete_stmt` | 165 | `fn(**args)` → `pass` | **补判据** | worker 删掉冷样本里的第一次调用（冷耗时只剩导入）；比时长在满载下会抖，改为数调用次数：新增 `test_worker_call_count`，进程内跑 worker，断言目标被调用 1 + inner 次。 | 抓住 |
| `sqlite_load:141:delete_stmt` | 239 | `conn.close()` → `pass` | **等价** | （旧源码）WAL 写者进程循环后的 `conn.close()` 删除：函数返回时局部变量 `conn` 引用计数归零，CPython 立即析构并关闭连接（最后一个连接关闭时的检查点照做），只是推迟到写完 JSON 之后；父进程在写者退出（join）之后才读结果、看库，观测不到先后。 | 不在第 3 轮样本里（源码已改） |

## C. 第 1 轮的 40 条漏网（第 2 轮前已补判据）

| id | 行 | 突变 | 判定 | 理由 | 第 3 轮 |
|---|---|---|---|---|---|
| `common:768:negate_cond` | 37 | `if line.startswith('model name'): return line.split(':', 1)[` → `if not line.startswith('model name'): return line.split(':',` | **补判据** | `cpu_model()` 的 `if line.startswith('model name')` 取反后返回第一行无关字段；新增 `test_common.test_cpu_model`：伪造 /proc/cpuinfo（mock open），断言取到第一条 model name 的值。 | 抓住 |
| `common:2065:const:2` | 38 | `1` → `2` | **补判据** | `split(':', 1)` → `split(':', 2)` 会把带冒号的型号名截断；`test_cpu_model` 的伪造型号名里带 `:`（`Fancy: CPU 9 @ 3GHz`），要求原样返回。 | 抓住 |
| `common:1191:const:9` | 57 | `10` → `9` | **等价** | `git rev-parse` 的超时 10 s → 9 s：本地 git 毫秒级返回，只有 git 卡在 9～10 s 之间才有差别；超时值不进入任何输出或判据，外部不可观测。 | 漏网 |
| `common:866:const:1` | 92 | `2` → `1` | **等价** | `rel_spread` 的 `len(values) < 2` → `< 1`：只剩 1 个值时，原代码提前返回 0.0，突变后走 `(max-min)/min = 0/v = 0.0`（v ≤ 0 时被同一行的 `min(values) <= 0` 拦下返回 0.0）；空列表仍被 `< 1` 拦下。对所有输入返回值相同。 | 漏网 |
| `common:1273:compare` | 115 | `ord(ch) > 11903` → `ord(ch) >= 11903` | **补判据** | 表格宽字符边界 `> 0x2E7F` → `>= 0x2E7F`；`test_common` 新增 U+2E7F 宽 1、U+2E80 宽 2 的边界断言。 | 抓住 |
| `common:272:const:61` | 118 | `60` → `61` | **补判据** | `sparkline` 缺省宽度 60 → 61；新增断言：缺省宽度下 60 个点出 60 格、61 个点按每格 2 个出 31 格。 | 抓住 |
| `common:686:const:419` | 218 | `420` → `419` | **补判据** | 审计日志文件权限 `0o644` → `0o643`；`test_audit_*` 新增断言日志文件 `st_mode & 0o777 == 0o644 & ~umask`。 | 抓住 |
| `common:1868:break_continue` | 249 | `continue` → `break` | **补判据** | 审计事件里解析不了的 fd 相对路径应跳过该路径（`continue`）而不是放弃整条事件（`break`）；新增断言：`os.rename` 的源是解析不了的 fd 相对名时，目标绝对路径 `/x/c` 仍被记录。 | 抓住 |
| `common:412:const:False` | 281 | `True` → `False` | **补判据** | `start_process` 缺省 `daemon=True` → `False`；新增断言：不传 daemon 时进程是 daemonic（父进程退出不留孤儿）。 | 抓住 |
| `http_load:618:unary_drop` | 103 | `-1` → `1` | **补判据** | `pick` 的浮点尾差兜底 `targets[-1]` → `targets[1]`：两个目标时二者相同所以原测试抓不到；新增 3 个目标、r = 1.0 时必须取最后一个。 | 抓住 |
| `http_load:1226:delete_stmt` | 136 | `conn.close()` → `pass` | **等价** | 删掉 `if resp.will_close: conn.close()`：CPython `HTTPConnection.getresponse()` 在 `response.will_close` 时自己已经 `self.close()`（把 socket 交给 response），下一次 `request()` 自动重连；我们这行是冗余的保险，删除后行为完全相同。 | 漏网 |
| `http_load:2728:binop` | 240 | `t + 1e-09` → `t - 1e-09` | **补判据** | A/B 判红 `change > tol + 1e-9` → `tol - 1e-9`：变化恰好等于容差时应判绿；新增 `edge` 断言（A=100、B=110、容差 10% → OK）。 | 抓住 |
| `index_advisor:408:delete_stmt` | 30 | `nd['table'] = amap.get(nd['table'], nd['table'])` → `pass` | **补判据** | `resolve_aliases` 不再把别名换回真表名；新增 `test_index_advisor.test_resolve_aliases` 直接断言（别名 → 表名，alias 字段保留别名）。 | 抓住 |
| `index_advisor:463:delete_stmt` | 67 | `already.append({'statement': rec['id'], 'table': table, 'col` → `pass` | **补判据** | 已有索引覆盖候选列时不再记入 `already_indexed`；新增断言：此时进 already_indexed、不出建议。 | 抓住 |
| `index_advisor:195:delete_stmt` | 84 | `host['why'].update(c['why'])` → `pass` | **补判据** | 前缀合并时丢掉被并入语句的 `why`；新增断言：合并后的建议保留两条语句的理由。 | 抓住 |
| `index_advisor:199:delete_stmt` | 88 | `o['name'] = index_name(o['table'], o['columns'])` → `pass` | **补判据** | `_merge` 不再给合并结果命名（`_create` 之后会补名，所以端到端看不出）；`propose` 是公开接口，新增断言其输出带 `name`。 | 抓住 |
| `index_advisor:209:compare` | 97 | `hi - lo + 1 <= k` → `hi - lo + 1 < k` | **等价** | `sample_rowids` 的 `hi - lo + 1 <= k` → `< k`：恰好 k 行时突变走等距公式 `lo + (hi-lo)·i // (k-1) = lo + (k-1)·i // (k-1) = lo + i`，与 `range(lo, hi+1)` 逐个相同；k = 1 的除零分支只在直接传 k = 1 时可达，库内唯一调用用常量 SAMPLE_ROWS = 2000。 | 漏网 |
| `index_advisor:502:binop` | 99 | `lo + (hi - lo) * i // (k - 1)` → `lo - (hi - lo) * i // (k - 1)` | **补判据** | 等距抽样 `lo + …` → `lo - …`；新增 `test_sample_rowids`：k 个、首 lo 尾 hi、单调、都在区间内。 | 抓住 |
| `index_advisor:2160:unary_drop` | 111 | `-1` → `1` | **补判据** | `row[:-1]` → `row[:1]`：单列索引时二者相同；新增 `test_estimate_size_matches_hand_computation` 用两列 (a, b) 按记录格式手算期望字节数。 | 抓住 |
| `index_advisor:952:const:1` | 112 | `0` → `1` | **补判据** | 分块 `range(0, …)` → `range(1, …)` 漏掉第一行；同上手算断言（3 行小表，期望值精确相等）。 | 抓住 |
| `index_advisor:618:const:2` | 157 | `3` → `2` | **等价** | `round(build_seconds, 3)` → `2`：只改报告里“建索引耗时”这个计时值的小数位；它不进任何判定，契约也不约定位数，且是不可复现的计时，无法写出确定性断言。 | 漏网 |
| `index_advisor:628:binop` | 159 | `used_bytes(conn) - before` → `used_bytes(conn) + before` | **补判据** | 没有 dbstat 时的大小 `used - before` → `used + before`；新增 `test_redundant_and_no_dbstat_size`：mock `object_size` 返回 None 时 size_source 为 page_count-freelist delta，size_bytes 是页大小的整数倍、大于 0 且小于 3000 行 × 40 字节（加上建前已用字节会远超）。 | 抓住 |
| `index_advisor:1095:boolop` | 174 | `c not in sug['columns'] and c not in extra and (c != rowid_c` → `c not in sug['columns'] or c not in extra or c != rowid_cols` | **补判据** | `_covering_variant` 补列条件 and/or 互换；新增 `test_covering_variant` 直接测（补引用列、不补 rowid 列、无列可补返回 None）。 | 抓住 |
| `index_advisor:109:negate_cond` | 176 | `if not extra: return None` → `if not not extra: return None` | **补判据** | `if not extra` 取反；同上 `test_covering_variant`，并加端到端：单值列上 ANALYZE 后普通索引被弃用、覆盖索引被采用。 | 抓住 |
| `index_advisor:322:const:3` | 181 | `4` → `3` | **等价** | `verify` 的最多轮数 4 → 3：第 1 轮可能删 / 换覆盖，第 2 轮只可能删（覆盖变体不再派生，被删的是没人用的或仍扫描的索引，删它不改变其他语句的计划），第 3 轮必然无变化而退出——有变化的轮最多 2 轮、总轮数 ≤ 3，上限 3 与 4 走完全相同的路径。 | 漏网 |
| `index_advisor:724:delete_stmt` | 219 | `cov['check_as_is'] = _checks(conn, cov, by_id, cols_map, exp` → `pass` | **补判据** | 覆盖变体建好后不重算 `check_as_is`（于是沿用从普通索引 dict 拷来的旧计划）；`test_covering_fallback_end_to_end` 断言变体自己的“建后、ANALYZE 前”计划用的是 `qdperf_t_a_b`。第一次补的断言只查了键 q 存在——而键是继承来的，重跑时仍漏网，于是收紧成查计划内容。 | 抓住 |
| `index_advisor:725:delete_stmt` | 220 | `live.append(cov)` → `pass` | **补判据** | 覆盖变体建好后不放回 `live`（于是不出现在验证结果里）；端到端测试断言 verified 里有 covering=True 的那条。 | 抓住 |
| `index_advisor:1199:const:1.0` | 237 | `0.0` → `1.0` | **补判据** | 写放大合计初值 0.0 → 1.0；新增 `test_write_amplification_exact`：合计 = Σ 频次 × B-tree 数（精确值）。 | 抓住 |
| `index_advisor:762:binop` | 249 | `per_sug[s['name']] += st['per_second']` → `per_sug[s['name']] -= st['per_second']` | **补判据** | 每条建议的额外 B-tree 写/秒 `+=` → `-=`；同上精确值断言。 | 抓住 |
| `profile_gate:330:const:31` | 128 | `30` → `31` | **补判据** | `top_functions` 缺省前 30 → 31；新增 `test_top_defaults_and_rounding`：40 个函数时缺省只返回 30 个。 | 抓住 |
| `profile_gate:351:delete_stmt` | 144 | `m['tottime'] = round(m['tottime'], 6)` → `pass` | **补判据** | `tottime` 不再保留 6 位小数；同上断言 tottime / cumtime 都等于 round(x, 6)。 | 抓住 |
| `profile_gate:516:const:4` | 279 | `3` → `4` | **补判据** | `baseline` 缺省 runs 3 → 4；新增 `test_runs_defaults`（mock measure）：缺省调用 3 次。 | 抓住 |
| `profile_gate:523:compare` | 281 | `runs < 1` → `runs <= 1` | **补判据** | `runs < 1` → `runs <= 1`；同上：runs = 1 可用、runs = 0 报错。 | 抓住 |
| `profile_gate:537:const:2` | 286 | `3` → `2` | **补判据** | `pair` 缺省 runs 3 → 2；同上（mock interleaved）：缺省 6 个臂、A B 交替。 | 抓住 |
| `profile_gate:1102:const:False` | 293 | `True` → `False` | **补判据** | `pair` 不再量冷启动；新增 `test_cli_pair`：两份 JSON 都有 cold。 | 抓住 |
| `profile_gate:584:const:11` | 320 | `10` → `11` | **补判据** | `compare` 缺省点名前 10 → 11；新增：第 10 名的新函数点名、第 11 名不点名。 | 抓住 |
| `profile_gate:3898:const:101` | 350 | `100` → `101` | **补判据** | 明细行里 `manual × 100` → `× 101`；改为逐字断言整行 `ok  hot.wall_s 1→1.15 +15.0% tol=16.0% (manual 10.0%, 2×noise 16.0%)`。 | 抓住 |
| `profile_gate:3461:compare` | 384 | `name == 'run'` → `name != 'run'` | **补判据** | CLI `--runs` 缺省（run 1 / baseline、pair 3）取反；新增断言 run 的首行是 `runs=1`。 | 抓住 |
| `profile_gate:1321:delete_stmt` | 436 | `common.write_json(args.out_b, doc_b)` → `pass` | **补判据** | CLI pair 不写 `--out-b`；新增 `test_cli_pair`：首行 PROFILE_B、两份文件都写出、B 里有 extra 而 A 没有。 | 抓住 |
| `profile_gate:248:delete_stmt` | 447 | `print(common.text_table(['累计s', '自身s', '调用', '函数'], [(f'{t['` → `pass` | **补判据** | CLI 首行之后不打印函数表；新增断言输出含表头与目标函数名。 | 抓住 |

## 超时（计入抓住）

- `sqlplan:1266:const:0` 行 308：`1` → `0`（解析 / 调度死循环）

## 未确认的失败（抖动，没有算抓住）

- `sqlite_load:2895:const:999`：`FAIL: test_replica_backoff_subprocess_bg (qdperf.tests.test_tick_jitter.TestRealProcesses.test_replica_backoff_subprocess_bg)`
- `sqlplan:1974:delete_stmt`：`FAIL: test_q0_no_jitter_and_wal_off_starves (qdperf.tests.test_tick_jitter.TestRealProcesses.test_q0_no_jitter_and_wal_off_starves)`
