"""工程约束判据：零第三方依赖（AST + grep）、零出网（socket 只在 cdp.py 且只连 127.0.0.1，静态 + 运行时审计）、零写仓外
（运行时审计：写文件只落 tempfile / qdtext/results），以及文件 / 函数长度。"""
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

from qdtext.tests._util import PKG, REPO, tmpdir

STDLIB = set(sys.stdlib_module_names)
REPO_INTERNAL = {"qdtext"}
EXTRA_ALLOWED = {"tools/cov.py": {"tools"}, "mutants/run.py": {"mutants"}}
NETWORK_MODULES = {"socket", "ssl", "urllib.request", "http.client", "http.server", "ftplib", "smtplib", "poplib", "imaplib",
                   "xmlrpc.client", "asyncio", "requests", "httpx", "aiohttp", "websocket", "websockets"}
SOCKET_ALLOWED = {"cdp.py", "tests/test_cdp.py"}


def py_files():
    for dirpath, dirs, files in os.walk(PKG):
        dirs[:] = [x for x in dirs if x not in ("__pycache__", "results", "_work")]
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
        """^import|^from 只许标准库 + qdtext 自己（tools/cov.py、mutants/run.py 另许复用仓内 tools / mutants）。"""
        pat = re.compile(r"^\s*(?:import|from)\s+([A-Za-z_][\w.]*)")
        offenders = {}
        for path in py_files():
            rel = os.path.relpath(path, PKG).replace(os.sep, "/")
            allowed = STDLIB | REPO_INTERNAL | EXTRA_ALLOWED.get(rel, set())
            grep = set()
            with open(path, encoding="utf-8") as f:
                for line in f:
                    m = pat.match(line)
                    if m and not line.lstrip().startswith(("from .", "#")):
                        grep.add(m.group(1))
            bad = sorted({n.split(".")[0] for n in imports(path) | grep} - allowed)
            if bad:
                offenders[rel] = bad
        self.assertEqual(offenders, {})

    def test_library_does_not_import_repo_packages(self):
        for name in sorted(os.listdir(PKG)):
            if name.endswith(".py"):
                tops = {n.split(".")[0] for n in imports(os.path.join(PKG, name))}
                self.assertEqual(tops & {"qbt", "strategies", "tools", "mutants", "qdcheck", "qdperf", "lob", "bench", "fuzz"}, set(), name)


class TestNoNetwork(unittest.TestCase):
    def test_static(self):
        """socket 只在 cdp.py（和它的测试服务器）；其他网络模块一个都不许；cdp 的 WebSocket 构造时断言 host == 127.0.0.1。"""
        for path in py_files():
            rel = os.path.relpath(path, PKG).replace(os.sep, "/")
            mods = imports(path)
            self.assertEqual(mods & (NETWORK_MODULES - {"socket"}), set(), rel)
            if "socket" in mods:
                self.assertIn(rel, SOCKET_ALLOWED, rel)
            self.assertFalse({m for m in mods if m and m.startswith("urllib")}, rel)
        src = canonical(os.path.join(PKG, "cdp.py"))
        self.assertEqual(src.count("socket.create_connection("), 1)
        self.assertIn("if host != LOCALHOST:\n            raise CDPError(", src)
        self.assertIn("LOCALHOST = '127.0.0.1'", src)
        self.assertIn("WebSocket(LOCALHOST, port, ws_path, timeout)", src)
        self.assertIn("'--disable-background-networking'", src)


def _allowed_path(p):
    tmp = os.path.realpath(tempfile.gettempdir())
    results = os.path.realpath(os.path.join(PKG, "results"))
    if not os.path.isabs(p):
        return False
    real = os.path.realpath(p)
    return real == "/dev/null" or real.startswith(tmp + os.sep) or real == results or real.startswith(results + os.sep)


class TestRuntimeAudit(unittest.TestCase):
    def test_writes_and_connects(self):
        """装审计钩子跑一遍四个工具（含起 Chromium）：所有写文件 / 建目录 / 删改名都落在 tempfile 或 qdtext/results/ 下，
        所有 socket.connect 都连 127.0.0.1。"""
        log = os.path.join(tmpdir(self), "audit.log")
        env = dict(os.environ, QDTEXT_AUDIT_LOG=log, PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.run([sys.executable, "-m", "qdtext.tests._audit_flow"], cwd=REPO, env=env, capture_output=True, text=True, timeout=600)
        self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
        self.assertIn("AUDIT FLOW OK", proc.stdout)
        with open(log, encoding="utf-8") as f:
            events = [json.loads(line) for line in f]
        kinds = {e["event"] for e in events}
        self.assertIn("write", kinds)
        bad = [e for e in events if e["event"] != "socket.connect" and not _allowed_path(e["target"])]
        self.assertEqual(bad, [])
        conns = [e["target"] for e in events if e["event"] == "socket.connect"]
        from qdtext import cdp
        if cdp.find_chrome():
            self.assertTrue(conns, "Chromium 在，应有一次 WebSocket 连接")
            self.assertTrue(all(isinstance(t, list) and t[0] == "127.0.0.1" for t in conns), conns[:5])
            self.assertTrue(any("audit_probe.json" in e["target"] for e in events if e["event"] == "write"))
        else:
            self.assertEqual(conns, [])
        writes = [e["target"] for e in events if e["event"] == "write"]
        self.assertTrue(any(w.endswith("locale_lint.json") for w in writes))

    def test_allowed_path_rule(self):
        tmp = tempfile.gettempdir()
        self.assertTrue(_allowed_path(os.path.join(tmp, "x", "y.json")))
        self.assertTrue(_allowed_path(os.path.join(PKG, "results", "g1.json")))
        self.assertTrue(_allowed_path("/dev/null"))
        self.assertFalse(_allowed_path("x.json"))
        self.assertFalse(_allowed_path(os.path.join(os.path.abspath(os.sep), "qdtext-outside", "x.json")))
        if not os.path.realpath(PKG).startswith(os.path.realpath(tmp) + os.sep):      # 突变测试时包副本本身在 tempfile 里
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

    def test_data_files_are_valid_json(self):
        d = os.path.join(PKG, "data")
        for name in sorted(os.listdir(d)):
            with open(os.path.join(d, name), encoding="utf-8") as f:
                json.load(f)


if __name__ == "__main__":
    unittest.main()
