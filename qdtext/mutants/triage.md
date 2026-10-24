# qdtext 突变测试逐条判定（自动生成：`python3.12 qdtext/mutants/triage.py`）

命令：`python3.12 qdtext/mutants/run.py --per-module 40 --jobs 4`；每模块抽样 40；基线 6.8 s；完成于 2026-10-24 00:10:22

总数 280，抓住 265（含超时 1），漏网 15，抓住率 94.6%

| 模块 | 候选 | 抽样 | 抓住 | 漏网 | 抓住率 |
|---|---|---|---|---|---|
| cdp | 346 | 40 | 33 | 7 | 82.5% |
| common | 317 | 40 | 37 | 3 | 92.5% |
| locale_lint | 170 | 40 | 39 | 1 | 97.5% |
| mobile_layout | 231 | 40 | 38 | 2 | 95.0% |
| synth | 240 | 40 | 40 | 0 | 100.0% |
| wordy | 328 | 40 | 38 | 2 | 95.0% |
| zh_pair | 209 | 40 | 40 | 0 | 100.0% |

## 漏网逐条

- `cdp:198:delete_stmt` 行 184 [delete_stmt] `time.sleep(0.05)` → `pass`：**equivalent** — 等 DevToolsActivePort 的轮询里去掉 sleep(0.05)：变成忙等，结果相同（性能差异测试不该钉）
- `cdp:428:compare` 行 217 [compare] `remaining <= 0` → `remaining < 0`：**equivalent** — _pump 里 remaining <= 0 → < 0：remaining 是浮点差值，恰好等于 0 的概率为零；为 0 时 settimeout(0) 也立刻超时
- `cdp:249:delete_stmt` 行 270 [delete_stmt] `self.evaluate(session, 'new Promise(r => requestAnimationFra` → `pass`：**equivalent** — 导航后去掉「等两帧让排版落定」的 Runtime.evaluate：只影响时序，无头 Chromium 上 load 之后排版已稳定，盒子坐标相同
- `cdp:890:delete_stmt` 行 283 [delete_stmt] `self.call('Browser.close', timeout=3)` → `pass`：**equivalent** — Browser.close 去掉 Browser.close 协议调用：后面的 terminate/kill 仍结束进程，socket、returncode、目录清理结果相同（只少了优雅关闭）
- `cdp:1894:const:4` 行 291 [const] `5` → `4`：**equivalent** — terminate 后 wait 的超时 5→4 秒：Chromium 退出远快于此，只有卡死时可观测
- `cdp:906:delete_stmt` 行 298 [delete_stmt] `break` → `pass`：**equivalent** — rmtree 重试循环里去掉 break：目录已删后再空转 49 次 rmtree + sleep，慢 5 秒但结果相同
- `cdp:522:delete_stmt` 行 299 [delete_stmt] `time.sleep(0.1)` → `pass`：**equivalent** — rmtree 重试循环里去掉 sleep(0.1)：变成忙重试，结果相同
- `common:279:const:3` 行 40 [const] `2` → `3`：**equivalent** — SEVERITY_ORDER 里 info 的序号 2→3：仍然排在 yellow(1) 之后，排序结果不变
- `common:1382:const:0` 行 154 [const] `1` → `0`：**equivalent** — write_json 的 indent 1→0：只改 JSON 缩进，读回来内容相同
- `common:1382:const:2` 行 154 [const] `1` → `2`：**equivalent** — write_json 的 indent 1→2：同上
- `locale_lint:1773:const:3` 行 110 [const] `2` → `3`：**equivalent** — over 字段 round(…, 2)→round(…, 3)：字宽都是 0.5 的倍数，两位小数已精确，值不变
- `mobile_layout:1161:const:-1` 行 178 [const] `0` → `-1`：**equivalent** — argparse 的 description 取模块 docstring 第 0 行→最后一行：只影响 --help 文本
- `mobile_layout:1231:const:1` 行 205 [const] `2` → `1`：**equivalent** — meta.seconds 的 round(…, 2)→round(…, 1)：耗时统计的小数位，不是判定
- `wordy:637:const:-1` 行 165 [const] `0` → `-1`：**equivalent** — resolve_key 里 hits[0]→hits[-1]：前一行已保证 len(hits) == 1，两者同一个元素
- `wordy:1466:const:1` 行 218 [const] `0` → `1`：**equivalent** — argparse 的 description 取 docstring 第 0 行→第 1 行：只影响 --help 文本
