"""qdperf — 量化交易系统性能体检工具包（纯 Python 3.12 标准库，零第三方依赖，零网络）。

模块：
    synth_db        同 seed 同结果的合成交易库（users / bots / orders / trades / settings / events）
    sqlplan         EXPLAIN QUERY PLAN 判定、WHERE / ORDER BY 解析、候选索引列顺序、记录字节估算（纯函数）
    sqlite_doctor   SQLite 体检器：计划判定 + 最贵语句排行 + 索引建议（临时副本上验证）+ 库文件体检（G1）
    sqlite_load     多进程实测：WAL 写争用（busy_timeout 等待分布 / SQLITE_BUSY）与 WAL 检查点增长曲线（G1）
    profile_gate    热路径剖析（墙钟 / CPU / tracemalloc / cProfile）+ 回归闸（G2）
    tick_jitter     引擎 / 面板 / 通知三进程 tick 抖动判据 + 参数扫描 + 缓解实测（G3）
    http_load       http.client + 线程压测、延迟分位、吞吐拐点、A/B 判定（G4）
    fake_backend    标准库 http.server 假后端（每路径延迟 / 抖动 / 错误率 / 并发上限）（G4）
    common          机器信息、分位数、子进程覆盖率 / 审计钩子、文本表格
    selftest        一键自检（G5）

所有数据都是合成的；只写 qdperf/results/ 与 tempfile；只连 127.0.0.1。
"""
__version__ = "1.0.0"
