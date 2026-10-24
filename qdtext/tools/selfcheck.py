"""四个工具的自检（判据对拍）：合成语言包（G5）喂给 G1 / G2 / G3，与答案表比召回 / 误报；合成站喂给 G4，比预期的三类问题。

    python3.12 -m qdtext.tools.selfcheck g1|g2|g3|g4|all [--seed 7] [--out qdtext/results]

每个目标写一个 JSON（g1_locale_lint.json …），字段都是实测数字；判据函数 gX_ok() 给 selftest 用。
"""
import argparse
import json
import os
import shutil
import sys
import time

from qdtext import common as C
from qdtext import locale_lint, mobile_layout, synth, wordy, zh_pair


def _synth(seed, work):
    packs, scr, answers = synth.generate(seed, plant=True)
    synth.write(work, packs, scr, answers)
    clean, scr2, _ = synth.generate(seed, plant=False)
    synth.write(os.path.join(work, "clean"), clean, scr2, {})
    return answers


def _key(f):
    return f"{f['namespace']}:{f['key']}"


# ---- G1 ----------------------------------------------------------------------------------
def g1(seed=synth.DEFAULT_SEED, out_dir=None):
    work = C.mkdtemp("g1-")
    try:
        answers = _synth(seed, work)
        cfg = C.load_config(None, locale_lint.DEFAULT_CONFIG)
        _line, _code, found = locale_lint.run(os.path.join(work, "locales"), os.path.join(work, "lint"), cfg)
        _l2, code_clean, found_clean = locale_lint.run(os.path.join(work, "clean", "locales"), os.path.join(work, "lint_clean"), cfg)
        wordy_keys = set(answers["wordy"]["wordy_keys"])
        planted = {(a["rule"], a["namespace"], a["key"], a["lang"]) for a in answers["lint"]}
        hit = {(f["rule"], f["namespace"], f["key"], f["lang"]) for f in found}
        caught = planted & hit
        missed = sorted(planted - caught)
        by_design = [f for f in found if f["rule"] == "over-budget" and (_key(f) in wordy_keys or
                     any(a["rule"] == "over-budget" and a["namespace"] == f["namespace"] and a["key"] == f["key"] for a in answers["lint"]))]
        false_pos = [f for f in found if (f["rule"], f["namespace"], f["key"], f["lang"]) not in planted and f not in by_design]
        per_rule = {}
        for r, ns, k, lang in planted:
            d = per_rule.setdefault(r, {"planted": 0, "caught": 0})
            d["planted"] += 1
            d["caught"] += (r, ns, k, lang) in caught
        s = {"seed": seed, "keys": answers["keys"], "planted": len(planted), "caught": len(caught), "missed": missed,
             "recall_pct": round(100.0 * len(caught) / len(planted), 1), "false_positives": len(false_pos),
             "false_positive_samples": [{"rule": f["rule"], "key": _key(f), "lang": f["lang"], "text": f["text"]} for f in false_pos[:20]],
             "by_design_over_budget": len(by_design), "clean_red": C.count_by_severity(found_clean)["red"],
             "clean_yellow_non_wordy": sum(1 for f in found_clean if _key(f) not in wordy_keys), "clean_exit": code_clean,
             "per_rule": per_rule, "total_findings": len(found)}
        res = {"summary": s}
        if out_dir:
            C.write_json(os.path.join(out_dir, "g1_locale_lint.json"), res)
        return res
    finally:
        shutil.rmtree(work, ignore_errors=True)


def g1_ok(s):
    return s["recall_pct"] == 100.0 and s["false_positives"] == 0 and s["clean_red"] == 0 and s["clean_yellow_non_wordy"] == 0


# ---- G2 ----------------------------------------------------------------------------------
def g2(seed=synth.DEFAULT_SEED, out_dir=None):
    work = C.mkdtemp("g2-")
    try:
        answers = _synth(seed, work)
        cfg = C.load_config(None, wordy.DEFAULT_CONFIG)
        scr = C.read_json(os.path.join(work, "screens.json"))
        _line, _code, found = wordy.run(os.path.join(work, "clean", "locales"), os.path.join(work, "wordy"), cfg, scr)
        wordy_keys, good_keys = set(answers["wordy"]["wordy_keys"]), set(answers["wordy"]["good_keys"])
        flagged = {_key(f) for f in found if f["rule"] == "explanatory"}
        dups = [f for f in found if f["rule"] == "same-screen-duplicate"]
        dup_screens = {g["screen"] for g in answers["wordy"]["dup_groups"]}
        good_screens = {g["screen"] for g in answers["wordy"]["good_screens"]}
        hit_screens = {f["screen"] for f in dups}
        s = {"seed": seed, "wordy_planted": len(wordy_keys), "wordy_caught": len(flagged & wordy_keys),
             "wordy_recall_pct": round(100.0 * len(flagged & wordy_keys) / len(wordy_keys), 1), "wordy_missed": sorted(wordy_keys - flagged),
             "good_planted": len(good_keys), "good_flagged": sorted(flagged & good_keys),
             "good_false_positive_pct": round(100.0 * len(flagged & good_keys) / len(good_keys), 1),
             "other_flagged": sorted(flagged - wordy_keys - good_keys),
             "dup_groups": len(dup_screens), "dup_caught": len(hit_screens & dup_screens), "dup_missed": sorted(dup_screens - hit_screens),
             "dup_recall_pct": round(100.0 * len(hit_screens & dup_screens) / len(dup_screens), 1),
             "dup_methods": sorted({f["method"] for f in dups if f["screen"] in dup_screens}),
             "good_screens": len(good_screens), "good_screen_pairs": sum(sum(len(g["keys"]) * (len(g["keys"]) - 1) // 2 for g in answers["wordy"]["good_screens"]) for _ in [0]),
             "good_screens_flagged": [[f["screen"], f["key"], f["other_key"], f["method"]] for f in dups if f["screen"] in good_screens],
             "screen_key_missing": sum(1 for f in found if f["rule"] == "screen-key-missing")}
        res = {"summary": s}
        if out_dir:
            C.write_json(os.path.join(out_dir, "g2_wordy.json"), res)
        return res
    finally:
        shutil.rmtree(work, ignore_errors=True)


def g2_ok(s):
    return (s["wordy_recall_pct"] >= 90.0 and s["good_false_positive_pct"] <= 10.0 and s["dup_recall_pct"] >= 90.0
            and not s["good_screens_flagged"] and s["screen_key_missing"] == 0)


# ---- G3 ----------------------------------------------------------------------------------
def g3(seed=synth.DEFAULT_SEED, out_dir=None):
    work = C.mkdtemp("g3-")
    try:
        answers = _synth(seed, work)
        T = zh_pair.load_tables()
        cfg = C.load_config(None, zh_pair.DEFAULT_CONFIG)
        _line, _code, found = zh_pair.run(os.path.join(work, "locales"), os.path.join(work, "zh_pair"), cfg, T)
        _l2, code_clean, found_clean = zh_pair.run(os.path.join(work, "clean", "locales"), os.path.join(work, "zh_pair_clean"), cfg, T)
        typos = answers["zh_pair"]["typos"]
        paras = {f"{p['namespace']}:{p['key']}" for p in answers["zh_pair"]["paraphrases"]}
        reds = {_key(f): f for f in found if f["severity"] == "red"}
        caught = [t for t in typos if _key(t) in reds and any(d["expected"] == t["expected"] and d["actual"] == t["actual"]
                                                                 for d in reds[_key(t)].get("typos", []))]
        typo_keys = {_key(t) for t in typos}
        para_red = sorted(k for k in reds if k in paras)
        other_red = sorted(k for k in reds if k not in paras and k not in typo_keys)
        s = {"seed": seed, "typos_planted": len(typos), "typos_caught": len(caught),
             "typo_recall_pct": round(100.0 * len(caught) / len(typos), 1), "typos_missed": [t for t in typos if t not in caught],
             "paraphrases_planted": len(paras), "paraphrases_red": para_red, "paraphrases_info": sum(1 for f in found if f["rule"] == "paraphrase" and _key(f) in paras),
             "paraphrases_silent_ok": len(paras) - len(para_red) - sum(1 for f in found if f["rule"] == "paraphrase" and _key(f) in paras),
             "other_red": other_red, "clean_red": C.count_by_severity(found_clean)["red"], "clean_info": C.count_by_severity(found_clean)["info"],
             "clean_red_samples": [{"key": _key(f), "text": f["text"], "suggestion": f["suggestion"]} for f in found_clean if f["severity"] == "red"][:20],
             "clean_exit": code_clean, "chars": len(T.chars), "words": len(T.words)}
        res = {"summary": s}
        if out_dir:
            C.write_json(os.path.join(out_dir, "g3_zh_pair.json"), res)
        return res
    finally:
        shutil.rmtree(work, ignore_errors=True)


def g3_ok(s):
    return s["typo_recall_pct"] == 100.0 and not s["paraphrases_red"] and not s["other_red"] and s["clean_red"] == 0


# ---- G4 ----------------------------------------------------------------------------------
EXPECT_SITE = {"index.html": set(), "overlap.html": {"text-overlap"}, "overflow.html": {"horizontal-overflow", "touch-target"}}


def g4(out_dir=None, snapshot=None):
    """合成站三页 × 两尺寸：正常页零报，重叠页 / 溢出页各抓到预期规则。snapshot=None 时起 Chromium；给路径则离线。"""
    cfg = C.load_config(None, mobile_layout.DEFAULT_CONFIG)
    site = os.path.join(C.FIXTURES, "site")
    pages = sorted(EXPECT_SITE)
    t0 = time.perf_counter()
    if snapshot:
        doc = C.read_json(snapshot)
        snaps, browser = doc["snapshots"], "offline:" + os.path.basename(snapshot)
    else:
        snaps, browser = mobile_layout.collect(site, pages, cfg["sizes"])
    work = C.mkdtemp("g4-")
    try:
        _line, _code, found = mobile_layout.run(snaps, work, cfg, {"browser": browser, "pages": pages, "sizes": cfg["sizes"]})
    finally:
        shutil.rmtree(work, ignore_errors=True)
    per_page = {}
    for f in found:
        per_page.setdefault(f["namespace"], {}).setdefault(f["key"], set()).add(f["rule"])
    sizes = [f"{w}x{h}" for w, h in cfg["sizes"]]
    checks = {}
    for page, expect in EXPECT_SITE.items():
        for size in sizes:
            got = per_page.get(page, {}).get(size, set())
            checks[f"{page}@{size}"] = {"expected": sorted(expect), "got": sorted(got), "ok": got == expect}
    s = {"browser": browser, "mode": "offline" if snapshot else "chromium", "seconds": round(time.perf_counter() - t0, 2),
         "pages": len(pages), "sizes": sizes, "checks": checks, "all_ok": all(c["ok"] for c in checks.values()),
         "findings": len(found), "normal_page_findings": sum(len(v) for v in per_page.get("index.html", {}).values())}
    res = {"summary": s}
    if out_dir:
        C.write_json(os.path.join(out_dir, "g4_mobile_layout.json" if not snapshot else "g4_mobile_layout_offline.json"), res)
    return res


def g4_ok(s):
    return s["all_ok"] and s["normal_page_findings"] == 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("target", choices=["g1", "g2", "g3", "g4", "all"])
    ap.add_argument("--seed", type=int, default=synth.DEFAULT_SEED)
    ap.add_argument("--out", default=C.RESULTS)
    ap.add_argument("--snapshot", default=None, help="g4 离线快照")
    args = ap.parse_args(argv)
    ok = True
    for t in (["g1", "g2", "g3", "g4"] if args.target == "all" else [args.target]):
        if t == "g4":
            res = g4(args.out, args.snapshot)
            good = g4_ok(res["summary"])
        else:
            res = globals()[t](args.seed, args.out)
            good = globals()[t + "_ok"](res["summary"])
        ok &= good
        print(f"{t}: {'OK' if good else 'FAIL'}")
        print(json.dumps(res["summary"], ensure_ascii=False, indent=1)[:3000])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
