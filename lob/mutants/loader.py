"""Run a unittest command with one module replaced by a mutant source file.

    LOB_MUTANT_MODULE=lob.book LOB_MUTANT_SRC=/tmp/m.py \
        python -m lob.mutants.loader lob.tests.test_cases ...
"""
import importlib
import importlib.util
import os
import sys
import unittest


def install(mod_name=None, src_path=None):
    mod_name = mod_name or os.environ['LOB_MUTANT_MODULE']
    src_path = src_path or os.environ['LOB_MUTANT_SRC']
    if mod_name in sys.modules:
        raise RuntimeError('%s already imported before the mutant was installed' % mod_name)
    spec = importlib.util.spec_from_file_location(mod_name, src_path)
    module = importlib.util.module_from_spec(spec)
    module.__package__ = mod_name.rpartition('.')[0]
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    parent, _, child = mod_name.rpartition('.')
    if parent:
        setattr(importlib.import_module(parent), child, module)
    return module


def main():
    install()
    unittest.main(module=None, argv=['python -m lob.mutants.loader'] + sys.argv[1:])


if __name__ == '__main__':
    main()
