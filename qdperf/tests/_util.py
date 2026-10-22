"""测试公用：快速模式开关、临时目录、小合成库缓存、捕获输出。

QDPERF_FAST=1（突变测试子进程里设置）只缩短实验时长 / 次数，不放宽任何判据。
QDPERF_COVERAGE=1（tools/cov.py 设置）或 QDPERF_NO_TIMING=1（突变测试设置：4 个突变体并行时机器很忙）时，
依赖绝对时长 / 机器空闲的断言跳过（TIMING = False），形状 / 结构类判据照跑。
"""
import atexit
import contextlib
import io
import os
import shutil
import tempfile

FAST = os.environ.get("QDPERF_FAST") == "1"
UNDER_COVERAGE = os.environ.get("QDPERF_COVERAGE") == "1"
TIMING = not (UNDER_COVERAGE or os.environ.get("QDPERF_NO_TIMING") == "1")
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PKG = os.path.join(REPO, "qdperf")
WORKLOAD = os.path.join(PKG, "examples", "workload_trading.json")
TINY_SCALE = 0.002                         # users 20 / bots 100 / orders 4000 / trades 10000 / settings 300 / events 2000

_CACHE = {}


def scale(full, fast):
    return fast if FAST else full


def tmpdir(test):
    d = tempfile.mkdtemp(prefix="qdperf-test-")
    test.addCleanup(shutil.rmtree, d, True)
    return d


def tiny_db(test, seed=7):
    """每个测试进程只生成一次的小合成库；返回它在本测试临时目录里的一份拷贝路径（测试可随意写）。"""
    from qdperf import synth_db
    key = ("tiny", seed)
    if key not in _CACHE:
        d = tempfile.mkdtemp(prefix="qdperf-tiny-")
        atexit.register(shutil.rmtree, d, True)                     # 进程退出时删掉缓存的母库
        path = os.path.join(d, "tiny.db")
        synth_db.generate(path, seed=seed, scale=TINY_SCALE)
        _CACHE[key] = path
    dst = os.path.join(tmpdir(test), "tiny.db")
    shutil.copyfile(_CACHE[key], dst)
    return dst


@contextlib.contextmanager
def captured():
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err
