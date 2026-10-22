# qdperf 突变测试漏网清单（自动生成；逐条判定见 qdperf/mutants/triage.md）

总数 500，抓住 477，漏网 23，抓住率 95.4%

- `common:1191:const:9` 行 57 [const]: `10` -> `9`
- `common:866:const:1` 行 92 [const]: `2` -> `1`
- `fake_backend:2194:const:501` 行 218 [const]: `500` -> `501`
- `fake_backend:292:delete_stmt` 行 272 [delete_stmt]: `common.child_finish()` -> `pass`
- `http_load:1226:delete_stmt` 行 136 [delete_stmt]: `conn.close()` -> `pass`
- `index_advisor:407:delete_stmt` 行 29 [delete_stmt]: `nd['alias'] = nd['table']` -> `pass`
- `index_advisor:209:compare` 行 97 [compare]: `hi - lo + 1 <= k` -> `hi - lo + 1 < k`
- `index_advisor:618:const:2` 行 157 [const]: `3` -> `2`
- `index_advisor:322:const:3` 行 181 [const]: `4` -> `3`
- `sqlite_doctor:7069:const:4` 行 436 [const]: `3` -> `4`
- `sqlite_doctor:835:delete_stmt` 行 478 [delete_stmt]: `ro.close()` -> `pass`
- `sqlite_load:268:const:1.0` 行 95 [const]: `0.0` -> `1.0`
- `sqlite_load:665:const:1.5` 行 137 [const]: `1.0` -> `1.5`
- `sqlite_load:1321:binop` 行 191 [binop]: `0.2 * len(writers)` -> `0.2 / len(writers)`
- `sqlite_load:2013:binop` 行 196 [binop]: `seed * 1000 + k` -> `seed * 1000 - k`
- `sqlite_load:2546:binop` 行 196 [binop]: `seed * 1000` -> `seed / 1000`
- `sqlite_load:2895:const:999` 行 196 [const]: `1000` -> `999`
- `sqlite_load:2575:const:59` 行 200 [const]: `60` -> `59`
- `sqlite_load:2147:const:3` 行 252 [const]: `2` -> `3`
- `sqlplan:1180:compare` 行 270 [compare]: `i < len(toks)` -> `i <= len(toks)`
- `sqlplan:1242:const:0` 行 298 [const]: `1` -> `0`
- `sqlplan:3042:const:2` 行 309 [const]: `1` -> `2`
- `tick_jitter:2606:const:2` 行 430 [const]: `1` -> `2`
