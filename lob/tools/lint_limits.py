"""Enforce the project rules: every file <= 800 lines, every function <= 60."""
import ast
import os
import sys

MAX_FILE_LINES = 800
MAX_FUNC_LINES = 60
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def python_files(root=ROOT):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ('data', '__pycache__', 'results')]
        for f in filenames:
            if f.endswith('.py'):
                out.append(os.path.join(dirpath, f))
    return sorted(out)


def check_file(path):
    with open(path) as fh:
        src = fh.read()
    lines = src.count('\n') + (0 if src.endswith('\n') else 1)
    problems = []
    if lines > MAX_FILE_LINES:
        problems.append('%s: %d lines > %d' % (path, lines, MAX_FILE_LINES))
    tree = ast.parse(src, path)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            n = node.end_lineno - node.lineno + 1
            if n > MAX_FUNC_LINES:
                problems.append('%s:%d %s: %d lines > %d' % (path, node.lineno, node.name,
                                                             n, MAX_FUNC_LINES))
    return problems


def main(root=ROOT):
    problems = []
    for p in python_files(root):
        problems.extend(check_file(p))
    for p in problems:
        print(p)
    print('%d files checked, %d problems' % (len(python_files(root)), len(problems)))
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
