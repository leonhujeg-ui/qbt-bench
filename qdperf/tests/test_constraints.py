"""工程约束判据：零第三方依赖、零出网、零写仓外（静态扫描 + 运行时审计钩子各一条），以及文件 / 函数长度。"""
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

from qdperf.tests._util import REPO, tmpdir

PKG = os.path.join(REPO, "qdperf")
STDLIB = set(sys.stdlib_module_names)
REPO_INTERNAL = {"qdperf"}
EXTRA_ALLOWED = {"examples/qbt_case.py": {"qbt", "strategies"}, "tools/cov.py": {"tools"},
                 "mutants/run.py": {"mutants"}}
NETWORK_MODULES = {"socket", "ssl", "urllib.request", "ftplib", "smtplib", "poplib", "imaplib", "telnetlib",
                   "xmlrpc.client", "asyncio", "requests", "httpx", "aiohttp"}


def py_files():
    for dirpath, dirs, files in os.walk(PKG):
        dirs[:] = [x for x in dirs if x not in ("__pycache__", "results")]
        for f in sorted(files):
            if f.endswith(".py"):
                yield os.path.join(dirpath, f)


def imports(path):
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            out.add(node.module)
    return out


class TestDependencies(unittest.TestCase):
    def test_stdlib_only(self):
        """grep ^import|^from 与 AST 两种方式：只许标准库 + qdperf 自己（examples / tools / mutants 另许复用的仓内包）。"""
        offenders = {}
        pat = re.compile(r"^\s*(?:import|from)\s+([A-Za-z_][\w.]*)")
        for path in py_files():
            rel = os.path.relpath(path, PKG).replace(os.sep, "/")
            allowed = STDLIB | REPO_INTERNAL | EXTRA_ALLOWED.get(rel, set())
            grep = set()
            with open(path, encoding="utf-8") as f:
                for line in f:
                    m = pat.match(line)
                    if m and not line.lstrip().startswith(("from .", "#")):
                        grep.add(m.group(1))
            tops = {n.split(".")[0] for n in imports(path) | grep}
            bad = sorted(tops - allowed)
            if bad:
                offenders[rel] = bad
        self.assertEqual(offenders, {})

    def test_library_does_not_import_repo_packages(self):
        for name in sorted(os.listdir(PKG)):
            if name.endswith(".py"):
                tops = {n.split(".")[0] for n in imports(os.path.join(PKG, name))}
                self.assertEqual(tops & {"qbt", "strategies", "tools", "mutants", "qdcheck", "lob"}, set(), name)


class TestNoNetwork(unittest.TestCase):
    def test_static(self):
        """不直接开 socket；http.client 只在 http_load（地址先过 parse_base 的本机校验）；urllib 只用 urllib.parse；
        假后端只绑 127.0.0.1。"""
        for path in py_files():
            rel = os.path.relpath(path, PKG).replace(os.sep, "/")
            mods = imports(path)
            self.assertEqual(mods & NETWORK_MODULES, set(), rel)
            self.assertFalse({m for m in mods if m.startswith("urllib")} - {"urllib.parse"}, rel)
            if "http.client" in mods:
                self.assertIn(rel, ("http_load.py", "tests/test_http_load.py"), rel)
            if "http.server" in mods:
                self.assertEqual(rel, "fake_backend.py")
        with open(os.path.join(PKG, "http_load.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertEqual(src.count("http.client.HTTPConnection("), 2)
        self.assertIn("host, port, prefix = parse_base(base, allow_remote)", src)
        with open(os.path.join(PKG, "fake_backend.py"), encoding="utf-8") as f:
            self.assertIn('Server(("127.0.0.1", port)', f.read())


def _allowed_path(p):
    tmp = os.path.realpath(tempfile.gettempdir())
    results = os.path.realpath(os.path.join(PKG, "results"))
    if p in (":memory:", ""):                     # 内存库 / SQLite 临时库：不落盘
        return True
    if p.startswith("file:"):
        p = p[5:].split("?", 1)[0]
    real = os.path.realpath(p)
    return real == "/dev/null" or real.startswith(tmp + os.sep) or real.startswith(results + os.sep)


class TestRuntimeAudit(unittest.TestCase):
    def test_writes_and_connects(self):
        """装审计钩子跑一遍四个工具：所有写文件 / 删改名 / sqlite3.connect 都落在 tempfile 或 qdperf/results/ 下，
        所有 socket.connect 都连 127.0.0.1。"""
        log = os.path.join(tmpdir(self), "audit.log")
        env = dict(os.environ, QDPERF_AUDIT_LOG=log, PYTHONDONTWRITEBYTECODE="1")
        env.pop("QDPERF_COV_DIR", None)
        proc = subprocess.run([sys.executable, "-m", "qdperf.tests._audit_flow"], cwd=REPO, env=env,
                              capture_output=True, text=True, timeout=600)
        self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
        with open(log, encoding="utf-8") as f:
            events = [json.loads(line) for line in f]
        pids = {e["pid"] for e in events}
        self.assertGreater(len(pids), 5)                                    # 子进程也装上了钩子
        kinds = {e["event"] for e in events}
        self.assertTrue({"write", "sqlite3.connect", "socket.connect"} <= kinds, kinds)
        bad = [e for e in events if e["event"] != "socket.connect" and not _allowed_path(e["target"])]
        self.assertEqual(bad, [])
        conns = [e["target"] for e in events if e["event"] == "socket.connect"]
        self.assertTrue(conns)
        self.assertTrue(all(isinstance(t, list) and t[0] in ("127.0.0.1", "localhost", "::1") for t in conns), conns[:5])
        writes = [e["target"] for e in events if e["event"] == "write"]
        self.assertTrue(any("audit_probe.json" in w for w in writes))

    def test_allowed_path_rule(self):
        tmp = tempfile.gettempdir()
        self.assertTrue(_allowed_path(os.path.join(tmp, "x", "y.db")))
        self.assertTrue(_allowed_path("file:" + os.path.join(tmp, "a.db") + "?mode=ro&immutable=1"))
        self.assertTrue(_allowed_path(os.path.join(PKG, "results", "g1.json")))
        self.assertTrue(_allowed_path("/dev/null"))
        self.assertTrue(_allowed_path(":memory:"))
        self.assertFalse(_allowed_path(os.path.join(PKG, "x.json")))
        self.assertFalse(_allowed_path(os.path.join(REPO, "qbt", "x")))
        self.assertFalse(_allowed_path(os.path.join(PKG, "results") + "_evil/x"))


class TestLimits(unittest.TestCase):
    def test_file_and_function_length(self):
        """与本仓 tests/test_limits.py 同口径：函数 ≤ 60 行，文件 ≤ 800 行。"""
        bad = []
        for path in py_files():
            with open(path, encoding="utf-8") as f:
                src = f.read()
            n = src.count("\n") + (0 if src.endswith("\n") else 1)
            if n > 800:
                bad.append(f"{path}: {n} lines")
            for node in ast.walk(ast.parse(src)):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.end_lineno - node.lineno + 1 > 60:
                    bad.append(f"{path}:{node.lineno} {node.name} {node.end_lineno - node.lineno + 1} lines")
        self.assertEqual(bad, [], "\n".join(bad))


if __name__ == "__main__":
    unittest.main()
