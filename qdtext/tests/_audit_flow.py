"""审计流程（被 test_constraints 以子进程跑，带 QDTEXT_AUDIT_LOG）：把四个工具都走一遍，写文件只落 tempfile / qdtext/results。"""
import os
import shutil
import sys

from qdtext import cdp
from qdtext import common as C
from qdtext import locale_lint, mobile_layout, synth, wordy, zh_pair


def main():
    work = C.mkdtemp("audit-")
    try:
        packs, scr, answers = synth.generate(7, plant=True)
        synth.write(work, packs, scr, answers)
        loc = os.path.join(work, "locales")
        locale_lint.run(loc, os.path.join(work, "lint"), C.load_config(None, locale_lint.DEFAULT_CONFIG))
        wordy.run(loc, os.path.join(work, "wordy"), C.load_config(None, wordy.DEFAULT_CONFIG), scr)
        zh_pair.run(loc, os.path.join(work, "zp"), C.load_config(None, zh_pair.DEFAULT_CONFIG), zh_pair.load_tables())
        cfg = C.load_config(None, mobile_layout.DEFAULT_CONFIG)
        snap = C.read_json(os.path.join(C.FIXTURES, "snapshots", "site.json"))
        mobile_layout.run(snap["snapshots"], os.path.join(work, "ml"), cfg, {"mode": "offline"})
        if cdp.find_chrome():
            snaps, _v = mobile_layout.collect(os.path.join(C.FIXTURES, "site"), ["index.html"], [[375, 812]])
            mobile_layout.run(snaps, os.path.join(C.RESULTS, "audit_probe"), cfg, {"mode": "chromium"})
            C.write_json(os.path.join(C.RESULTS, "audit_probe", "audit_probe.json"), {"pages": len(snaps)})
            shutil.rmtree(os.path.join(C.RESULTS, "audit_probe"), ignore_errors=True)
        else:
            C.write_json(os.path.join(C.RESULTS, "audit_probe.json"), {"chrome": None})
            os.remove(os.path.join(C.RESULTS, "audit_probe.json"))
        print("AUDIT FLOW OK")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
