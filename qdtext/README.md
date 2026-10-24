```bash
python3.12 -m qdtext.selftest
```

<!-- SELFTEST_OUTPUT_START -->
（最近一次真实输出贴在这里，由最后一次提交后的运行产生）
<!-- SELFTEST_OUTPUT_END -->

---

# qdtext — 前端文案与手机版式体检工具包

纯 Python 3.12 标准库（零第三方依赖、零出网、只写 `qdtext/results/` 与 tempfile）。输入全是本地文件：语言包目录、同屏键组
JSON、构建产物目录。唯一允许的外部东西是本机已装的 Chromium / Chrome（G4），找不到就整项 SKIP 并写明原因。

| 工具 | 干什么 | stdout 第一行 / 退出码 |
|---|---|---|
| `qdtext.locale_lint`（G1） | 十语对齐（缺键 / 空值 / 多余键）、占位符集合与 `a \| b` 段数逐键一致、残留（非中日语种汉字、`en` 全角标点、`ar` 未包裹 LTR 片段）、按键类型的字数预算（超出按超出量排序） | `QDTEXT LOCALE OK\|FAIL n=总数 red=红 yellow=黄`；红 → 1，目录 / 配置错 → 2 |
| `qdtext.wordy`（G2） | 对 `zh` 源文案打分找「说明句」（分句数、解释词表、括号补充、长度；每条给分数与命中规则）；同屏键组里找意思重复的状态 / 提示（归一化相等、字面包含、同义词组、编辑距离；反义词否决） | `QDTEXT WORDY …`；同屏重复或按钮 / 状态 / 标签里的说明句红 → 1 |
| `qdtext.zh_pair`（G3） | 词级例外表 → 字表把 `zh` 转繁体，与 `zh-TW` 对齐比对：同位置字不同且不是候选 / 异体 / 标点对应 → 「X ≠ Y，疑似错字」红；增删换词 → 「意译，人工看」提示；没转的简体字 → 红 | `QDTEXT ZHPAIR …`；红 → 1 |
| `qdtext.mobile_layout`（G4） | 无头 Chromium + DevTools 协议（自写最小 WebSocket 客户端，只连 127.0.0.1）在 375×812 / 414×896 打开每页：文字盒子两两相交红、横向溢出红、可点元素触控区 < 44×44 黄、首屏文字字宽超预算黄；`--snapshot` 离线模式吃盒子快照 JSON | `QDTEXT MOBILE …`；红 → 1；没 Chromium → `QDTEXT MOBILE SKIP reason=…`，退出码 3 |
| `qdtext.synth`（G5） | 十语合成语言包（5 命名空间 441 键、六种键名族），按种子埋入已知问题并输出答案表 | `QDTEXT SYNTH OK keys=…` |

- 目标达成表（每条判据的实测数字、没抓到的及原因、已知盲区、搬运建议）：[REPORT.md](REPORT.md)
- 突变漏网逐条判定：[mutants/triage.md](mutants/triage.md)

## 输入约定

- 语言包：目录下 `<命名空间>.<语言>.json`，嵌套 JSON，叶子是字符串（数组下标也当一段键）。十种语言：`zh`（基准）、`zh-TW`、`en`、
  `vi`、`th`、`ja`、`ko`、`ar`、`fr`、`de`；语言列表可配。
- 占位符 `{name}` / `{0}`；复数 / 管道写法 `a | b`（按 `|` 切段）。
- 键类型看键名最后一段（小写、子串匹配，先命中先得）：含 `btn` / `button` / `action` → 按钮；`title` / `label` / `tab` → 标签；
  `chip` / `status` / `badge` → 状态；`hint` / `note` / `desc` / `intro` / `tip` / `help` / `disclaimer` → 说明；其余 → 正文。
- 字宽：CJK（东亚宽度 W/F）1、其余可见字符 0.5、空白 / 组合记号 / 零宽字符 0。预算表（可配）：

| 类型 | 中 / 日 / 韩 | 其余（字符数，不含空格） |
|---|---|---|
| 按钮 | ≤ 4 | ≤ 14 |
| 状态标签 | ≤ 4 | ≤ 12 |
| 标签 | ≤ 8 | ≤ 24 |
| 说明 | ≤ 24 | ≤ 60 |
| 正文 | 不限 | 不限 |

## 接到真实前端仓

```bash
# G1：语言包目录 → md + json；--config 覆盖 langs / key_rules / budgets / budget_severity（默认超预算是黄，可改红）
python3.12 -m qdtext.locale_lint src/locales --out out/lint --config qdtext_cfg.json

# G2：同屏键组 {"屏名": ["ns:key", ...]} 由你按页面组件维护；--tables 换成自己产品的状态同义词 / 反义词表
python3.12 -m qdtext.wordy src/locales --screens screens.json --out out/wordy

# G3：--words 叠加你们自己的词级例外表 {"words": {"简体词": ["台湾写法", "其他可接受写法"]}}；--text 只做转换
python3.12 -m qdtext.zh_pair src/locales --out out/zh --words my_terms.json
python3.12 -m qdtext.zh_pair --text "杠杆余额 马丁机器人"

# G4：构建产物目录 + 页面路径；--snapshot-out 存盒子快照，之后可用 --snapshot 离线复判
python3.12 -m qdtext.mobile_layout dist --pages index.html bot.html --sizes 375x812 414x896 --snapshot-out out/boxes.json
python3.12 -m qdtext.mobile_layout --snapshot out/boxes.json --out out/ml
```

配置 JSON 都是「顶层键整个替换」的浅覆盖，默认值见各模块 `DEFAULT_CONFIG`。

## 输出 JSON

每个工具写 `<out>/<tool>.json`：`{"tool", "status"（首行原文）, "counts": {"red", "yellow", "info"}, "meta", "findings": [...]}`，
每条 finding 固定有 `namespace / key / lang / rule / severity / text / suggestion`，各规则另带结构化字段（如 `expected` / `actual` /
`width` / `limit` / `over` / `score` / `hits` / `screen` / `other_key` / `method` / `typos` / `rect_a` / `rect_b`）。
同目录的 `<tool>.md` 是给人看的同一份内容（G1 另有超预算排行、按规则计数；G4 另有每页每尺寸的盒子统计）。

## 其他命令

```bash
python3.12 -m qdtext.selftest --quick                        # 跳过覆盖率
python3.12 -m unittest discover -s qdtext/tests -t .        # 只跑单元测试
python3.12 qdtext/tools/cov.py --fail-under 90              # 语句覆盖率（复用仓内 tools/cov.py，sys.monitoring）
python3.12 qdtext/mutants/run.py --per-module 40 --jobs 4   # 突变测试（复用仓内 mutants/run_mutations.py）-> results/mutation.json
python3.12 qdtext/mutants/triage.py [--check]              # 漏网逐条判定 -> mutants/triage.md
python3.12 -m qdtext.tools.selfcheck g1|g2|g3|g4|all        # 单独重跑某个目标的判据对拍 -> results/g*.json
python3.12 -m qdtext.synth --out /tmp/x --seed 7 [--clean]  # 合成语言包 + screens.json + answers.json
```

## 目录

| 路径 | 内容 |
|---|---|
| `common.py` | 路径、语言包读取、键类型、字宽、发现 / 报告、审计钩子 |
| `locale_lint.py` `wordy.py` `zh_pair.py` `mobile_layout.py` `cdp.py` | 四个工具（cdp 是 WebSocket + DevTools 客户端） |
| `synth.py` `data/synth_vocab.json` | 合成语言包生成器与十语词库 |
| `data/s2t_chars.json` `data/s2t_words.json` `data/zh_variants.json` `data/zh_confusable.json` | 简繁字表、词级例外表、合法异体字、形近字 |
| `data/wordy_synonyms.json` | 同义词组 / 反义词对 |
| `fixtures/site/` `fixtures/snapshots/site.json` | 三页合成站（正常 / 重叠 / 溢出 + 小按钮）与它的离线盒子快照 |
| `tests/` | 单元测试 + 工程约束（依赖 / 出网 / 写仓外）+ 运行时审计流程 |
| `tools/` `mutants/` | 覆盖率、判据对拍、突变测试、漏网判定 |
| `results/` | 自检产物（JSON / md） |
