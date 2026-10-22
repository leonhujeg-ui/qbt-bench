"""test_constraints 的运行时审计负载：在装了审计钩子的子进程里，把四个工具各走一遍（极小规模）。

    QDPERF_AUDIT_LOG=<日志> python3.12 -m qdperf.tests._audit_flow

子进程（写者 / 引擎 / 面板 / 测量 worker / 假后端）继承环境变量，入口 common.child_init() 也会装同一个钩子。
"""
import os
import shutil
import tempfile

from qdperf import common


def main():
    common.child_init()
    from qdperf import fake_backend, http_load, profile_gate, sqlite_doctor, synth_db, tick_jitter
    d = tempfile.mkdtemp(prefix="qdperf-audit-")
    try:
        db = os.path.join(d, "a.db")
        synth_db.generate(db, scale=0.002)
        wl = os.path.join(common.PKG, "examples", "workload_trading.json")
        rep = sqlite_doctor.diagnose(db, wl, duration=0.5, wal_duration=0.6, wal_checkpoints=(100,), rate=20.0)
        common.write_json(os.path.join(common.RESULTS, "selftest", "audit_probe.json"), {"ok": rep["ok"]})
        profile_gate.measure("qdperf.examples.demo_work:spin", {"n": 2000}, repeat=1)
        work, master = tick_jitter.prepare(scale=0.002)
        try:
            cal = tick_jitter.calibrate_panel(master, 0.02)
            tick_jitter.run_once(master, {"T": 0.05, "seconds": 0.5, "q": 5.0, "mitigation": "replica",
                                          "replica_every": 0.2}, work, cal)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        proc, url = fake_backend.start({"format": fake_backend.FORMAT, "routes": {"/x": {}}})
        try:
            http_load.ramp(url, {"format": http_load.FORMAT, "targets": [{"path": "/x"}]}, steps=[1, 2], seconds=0.2,
                           warmup=0.0)
        finally:
            fake_backend.stop(proc)
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    main()
