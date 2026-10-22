# qdcheck 突变测试漏网清单

总数 720，抓住 571，漏网 149，抓住率 79.3%

- `compare:1018:const:1` 行 70 [const]: `0` -> `1`
- `compare:647:compare` 行 70 [compare]: `rel_tol * scale >= abs_tol` -> `rel_tol * scale > abs_tol`
- `compare:654:const:1` 行 74 [const]: `0` -> `1`
- `compare:1037:binop` 行 75 [binop]: `diff / allowed` -> `diff * allowed`
- `compare:462:const:1.0` 行 144 [const]: `0.0` -> `1.0`
- `determinism:1032:const:False` 行 56 [const]: `True` -> `False`
- `determinism:427:binop` 行 91 [binop]: `time.perf_counter() - t0` -> `time.perf_counter() + t0`
- `determinism:428:const:4` 行 91 [const]: `3` -> `4`
- `determinism:795:compare` 行 130 [compare]: `len(text) <= 200` -> `len(text) < 200`
- `determinism:239:boolop` 行 134 [boolop]: `math.isnan(a) or math.isnan(b)` -> `math.isnan(a) and math.isnan(b)`
- `determinism:2193:const:21` 行 163 [const]: `20` -> `21`
- `determinism:269:const:1.5000000000000002e-09` 行 179 [const]: `1e-09` -> `1.5000000000000002e-09`
- `determinism:1306:const:4243` 行 232 [const]: `4242` -> `4243`
- `examples:61:delete_stmt` 行 89 [delete_stmt]: `out['side'] = side` -> `pass`
- `examples:875:const:501` 行 123 [const]: `500` -> `501`
- `examples:241:delete_stmt` 行 130 [delete_stmt]: `raise ValidationError('config must be an object')` -> `pass`
- `examples:483:compare` 行 132 [compare]: `period > 500` -> `period >= 500`
- `examples:899:const:-1` 行 132 [const]: `0` -> `-1`
- `examples:85:delete_stmt` 行 136 [delete_stmt]: `out = {'period': period, 'bars_per_period': bars_per_period, 'symbol': _str(_require(cfg, 'symbol'), 1, 16, 'symbol')}` -> `pass`
- `examples:86:return_none` 行 138 [return_none]: `return _rest(cfg, out)` -> `return None`
- `examples:506:const:False` 行 143 [const]: `True` -> `False`
- `examples:1297:const:2` 行 144 [const]: `1` -> `2`
- `examples:926:binop` 行 144 [binop]: `i + 1` -> `i - 1`
- `examples:946:const:17` 行 155 [const]: `16` -> `17`
- `examples:950:const:2` 行 155 [const]: `1` -> `2`
- `examples:951:const:501` 行 155 [const]: `500` -> `501`
- `examples:1868:const:0.75` 行 170 [const]: `0.5` -> `0.75`
- `examples:568:const:0` 行 178 [const]: `1` -> `0`
- `examples:1764:unary_drop` 行 191 [unary_drop]: `-held[s]` -> `held[s]`
- `examples:1020:delete_stmt` 行 192 [delete_stmt]: `held[s] = 0` -> `pass`
- `examples:1857:const:0` 行 193 [const]: `1` -> `0`
- `examples:323:binop` 行 210 [binop]: `tr['price'] *= 1.0 + eps` -> `tr['price'] /= 1.0 + eps`
- `examples:636:const:49` 行 223 [const]: `50` -> `49`
- `fuzz:171:return_none` 行 273 [return_none]: `return rnd.choice(pool)` -> `return None`
- `fuzz:2366:const:1.5000000000000002e-09` 行 282 [const]: `1e-09` -> `1.5000000000000002e-09`
- `fuzz:5099:const:65` 行 287 [const]: `64` -> `65`
- `fuzz:2385:const:-1` 行 290 [const]: `0` -> `-1`
- `fuzz:2387:const:2` 行 290 [const]: `1` -> `2`
- `fuzz:618:compare` 行 294 [compare]: `rnd.random() < 0.25` -> `rnd.random() <= 0.25`
- `fuzz:2399:const:2` 行 297 [const]: `1` -> `2`
- `fuzz:3767:unary_drop` 行 318 [unary_drop]: `-10 ** 6` -> `10 ** 6`
- `fuzz:194:negate_cond` 行 326 [negate_cond]: `if cat == 'unicode':
    return rnd.choice(UNICODE_SAMPLES)` -> `if not cat == 'unicode':
    return rnd.choice(UNICODE_SAMPLES)`
- `fuzz:1419:compare` 行 341 [compare]: `cat == 'type_wrong'` -> `cat != 'type_wrong'`
- `fuzz:3852:const:2` 行 361 [const]: `1` -> `2`
- `fuzz:2567:const:2` 行 377 [const]: `1` -> `2`
- `fuzz:2571:const:1` 行 377 [const]: `2` -> `1`
- `fuzz:2621:compare` 行 405 [compare]: `len(cur) == 1` -> `len(cur) != 1`
- `fuzz:2647:compare` 行 417 [compare]: `v == 0` -> `v != 0`
- `fuzz:2666:const:65` 行 424 [const]: `64` -> `65`
- `fuzz:5577:const:65` 行 646 [const]: `64` -> `65`
- `fuzz:4407:const:2` 行 676 [const]: `1` -> `2`
- `fuzz:7250:binop` 行 684 [binop]: `i + 1` -> `i - 1`
- `fuzz:4538:const:1001` 行 752 [const]: `1000` -> `1001`
- `fuzz:4547:const:1001` 行 754 [const]: `1000` -> `1001`
- `fuzz:1942:const:2` 行 786 [const]: `1` -> `2`
- `fuzz:4638:const:0` 行 787 [const]: `1` -> `0`
- `fuzz:994:negate_cond` 行 800 [negate_cond]: `if depth > 40:
    return True` -> `if not depth > 40:
    return True`
- `fuzz:4681:const:0` 行 811 [const]: `1` -> `0`
- `fuzz:4683:const:False` 行 811 [const]: `True` -> `False`
- `fuzz:3270:const:20001` 行 827 [const]: `20000` -> `20001`
- `gen_vectors:196:compare` 行 43 [compare]: `step > 0` -> `step >= 0`
- `gen_vectors:2487:const:2` 行 62 [const]: `1` -> `2`
- `gen_vectors:2822:const:2` 行 67 [const]: `1` -> `2`
- `gen_vectors:2878:const:2` 行 150 [const]: `1` -> `2`
- `gen_vectors:3070:const:1` 行 176 [const]: `2` -> `1`
- `gen_vectors:2632:const:0` 行 205 [const]: `1` -> `0`
- `gen_vectors:710:const:1` 行 249 [const]: `2` -> `1`
- `gen_vectors:718:const:-1` 行 255 [const]: `0` -> `-1`
- `gen_vectors:359:delete_stmt` 行 264 [delete_stmt]: `out['bars'] += len(vec['closes'])` -> `pass`
- `gen_vectors:369:compare` 行 287 [compare]: `abs(a - b) <= rel_tol * max(abs(a), abs(b), 1e-300)` -> `abs(a - b) < rel_tol * max(abs(a), abs(b), 1e-300)`
- `gen_vectors:2418:const:False` 行 301 [const]: `True` -> `False`
- `gen_vectors:1280:const:21` 行 304 [const]: `20` -> `21`
- `gen_vectors:388:delete_stmt` 行 305 [delete_stmt]: `print(line)` -> `pass`
- `gen_vectors:2448:const:2` 行 312 [const]: `1` -> `2`
- `indicators_alt:1032:const:1.0` 行 101 [const]: `0.0` -> `1.0`
- `indicators_alt:1065:binop` 行 112 [binop]: `ag + al` -> `ag - al`
- `indicators_ref:725:const:4` 行 51 [const]: `3` -> `4`
- `indicators_ref:79:return_none` 行 127 [return_none]: `return n` -> `return None`
- `indicators_ref:423:const:1.5` 行 177 [const]: `1.0` -> `1.5`
- `indicators_ref:3364:const:2` 行 226 [const]: `1` -> `2`
- `indicators_ref:678:delete_stmt` 行 395 [delete_stmt]: `raise ValueError("warmup must be 'none' or 'nan'")` -> `pass`
- `ledger:190:boolop` 行 73 [boolop]: `not isinstance(bar, int) or isinstance(bar, bool) or bar < 0` -> `not isinstance(bar, int) and isinstance(bar, bool) and (bar < 0)`
- `ledger:1109:compare` 行 167 [compare]: `p > self.rel_tol * self.avg` -> `p >= self.rel_tol * self.avg`
- `ledger:1138:binop` 行 184 [binop]: `closing * (p - self.avg) * _sign(Q)` -> `closing * (p - self.avg) / _sign(Q)`
- `ledger:3509:const:-1` 行 197 [const]: `0` -> `-1`
- `ledger:1965:binop` 行 253 [binop]: `self.cash + self.qty * p` -> `self.cash - self.qty * p`
- `ledger:2726:binop` 行 289 [binop]: `abs(q) * avg` -> `abs(q) / avg`
- `ledger_alt:207:const:2` 行 15 [const]: `1` -> `2`
- `ledger_alt:72:const:-1` 行 15 [const]: `0` -> `-1`
- `ledger_alt:73:const:0` 行 15 [const]: `1` -> `0`
- `ledger_alt:75:const:-1` 行 15 [const]: `0` -> `-1`
- `ledger_alt:505:const:1` 行 47 [const]: `0` -> `1`
- `ledger_alt:1233:const:1.5` 行 52 [const]: `1.0` -> `1.5`
- `ledger_alt:247:compare` 行 52 [compare]: `abs(x) <= 1e-12 * max(1.0, abs(ref))` -> `abs(x) < 1e-12 * max(1.0, abs(ref))`
- `ledger_alt:943:const:-1` 行 95 [const]: `0` -> `-1`
- `ledger_alt:597:binop` 行 99 [binop]: `q - self.size` -> `q + self.size`
- `ledger_alt:997:const:1` 行 112 [const]: `0` -> `1`
- `ledger_alt:684:compare` 行 142 [compare]: `self.cash.value() < -self.cfg['rel_tol'] * scale` -> `self.cash.value() <= -self.cfg['rel_tol'] * scale`
- `props:506:binop` 行 30 [binop]: `1.0 + 300.0 * rnd()` -> `1.0 - 300.0 * rnd()`
- `props:816:const:1.5` 行 30 [const]: `1.0` -> `1.5`
- `props:114:delete_stmt` 行 32 [delete_stmt]: `d['high'][i] = max(o, c) * (1.0 + 0.05 * rnd())` -> `pass`
- `props:836:binop` 行 32 [binop]: `0.05 * rnd()` -> `0.05 / rnd()`
- `props:844:const:1.5` 行 33 [const]: `1.0` -> `1.5`
- `props:1325:binop` 行 43 [binop]: `seed * 1000` -> `seed / 1000`
- `props:159:delete_stmt` 行 72 [delete_stmt]: `worst = max(worst, res['max_tol_ratio'])` -> `pass`
- `stream:133:const:0.22499999999999998` 行 26 [const]: `0.15` -> `0.22499999999999998`
- `stream:2516:const:4` 行 35 [const]: `3` -> `4`
- `stream:1644:const:0.015` 行 37 [const]: `0.01` -> `0.015`
- `stream:1665:const:0.003` 行 41 [const]: `0.002` -> `0.003`
- `stream:1000:const:2` 行 60 [const]: `1` -> `2`
- `stream:1018:compare` 行 72 [compare]: `order.side < 0` -> `order.side <= 0`
- `stream:1746:const:1` 行 72 [const]: `0` -> `1`
- `stream:486:compare` 行 84 [compare]: `mode == 'isolated'` -> `mode != 'isolated'`
- `stream:3728:binop` 行 85 [binop]: `q - inc` -> `q + inc`
- `stream:1790:const:4.5` 行 89 [const]: `3.0` -> `4.5`
- `stream:3742:const:-1` 行 90 [const]: `0` -> `-1`
- `stream:1897:const:1` 行 125 [const]: `0` -> `1`
- `stream:1138:compare` 行 132 [compare]: `Q != 0` -> `Q == 0`
- `stream:1910:const:1` 行 132 [const]: `0` -> `1`
- `stream:1935:binop` 行 138 [binop]: `2.0 * self.rnd()` -> `2.0 / self.rnd()`
- `stream:579:negate_cond` 行 158 [negate_cond]: `if rnd() < sim['p_cancel']:
    self.orders.remove(order)
    self.stats['cancelled_user'] += 1` -> `if not rnd() < sim['p_cancel']:
    self.orders.remove(order)
    self.stats['cancelled_user'] += 1`
- `stream:580:boolop` 行 161 [boolop]: `rnd() >= sim['p_order'] or self.bars['volume'][t] == 0` -> `rnd() >= sim['p_order'] and self.bars['volume'][t] == 0`
- `stream:222:negate_cond` 行 167 [negate_cond]: `if Q == 0 or u < 0.45:
    side = (1 if rnd() < 0.5 else -1) if Q == 0 else 1 if Q > 0 else -1
    qty = size
elif u < 0` -> `if not (Q == 0 or u < 0.45):
    side = (1 if rnd() < 0.5 else -1) if Q == 0 else 1 if Q > 0 else -1
    qty = size
elif`
- `stream:2896:binop` 行 182 [binop]: `t + 1` -> `t - 1`
- `stream:3448:const:0` 行 182 [const]: `1` -> `0`
- `stream:3448:const:2` 行 182 [const]: `1` -> `2`
- `stream:2956:const:1.0` 行 216 [const]: `0.0` -> `1.0`
- `stream:685:const:0.0075` 行 238 [const]: `0.005` -> `0.0075`
- `stream:2159:const:151.5` 行 243 [const]: `101.0` -> `151.5`
- `stream:3516:const:9` 行 244 [const]: `8` -> `9`
- `stream:3532:unary_drop` 行 248 [unary_drop]: `-1.25` -> `1.25`
- `stream:2186:const:3` 行 250 [const]: `4` -> `3`
- `stream:3543:const:16` 行 250 [const]: `15` -> `16`
- `stream:2195:const:148.5` 行 252 [const]: `99.0` -> `148.5`
- `stream:2201:const:145.5` 行 252 [const]: `97.0` -> `145.5`
- `stream:2244:const:1.4249999999999998` 行 262 [const]: `0.95` -> `1.4249999999999998`
- `stream:2266:const:150000.0` 行 267 [const]: `100000.0` -> `150000.0`
- `stream:830:delete_stmt` 行 332 [delete_stmt]: `agg_counters[k] = agg_counters.get(k, 0) + v` -> `pass`
- `stream:1590:const:3001` 行 356 [const]: `3000` -> `3001`
- `stream:3235:const:0` 行 367 [const]: `1` -> `0`
- `synth:385:const:3` 行 39 [const]: `4` -> `3`
- `synth:385:const:5` 行 39 [const]: `4` -> `5`
- `synth:734:const:2` 行 56 [const]: `1` -> `2`
- `synth:501:binop` 行 120 [binop]: `sign * tick` -> `sign / tick`
- `synth:943:const:2` 行 146 [const]: `1` -> `2`
- `synth:298:compare` 行 155 [compare]: `u_gap < spec['gap_prob']` -> `u_gap <= spec['gap_prob']`
- `synth:310:const:-1` 行 162 [const]: `0` -> `-1`
- `synth:1660:binop` 行 187 [binop]: `i + 1` -> `i - 1`
- `synth:1118:const:-1` 行 215 [const]: `0` -> `-1`
