# qdexsim 突变测试漏网清单（自动生成；逐条判定见 qdexsim/mutants/triage.md）

总数 510，抓住 476，漏网 34，抓住率 93.3%

- `adapters:666:const:0.44999999999999996` 行 76 [const]: `0.3` -> `0.44999999999999996`
- `adapters:4846:const:-1` 行 418 [const]: `0` -> `-1`
- `adapters:4875:const:-1` 行 430 [const]: `0` -> `-1`
- `adapters:3428:const:True` 行 433 [const]: `False` -> `True`
- `adapters:3519:const:-1` 行 470 [const]: `0` -> `-1`
- `chaos:1635:const:2` 行 171 [const]: `1` -> `2`
- `clients:2059:binop` 行 210 [binop]: `e.retry_after_ms / 1000.0 + 0.01` -> `e.retry_after_ms / 1000.0 - 0.01`
- `clients:1361:const:0.03` 行 318 [const]: `0.02` -> `0.03`
- `dialects/base:259:delete_stmt` 行 179 [delete_stmt]: `raise NotImplementedError` -> `pass`
- `dialects/bitget:1534:const:999` 行 87 [const]: `1000` -> `999`
- `dialects/bybit:1302:const:1001` 行 148 [const]: `1000` -> `1001`
- `dialects/okx:2326:const:0` 行 185 [const]: `1` -> `0`
- `engine:1611:const:1.0` 行 151 [const]: `0.0` -> `1.0`
- `engine:2976:compare` 行 348 [compare]: `old > 0` -> `old >= 0`
- `engine:3022:delete_stmt` 行 370 [delete_stmt]: `break` -> `pass`
- `engine:4186:binop` 行 371 [binop]: `o['qty'] - o['filled']` -> `o['qty'] + o['filled']`
- `engine:2202:compare` 行 398 [compare]: `abs(p['qty']) * p['entry'] / p['leverage'] + upl <= mmr * notional` -> `abs(p['qty']) * p['entry'] / p['leverage'] + upl < mmr * notional`
- `harness:690:const:1` 行 131 [const]: `2` -> `1`
- `harness:1144:const:49` 行 133 [const]: `50` -> `49`
- `harness:1144:const:51` 行 133 [const]: `50` -> `51`
- `harness:977:const:False` 行 146 [const]: `True` -> `False`
- `judges:937:const:1.0` 行 97 [const]: `0.0` -> `1.0`
- `judges:940:const:1.0` 行 97 [const]: `0.0` -> `1.0`
- `judges:593:compare` 行 98 [compare]: `abs(mine.get(s, 0.0) - server.get(s, 0.0)) > 1e-09` -> `abs(mine.get(s, 0.0) - server.get(s, 0.0)) >= 1e-09`
- `judges:339:compare` 行 104 [compare]: `abs(float(mine) - server) < 1e-06` -> `abs(float(mine) - server) <= 1e-06`
- `recon:287:const:1799999` 行 123 [const]: `1800000` -> `1799999`
- `recon:296:compare` 行 129 [compare]: `abs(med) < min_ms` -> `abs(med) <= min_ms`
- `recon:669:compare` 行 142 [compare]: `abs(l['ts'] - ts_offset_ms - r['ts']) > ts_tol_ms` -> `abs(l['ts'] - ts_offset_ms - r['ts']) >= ts_tol_ms`
- `server:184:delete_stmt` 行 266 [delete_stmt]: `self.httpd.daemon_threads = True` -> `pass`
- `server:221:const:1` 行 311 [const]: `0` -> `1`
- `store:406:const:-1` 行 93 [const]: `0` -> `-1`
- `store:426:const:-1` 行 103 [const]: `0` -> `-1`
- `store:144:const:-1` 行 105 [const]: `0` -> `-1`
- `store:447:const:-1` 行 117 [const]: `0` -> `-1`
