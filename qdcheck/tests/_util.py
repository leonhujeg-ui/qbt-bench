"""测试公用：快速模式开关与小工具。

QDCHECK_FAST=1（突变测试子进程里设置）只缩小“随机用例数 / 序列条数”，不放宽任何判据。
"""
import contextlib
import io
import os

FAST = os.environ.get("QDCHECK_FAST") == "1"
UNDER_COVERAGE = os.environ.get("QDCHECK_COVERAGE") == "1"
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def scale(full, fast):
    return fast if FAST else full


@contextlib.contextmanager
def captured():
    """捕获 stdout / stderr，返回 (out, err) 两个 StringIO。"""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err
