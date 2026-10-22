"""自检 / 测试用的确定性小负载（纯 CPU + 可控内存分配），给 profile_gate 当目标。
环境变量 QDPERF_DEMO_BURN=k（> 0）时 spin 额外空转 k 次；QDPERF_DEMO_SLOW=f（> 0）时 spin 末尾空转 f × 本次已用时长
（恰好慢 f）。两者都模拟“新版本变慢”，调用参数不变。"""
import os
import time


def _inner(k):
    s = 0
    for i in range(k):
        s += i * i
    return s


def spin(n=200_000, alloc_kb=0, burn=0):
    """n 次整数平方累加（分 100 段调用 _inner）+ 分配 alloc_kb KB 的 bytearray + burn 次空转。"""
    t0 = time.perf_counter()
    total = 0
    step = max(1, n // 100)
    for _ in range(100):
        total += _inner(step)
    buf = bytearray(alloc_kb * 1024)
    burn = burn or int(os.environ.get("QDPERF_DEMO_BURN", "0") or 0)
    if burn:
        extra(burn)
    slow = float(os.environ.get("QDPERF_DEMO_SLOW", "0") or 0)
    if slow > 0:
        spin_until(time.perf_counter() + slow * (time.perf_counter() - t0))
    return total + len(buf)


def spin_until(t_end):
    """空转到 perf_counter() >= t_end。"""
    x = 0
    while time.perf_counter() < t_end:
        x = extra(1000)
    return x


def extra(burn):
    x = 0
    for i in range(burn):
        x ^= i
    return x


class Worker:
    @staticmethod
    def run(n=1000):
        return _inner(n)


def boom():
    raise RuntimeError("boom")


NOT_CALLABLE = 3
