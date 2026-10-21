# 突变测试漏网清单（survivors）

总数 320，抓住 262，漏网 58，抓住率 81.9%

- `broker:1066:compare` 行 145 [compare]: `side < 0` -> `side <= 0`
- `broker:2466:const` 行 203 [const]: `0` -> `1`
- `broker:1153:break_continue` 行 213 [break_continue]: `continue` -> `break`
- `broker:1168:compare` 行 226 [compare]: `order.remaining < cap` -> `order.remaining <= cap`
- `broker:2532:const` 行 234 [const]: `0` -> `-1`
- `broker:1198:compare` 行 239 [compare]: `side > 0` -> `side >= 0`
- `broker:1880:const` 行 239 [const]: `0` -> `-1`
- `broker:3106:compare` 行 242 [compare]: `o < p` -> `o <= p`
- `broker:1357:compare` 行 333 [compare]: `self.cash < 0` -> `self.cash <= 0`
- `broker:1424:compare` 行 372 [compare]: `abs(remaining) <= 1e-12` -> `abs(remaining) < 1e-12`
- `engine:743:const` 行 154 [const]: `1e-09` -> `1.5000000000000002e-09`
- `engine:419:compare` 行 156 [compare]: `delta < -1e-09` -> `delta <= -1e-09`
- `engine:1202:const` 行 189 [const]: `0` -> `-1`
- `engine:1358:const` 行 255 [const]: `0.0` -> `1.0`
- `engine:958:const` 行 256 [const]: `1` -> `0`
- `metrics:1322:const` 行 21 [const]: `0` -> `1`
- `metrics:326:compare` 行 30 [compare]: `len(equity) < 1` -> `len(equity) <= 1`
- `metrics:150:compare` 行 40 [compare]: `equity[-1] <= 0` -> `equity[-1] < 0`
- `metrics:341:const` 行 40 [const]: `0` -> `-1`
- `metrics:358:const` 行 53 [const]: `0` -> `-1`
- `metrics:1062:const` 行 58 [const]: `2` -> `1`
- `metrics:1355:const` 行 58 [const]: `1e-14` -> `1.5e-14`
- `metrics:711:binop` 行 58 [binop]: `(1e-14 * scale) ** 2` -> `1e-14 * scale * 2`
- `metrics:177:compare` 行 63 [compare]: `len(returns) < 2` -> `len(returns) <= 2`
- `metrics:189:compare` 行 72 [compare]: `len(returns) < 2` -> `len(returns) <= 2`
- `metrics:733:binop` 行 74 [binop]: `rf / ppy` -> `rf * ppy`
- `metrics:412:const` 行 82 [const]: `0` -> `1`
- `metrics:412:const` 行 82 [const]: `0` -> `-1`
- `metrics:1115:compare` 行 86 [compare]: `v >= peak` -> `v > peak`
- `metrics:437:delete_stmt` 行 93 [delete_stmt]: `break` -> `pass`
- `metrics:1414:binop` 行 104 [binop]: `i - start` -> `i + start`
- `metrics:1159:const` 行 105 [const]: `False` -> `True`
- `metrics:1183:const` 行 116 [const]: `0` -> `-1`
- `metrics:1307:const` 行 199 [const]: `1` -> `0`
- `optimizer:1264:const` 行 110 [const]: `1` -> `0`
- `optimizer:248:delete_stmt` 行 110 [delete_stmt]: `d = d.slice(task['slice'][0], task['slice'][1])` -> `pass`
- `optimizer:102:delete_stmt` 行 115 [delete_stmt]: `out['fills'] = len(res.fills)` -> `pass`
- `optimizer:561:const` 行 139 [const]: `20` -> `21`
- `optimizer:565:const` 行 142 [const]: `1` -> `2`
- `optimizer:278:negate_cond` 行 143 [negate_cond]: `if self._pending >= flush_every:
    self.flush()` -> `if not self._pending >= flush_every:
    self.flush()`
- `optimizer:283:delete_stmt` 行 151 [delete_stmt]: `self.flush()` -> `pass`
- `optimizer:292:const` 行 156 [const]: `1` -> `0`
- `optimizer:296:const` 行 156 [const]: `20` -> `21`
- `optimizer:306:compare` 行 162 [compare]: `workers <= 1` -> `workers < 1`
- `optimizer:1372:compare` 行 175 [compare]: `k % progress == 0` -> `k % progress != 0`
- `optimizer:1676:binop` 行 175 [binop]: `k % progress` -> `k * progress`
- `optimizer:1678:const` 行 175 [const]: `0` -> `1`
- `optimizer:604:negate_cond` 行 175 [negate_cond]: `if progress and k % progress == 0:
    print(f'[{k}/{len(todo)}] {r['key'][:60]} objective={r['objective']:.4f}', flush=` -> `if not (progress and k % progress == 0):
    print(f'[{k}/{len(todo)}] {r['key'][:60]} objective={r['objective']:.4f}', `
- `optimizer:2059:const` 行 176 [const]: `60` -> `59`
- `optimizer:987:const` 行 178 [const]: `1` -> `2`
- `optimizer:606:delete_stmt` 行 179 [delete_stmt]: `pool.shutdown()` -> `pass`
- `optimizer:1009:const` 行 198 [const]: `1` -> `2`
- `optimizer:1011:compare` 行 198 [compare]: `0 < train_frac < 1` -> `0 <= train_frac < 1`
- `optimizer:1892:const` 行 206 [const]: `1` -> `0`
- `optimizer:351:delete_stmt` 行 209 [delete_stmt]: `train_task['id'] = task['id'] + f':wf{k}:train'` -> `pass`
- `optimizer:1081:const` 行 228 [const]: `5000` -> `5001`
- `optimizer:745:binop` 行 250 [binop]: `time.perf_counter() - t0` -> `time.perf_counter() + t0`
- `optimizer:1180:const` 行 253 [const]: `1` -> `2`
