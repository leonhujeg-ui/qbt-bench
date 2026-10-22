## 8. 验证运行记录

- `LOB_TEST_SCALE=full python3.12 -m unittest discover -s lob/tests -t .` → **382 tests OK，788.5 s**（**原 VM**，阶段 7 时的测试数；含 1,000 万条 4 进程 lockstep 对拍、100 万对拍、分片回放逐字节一致、模糊 2 万、排队模型、CLI 端到端）；提交 9fd7864 的信息只有标题行，并没有日志摘要（审核核实，原文「日志摘要见提交 9fd7864 说明」不成立）。full 规模（含 1,000 万对拍）**只在原 VM 上跑过，本次审核未重跑**。
- 审核复跑（standard 规模，10M 级在 standard 下 skip；测试数 382 → 384 是阶段 7 之后新增的判据，384 → 442 是审核新增的 `test_contract`（23）与 `test_boundaries`（35））：
  - 本审核 VM（Intel Xeon @ 2.10GHz，os.cpu_count()=4，Linux 6.18.44-fc-v140 x86_64，CPython 3.12.3）：`python3.12 -m lob.cli test`（= `unittest discover -s lob/tests -t .`，standard）→ `Ran 442 tests in 85.4s` / `OK (skipped=1)`，退出码 0；`python3.12 -m lob.cli test --quick` → `Ran 87 tests in 8.4s` / `OK`。
  - 审核员另一台机器（macOS arm64，Python 3.12.15，契约 / 边界判据落地前的树）同一 discover 命令 → 384 OK（skipped 1）；442 条的树未在该机复跑。
- 覆盖率：`python3.12 -m lob.cli coverage --json lob/results/coverage.json`（standard 规模，原 VM）→ 384 tests OK，核心 97.40%（**历史**）。审核复现时同一命令只得 95.49%，差异全在 `events.py`（258/308）：`lob/cli.py` 入口在采集器启动前已 import `events`，模块级语句计不到；生成器路径只在套件内重新生成 `events_1m.bin` 时执行。采集器已修（启动前移除已 import 的被测模块；流文件在临时目录重新生成，`LOB_DATA_DIR`），修后 `python3.12 -m lob.cli coverage --json lob/results/coverage_audit.json` → 442 tests OK，`events.py` 323/326，核心 **96.77%**（本机复测列见 6.y；低于 97.40% 的部分是审核新增的防御分支未全部被判据执行到）。
- `python3.12 -m lob.tools.lint_limits` → 37 个文件，0 个问题（≤ 800 行 / 函数 ≤ 60 行；阶段 7 时的文件数，审核收尾时为 39 个文件、0 个问题），`tests/test_limits.py` 同时断言只用标准库；审核复跑 `python3.12 -m unittest lob.tests.test_limits` → `Ran 2 tests` / `OK`。
- 所有随机过程显式 seed；价格 / 数量全程整数；无浮点比较（排队模型的 Brier 等统计量用浮点计算并按 4–5 位小数记录）。
- 每阶段提交并合回 `main`：阶段 0 babadfd、阶段 1 7318d87、阶段 2 2aaa147、阶段 3/4 16547bb 起多次、阶段 5/6 2f9aebc 起多次、阶段 7 本提交。
