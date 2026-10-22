# qdcheck 突变测试漏网清单

总数 720，抓住 701，漏网 19，抓住率 97.4%

- `fuzz:719:compare` 行 375 [compare]: `rnd.random() < 0.05` -> `rnd.random() <= 0.05`
- `fuzz:2884:const:False` 行 617 [const]: `True` -> `False`
- `fuzz:4422:const:True` 行 697 [const]: `False` -> `True`
- `gen_vectors:196:compare` 行 43 [compare]: `step > 0` -> `step >= 0`
- `indicators_alt:1032:const:1.0` 行 101 [const]: `0.0` -> `1.0`
- `indicators_ref:2509:const:0` 行 327 [const]: `1` -> `0`
- `ledger:1109:compare` 行 167 [compare]: `p > self.rel_tol * self.avg` -> `p >= self.rel_tol * self.avg`
- `ledger:1138:binop` 行 184 [binop]: `closing * (p - self.avg) * _sign(Q)` -> `closing * (p - self.avg) / _sign(Q)`
- `ledger:3509:const:-1` 行 197 [const]: `0` -> `-1`
- `ledger_alt:72:const:-1` 行 15 [const]: `0` -> `-1`
- `ledger_alt:75:const:-1` 行 15 [const]: `0` -> `-1`
- `ledger_alt:505:const:1` 行 47 [const]: `0` -> `1`
- `ledger_alt:247:compare` 行 52 [compare]: `abs(x) <= 1e-12 * max(1.0, abs(ref))` -> `abs(x) < 1e-12 * max(1.0, abs(ref))`
- `ledger_alt:943:const:-1` 行 95 [const]: `0` -> `-1`
- `ledger_alt:597:binop` 行 99 [binop]: `q - self.size` -> `q + self.size`
- `ledger_alt:997:const:1` 行 112 [const]: `0` -> `1`
- `ledger_alt:684:compare` 行 142 [compare]: `self.cash.value() < -self.cfg['rel_tol'] * scale` -> `self.cash.value() <= -self.cfg['rel_tol'] * scale`
- `props:1305:const:-1` 行 158 [const]: `0` -> `-1`
- `stream:3295:compare` 行 90 [compare]: `side > 0` -> `side >= 0`
