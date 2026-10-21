"""Project rules: file <= 800 lines, function <= 60 lines, stdlib only."""
import ast
import os
import unittest

from lob.tools.lint_limits import python_files, check_file, ROOT

STDLIB_OK = {'ast', 'array', 'argparse', 'bisect', 'collections', 'filecmp', 'hashlib',
             'heapq', 'json', 'math', 'mmap', 'multiprocessing', 'os', 'platform',
             'random', 'resource', 'shutil', 'statistics', 'struct', 'subprocess',
             'sys', 'tempfile', 'time', 'tracemalloc', 'unittest', 'importlib',
             'io', 'textwrap', 'types', 'traceback', 'signal', 'sqlite3',
             'concurrent', 'itertools', 'functools', 'operator', 'string', 'html',
             'contextlib', 'lob'}


class LimitsTest(unittest.TestCase):
    def test_file_and_function_limits(self):
        problems = []
        for p in python_files(ROOT):
            problems.extend(check_file(p))
        self.assertEqual(problems, [])

    def test_only_stdlib_imports(self):
        bad = []
        for p in python_files(ROOT):
            with open(p) as fh:
                tree = ast.parse(fh.read(), p)
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module]
                for n in names:
                    if n.split('.')[0] not in STDLIB_OK:
                        bad.append('%s: %s' % (os.path.relpath(p, ROOT), n))
        self.assertEqual(bad, [])
