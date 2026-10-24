# qdtext 突变测试漏网清单（自动生成；逐条判定见 qdtext/mutants/triage.md）

总数 280，抓住 265，漏网 15，抓住率 94.6%

- `cdp:198:delete_stmt` 行 184 [delete_stmt]: `time.sleep(0.05)` -> `pass`
- `cdp:428:compare` 行 217 [compare]: `remaining <= 0` -> `remaining < 0`
- `cdp:249:delete_stmt` 行 270 [delete_stmt]: `self.evaluate(session, 'new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => r(1))))', True)` -> `pass`
- `cdp:890:delete_stmt` 行 283 [delete_stmt]: `self.call('Browser.close', timeout=3)` -> `pass`
- `cdp:1894:const:4` 行 291 [const]: `5` -> `4`
- `cdp:906:delete_stmt` 行 298 [delete_stmt]: `break` -> `pass`
- `cdp:522:delete_stmt` 行 299 [delete_stmt]: `time.sleep(0.1)` -> `pass`
- `common:279:const:3` 行 40 [const]: `2` -> `3`
- `common:1382:const:0` 行 154 [const]: `1` -> `0`
- `common:1382:const:2` 行 154 [const]: `1` -> `2`
- `locale_lint:1773:const:3` 行 110 [const]: `2` -> `3`
- `mobile_layout:1161:const:-1` 行 178 [const]: `0` -> `-1`
- `mobile_layout:1231:const:1` 行 205 [const]: `2` -> `1`
- `wordy:637:const:-1` 行 165 [const]: `0` -> `-1`
- `wordy:1466:const:1` 行 218 [const]: `0` -> `1`
