"""测试公用：仓库路径、临时目录（测试结束自动删）、小语言包的生成。"""
import os
import shutil
import tempfile

from qdtext import common as C

REPO, PKG = C.REPO, C.PKG
FAST = os.environ.get("QDTEXT_FAST") == "1"


def tmpdir(case, suffix=""):
    d = tempfile.mkdtemp(prefix="qdtext-test-" + suffix)
    case.addCleanup(shutil.rmtree, d, True)
    return d


def packs_from(zh, **others):
    """{key: zh文本} + 其他语言字典 → {ns: {lang: flat}}（命名空间固定 app）。"""
    by_lang = {"zh": dict(zh)}
    for lang, flat in others.items():
        by_lang[lang.replace("_", "-")] = dict(flat)
    return {"app": by_lang}


def write_packs(d, packs):
    C.write_locale_dir(d, packs)
    return d
