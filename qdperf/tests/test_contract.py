"""CONTRACT.md 与代码一致：命令行参数都存在、第一行正则能匹配真实输出、退出码、JSON 字段名。"""
import ast
import os
import re
import unittest

from qdperf import fake_backend as B
from qdperf import http_load as H
from qdperf import profile_gate as G
from qdperf import sqlite_doctor as D
from qdperf import synth_db as S
from qdperf import tick_jitter as J
from qdperf.tests._util import PKG, captured

with open(os.path.join(PKG, "CONTRACT.md"), encoding="utf-8") as _f:
    CONTRACT = _f.read()
with open(os.path.join(PKG, "sqlite_doctor.py"), encoding="utf-8") as _f:
    DOCTOR_SRC = _f.read()
DOCTOR_STRINGS = {n.value for n in ast.walk(ast.parse(DOCTOR_SRC)) if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def regexes():
    """CONTRACT.md 里的第一行正则：代码块里以 ^ 开头、$ 结尾的行（可带“说明前缀”），以及行内 `^...$`。"""
    out = []
    for line in CONTRACT.splitlines():
        inline = re.findall(r"`(\^[^`]*\$)`", line)
        if inline:
            out += inline
            continue
        m = re.search(r"(\^.*\$)\s*$", line)
        if m:
            out.append(m.group(1))
    return out


def find(prefix):
    return [r for r in regexes() if r.startswith(prefix)]


def help_text(main, args):
    with captured() as (o, _e):
        try:
            main(args + ["--help"])
        except SystemExit as exc:
            assert exc.code == 0
    return o.getvalue()


class TestOptions(unittest.TestCase):
    CLI = {
        (D.main, ("x.db", "--workload", "w.json")): ["--workload", "--sections", "--duration", "--wal-duration", "--writers",
                                                     "--rate", "--busy-timeout", "--budget-ms", "--scan-cost-red",
                                                     "--deferred-demo", "--quick", "--json", "--emit-sql"],
        (S.main, ()): ["--out", "--seed", "--scale", "--force", "--fingerprint", "--json"],
        (G.main, ("run",)): ["--args", "--repeat", "--inner", "--runs", "--no-cold", "--top", "--path", "--env", "--out"],
        (G.main, ("pair",)): ["--out-b", "--path-b", "--env-b"],
        (G.main, ("compare",)): ["--wall-tol", "--mem-tol", "--gate", "--json"],
        (J.main, ()): ["--T", "--seconds", "--q", "--wal", "--busy", "--compute", "--mitigation", "--panel-ratio",
                       "--compute-ratio", "--panel-threads", "--notifier-rate", "--bg-threads", "--db", "--scale", "--quick",
                       "--json"],
        (H.main, ("ramp",)): ["--base", "--manifest", "--cmax", "--steps", "--seconds", "--warmup", "--growth",
                              "--max-error-rate", "--allow-remote", "--json"],
        (H.main, ("ab",)): ["--a", "--b", "--c", "--tol"],
        (B.main, ()): ["--config", "--port", "--ready-file", "--seconds"],
    }

    def test_every_documented_option_exists(self):
        for (main, args), opts in self.CLI.items():
            text = help_text(main, list(args))
            for o in opts:
                self.assertIn(o, text, (main.__module__, o))
                self.assertIn(o, CONTRACT, o)

    def test_enumerations_documented(self):
        for v in ("plans", "indexes", "contention", "wal", "file"):
            self.assertIn(v, D.SECTIONS)
        self.assertIn("`plans,indexes,contention,wal,file`", CONTRACT)
        for v in D.__dict__["DEFAULTS"]:
            self.assertIn(v, CONTRACT, v)
        for v in ("full_scan", "temp_btree", "automatic_index", "index", "covering_index", "rowid", "none"):
            self.assertIn(f"`{v}`", CONTRACT)
        for m in J.MITIGATIONS[1:]:
            self.assertIn(f"`{m}`", CONTRACT)
        for k in J.DEFAULTS:
            if k != "seed":
                self.assertTrue(f"--{k.replace('_', '-')}" in CONTRACT or f"`{k}`" in CONTRACT or k in CONTRACT, k)
        for m in H.METHODS:
            self.assertIn(m, CONTRACT)
        for code in ("HOT_FULL_SCAN", "CONTENTION", "ORIGINAL_MODIFIED", "FULL_SCAN", "TEMP_BTREE", "CONTENTION_DEMO",
                     "WAL_GROWTH", "FRAGMENTED", "NO_PRIMARY_KEY", "FK_WITHOUT_INDEX", "NEVER_ANALYZED", "STATS_STALE",
                     "STATS_MISSING", "SUGGESTION_REJECTED"):
            self.assertIn(f"`{code}`", CONTRACT)
            self.assertIn(code, DOCTOR_STRINGS)                                # 代码里的字符串常量（与引号 / 排版无关）
        for fmt in (D.FORMAT, D.REPORT_FORMAT, G.FORMAT, J.FORMAT, H.FORMAT, B.FORMAT, "qdperf.http_load.v1"):
            self.assertIn(fmt, CONTRACT)


class TestFirstLines(unittest.TestCase):
    def test_regexes_present(self):
        prefixes = ["^SQLITE_DOCTOR", "^SYNTH_DB", "^PROFILE target", "^REGRESSION (hot", "^OK hot", "^TICK_JITTER",
                    "^TICK_SCAN", "^HTTP_LOAD", "^REGRESSION \\S+", "^OK \\S+", "^FAKE_BACKEND"]
        for p in prefixes:
            self.assertEqual(len(find(p)), 1, p)

    def test_doctor_and_synth(self):
        rep = {"statements": [{"verdict": "full_scan", "flags": ["full_scan"]}], "findings": [], "ok": True}
        self.assertRegex(D.first_line(rep), find("^SQLITE_DOCTOR")[0])
        rep.update(indexes={"suggestions": [1], "rejected": []},
                   contention={"scenarios": [{"gate": True, "overall": {"wait_ms": {"p99": 0.5}, "busy": 0}}]},
                   wal={"scenarios": [{"max_bytes": 1e6}]})
        self.assertRegex(D.first_line(rep), find("^SQLITE_DOCTOR")[0])
        self.assertRegex("SYNTH_DB rows=10 seconds=0.1 size_mb=0.1 fingerprint=- path=/tmp/x.db", find("^SYNTH_DB")[0])

    def test_gate_lines(self):
        def doc(hot):
            return {"format": G.FORMAT, "target": "m:f", "args": {}, "hot": {"wall_s": hot}, "memory": {"peak_bytes": 10},
                    "profile": {"top": []}, "noise": None, "runs": 1}
        self.assertRegex(G.compare(doc(1.0), doc(2.0))["lines"][0], find("^REGRESSION (hot")[0])
        self.assertRegex(G.compare(doc(1.0), doc(1.0))["lines"][0], find("^OK hot")[0])
        d = dict(doc(1.0), cold={"wall_s": 2.0})
        self.assertRegex(G.first_line_profile(d), find("^PROFILE target")[0])
        self.assertRegex(G.first_line_profile(dict(doc(1.0), noise={"hot_wall_rel": 0.01}, runs=3)),
                         find("^PROFILE target")[0])

    def test_tick_and_http_lines(self):
        r = {"config": dict(J.DEFAULTS), "ticks": 3, "interval_T": {"p50": 1.0, "p99": 1.1, "max": 1.2}, "over_2T": 0,
             "starved": False, "segments_ms": {k: {"p99": 1.0} for k in ("read", "compute", "write")}}
        self.assertRegex(J.first_line_run(r), find("^TICK_JITTER")[0])
        s = {"knee": {"wal": False, "busy": 5.0, "compute": "inline", "knee_q": 5.0}, "results": [r],
             "mitigations": [dict(r, config=dict(J.DEFAULTS, mitigation="replica"))]}
        self.assertRegex(J.first_line_scan(s), find("^TICK_SCAN")[0])
        self.assertRegex(J.first_line_scan(dict(s, knee=None, mitigations=[])), find("^TICK_SCAN")[0])
        st = {"c": 1, "total": {"ok_rps": 10.0, "error_rate": 0.0, "latency_ms": {"p99": 3.0}}}
        self.assertRegex(H.first_line_ramp({"steps": [st], "knee": None, "max_rps": 10.0, "max_rps_c": 1}),
                         find("^HTTP_LOAD")[0])
        run = {"per_target": {"x": {"latency_ms": {"p50": 1.0, "p99": 2.0}}}}
        slow = {"per_target": {"x": {"latency_ms": {"p50": 3.0, "p99": 9.0}}}}
        self.assertRegex(H.judge(run, run, slow, 0.1)["first_line"], find("^REGRESSION \\S+")[0])
        self.assertRegex(H.judge(run, run, run, 0.1)["first_line"], find("^OK \\S+")[0])
        none = {"per_target": {"x": {"latency_ms": {"p50": None, "p99": None}}}}
        self.assertRegex(H.judge(none, run, run, 0.1)["first_line"], find("^REGRESSION \\S+")[0])
        self.assertRegex("FAKE_BACKEND LISTENING http://127.0.0.1:8080", find("^FAKE_BACKEND")[0])


class TestFields(unittest.TestCase):
    def test_result_fields_documented(self):
        """结果文件里出现的顶层字段在 CONTRACT.md 里都有名字。"""
        from qdperf import common
        res = os.path.join(PKG, "results")
        checks = {"g1_doctor.json": ("db", "statements", "indexes", "contention", "wal", "file", "findings", "ok"),
                  "g3_tick_jitter.json": ("base", "panel_calibration", "results", "knees", "knee", "mitigations", "gil")}
        for name, keys in checks.items():
            path = os.path.join(res, name)
            if not os.path.exists(path):
                continue
            doc = common.read_json(path)
            for k in keys:
                self.assertIn(k, doc, (name, k))
                self.assertIn(k, CONTRACT, (name, k))
        for k in ("wait_ms", "hold_ms", "late_ms", "lock_share", "achieved_rate", "max_live_frames", "resets", "curve_fields",
                  "fragmentation", "stats_stale", "fk_without_index", "serves", "check_analyzed", "size_est_bytes",
                  "wall_samples", "process_wall_samples", "peak_bytes", "primitive_calls", "hot_wall_rel", "interval_T",
                  "over_2T", "segments_ms", "replica_refreshes", "ok_rps", "statuses", "max_rps_c"):
            self.assertIn(k, CONTRACT, k)


if __name__ == "__main__":
    unittest.main()
