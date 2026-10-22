# 突变测试漏网清单（survivors）

总数 640，抓住 608，漏网 32，抓住率 95.0%

- `cli:798:const:0` 行 50 [const]: `1` -> `0`
- `cli:798:const:2` 行 50 [const]: `1` -> `2`
- `cli:1038:const:0` 行 80 [const]: `1` -> `0`
- `cli:1038:const:2` 行 80 [const]: `1` -> `2`
- `cli:881:const:True` 行 101 [const]: `False` -> `True`
- `engine:1503:const:-1` 行 281 [const]: `0` -> `-1`
- `indicators:1138:const:0` 行 55 [const]: `1` -> `0`
- `indicators:329:delete_stmt` 行 567 [delete_stmt]: `_check_window(n)` -> `pass`
- `metrics:1595:binop` 行 65 [binop]: `x - m` -> `x + m`
- `metrics:1597:binop` 行 65 [binop]: `x - m` -> `x + m`
- `metrics:1357:const:True` 行 114 [const]: `False` -> `True`
- `metrics:947:delete_stmt` 行 114 [delete_stmt]: `under = False` -> `pass`
- `metrics:594:const:-1` 行 150 [const]: `0` -> `-1`
- `metrics:1433:const:-1` 行 159 [const]: `0` -> `-1`
- `metrics:1444:const:146095` 行 161 [const]: `146096` -> `146095`
- `metrics:1180:compare` 行 226 [compare]: `len(rets) > 1` -> `len(rets) >= 1`
- `optimizer:1320:const:1` 行 81 [const]: `0` -> `1`
- `optimizer:869:const:61` 行 81 [const]: `60` -> `61`
- `optimizer:96:delete_stmt` 行 109 [delete_stmt]: `load_data(data_spec)` -> `pass`
- `optimizer:285:delete_stmt` 行 140 [delete_stmt]: `self.conn.commit()` -> `pass`
- `optimizer:307:const:2` 行 164 [const]: `1` -> `2`
- `optimizer:120:delete_stmt` 行 166 [delete_stmt]: `combos = dedupe(combos)` -> `pass`
- `optimizer:321:compare` 行 170 [compare]: `workers <= 1` -> `workers < 1`
- `optimizer:2178:const:59` 行 184 [const]: `60` -> `59`
- `optimizer:372:delete_stmt` 行 223 [delete_stmt]: `_worker_init(task['data_spec'])` -> `pass`
- `optimizer:1144:const:4999` 行 239 [const]: `5000` -> `4999`
- `optimizer:1148:const:0` 行 240 [const]: `1` -> `0`
- `optimizer:1152:const:2` 行 241 [const]: `1` -> `2`
- `optimizer:1620:const:49` 行 259 [const]: `50` -> `49`
- `report:1465:const:True` 行 47 [const]: `False` -> `True`
- `report:501:const:1.5` 行 93 [const]: `1.0` -> `1.5`
- `report:1206:const:True` 行 134 [const]: `False` -> `True`
