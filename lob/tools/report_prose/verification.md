## 8. 验证运行记录

- `LOB_TEST_SCALE=full python3.12 -m unittest discover -s lob/tests -t .` → **382 tests OK，788.5 s**（含 1,000 万条 4 进程 lockstep 对拍、100 万对拍、分片回放逐字节一致、模糊 2 万、排队模型、CLI 端到端）；日志摘要见提交 9fd7864 说明。
- `python3.12 -m lob.cli coverage --json lob/results/coverage.json`（standard 规模）→ 384 tests OK（10M 级在 standard 下 skip），核心模块语句覆盖 97.40%。
- `python3.12 -m lob.tools.lint_limits` → 37 个文件，0 个问题（≤ 800 行 / 函数 ≤ 60 行），`tests/test_limits.py` 同时断言只用标准库。
- 所有随机过程显式 seed；价格 / 数量全程整数；无浮点比较（排队模型的 Brier 等统计量用浮点计算并按 4–5 位小数记录）。
- 每阶段提交并合回 `main`：阶段 0 babadfd、阶段 1 7318d87、阶段 2 2aaa147、阶段 3/4 16547bb 起多次、阶段 5/6 2f9aebc 起多次、阶段 7 本提交。
