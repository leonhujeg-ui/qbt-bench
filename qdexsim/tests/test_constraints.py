"""工程约束判据：零第三方依赖、零出网（socket 只许 127.0.0.1）、零写仓外（只写 qdexsim/results/ 与 tempfile）——
静态扫描 + 运行时审计钩子各一条钉死；外加函数 ≤ 60 行 / 文件 ≤ 800 行、库不 import 仓内其他包。"""
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

from qdexsim.tests._util import PKG, REPO, tmpdir

STDLIB = set(sys.stdlib_module_names)
REPO_INTERNAL = {"qdexsim"}
EXTRA_ALLOWED = {"tools/cov.py": {"tools"}, "mutants/run.py": {"mutants"}}
NETWORK_MODULES = {"socket", "ssl", "urllib.request", "ftplib", "smtplib", "poplib", "imaplib", "telnetlib", "xmlrpc.client",
                   "asyncio", "socketserver", "requests", "httpx", "aiohttp", "websockets", "websocket"}


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


def canonical(path):
    with open(path, encoding="utf-8") as f:
        return ast.unparse(ast.parse(f.read()))


class TestDependencies(unittest.TestCase):
    def test_stdlib_only(self):
        """grep ^import|^from 与 AST 两种方式：只许标准库 + qdexsim 自己（tools / mutants 另许复用仓内 tools、mutants）。"""
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
        for dirpath in (PKG, os.path.join(PKG, "dialects")):
            for name in sorted(os.listdir(dirpath)):
                if name.endswith(".py"):
                    tops = {n.split(".")[0] for n in imports(os.path.join(dirpath, name))}
                    self.assertEqual(tops & {"qbt", "strategies", "tools", "mutants", "qdcheck", "lob", "qdperf"}, set(), name)


class TestNoNetwork(unittest.TestCase):
    def test_static(self):
        """不直接 import socket（adapters 只为捕获 socket.timeout 例外）；http.client 只在 adapters，http.server 只在 server，
        urllib 只用 urllib.parse / urllib.request（后者仅测试里）；服务端绑定与客户端目标都写死 127.0.0.1。"""
        for path in py_files():
            rel = os.path.relpath(path, PKG).replace(os.sep, "/")
            mods = imports(path)
            self.assertEqual(mods & (NETWORK_MODULES - {"socket", "urllib.request"}), set(), rel)
            if "socket" in mods:
                self.assertEqual(rel, "adapters.py", rel)
            if "http.client" in mods:
                self.assertEqual(rel, "adapters.py", rel)
            if "http.server" in mods:
                self.assertEqual(rel, "server.py", rel)
            if "urllib.request" in mods:
                self.assertTrue(rel.startswith("tests/"), rel)
        srv = canonical(os.path.join(PKG, "server.py"))
        self.assertIn("http.server.ThreadingHTTPServer((common.LOOPBACK, self.port), Handler)", srv)
        self.assertEqual(srv.count("ThreadingHTTPServer("), 1)
        ad = canonical(os.path.join(PKG, "adapters.py"))
        self.assertIn("if u.hostname not in ('127.0.0.1', 'localhost'):", ad)
        self.assertEqual(ad.count("http.client.HTTPConnection("), 1)
        self.assertIn("socket.timeout", ad)
        self.assertNotIn("socket.socket(", ad)
        with open(os.path.join(PKG, "common.py"), encoding="utf-8") as f:
            self.assertIn('LOOPBACK = "127.0.0.1"', f.read())


def _allowed_path(p):
    tmp = os.path.realpath(tempfile.gettempdir())
    results = os.path.realpath(os.path.join(PKG, "results"))
    if p in (":memory:", ""):
        return True
    if p.startswith("file:"):
        p = p[5:].split("?", 1)[0]
    if not os.path.isabs(p):
        return False
    real = os.path.realpath(p)
    return real == "/dev/null" or real.startswith(tmp + os.sep) or real.startswith(results + os.sep)


class TestRuntimeAudit(unittest.TestCase):
    def test_writes_binds_and_connects(self):
        """装审计钩子跑一遍（服务端子进程 + 五个适配器 + 一个场景 + 一次对账）：所有写文件 / 删改名 / sqlite3.connect 都落在
        tempfile 或 qdexsim/results/ 下；所有 socket.connect 连 127.0.0.1；所有 socket.bind 绑 127.0.0.1。"""
        log = os.path.join(tmpdir(self), "audit.log")
        env = dict(os.environ, QDEXSIM_AUDIT_LOG=log, PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.run([sys.executable, "-m", "qdexsim.tests._audit_flow"], cwd=REPO, env=env, capture_output=True, text=True,
                              timeout=600)
        self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
        with open(log, encoding="utf-8") as f:
            events = [json.loads(line) for line in f]
        self.assertGreater(len({e["pid"] for e in events}), 1)                          # 服务端子进程也装上了钩子
        kinds = {e["event"] for e in events}
        self.assertTrue({"write", "sqlite3.connect", "socket.connect", "socket.bind"} <= kinds, kinds)
        bad = [e for e in events if e["event"] not in ("socket.connect", "socket.bind") and not _allowed_path(e["target"])]
        self.assertEqual(bad, [])
        for kind in ("socket.connect", "socket.bind"):
            addrs = [e["target"] for e in events if e["event"] == kind]
            self.assertTrue(addrs)
            self.assertTrue(all(isinstance(a, list) and a[0] in ("127.0.0.1", "localhost", "::1") for a in addrs), addrs[:5])
        self.assertTrue(any("audit_probe.json" in e["target"] for e in events if e["event"] == "write"))
        probe = os.path.join(PKG, "results", "selftest", "audit_probe.json")
        with open(probe, encoding="utf-8") as f:
            self.assertTrue(json.load(f)["scenario_ok"])

    def test_allowed_path_rule(self):
        tmp = tempfile.gettempdir()
        self.assertTrue(_allowed_path(os.path.join(tmp, "x", "y.db")))
        self.assertTrue(_allowed_path("file:" + os.path.join(tmp, "a.db") + "?mode=ro"))
        self.assertTrue(_allowed_path(os.path.join(PKG, "results", "g1.json")))
        self.assertTrue(_allowed_path("/dev/null"))
        self.assertTrue(_allowed_path(":memory:"))
        self.assertFalse(_allowed_path("rel.db"))
        self.assertFalse(_allowed_path("<unresolved dir_fd=7>/x.db"))
        outside = os.path.join(os.path.abspath(os.sep), "qdexsim-outside-check", "x.json")
        self.assertFalse(_allowed_path(outside))
        if not os.path.realpath(PKG).startswith(os.path.realpath(tmp) + os.sep):      # 突变测试时仓库副本本身就在 tempfile 里
            self.assertFalse(_allowed_path(os.path.join(PKG, "x.json")))
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
