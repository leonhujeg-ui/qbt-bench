"""统一异常层级：所有对非法输入的拒绝都抛 QBTError 的子类。

模糊测试的判定规则：QBTError 及其子类视为“受控拒绝”，其他任何异常视为崩溃。
"""


class QBTError(ValueError):
    """所有引擎受控异常的基类（继承 ValueError 以便调用方按惯例捕获）。"""


class DataError(QBTError):
    """K 线数据非法：长度不一致、非有限数、负价、high<low、时间戳非递增等。"""


class OrderError(QBTError):
    """订单非法：数量非正/非有限、未知订单类型、缺少价格、未知标的等。"""


class ConfigError(QBTError):
    """配置非法：负手续费率、杠杆 <= 0、参数空间为空等。"""
