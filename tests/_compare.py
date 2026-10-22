"""形状 + 容差比较器（跨机器可复现判据用）。

规则（与 qdcheck.determinism 的口径一致，但不依赖它）：
- int / bool / str / None：必须完全相等（int 与 float 不互换；bool 不当作 int）；
- float：|a-b| <= rel * max(|a|, |b|) + abs_floor；assert_close 默认 abs_floor = 1e-9 * 序列量级（只为 0 附近的
  抵消型值兜底，如 CCI / z-score / 回撤），混合量级的结构（成交记录里价格与费用）显式传 abs_floor=0.0；
  NaN 视为相等，inf 必须同号；
- list / tuple：长度相等且逐元素比较；dict：键集合相等、逐键比较；
- 第一处差异以路径报告（例如 strategies.ma_cross.fills[17].price）。

容差档位（理由见 REVIEW_QBT_LOB.md §1）：
- MONEY_REL = 1e-9：价格 / 费用 / 权益等金额，纯 IEEE 加减乘除，跨机差异只来自上游 libm 的 1 ulp；
- IND_REL = 5e-7：指标 / 风险度量，含滚动和漂移与抵消，取规格预算 1e-6 的一半，使“故意改错 1e-6”必红。
"""
import math

MONEY_REL = 1e-9
IND_REL = 5e-7
FLOOR_REL = 1e-9      # 绝对下限 = FLOOR_REL * 序列量级：只为 0 附近的抵消型值兜底，远小于 IND_REL 的相对容差


class ShapeMismatch(AssertionError):
    pass


def _scale(obj):
    """序列量级：嵌套结构里所有有限浮点绝对值的最大值（用于 abs_floor = rel * scale）。"""
    if isinstance(obj, bool) or obj is None:
        return 0.0
    if isinstance(obj, float):
        return abs(obj) if math.isfinite(obj) else 0.0
    if isinstance(obj, int):
        return 0.0                      # 整数精确比较，不参与量级
    if isinstance(obj, dict):
        return max((_scale(v) for v in obj.values()), default=0.0)
    if isinstance(obj, (list, tuple)):
        return max((_scale(v) for v in obj), default=0.0)
    return 0.0


def compare(a, b, rel=MONEY_REL, abs_floor=0.0, path="$"):
    """相等返回 None；否则返回第一处差异的描述字符串（不抛异常，便于“放水判据”检查）。"""
    if isinstance(a, bool) or isinstance(b, bool) or a is None or b is None or isinstance(a, str) or isinstance(b, str):
        return None if (type(a) is type(b) and a == b) else f"{path}: {a!r} != {b!r}"
    if isinstance(a, int) and isinstance(b, int):
        return None if a == b else f"{path}: {a} != {b}"
    if isinstance(a, float) or isinstance(b, float):
        if not (isinstance(a, (int, float)) and isinstance(b, (int, float))):
            return f"{path}: type {type(a).__name__} vs {type(b).__name__}"
        if type(a) is not type(b):
            return f"{path}: int/float mismatch {a!r} vs {b!r}"
        if math.isnan(a) or math.isnan(b):
            return None if (math.isnan(a) and math.isnan(b)) else f"{path}: nan vs {b!r}"
        if math.isinf(a) or math.isinf(b):
            return None if a == b else f"{path}: {a!r} vs {b!r}"
        if abs(a - b) <= rel * max(abs(a), abs(b)) + abs_floor:
            return None
        return f"{path}: {a!r} vs {b!r} (rel diff {abs(a - b) / max(abs(a), abs(b), 1e-300):.3e})"
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        if len(a) != len(b):
            return f"{path}: length {len(a)} != {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            d = compare(x, y, rel, abs_floor, f"{path}[{i}]")
            if d:
                return d
        return None
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a) != set(b):
            return f"{path}: keys differ {sorted(set(a) ^ set(b))}"
        for k in sorted(a, key=str):
            d = compare(a[k], b[k], rel, abs_floor, f"{path}.{k}")
            if d:
                return d
        return None
    return f"{path}: type {type(a).__name__} vs {type(b).__name__}"


def assert_close(a, b, rel=MONEY_REL, abs_floor=None, what=""):
    """abs_floor=None 时按 b 的序列量级取 FLOOR_REL * scale（混合量级的结构请显式传 0.0 或按字段分别比较）。"""
    if abs_floor is None:
        abs_floor = FLOOR_REL * _scale(b)
    d = compare(a, b, rel, abs_floor)
    if d:
        raise ShapeMismatch(f"{what} {d}")


def perturb(obj, rel):
    """把结构里的每个浮点乘以 (1 + rel)（“放水判据”用：容差必须能抓住这样的错）。"""
    if isinstance(obj, bool) or obj is None:
        return obj
    if isinstance(obj, float):
        return obj * (1.0 + rel) if obj != 0.0 else rel
    if isinstance(obj, dict):
        return {k: perturb(v, rel) for k, v in obj.items()}
    if isinstance(obj, list):
        return [perturb(v, rel) for v in obj]
    if isinstance(obj, tuple):
        return tuple(perturb(v, rel) for v in obj)
    return obj
