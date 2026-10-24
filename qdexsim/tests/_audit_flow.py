"""test_constraints 的运行时审计负载：装了审计钩子的进程里，起仿真器子进程、五个适配器各走一遍、跑一个场景、跑一次对账，
把一个探针文件写到 qdexsim/results/selftest/。所有写盘 / 连接 / 监听事件由 common.install_audit 记录。

    QDEXSIM_AUDIT_LOG=<日志> python3.12 -m qdexsim.tests._audit_flow
"""
import os
import shutil
import subprocess
import sys
import tempfile

from qdexsim import adapters, common, harness, recon, scenarios
from qdexsim.server import LISTEN_LINE


def main():
    common.child_init()
    d = tempfile.mkdtemp(prefix="qdexsim-audit-")
    proc = None
    try:
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.Popen([sys.executable, "-m", "qdexsim.server", "--db", os.path.join(d, "sim.db"),
                                 "--chaos-log", os.path.join(d, "chaos.jsonl")], cwd=common.REPO, env=env, stdout=subprocess.PIPE, text=True)
        url = proc.stdout.readline().strip()[len(LISTEN_LINE):]
        for name in adapters.ADAPTERS:
            ad = adapters.make(name, url, timeout_s=5)
            ad.place("BTCUSDT", "BUY", "LIMIT", 0.01, 59000.0, cid=f"audit-{name}")
            ad.cancel("BTCUSDT", cid=f"audit-{name}")
            ad.positions(), ad.account(), ad.fills("BTCUSDT")
        res = harness.run_scenario(scenarios.load("binance/unknown_drop"), "correct")
        local, remote, _p = recon.synth(3)
        rep = recon.reconcile(local, remote["orders"], recon.SYNTH_MAP, recon.REMOTE_MAP)
        common.write_json(os.path.join(common.RESULTS, "selftest", "audit_probe.json"),
                          {"scenario_ok": res["ok"], "recon_total": rep["summary"]["total"]})
    finally:
        if proc is not None:
            proc.terminate()
            proc.wait(timeout=10)
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    main()
