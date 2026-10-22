# qdcheck 突变测试漏网清单

总数 780，抓住 757，漏网 23，抓住率 97.1%

- `examples:128:delete_stmt` 行 220 [delete_stmt]: `random.seed(f'{os.getpid()}:{time.perf_counter_ns()}')` -> `pass`
- `examples:130:delete_stmt` 行 222 [delete_stmt]: `random.shuffle(scratch)` -> `pass`
- `fuzz:3216:const:5` 行 757 [const]: `6` -> `5`
- `fuzz:2093:const:1` 行 868 [const]: `0` -> `1`
- `gen_vectors:194:compare` 行 42 [compare]: `step > 0` -> `step >= 0`
- `indicators_alt:1030:const:1.0` 行 100 [const]: `0.0` -> `1.0`
- `indicators_ref:2507:const:0` 行 326 [const]: `1` -> `0`
- `ledger:547:compare` 行 119 [compare]: `x > 0` -> `x >= 0`
- `ledger:1164:delete_stmt` 行 187 [delete_stmt]: `closing = abs(Q)` -> `pass`
- `ledger:1167:binop` 行 188 [binop]: `closing * (p - self.avg) * _sign(Q)` -> `closing * (p - self.avg) / _sign(Q)`
- `ledger:3547:const:-1` 行 201 [const]: `0` -> `-1`
- `ledger:770:const:2` 行 306 [const]: `1` -> `2`
- `ledger_alt:70:const:-1` 行 14 [const]: `0` -> `-1`
- `ledger_alt:73:const:-1` 行 14 [const]: `0` -> `-1`
- `ledger_alt:503:const:1` 行 46 [const]: `0` -> `1`
- `ledger_alt:245:compare` 行 51 [compare]: `abs(x) <= 1e-12 * max(1.0, abs(ref))` -> `abs(x) < 1e-12 * max(1.0, abs(ref))`
- `ledger_alt:941:const:-1` 行 94 [const]: `0` -> `-1`
- `ledger_alt:595:binop` 行 98 [binop]: `q - self.size` -> `q + self.size`
- `ledger_alt:995:const:1` 行 111 [const]: `0` -> `1`
- `ledger_alt:682:compare` 行 141 [compare]: `self.cash.value() < -self.cfg['rel_tol'] * scale` -> `self.cash.value() <= -self.cfg['rel_tol'] * scale`
- `props:1303:const:-1` 行 157 [const]: `0` -> `-1`
- `stream:3293:compare` 行 89 [compare]: `side > 0` -> `side >= 0`
- `vector_schema:412:const:0` 行 142 [const]: `1` -> `0`
