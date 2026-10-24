"""测试公用：临时目录、进程内仿真器（不开 socket，直接调 Simulator.handle）、把适配器接到进程内仿真器上。

QDEXSIM_FAST=1（突变测试子进程里设置）只缩短等待，不放宽判据。
"""
import os
import shutil
import tempfile

from qdexsim import adapters
from qdexsim.dialects import base
from qdexsim.server import Simulator

FAST = os.environ.get("QDEXSIM_FAST") == "1"
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PKG = os.path.join(REPO, "qdexsim")


def tmpdir(test):
    d = tempfile.mkdtemp(prefix="qdexsim-test-")
    test.addCleanup(shutil.rmtree, d, True)
    return d


def inproc_sim(**kw):
    """没 start() 的仿真器：handle() 直接可用，没有线程、没有 socket。"""
    kw.setdefault("run_feed", False)
    return Simulator(**kw)


def wire(adapter, sim):
    """把适配器的 HTTP 传输换成进程内调用：请求 → sim.handle → (status, headers, bytes)；不回包 → UnknownResult。"""
    def request(method, path, headers=None, body=b""):
        p, _, q = path.partition("?")
        req = base.Request(method, p, q, headers, body)
        resp, decision = sim.handle(req)
        if resp is None:
            raise adapters.UnknownResult("dropped (inproc)")
        data = resp.bytes() if not (resp.status == 502 and resp.body is None) else b"<html>502</html>"
        return resp.status, {k.lower(): v for k, v in resp.headers.items()}, data
    adapter.t.request = request
    return adapter


def inproc_adapter(name, sim, creds=None):
    return wire(adapters.make(name, "http://127.0.0.1:1", creds), sim)
