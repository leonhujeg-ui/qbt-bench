"""工程约束判据：每个函数 <= 60 行，每个文件 <= 800 行；所有随机都显式 seed。"""
import ast
import os
import re
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DIRS = ("qbt", "strategies", "tests", "tools", "mutants", "fuzz", "bench")
MAX_FUNC, MAX_FILE = 60, 800


def _py_files():
    for d in DIRS:
        base = os.path.join(ROOT, d)
        if not os.path.isdir(base):
            continue
        for dirpath, dirs, files in os.walk(base):
            dirs[:] = [x for x in dirs if not x.startswith("_")]
            for f in sorted(files):
                if f.endswith(".py"):
                    yield os.path.join(dirpath, f)


class TestCodeLimits(unittest.TestCase):
    def test_file_and_function_length(self):
        violations = []
        for path in _py_files():
            with open(path, "r", encoding="utf-8") as f:
                src = f.read()
            n = src.count("\n") + (0 if src.endswith("\n") else 1)
            if n > MAX_FILE:
                violations.append(f"{path}: {n} lines > {MAX_FILE}")
            for node in ast.walk(ast.parse(src)):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    length = node.end_lineno - node.lineno + 1
                    if length > MAX_FUNC:
                        violations.append(f"{path}:{node.lineno} {node.name} {length} lines > {MAX_FUNC}")
        self.assertEqual(violations, [], "\n".join(violations))

    def test_no_unseeded_random(self):
        """禁止模块级无 seed 的随机调用（random 模块函数直呼）；只允许 random.Random(seed) 实例。"""
        bad = []
        pat = re.compile(r"\brandom\.(random|choice|randint|uniform|gauss|shuffle|sample|randrange)\(")
        for path in _py_files():
            with open(path, "r", encoding="utf-8") as f:
                for i, line in enumerate(f, 1):
                    if pat.search(line) and "# seeded" not in line:
                        bad.append(f"{path}:{i}: {line.strip()}")
        self.assertEqual(bad, [], "\n".join(bad))

    def test_no_third_party_imports(self):
        allowed_prefix = ("qbt", "strategies", "tests", "tools", "mutants", "fuzz", "bench", "qdcheck", "lob")   # 仓库内包
        import sys
        std = set(sys.stdlib_module_names)
        bad = []
        for path in _py_files():
            with open(path, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module]
                for name in names:
                    top = name.split(".")[0]
                    if top not in std and top not in allowed_prefix:
                        bad.append(f"{path}: {name}")
        self.assertEqual(bad, [], "\n".join(bad))


if __name__ == "__main__":
    unittest.main()
