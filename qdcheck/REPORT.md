# qdcheck — 量化回测系统验证工具包（REPORT）

纯 Python 3.12 标准库，零第三方依赖，零网络。给别的量化回测系统当判据用：指标参考实现 + 测试向量 + 对拍器（G1）、
入参模糊器（G2）、会计恒等式检查器 + 参考账本 + 账本向量（G3）、跨进程 / 跨哈希种子一致性 harness（G4）。
**所有数据都是合成的，不构成任何交易结论。**

> 本文件由 `python3.12 qdcheck/tools/build_report.py` 从 `qdcheck/tools/report_template.md` 与 `qdcheck/results/*.json`
> 生成，表中每个数字都取自入库的结果文件（`qdcheck/tests/test_report.py` 检查本文件与结果文件一致）。
> 结果文件生成机器：Linux-6.18.44-fc-v140-x86_64-with-glibc2.39，Python 3.12.3，4 核。

## 0. 复现命令

```bash
python3.12 -m unittest discover -s qdcheck/tests            # 全部测试（unittest；不用 pytest）
python3.12 -m qdcheck.gen_vectors --check                  # 在本机重新生成全部向量，与入库向量逐点比较（1e-9）
python3.12 -m qdcheck.tools.selfcheck g1                   # -> results/g1_selfcheck.json（因果性 / 长度 / 预热期 / 闭式解 / 对拍）
python3.12 -m qdcheck.tools.qbt_indicators_vs_vectors --json qdcheck/results/g1_qbt_indicators.json
python3.12 -m qdcheck.tools.selfcheck g2                   # -> results/g2_selfcheck.json（模糊器自检 + qbt GenSpec）
python3.12 -m qdcheck.stream --streams 200 --bars 3000 --out qdcheck/results/g3_streams.json
python3.12 -m qdcheck.tools.qbt_ledger_check --n 20000 --json qdcheck/results/g3_qbt_ledger.json
python3.12 -m qdcheck.tools.selfcheck g4                   # -> results/g4_selfcheck.json（假引擎 + qbt 跨进程）
python3.12 qdcheck/tools/cov.py --fail-under 90 --json qdcheck/results/coverage.json
python3.12 qdcheck/tools/mutate.py --per-module 60 --jobs 4                    # -> results/mutation.json
python3.12 qdcheck/tools/build_report.py                   # 由结果文件重新生成本报告
```

## 1. 目标达成表

| 目标 | 要求 | 实测 | 达成 | 复现命令 |
|---|---|---|---|---|
| G1 | 9 个指标朴素实现（每窗口从头算、不递推、不优化） | `indicators_ref.py`：DEMA / TEMA / TRIX / Stoch RSI / CCI / AO / Force / KDJ / Ichimoku，每个输出点从输入前缀重新计算（嵌套平滑每层 O(n²)） | ✅ | 见源码 |
| G1 | 常见口径做成参数，每个变体一个 variant 字符串并列全 | 61 个 variant（105 条“变体×输出线”），全部列在第 3 节；预热期 None / NaN 由 `warmup=` 参数控制 | ✅ | `python3.12 -c "from qdcheck import indicators_ref as R; print(R.VARIANTS)"` |
| G1 | ≥ 6 组合成 K 线 × 每指标 × 每变体的 JSON 向量，17 位有效数字，预热期 null | 10 组序列（常数 / 单调升 / 单调降 / 随机游走 / 趋势 / 带跳跃 / 带长横盘+停牌 / n=1 / n=2 / n=5），1,260 条向量、90,303 个有值点；`%.17g` 写出 | ✅ | `python3.12 -m qdcheck.gen_vectors` |
| G1 | 因果性（篡改 i ≥ cut，[0,cut) 不变） | 2,884 项，失败 0（容差 0；前移口径的迟行线改用位移判据 8 项，失败 0） | ✅ | `python3.12 -m qdcheck.tools.selfcheck g1` |
| G1 | 长度保持 | 61 个变体 × n = 0..60，失败 0 | ✅ | 同上 |
| G1 | 常数序列闭式解 | 630 项（3 个常数 × 每变体每线 × ref/alt 两份实现），失败 0；另有预热期位置公式 252 项，失败 0 | ✅ | 同上 |
| G1 | 与第二份独立写法逐点对拍（1e-9） | `indicators_alt.py`（EMA 闭式加权和、fsum、单调队列、前缀和）：向量序列 820 组 + 额外合成序列 656 组，失败 0 / 0；最坏误差占容差 21.4% | ✅ | 同上 |
| G1 | compare.py：相对容差 1e-9、预热期位置一致、长度一致，打印第一处不一致 | `compare.compare_series / compare_file / main`；对本仓 qbt 指标的真实用例见第 4 节 | ✅ | `python3.12 -m qdcheck.compare <向量> <实际输出>` |
| G2 | schema 驱动、固定 seed 的乱喂序列（类型乱放 / NaN / inf / -0.0 / 超大 / 负数 / 空串 / 缺键 / 多键 / 大小写 / 嵌套乱 / 1e6 长串 / Unicode） | 15 类乱喂，每个用例独立 seed（可单独重放，与 PYTHONHASHSEED 无关，测试里跨进程比对） | ✅ | `python3.12 -m qdcheck.fuzz qdcheck.examples:validate_good --schema qdcheck.examples:BACKTEST_SCHEMA` |
| G2 | 2 万次里 NaN / inf / 缺键 / 类型错各 ≥ 500 | NaN 1830、inf 1785、缺键 1845、类型错 1798 | ✅ | `python3.12 -m qdcheck.tools.selfcheck g2` |
| G2 | 三个故意写坏的 validate 必须抓到 | KeyError ×510、ZeroDivisionError ×103、RecursionError（超长字符串）×430 | ✅ | 同上 |
| G2 | 最小复现 ≤ 原输入 30% | KeyError 2.3%（最小复现 `{}`）、ZeroDivisionError 8.4%（`{'period': 0}`）、RecursionError 0.11%（1,106 / 1,000,098 字符） | ✅ | 同上 |
| G2 | 2 万次 × 普通 validate ≤ 20 s（单核） | `validate_good` 2.00 s（含生成与参考判定），裸异常 0，与参考判定 `schema_errors` 逐条一致（误收 0、误拒 0） | ✅ | 同上 |
| G3 | synth.py：同 seed 同结果，停牌 / 缺口 ≥ 3% / 单根 ±8% / 长横盘 / 趋势 | 只用 `random()` 与四则运算（无 libm），逐位可复现且前缀稳定；特征由 `test_synth.py` 逐项验证 | ✅ | `python3.12 -m qdcheck.synth --n 1000 --seed 1` |
| G3 | ledger.py：逐根现金 / 持仓 / 均价 / 浮盈 / 已实现 / 手续费 / 资金费 / 权益 + 恒等式；多空 / 反手 / 部分平仓 / 加仓 / 逐仓 1–20 倍 / 强平 | 见第 2.4 节；随机流覆盖：开仓 10,428、加仓 35,386、部分平仓 19,721、全平 10,294、反手 11,273、强平 812（其中穿仓封顶 225）、跨日结算 2,520、资金费 4,406 | ✅ | `python3.12 -m qdcheck.stream --streams 200 --bars 3000` |
| G3 | stream.py：≥ 200 条 × ≥ 3000 根，≤ 3 分钟 | 200 条 × 3000 根 = 600,000 根、94,028 条记录，单进程 21.8 s（含两份账本重算与逐根对拍）；下单 86,413、部分成交 23,966、撤单 4,301、到期 4,625、保证金拒单 14,674 | ✅ | 同上 |
| G3 | 全部随机流每根恒等式零违背；第二份独立写法逐根对拍（浮点 1e-9、数量 / 根序号整数完全相等） | 恒等式违背 0；`ledger_alt` 独立判定问题 0；逐根逐字段不一致 0 条流（共比较 10,800,000 个值） | ✅ | 同上 |
| G3 | 导出 `vectors/ledger_*.json`（成交流 + 收盘价 + 每根期望表） | 7 个文件、1,227 根，每根 18 列期望值；重新生成不一致 0 | ✅ | `python3.12 -m qdcheck.gen_vectors --check` |
| G4 | 本进程 + PYTHONHASHSEED=0 / 4242 两个子进程，结构化比较、打印第一处路径，不用 sha256 / 逐字节 | `determinism.check`；哈希种子生效用 hash 探针确认 | ✅ | `python3.12 -m qdcheck.determinism qdcheck.examples:set_order_engine --task '{"n_bars": 60}'` |
| G4 | 依赖 set 迭代顺序的假引擎必须抓到；确定性假引擎必须通过 | set_order_engine：0 vs 4242 第一处差异 `final_cash`（232.2603971576893 vs 429.18570405488833）；deterministic_engine 三对一致（691 个叶子）；噪声 1e-12 通过 / 1e-6 抓到 | ✅ | `python3.12 -m qdcheck.tools.selfcheck g4` |
| G4 | 对 qbt 自己的回测引擎跑一遍 | 12/12 个回测（6 策略 × 2 种 BrokerConfig × 20000 根）三份结果完全一致，比较 212,340 个叶子 | ✅ | 同上 |
| G5 | unittest 全绿 | 164 个测试，失败 0，跳过 4（覆盖率监控下跳过 2 个计时判据和 2 个“报告与结果文件一致”判据——后者依赖覆盖率结果本身；它们在普通运行里都执行） | ✅ | `python3.12 -m unittest discover -s qdcheck/tests` |
| G5 | 语句覆盖率 ≥ 90%（复用 tools/cov.py 的做法） | **100.00%**（2679/2679 条语句，见第 5 节） | ✅ | `python3.12 qdcheck/tools/cov.py --fail-under 90` |
| G5 | 突变抓住率 ≥ 85%，每模块 ≥ 40 个突变体，活下来的逐条判定 | **97.4%**（701/720，每模块 60 个；漏网 19 条逐条判定见第 6 节） | ✅ | `python3.12 qdcheck/tools/mutate.py --per-module 60 --jobs 4` |
| G5 | 分阶段提交，每笔写清做了什么、哪些数字变了，全部推到 `main` | G1 `79e88df` → G2 `0bc2f4c` → G3 `861202e` → G4 `bcbdf64` → G5 中间提交 `90ecf9f`（第一轮突变 + 补判据）→ G5 收尾（本报告所在提交） | ✅ | `git log --oneline -- qdcheck` |
| G5 | 跨平台：判据只用容差和形状；生成脚本 + seed 入库，别的机器重新生成在 1e-9 内对上 | 向量、合成数据、随机流只用 `random()` 与四则运算（不依赖 libm）；`gen_vectors --check` 本机重新生成：1,260 条指标向量 + 7 个账本向量，不一致 0，最大相对误差 0.0 | ✅ | `python3.12 -m qdcheck.gen_vectors --check` |

## 2. 接口说明（别的系统怎么调）

### 2.1 指标向量 + compare

向量文件 `qdcheck/vectors/<指标>.json` = `{"format", "indicator", "seed", "variants", "lines", "count", "vectors": [...]}`，
每条向量：

```json
{"id": "dema|ma=ema;seed=sma|value|randwalk", "indicator": "dema", "variant": "ma=ema;seed=sma", "line": "value",
 "series": "randwalk", "params": {"period": 5}, "input": {"open": [...], "high": [...], "low": [...], "close": [...], "volume": [...]},
 "output": [null, null, ..., <17 位有效数字的浮点数>, ...]}
```

多输出线的指标（Stoch RSI 的 k/d、KDJ 的 k/d/j、Ichimoku 五线）每条线一条向量（`line` 字段区分），`output` 始终是一维数组；
DEMA / TEMA / TRIX 另有一组周期（id 末尾 `|period=14` / `|period=15`）。

```python
from qdcheck import compare
vecs = compare.load_vectors("qdcheck/vectors/kdj.json")
actual = {v["id"]: my_kdj(v["input"], **v["params"])[v["line"]]                  # 你的实现，按 variant 选口径
          for v in vecs if v["variant"] == "init=50;ma=rma;zero_range=zero"}
rep = compare.compare_file(vecs, actual)        # {"checked", "passed", "failed", "failures": [第一处不一致...], "missing", ...}
res = compare.compare_series(expected, actual)  # {"ok", "reason": length|warmup|type|value, "index", "expected", "actual", ...}
```

命令行：`python3.12 -m qdcheck.compare qdcheck/vectors/kdj.json mine.json [--id ID] [--rel 1e-9] [--abs 1e-12] [--warmup either|none|nan] [--strict]`，
`mine.json` 为 `{vector_id: [...]}` 或裸数组；退出码 0 一致 / 1 不一致 / 2 用法错误。判定规则：长度相等；无值位置一致
（期望 null 处实际必须是 None/NaN，反之亦然）；有值处 `|a-e| <= max(1e-9*max(|a|,|e|), 1e-12)`（abs 1e-12 只为数学上恰为 0
的点兜底）；inf / 非数值直接判不一致。

### 2.2 props：拿来测你自己的指标实现

```python
from qdcheck import props
fn = lambda ohlcv: {"value": my_dema(ohlcv["close"], 5)}
props.check_causality(fn, ohlcv, cuts=[10, 50, 100])       # 篡改 i >= cut 的输入，[0,cut) 必须不变
props.check_length(fn, make_input, [0, 1, 2, 5, 60])
props.expected_warmup("dema", "ma=ema;seed=sma")           # {"value": 8}：预热期位置公式
props.constant_expectation("cci", "src=hlc3;zero_dev=zero", None, 80, 100.0)   # 常数输入的闭式解
```

### 2.3 fuzz：validate 不许裸异常

```python
from qdcheck import fuzz
rep = fuzz.run(my_validate, schema, n=20000, seed=0, controlled=MyValidationError)   # controlled 可为类 / 元组 / 判定函数
rep["outcomes"]            # {"pass", "controlled", "bare"}
rep["bare"][0]             # {"exc_type", "count", "first_case", "top3": 栈顶三行, "minimal_input": 编码后的最小复现, "ratio", ...}
fuzz.decode(rep["bare"][0]["minimal_input"])    # 精确还原最小复现（NaN / -0.0 / 1e6 长串 / 深链都能还原）
fuzz.make_case(schema, seed=0, index=123)       # 单独重放第 123 个用例
fuzz.schema_errors(schema, obj)                 # 参考判定；报告里的 accepted_invalid / rejected_valid 与它对照
```

schema 节点：`{"type": int|float|bool|str|enum|list|object, "min", "max", "min_len", "max_len", "charset": "ascii_printable",
"choices", "items", "fields", "required"(缺省 True), "extra": "forbid"(缺省)|"allow"}`。裸异常按（类型, 最内帧, 消息形状）归组；
缩小 = 逐键删 + 值简化 + 字符串长度 / 嵌套深度二分，每次尝试在新线程里跑（栈深一致），RecursionError 留 max(64, 10%) 余量。
命令行：`python3.12 -m qdcheck.fuzz 模块:函数 --schema schema.json|模块:变量 --controlled 模块:类 --n 20000 --out report.json`。

### 2.4 ledger：每根权益守恒

成交流记录：`{"bar", "kind": trade|funding|settlement|liquidation, "side": buy|sell|none, "qty", "price", "fee", "funding"}`，
按 bar 非降序；同一根内先处理全部记录再以收盘价盯市。

```python
from qdcheck import ledger
res = ledger.run(records, closes, initial_cash=1e5, margin_mode="isolated", leverage=20, mmr=0.005)
res["rows"]          # 每根：cash qty avg_price unrealized realized fees funding insurance equity market_value
                     #       margin_used maint_margin position_equity liq_price fills liquidations
res["violations"]    # 恒等式 / 强平规则违背（空 = 通过）
ledger.check_engine(records, closes, engine_equity, **cfg)   # 你的引擎自报权益 vs 参考账本，逐根，给出第一处不一致
ledger.check_vector(vec, {"equity": my_equity, "cash": my_cash})   # 用 vectors/ledger_*.json 测你的账本实现
```

口径：平均成本法；现金法记账（现金 -= side·qty·price + fee + funding，持仓盯市 = qty·close）；settlement 把浮盈转入已实现、
均价重置为结算价；逐仓保证金 = 名义额/杠杆，减仓按比例释放、结算时按结算价重算；有效保证金 = min(逐仓保证金, 钱包)；
强平 ⇔ 仓位权益 ≤ 维持保证金（mmr·|qty|·价格）；逐仓强平亏损封顶在有效保证金（穿仓由保险基金补足，计入已实现与现金），
所以强平后现金 ≥ 0。全仓（cross）下仓位权益 = 账户权益，不封顶，强平后为负记为 warning（bankrupt）。
逐根检查：I1 权益 == 初始 + 已实现 + 浮动 − 手续费 − 资金费；I2 权益 == 现金 + 持仓盯市（“权益”取第三条独立路径：
逐根累加 持仓·价格变化 + 当根成交按收盘价的盈亏 − 费用 + 保险补偿）；I3 平仓后均价 / 浮动 / 保证金为 0 且现金 == 权益；
I4 保证金一致性与漏强平；I5 强平合法性 / 必须全平 / 强平后余额。

### 2.5 stream：随机订单流

`stream.generate(seed, n_bars)` → `{"config", "closes", "records", "stats", ...}`；`stream.check_stream(st)` 用全新的 `ledger.run`
与独立的 `ledger_alt.run` 各算一遍并逐根逐字段比较；`stream.run_many(200, 3000)` 汇总。

### 2.6 determinism：换进程 / 换哈希种子结果不变

```python
from qdcheck import determinism
rep = determinism.check({"strategy": "ma_cross", "n": 20000}, "mypkg.engine:run_backtest")   # 任务须可 JSON 序列化
rep["ok"]; rep["hash_seeds_effective"]; rep["pairs"][k]["first_diff"]   # {"path": "trades[17].price", "reason", "a", "b"}
```

命令行：`python3.12 -m qdcheck.determinism 模块:函数 --task '{...}'|@task.json [--seeds 0 4242] [--rel 1e-9] [--dict-order]`。
被测函数的返回值必须能 `json.dumps`（set / bytes / 自定义对象直接报错，不替你排序）。

### 2.7 synth：合成 K 线

`synth.generate(n, seed, segments=[(start, len, kind)], events={i: kind}, **参数)` → 五列 + `segments` + `events`；
kind ∈ random / trend_up / trend_down / flat / halt，事件 ∈ gap_up / gap_down（≥ 3%）/ jump_up / jump_down（±8%）。

## 3. 变体清单

| 指标 | 输出线 | variant（全部） | 默认参数 | 预热期（前导无值个数，非退化数据） |
|---|---|---|---|---|
| dema | value | `ma=ema;seed=sma` | {"period": 5} | value=8 |
|  |  | `ma=ema;seed=first` |  | value=0 |
|  |  | `ma=ema;seed=adjusted` |  | value=0 |
|  |  | `ma=rma;seed=sma` |  | value=8 |
|  |  | `ma=rma;seed=first` |  | value=0 |
|  |  | `ma=rma;seed=adjusted` |  | value=0 |
|  |  | `ma=sma` |  | value=8 |
| tema | value | `ma=ema;seed=sma` | {"period": 5} | value=12 |
|  |  | `ma=ema;seed=first` |  | value=0 |
|  |  | `ma=ema;seed=adjusted` |  | value=0 |
|  |  | `ma=rma;seed=sma` |  | value=12 |
|  |  | `ma=rma;seed=first` |  | value=0 |
|  |  | `ma=rma;seed=adjusted` |  | value=0 |
|  |  | `ma=sma` |  | value=12 |
| trix | value | `ma=ema;seed=sma` | {"period": 5, "scale": 100.0} | value=13 |
|  |  | `ma=ema;seed=first` |  | value=1 |
|  |  | `ma=ema;seed=adjusted` |  | value=1 |
|  |  | `ma=rma;seed=sma` |  | value=13 |
|  |  | `ma=rma;seed=first` |  | value=1 |
|  |  | `ma=rma;seed=adjusted` |  | value=1 |
|  |  | `ma=sma` |  | value=13 |
| stochrsi | k, d | `flat=talib;rsi=wilder` | {"rsi_period": 14, "stoch_period": 14, "k_period": 3, "d_period": 3, "scale": 100.0, "range_eps": 1e-09} | k=29, d=31 |
|  |  | `flat=neutral;rsi=wilder` |  | k=29, d=31 |
|  |  | `flat=null;rsi=wilder` |  | k=29, d=31 |
|  |  | `flat=pine;rsi=wilder` |  | k=29, d=31 |
|  |  | `flat=talib;rsi=ema` |  | k=29, d=31 |
|  |  | `flat=neutral;rsi=ema` |  | k=29, d=31 |
|  |  | `flat=null;rsi=ema` |  | k=29, d=31 |
|  |  | `flat=pine;rsi=ema` |  | k=29, d=31 |
|  |  | `flat=talib;rsi=sma` |  | k=29, d=31 |
|  |  | `flat=neutral;rsi=sma` |  | k=29, d=31 |
|  |  | `flat=null;rsi=sma` |  | k=29, d=31 |
|  |  | `flat=pine;rsi=sma` |  | k=29, d=31 |
| cci | value | `src=hlc3;zero_dev=zero` | {"period": 20, "constant": 0.015} | value=19 |
|  |  | `src=hlc3;zero_dev=null` |  | value=19 |
|  |  | `src=close;zero_dev=zero` |  | value=19 |
|  |  | `src=close;zero_dev=null` |  | value=19 |
| ao | value | `ma=sma;src=hl2` | {"fast": 5, "slow": 34} | value=33 |
|  |  | `ma=sma;src=close` |  | value=33 |
|  |  | `ma=ema;src=hl2` |  | value=33 |
|  |  | `ma=ema;src=close` |  | value=33 |
|  |  | `ma=rma;src=hl2` |  | value=33 |
|  |  | `ma=rma;src=close` |  | value=33 |
| force | value | `ma=ema;seed=sma` | {"period": 13} | value=13 |
|  |  | `ma=ema;seed=first` |  | value=1 |
|  |  | `ma=ema;seed=adjusted` |  | value=1 |
|  |  | `ma=sma` |  | value=13 |
|  |  | `ma=raw` |  | value=1 |
| kdj | k, d, j | `init=50;ma=rma;zero_range=zero` | {"n": 9, "m1": 3, "m2": 3} | k=8, d=8, j=8 |
|  |  | `init=50;ma=rma;zero_range=half` |  | k=8, d=8, j=8 |
|  |  | `init=first;ma=rma;zero_range=zero` |  | k=8, d=8, j=8 |
|  |  | `init=first;ma=rma;zero_range=half` |  | k=8, d=8, j=8 |
|  |  | `init=50;ma=ema;zero_range=zero` |  | k=8, d=8, j=8 |
|  |  | `init=50;ma=ema;zero_range=half` |  | k=8, d=8, j=8 |
|  |  | `init=first;ma=ema;zero_range=zero` |  | k=8, d=8, j=8 |
|  |  | `init=first;ma=ema;zero_range=half` |  | k=8, d=8, j=8 |
|  |  | `ma=sma;zero_range=zero` |  | k=10, d=12, j=12 |
|  |  | `ma=sma;zero_range=half` |  | k=10, d=12, j=12 |
| ichimoku | tenkan, kijun, senkou_a, senkou_b, chikou | `shift=forward` | {"tenkan": 9, "kijun": 26, "senkou_b": 52, "displacement": 26} | tenkan=8, kijun=25, senkou_a=51, senkou_b=77, chikou=0 |
|  |  | `shift=forward_tv` |  | tenkan=8, kijun=25, senkou_a=50, senkou_b=76, chikou=0 |
|  |  | `shift=none` |  | tenkan=8, kijun=25, senkou_a=25, senkou_b=51, chikou=0 |

口径说明：

- 平滑 `ma`：`sma` 简单均值；`ema` alpha = 2/(n+1)；`rma` = Wilder，alpha = 1/n（KDJ 的 `rma` 即通达信 `SMA(X, N, 1)`）。
  `seed`（只对 ema/rma）：`sma` = 前 n 个有效值的均值作为第 n 个点（TA-Lib，预热 n−1）；`first` = 首值为种子（pandas
  `adjust=False`，无预热）；`adjusted` = Σ(1−a)^k·x / Σ(1−a)^k（pandas `adjust=True`，无预热）。EMA 不跨越有效段中间的缺值。
- TRIX = scale·(E3[t] − E3[t−1]) / E3[t−1]，scale 缺省 100（qbt 等返回比例的实现乘 100 对齐）。
- Stoch RSI：`rsi` = wilder / ema / sma（Cutler）；`flat` 规定两个 0/0 退化点：RSI 在平均涨跌都为 0 时取值，Stoch 在窗口极差
  ≤ `range_eps`（缺省 1e-9）时取值——`talib` (0, 0)、`neutral` (50, 50)、`null` (null, null)、`pine` (100, null)。
  `range_eps` 必要：横盘段 RSI 理论上恒定，但 Wilder 递推的浮点漂移让极差约 1e−14，不设阈值则 Stoch 输出任意值。
  K = SMA(stoch, k_period)，D = SMA(K, d_period)。
- CCI：`src` = hlc3 / close；常数 `constant` = 0.015（参数）；`zero_dev`：平均绝对偏差为 0 时输出 0 或 null。
- AO：`src` = hl2 / close；`ma` = sma / ema / rma（ema、rma 以 SMA 为种子）；AO = MA(fast) − MA(slow)。
- Force：raw = (C_t − C_{t−1})·V_t；`ma` = ema（种子 sma / first / adjusted）/ sma / raw。
- KDJ：RSV 窗口 n（缺省 9），`zero_range`：HHV == LLV 时 RSV 取 0 或 50；`ma` = rma / ema / sma，`init` = 50（前一点视为 50）
  或 first（首个 RSV 作初值）；J = 3K − 2D。
- Ichimoku：(9, 26, 52, 位移 26)；`shift=forward` 先行 A/B 前移 26、迟行线 = close[t+26]；`forward_tv` 前移 25（TradingView 的
  displacement−1）；`none` 不位移、迟行线 = close[t]。前移后截断到输入长度（未来 26 根云不输出）。前移口径的迟行线按定义非因果，
  因果性判据对它改用“恰为 close[t+位移]”。
- 预热期 / 未定义：Python 接口 `warmup="none"` 给 None、`"nan"` 给 NaN，不进 variant 字符串；JSON 一律 null。

## 4. 真实用例：对本仓 qbt 跑一遍（qbt 代码与结果未修改）

| 用例 | 结果 | 复现 |
|---|---|---|
| qbt/indicators.py 对指标向量（`qbt.dema/tema` ↔ `ma=ema;seed=first`，`100*qbt.trix` ↔ trix，`qbt.cci` ↔ `src=hlc3;zero_dev=zero`，`qbt.ichimoku` ↔ `shift=forward`） | DEMA 20/20、TEMA 20/20、TRIX 20/20、Ichimoku 前四线 40/40 一致；**CCI 1 处不一致**：`MISMATCH [value] first index 59: expected 0.0, actual 66.66666666666667 -- value differs at 59: expected 0.0, actual 66.66666666666667, rel_err 1.000e+00`——qbt 的 SMA 用滚动和，长横盘里均值漂移 ~1e−14，使“偏差为 0”分支失效，输出 ±1/0.015 = ±66.67 而非 0（数值稳健性缺陷）；qbt 的 chikou 定义为 close[i] − close[i−26]，与传统迟行线不同（7/10 不一致，属口径差异） | `python3.12 -m qdcheck.tools.qbt_indicators_vs_vectors` |
| 模糊 `qbt.data.GenSpec.from_dict(d).check()`（受控 = QBTError） | 2 万次中裸 TypeError 3,540 次，全部来自 `qbt/data.py:164 cls(**d)`：未知键 / 非 str 键 / 非 mapping 顶层没有转成 DataError | `python3.12 -m qdcheck.tools.selfcheck g2` |
| qbt 回测的成交流喂参考账本（6 策略 × 3 seed × 3 种 BrokerConfig，含 3 倍杠杆 + 资金费、8 倍杠杆 + 强平） | 54/54 次回测逐根权益一致（1,080,000 根，最大相对误差 1.6e-14），含 qbt 的 14 次强平，强平合法性 / 漏强平零违背 | `python3.12 -m qdcheck.tools.qbt_ledger_check` |
| qbt 回测跨进程 / 跨哈希种子 | 12/12 完全一致 | `python3.12 -m qdcheck.tools.selfcheck g4` |

## 5. 语句覆盖率

复用本仓 `tools/cov.py` 的 `executable_lines` / `Collector`（sys.monitoring）/ `format_ranges`，统计范围换成 qdcheck 的库模块；
`qdcheck/tools/*` 报告脚本单列、不计入；与 tools/cov.py 一样只统计主进程（determinism 子进程里执行的行不计入，
`_child_main` 另有进程内测试）。

| 文件 | 语句 | 未覆盖 | 覆盖率 |
|---|---:|---:|---:|
| `qdcheck/__init__.py` | 1 | 0 | 100.0% |
| `qdcheck/compare.py` | 140 | 0 | 100.0% |
| `qdcheck/determinism.py` | 175 | 0 | 100.0% |
| `qdcheck/examples.py` | 144 | 0 | 100.0% |
| `qdcheck/fuzz.py` | 663 | 0 | 100.0% |
| `qdcheck/gen_vectors.py` | 202 | 0 | 100.0% |
| `qdcheck/indicators_alt.py` | 168 | 0 | 100.0% |
| `qdcheck/indicators_ref.py` | 262 | 0 | 100.0% |
| `qdcheck/ledger.py` | 259 | 0 | 100.0% |
| `qdcheck/ledger_alt.py` | 149 | 0 | 100.0% |
| `qdcheck/props.py` | 122 | 0 | 100.0% |
| `qdcheck/stream.py` | 241 | 0 | 100.0% |
| `qdcheck/synth.py` | 153 | 0 | 100.0% |
| **合计（计入判据）** | 2679 | 0 | **100.00%** |
| qdcheck/tools/*（不计入） | 534 | 484 | 9.4% |

## 6. 突变测试

复用本仓 `mutants/run_mutations.py` 的突变算子、候选枚举、按 `seed:模块` 抽样与校验，被测包换成 qdcheck，每模块
60 个突变体，子进程跑 `QDCHECK_FAST=1 python -m unittest -f <该模块最相关的测试在前>`
（FAST 只缩小随机用例数 / 序列条数，不放宽判据），失败 = 抓住，超时（> 124.7 s）= 抓住并单列。
过程（每轮原始结果都在 `results/mutation_history/`）：

| 轮次 | 内容 | 抓住率 |
|---|---|---|
| round1 | 全部 720 个，初版测试 | 79.3%（漏网 149） |
| round2 | 逐条判定 round1 漏网后补判据、修复 2 处缺陷，全部 720 个在新代码上重新抽样重跑 | 93.5%（漏网 47） |
| round3–5 | 继续补判据；fuzz / stream 删除死代码、修复缩小器的无效候选后整模块重跑，其余按 `--ids` 重跑 | 97.4%（合并结果） |
| **final** | 最终代码 + 最终测试上全部 720 个从头重跑（下表，即 `results/mutation.json`） | **97.4%** |

| 模块 | 候选 | 突变体 | 抓住 | 超时 | 漏网 | 抓住率 |
|---|---:|---:|---:|---:|---:|---:|
| `compare.py` | 219 | 60 | 60 | 0 | 0 | 100.0% |
| `determinism.py` | 258 | 60 | 60 | 0 | 0 | 100.0% |
| `examples.py` | 332 | 60 | 60 | 0 | 0 | 100.0% |
| `fuzz.py` | 1377 | 60 | 55 | 2 | 3 | 95.0% |
| `gen_vectors.py` | 364 | 60 | 59 | 0 | 1 | 98.3% |
| `indicators_alt.py` | 494 | 60 | 59 | 0 | 1 | 98.3% |
| `indicators_ref.py` | 637 | 60 | 59 | 0 | 1 | 98.3% |
| `ledger.py` | 653 | 60 | 57 | 0 | 3 | 95.0% |
| `ledger_alt.py` | 289 | 60 | 52 | 0 | 8 | 86.7% |
| `props.py` | 314 | 60 | 59 | 0 | 1 | 98.3% |
| `stream.py` | 784 | 60 | 59 | 0 | 1 | 98.3% |
| `synth.py` | 335 | 60 | 59 | 1 | 0 | 100.0% |
| **合计** | | 720 | | | 19 | **97.4%** |

最终仍漏网的突变体逐条判定（判定写在 `results/mutation_triage.json`）：

| 突变 id | 行 | 改动 | 判定 | 理由 |
|---|---:|---|---|---|
| `fuzz:719:compare` | 375 | `rnd.random() < 0.05` → `rnd.random() <= 0.05` | 实践中等价 | make_case 里 rnd.random() < 0.05 改成 <=，只在 random() 恰好返回 0.05 时不同（概率约 2^-53），用到的 seed 下不会出现 |
| `fuzz:2884:const:False` | 617 | `True` → `False` | 等价 | call_isolated 的线程启动后立即 join，daemon 标志只影响解释器退出时是否等待该线程，结果相同 |
| `fuzz:4422:const:True` | 697 | `False` → `True` | 等价 | _pass 对非容器的顶层（None / 42 / 字符串）返回 changed=True：此时没有任何可缩小的路径，后续轮次也不调用 validate，最小复现与调用次数都不变 |
| `gen_vectors:196:compare` | 43 | `step > 0` → `step >= 0` | 等价 | _mono 只用 step=±0.5；step=0 时 open==close，两个分支给出相同的 high/low，故 step>0 与 step>=0 对任何输入结果相同 |
| `indicators_alt:1032:const:1.0` | 101 | `0.0` → `1.0` | 等价（数值） | 前缀和起点 0.0→1.0 只给所有前缀加同一常数，窗口差 pg[t]-pg[t-n] 中抵消；价格按 1/64 量化时前缀和精确，结果逐位相同，一般浮点输入也只差舍入（远小于 1e-9） |
| `indicators_ref:2509:const:0` | 327 | `1` → `0` | 等价（数值） | KDJ init=first 的递推起点 s+1→s 只多做一步 y=(x[s]+(m-1)*x[s])/m（或 a*x+(1-a)*x），数学上仍等于 x[s]，差异仅为舍入（<1e-15 相对），向量 1e-9 判据下不可区分 |
| `ledger:1109:compare` | 167 | `p > self.rel_tol * self.avg` → `p >= self.rel_tol * self.avg` | 实践中等价 | 强平价阈值 p > rel_tol*avg 改成 >=，只在 p 恰好等于 1e-9*均价 时不同；该值本身就是“数学上为 0、只剩舍入噪声”的判定线，无法构造有意义的输入使两者不同 |
| `ledger:1138:binop` | 184 | `closing * (p - self.avg) * _sign(Q)` → `closing * (p - self.avg) / _sign(Q)` | 等价 | _sign(Q) ∈ {1, -1}：IEEE 下 x*1 == x/1、x*(-1) == x/(-1) 逐位相同 |
| `ledger:3509:const:-1` | 197 | `0` → `-1` | 等价 | side 只取 ±1（normalize_record 保证），side > 0 与 side > -1 对这两个值结果相同 |
| `ledger_alt:72:const:-1` | 15 | `0` → `-1` | 等价 | _SIDE['none'] 只出现在 funding / settlement 记录上，ledger_alt 对这两类记录不使用 side |
| `ledger_alt:75:const:-1` | 15 | `0` → `-1` | 等价 | _SIDE[0] 同上：只用于 funding / settlement 记录，side 不参与计算 |
| `ledger_alt:505:const:1` | 47 | `0` → `1` | 等价 | r.get('qty', 0) 的缺省值只在记录缺 qty 时生效；trade / liquidation 记录必须带 qty（ledger.normalize_record 强制 qty>0），funding / settlement 记录在 ledger_alt 里不读 qty |
| `ledger_alt:247:compare` | 52 | `abs(x) <= 1e-12 * max(1.0, abs(ref))` → `abs(x) < 1e-12 * max(1.0, abs(ref))` | 实践中等价 | 数量归零阈值 <= 与 < 只在残差恰好等于 1e-12*max(1,/ref/) 时不同；浮点残差无法精确落在该值上（已用 1.2e-12 / 4e-13 两侧的残差测试阈值两边的行为） |
| `ledger_alt:943:const:-1` | 95 | `0` → `-1` | 等价 | 无仓时（size==0）走 else 分支：closing=0、realized 加 0、remaining=0 归零、leftover=q 开新仓，最终状态与开仓分支完全相同 |
| `ledger_alt:597:binop` | 99 | `q - self.size` → `q + self.size` | 等价（数值） | _is_zero(q-size) 改成 _is_zero(q+size) 后不再“视为恰好平仓”，但随后 remaining=size-q 的残差同样被 _is_zero 归零，持仓 / 方向相同；已实现盈亏只差 closing 的 1e-12 级差异 × 价差，远小于 1e-9 金额容差 |
| `ledger_alt:997:const:1` | 112 | `0` → `1` | 等价 | 全平且无剩余时 direction=0 改成 1：size=0 时 signed_qty、浮盈（空批次）恒为 0，下一笔成交走 size==0 的开仓分支重新设定方向，强平合法性检查对 size==0 同样判“非全平”，结果不变 |
| `ledger_alt:684:compare` | 142 | `self.cash.value() < -self.cfg['rel_tol'] * scale` → `self.cash.value() <= -self.cfg['rel_tol'] * scale` | 实践中等价 | 强平后余额检查 < 与 <= 只在现金恰好等于 -1e-9*尺度 时不同；按有效保证金封顶后现金 >= 0（见 test_wallet_erosion_caps_loss_at_wallet），不可能落在该负值上 |
| `props:1305:const:-1` | 158 | `0` → `-1` | 等价 | shift=none 时迟行线闭式解为“每个 t 都是 c”：off=0 时 t<n 恒真，off=-1 时 t-1<n 也恒真，两者生成相同列表 |
| `stream:3295:compare` | 90 | `side > 0` → `side >= 0` | 等价 | _affordable 里 side 只取 ±1，side > 0 与 side >= 0 对这两个值相同 |

## 7. 已知限制

- **单一标的账本**：`ledger` 一次只核一个标的；多标的共享现金的账户要按标的拆开（或把各标的盯市加总后用 `check_engine` 只核总权益）。
- **强平按收盘判定**：参考账本和随机流在收盘价上判断强平线；引擎若在盘中（最低价）强平，强平记录本身仍可核（合法性按记录价格判），
  但“漏强平”只在收盘检查。逐仓下资金费 / 结算亏损侵蚀钱包时，有效保证金 = min(逐仓保证金, 钱包)，这是本工具包的约定，
  与具体交易所的扣费顺序可能不同。
- **数值退化点是约定**：CCI 零偏差、KDJ 零极差用精确相等判断；Stoch RSI 零极差用 `range_eps`。别的实现在非量化价格上
  若有 1e−14 级漂移会在这些点上与向量不一致——这正是向量要暴露的数值稳健性问题（见第 4 节 qbt 的 CCI），不是容差问题。
- **接近 0 的值**：相对容差在 0 附近无意义，compare 用 abs 1e−12 兜底；TRIX / AO 接近 0 的点对求和顺序敏感（本机两份实现
  最坏用到容差的 21%），需要时可放宽 `--abs`。
- **模糊器**：单线程，不对挂死的 validate 设超时；只覆盖 schema 能描述的结构；RecursionError 的最小复现受解释器递归上限约束：
  深嵌套的最小复现不会短于约 1000 层（递归上限）加余量，原输入只有 1200 层时比例会远高于 30%（`test_recursion_on_deep_nesting`
  只要求二分后 < 1150 层且仍可复现）；30% 判据针对的是超长字符串上的 RecursionError。
- **determinism**：只比较返回值；不检查文件 / 数据库副作用；子进程用同一个解释器（`python=` 可换）；dict 键顺序默认不比
  （`dict_order=True` 可比）。
- **跨平台**：结果文件来自上面一台 Linux / CPython 3.12.3 机器；向量与合成数据不依赖 libm，理论上在任何 IEEE-754 机器上逐位相同，
  但判据只要求 1e−9 内一致。
- 覆盖率与突变测试只统计 / 突变库模块（`qdcheck/*.py`），不含 `qdcheck/tools/*` 报告脚本。

## 8. 文件清单

| 路径 | 内容 |
|---|---|
| `qdcheck/indicators_ref.py` / `indicators_alt.py` / `props.py` | 指标朴素参考实现 / 独立第二写法 / 性质判据 |
| `qdcheck/gen_vectors.py`、`qdcheck/vectors/` | 向量生成与复核脚本（SEED = 20261022）、指标向量 9 个文件 + 账本向量 7 个 + manifest |
| `qdcheck/compare.py` | 指标对拍器（库 + CLI） |
| `qdcheck/fuzz.py`、`qdcheck/examples.py` | 模糊器；示例 schema、validate（1 好 3 坏）与 G4 假引擎、qbt 适配器 |
| `qdcheck/synth.py`、`qdcheck/ledger.py`、`qdcheck/ledger_alt.py`、`qdcheck/stream.py` | 合成 K 线、参考账本、独立重算账本、随机订单流 |
| `qdcheck/determinism.py` | 跨进程一致性 harness |
| `qdcheck/tests/` | unittest（164 个） |
| `qdcheck/tools/` | 自检、qbt 真实用例、覆盖率、突变、报告生成脚本 |
| `qdcheck/results/` | 本报告引用的全部 JSON（含 `mutation_history/`） |
