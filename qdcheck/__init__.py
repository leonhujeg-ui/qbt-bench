"""qdcheck — 量化回测系统验证工具包（纯 Python 3.12 标准库，零第三方依赖，零网络）。

模块：
    indicators_ref   9 个指标的朴素参考实现（含口径变体）
    indicators_alt   同 9 个指标的第二份独立写法（闭式求和 / 单调队列 / fsum），只用于对拍
    gen_vectors      指标测试向量生成 / 复核脚本（固定 seed）
    compare          指标输出对拍器（相对容差 1e-9、预热期位置、长度）
    fuzz             schema 驱动的入参模糊器 + 运行器 + 自动缩小
    synth            同 seed 同结果的合成 K 线（停牌 / 缺口 / 跳跃 / 横盘 / 趋势）
    ledger           参考账本（多空 / 反手 / 部分平仓 / 加仓 / 逐仓保证金 / 强平）+ 恒等式检查
    ledger_alt       账本的第二份独立写法（按成交流分段重算）
    stream           随机订单流生成器（下单 / 撤单 / 部分成交 / 跨日结算 / 强平）
    determinism      跨进程 / 跨哈希种子一致性 harness
    examples         自检用的示例（故意写坏的 validate、依赖 set 顺序的假引擎、qbt 适配器）

所有数据都是合成的，不构成任何交易结论。
"""
__version__ = "1.0.0"
