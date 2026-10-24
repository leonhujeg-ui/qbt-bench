"""G3 简繁一致性：逐键把 zh 转成繁体（词级例外表 → 字表），与 zh-TW 逐字比对。

    python3.12 -m qdtext.zh_pair <语言包目录> [--out qdtext/results/zh_pair] [--words 自己的词表.json] [--strict]
    python3.12 -m qdtext.zh_pair --text "马丁机器人 杠杆 余额"          # 只做转换，打印期望繁体

数据（随仓提交，不依赖 OpenCC）：data/s2t_chars.json 字表（一对多保留全部候选，首个为默认）、data/s2t_words.json 词级例外表
（歧义字的词、交易 / 金融 / UI 词的台湾用法，可多个可接受写法）、data/zh_variants.json 合法异体字对与标点对应、
data/zh_confusable.json 形近字对。「台」默认保留「台」（台灣 / 平台 / 台幣），「臺」作合法异体接受。

判定（规则 / 级别）：
- simplified-residue 红：zh-TW 里出现只在简体里存在的字（没转）；
- suspect-typo 红：对齐后同位置字不同，且不是字表候选 / 合法异体 / 标点对应（如 zh「马丁机器人」期望「馬丁機器人」、实际「馬工機器人」
  → 「丁 ≠ 工，疑似错字（形近）」）；一个替换块里连续 ≥ 2 字都不同且都不是形近字 → 当作换词（意译）而不是错字；
  句子别处已有增删换词时，孤立的非形近单字差也并入意译（结构不同的句子里对不齐的单字不是错字证据）；
- paraphrase 提示：有增删 / 换词（整句结构不同的意译），只报「意译，人工看」，不判红。
已知盲区：整句其余完全一致、只换了一个非形近字的用词差异（和 → 與、按 → 以）会被判红——规则无法区分它和同音错字；
这类词要么进词级例外表（--words），要么人工看。
词级例外表里一个词有多个可接受写法时，zh-TW 用了任何一个都算对（登录 → 登入 / 登錄）。
stdout 第一行 `QDTEXT ZHPAIR OK|FAIL n=…`；有红退出码 1；目录不存在退出码 2。
"""
import argparse
import difflib
import os
import sys

from qdtext import common as C

DEFAULT_CONFIG = {"base_lang": "zh", "target_lang": "zh-TW", "key_rules": C.DEFAULT_KEY_RULES}


class Tables:
    """字表 / 词表 / 异体 / 形近，全部由 JSON 数据文件装入；extra_words 可叠加项目自己的词表。"""

    def __init__(self, chars, words, variants, confusable):
        self.chars = {k: v for k, v in chars.items() if k and v}
        self.words = {k: list(v) for k, v in words.items() if k and v}
        self.max_word = max((len(w) for w in self.words), default=1)
        self.equiv = {}
        for a, b in variants["pairs"] + variants.get("punct", []):
            self.equiv.setdefault(a, set()).add(b)
            self.equiv.setdefault(b, set()).add(a)
        self.confusable = {}
        for a, b in confusable["pairs"]:
            self.confusable.setdefault(a, set()).add(b)
            self.confusable.setdefault(b, set()).add(a)
        trad = {c for v in self.chars.values() for c in v} | {c for v in self.words.values() for w in v for c in w}
        self.simplified_only = {k for k in self.chars if k not in trad}

    def equivalent(self, a, b):
        return a == b or b in self.equiv.get(a, ())

    def is_confusable(self, a, b):
        return b in self.confusable.get(a, ())


def load_tables(extra_words=None):
    words = C.data_file("s2t_words.json")["words"]
    if extra_words:
        words = dict(words, **C.read_json(extra_words).get("words", {}))
    return Tables(C.data_file("s2t_chars.json")["map"], words, C.data_file("zh_variants.json"),
                  C.data_file("zh_confusable.json"))


def convert(text, T):
    """zh → (期望繁体, 词级 span 列表 [(起, 止, [可接受写法])], 每个位置的可接受字集合)。词表最长匹配优先，其余逐字查字表。"""
    out, spans, alts = [], [], []
    i = 0
    while i < len(text):
        for L in range(min(T.max_word, len(text) - i), 0, -1):
            w = text[i:i + L]
            if w in T.words:
                word = T.words[w][0]
                spans.append((len(alts), len(alts) + len(word), T.words[w]))
                out.append(word)
                alts.extend({c} for c in word)
                i += L
                break
        else:
            cands = T.chars.get(text[i], text[i])
            out.append(cands[0])
            alts.append(set(cands))
            i += 1
    return "".join(out), spans, alts


def pick_alternatives(expected, spans, alts, actual):
    """词有多个可接受写法而 zh-TW 用的不是默认那个：换成 zh-TW 里出现的那个，再做对齐。"""
    for start, end, options in sorted(spans, reverse=True):
        default = expected[start:end]
        if default in actual:
            continue
        for alt in options[1:]:
            if alt in actual:
                expected = expected[:start] + alt + expected[end:]
                alts[start:end] = [{c} for c in alt]
                break
    return expected, alts


def compare(src, actual, T):
    """→ {"status": ok|residue|typo|paraphrase, "expected", "typos": [...], "paraphrase": [...], "residue": [...]}"""
    residue = sorted({c for c in actual if c in T.simplified_only})
    expected, spans, alts = convert(src, T)
    expected, alts = pick_alternatives(expected, spans, alts, actual)
    res = {"expected": expected, "typos": [], "paraphrase": [], "residue": residue}
    if residue:
        res["status"] = "residue"
        return res
    if expected == actual:
        res["status"] = "ok"
        return res
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, expected, actual, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace" and i2 - i1 == j2 - j1:
            classify_block(expected, actual, i1, j1, i2 - i1, alts, T, res)
        else:
            res["paraphrase"].append({"expected": expected[i1:i2], "actual": actual[j1:j2], "pos": i1})
    if res["paraphrase"] and res["typos"] and not any(d["confusable"] for d in res["typos"]):
        # 句子别处已经增删换词（结构不同），孤立的非形近单字差多半也是改写的一部分 → 并入意译，不判红
        res["paraphrase"].extend({"expected": d["expected"], "actual": d["actual"], "pos": d["pos"]} for d in res["typos"])
        res["paraphrase"].sort(key=lambda x: x["pos"])
        res["typos"] = []
    res["status"] = "typo" if res["typos"] else ("paraphrase" if res["paraphrase"] else "ok")
    return res


def classify_block(expected, actual, i1, j1, n, alts, T, res):
    """等长替换块：逐位判合法（候选 / 异体 / 标点）；剩下的不同位置：有形近字或只有 1 处 → 错字，连续 ≥ 2 处换词 → 意译。"""
    diffs = []
    for k in range(n):
        e, a = expected[i1 + k], actual[j1 + k]
        if e == a or a in alts[i1 + k] or T.equivalent(e, a):
            continue
        diffs.append({"pos": i1 + k, "expected": e, "actual": a, "confusable": T.is_confusable(e, a)})
    if not diffs:
        return
    if len(diffs) >= 2 and not any(d["confusable"] for d in diffs):
        res["paraphrase"].append({"expected": expected[i1:i1 + n], "actual": actual[j1:j1 + n], "pos": i1})
    else:
        res["typos"].extend(diffs)


def check_packs(packs, cfg, T):
    out = []
    for ns in sorted(packs):
        base, tw = packs[ns].get(cfg["base_lang"], {}), packs[ns].get(cfg["target_lang"], {})
        for key, src in sorted(base.items()):
            actual = tw.get(key)
            if not isinstance(src, str) or not isinstance(actual, str) or not src.strip() or not actual.strip():
                continue                                             # 缺键 / 空值由 G1 报
            r = compare(src, actual, T)
            if r["status"] == "residue":
                out.append(C.finding("simplified-residue", "red", ns, key, cfg["target_lang"], actual,
                                     f"残留简体字 {''.join(r['residue'])}；期望「{r['expected']}」", expected=r["expected"],
                                     residue=r["residue"]))
            elif r["status"] == "typo":
                parts = [f"{d['expected']} ≠ {d['actual']}（位置 {d['pos'] + 1}{'，形近' if d['confusable'] else ''}）" for d in r["typos"]]
                out.append(C.finding("suspect-typo", "red", ns, key, cfg["target_lang"], actual,
                                     f"{'；'.join(parts)}，疑似错字；期望「{r['expected']}」", expected=r["expected"],
                                     typos=r["typos"], paraphrase=r["paraphrase"]))
            elif r["status"] == "paraphrase":
                out.append(C.finding("paraphrase", "info", ns, key, cfg["target_lang"], actual,
                                     f"意译，人工看；期望「{r['expected']}」", expected=r["expected"], paraphrase=r["paraphrase"]))
    return out


def run(locale_dir, out_dir, cfg, T, strict=False):
    packs = C.load_locale_dir(locale_dir, (cfg["base_lang"], cfg["target_lang"]))
    findings = C.sort_findings(check_packs(packs, cfg, T))
    meta = {"dir": os.path.abspath(locale_dir), "pairs": sum(len(p.get(cfg["base_lang"], {})) for p in packs.values()),
            "chars": len(T.chars), "words": len(T.words)}
    line, code = C.status_line("ZHPAIR", findings, strict)
    C.write_json(os.path.join(out_dir, "zh_pair.json"), {"tool": "zh_pair", "status": line, "counts": C.count_by_severity(findings),
                                                          "meta": meta, "findings": findings})
    doc = C.findings_markdown("QDTEXT 简繁一致性", findings, extra_cols=("expected",))
    doc += f"\n输入：{meta['dir']}；比对键 {meta['pairs']}；字表 {meta['chars']} 字，词表 {meta['words']} 词\n"
    C.write_text(os.path.join(out_dir, "zh_pair.md"), doc)
    return line, code, findings


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("locale_dir", nargs="?")
    ap.add_argument("--out", default=os.path.join(C.RESULTS, "zh_pair"))
    ap.add_argument("--config", default=None)
    ap.add_argument("--words", default=None, help="叠加的词级例外表 JSON（{\"words\": {简: [繁, ...]}}）")
    ap.add_argument("--text", default=None, help="只转换这段文字并打印")
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args(argv)
    T = load_tables(args.words)
    if args.text is not None:
        print(convert(args.text, T)[0])
        return 0
    if not args.locale_dir or not os.path.isdir(args.locale_dir):
        print(f"QDTEXT ZHPAIR ERROR 目录不存在: {args.locale_dir}", file=sys.stderr)
        return 2
    cfg = C.load_config(args.config, DEFAULT_CONFIG)
    line, code, _ = run(args.locale_dir, args.out, cfg, T, args.strict)
    print(line)
    print(f"report: {os.path.join(args.out, 'zh_pair.md')}  json: {os.path.join(args.out, 'zh_pair.json')}")
    return code


if __name__ == "__main__":
    sys.exit(main())
