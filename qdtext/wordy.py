"""G2 「说明句」探测 + 同屏重复：对 zh 源文案打分（规则可解释、不用模型），并在「同屏键组」里找意思重复的状态 / 提示。

    python3.12 -m qdtext.wordy <语言包目录> [--screens screens.json] [--out qdtext/results/wordy] [--config cfg.json] [--strict]

说明句（rule explanatory）：分句数（，；。！？：）≥ 3 记 2 分、= 2 记 1 分；解释词表逐词加权（因为 / 所以 / 如果 / 注意 / 请 / 将会 /
不代表 / 仅供 / 说明 / 也就是 / 不是…而是 …，可配置，最多 3 分）；括号补充 1 分；宽度超「说明」预算（24）2 分、超 16 记 1 分。
总分 ≥ 阈值（默认 3）即报：按钮 / 状态 / 标签里的说明句红，说明 / 正文里的黄。每条给出分数与命中的规则。

同屏重复（rule same-screen-duplicate，红）：screens.json 形如 {"屏名": ["命名空间:键", ...]}（没有冒号时在所有命名空间里找）。
同屏两键的 zh 文案满足其一即报：去掉首尾虚词（已 / 正在 / 中 / 了）后相等；去标点后字面包含；同义词组概念集合 Jaccard ≥ 0.5（data/wordy_synonyms.json，可配置）；
归一化后编辑距离相似度 ≥ 0.7（两边都 ≥ 4 字）。反义词对（买入 / 卖出、成功 / 失败 …）命中则否决，避免把相反状态当重复。
stdout 第一行 `QDTEXT WORDY OK|FAIL n=…`；有红退出码 1；目录不存在退出码 2。
"""
import argparse
import os
import re
import sys

from qdtext import common as C

DEFAULT_CONFIG = {
    "base_lang": C.BASE_LANG,
    "key_rules": C.DEFAULT_KEY_RULES,
    "desc_budget": 24,
    "threshold": 3.0,
    "clause_separators": "，；。！？：,;!?:",
    "explain_words": {
        "因为": 1, "由于": 1, "所以": 1, "因此": 1, "如果": 1, "若": 0.5, "注意": 1, "请注意": 1, "请": 0.5, "将会": 1,
        "不代表": 1, "仅供": 1, "说明": 1, "也就是": 1, "不是": 0.5, "而是": 1, "以便": 1, "以免": 1, "否则": 1,
        "例如": 1, "比如": 1, "意味着": 1, "可能": 0.5, "建议": 0.5, "您可以": 1, "可以通过": 1, "需要": 0.5, "为了": 1,
        "一旦": 1, "之后": 0.5, "然后": 0.5, "系统将": 1, "请勿": 0.5, "务必": 1, "无法": 0.5, "表示": 1, "是指": 1,
    },
    "red_types": ["button", "status", "label"],
    "synonym_jaccard": 0.5,
    "edit_similarity": 0.7,
    "edit_min_len": 4,
    "strip_prefixes": ["已", "正在", "请", "正"],
    "strip_suffixes": ["中", "了"],
}
PAREN_RE = re.compile(r"[（(][^（）()]{2,}[）)]")
PUNCT_RE = re.compile(r"[\s，。、；：？！（）()【】《》「」『』…～—,.;:?!\"'`\-]+")


# ---- 说明句 ------------------------------------------------------------------------------
def score_text(text, cfg):
    """→ (分数, 命中规则列表)。"""
    hits, score = [], 0.0
    clauses = [c for c in re.split("[" + re.escape(cfg["clause_separators"]) + "]", text) if c.strip()]
    if len(clauses) >= 3:
        score += 2
        hits.append(f"clauses={len(clauses)}")
    elif len(clauses) == 2:
        score += 1
        hits.append("clauses=2")
    wscore = 0.0
    for w, wt in cfg["explain_words"].items():
        if w in text:
            wscore += wt
            hits.append(f"word:{w}")
    score += min(wscore, 3.0)
    if PAREN_RE.search(text):
        score += 1
        hits.append("parens")
    w = C.width(text)
    if w > cfg["desc_budget"]:
        score += 2
        hits.append(f"width={w:g}>{cfg['desc_budget']}")
    elif w > cfg["desc_budget"] * 2 / 3:
        score += 1
        hits.append(f"width={w:g}>{cfg['desc_budget'] * 2 / 3:g}")
    return score, hits


def find_explanatory(packs, cfg):
    out = []
    for ns in sorted(packs):
        for key, text in sorted(packs[ns].get(cfg["base_lang"], {}).items()):
            if not isinstance(text, str):
                continue
            score, hits = score_text(text, cfg)
            if score >= cfg["threshold"]:
                ktype = C.classify_key(key, cfg["key_rules"])
                sev = "red" if ktype in cfg["red_types"] else "yellow"
                out.append(C.finding("explanatory", sev, ns, key, cfg["base_lang"], text,
                                     f"说明句（{score:g} 分：{', '.join(hits)}）；{ktype} 文案应改成一句话陈述结果",
                                     score=score, hits=hits, key_type=ktype))
    return out


# ---- 同屏重复 ----------------------------------------------------------------------------
def normalize(text, cfg):
    t = PUNCT_RE.sub("", text).lower()
    changed = True
    while changed and t:
        changed = False
        for p in cfg["strip_prefixes"]:
            if t.startswith(p) and len(t) > len(p):
                t, changed = t[len(p):], True
        for s in cfg["strip_suffixes"]:
            if t.endswith(s) and len(t) > len(s):
                t, changed = t[:-len(s)], True
    return t


def concepts(text, groups):
    """命中的同义词组编号集合：按成员长度从长到短匹配，匹配过的片段抹掉，避免「执行异常」又算成「异常」。"""
    found, t = set(), text
    members = sorted(((m, gi) for gi, g in enumerate(groups) for m in g), key=lambda x: -len(x[0]))
    for m, gi in members:
        if m in t:
            found.add(gi)
            t = t.replace(m, "\0" * len(m))
    return found


def levenshtein(a, b):
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def similarity(a, b):
    n = max(len(a), len(b))
    return 1.0 - levenshtein(a, b) / n if n else 1.0


def antonym_veto(t1, t2, antonyms):
    for a, b in antonyms:
        if (a in t1 and b in t2 and b not in t1) or (b in t1 and a in t2 and a not in t1):
            return f"{a}/{b}"
    return None


def duplicate_reason(t1, t2, cfg, tables):
    """两条 zh 文案是否重复 → 命中方法字符串或 None。"""
    if antonym_veto(t1, t2, tables["antonyms"]):
        return None
    n1, n2 = normalize(t1, cfg), normalize(t2, cfg)
    if n1 and n1 == n2:
        return "normalized-equal"
    r1, r2 = PUNCT_RE.sub("", t1).lower(), PUNCT_RE.sub("", t2).lower()
    if len(min(r1, r2, key=len)) >= 2 and (r1 in r2 or r2 in r1):
        return "containment"
    c1, c2 = concepts(t1, tables["synonyms"]), concepts(t2, tables["synonyms"])
    if c1 and c2 and len(c1 & c2) / len(c1 | c2) >= cfg["synonym_jaccard"]:
        return "synonym"
    if min(len(n1), len(n2)) >= cfg["edit_min_len"] and similarity(n1, n2) >= cfg["edit_similarity"]:
        return "edit-distance"
    return None


def resolve_key(spec, packs, base_lang):
    """'ns:key' 或裸 key → (ns, key, text)；找不到 / 不唯一返回 (None, spec, 原因)。"""
    if ":" in spec:
        ns, key = spec.split(":", 1)
        text = packs.get(ns, {}).get(base_lang, {}).get(key)
        return (ns, key, text) if isinstance(text, str) else (None, spec, "键不存在")
    hits = [(ns, key) for ns in packs for key in [spec] if isinstance(packs[ns].get(base_lang, {}).get(key), str)]
    if len(hits) != 1:
        return None, spec, "键不存在" if not hits else "多个命名空间都有，请写成 ns:key"
    ns, key = hits[0]
    return ns, key, packs[ns][base_lang][key]


def find_duplicates(packs, screens, cfg, tables):
    out = []
    for screen in sorted(screens):
        items = []
        for spec in screens[screen]:
            ns, key, text = resolve_key(spec, packs, cfg["base_lang"])
            if ns is None:
                out.append(C.finding("screen-key-missing", "yellow", "-", key, cfg["base_lang"], "", f"屏「{screen}」：{text}",
                                     screen=screen))
            else:
                items.append((ns, key, text))
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                (ns1, k1, t1), (ns2, k2, t2) = items[i], items[j]
                why = duplicate_reason(t1, t2, cfg, tables)
                if why:
                    keep = t1 if len(t1) <= len(t2) else t2
                    out.append(C.finding("same-screen-duplicate", "red", ns1, k1, cfg["base_lang"], f"「{t1}」 vs 「{t2}」",
                                         f"屏「{screen}」同一状态说了两遍（{why}）；保留一条，建议留「{keep}」",
                                         screen=screen, other_namespace=ns2, other_key=k2, method=why))
    return out


# ---- 运行 ---------------------------------------------------------------------------------
def load_tables(path=None):
    return C.read_json(path) if path else C.data_file("wordy_synonyms.json")


def run(locale_dir, out_dir, cfg, screens=None, tables=None, strict=False):
    packs = C.load_locale_dir(locale_dir, (cfg["base_lang"],))
    tables = tables or load_tables()
    findings = find_explanatory(packs, cfg) + (find_duplicates(packs, screens, cfg, tables) if screens else [])
    findings = C.sort_findings(findings)
    meta = {"dir": os.path.abspath(locale_dir), "screens": len(screens or {}), "threshold": cfg["threshold"],
            "base_keys": sum(len(b.get(cfg["base_lang"], {})) for b in packs.values())}
    line, code = C.status_line("WORDY", findings, strict)
    C.write_json(os.path.join(out_dir, "wordy.json"), {"tool": "wordy", "status": line, "counts": C.count_by_severity(findings),
                                                        "meta": meta, "findings": findings})
    C.write_text(os.path.join(out_dir, "wordy.md"), markdown(findings, meta))
    return line, code, findings


def markdown(findings, meta):
    doc = C.findings_markdown("QDTEXT 说明句 + 同屏重复", findings, extra_cols=("screen", "method", "score"))
    doc += f"\n输入：{meta['dir']}；基准键 {meta['base_keys']}；同屏键组 {meta['screens']} 屏；阈值 {meta['threshold']}\n"
    return doc


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("locale_dir")
    ap.add_argument("--screens", default=None, help="同屏键组 JSON：{屏名: [ns:key, ...]}")
    ap.add_argument("--out", default=os.path.join(C.RESULTS, "wordy"))
    ap.add_argument("--config", default=None)
    ap.add_argument("--tables", default=None, help="同义词 / 反义词表 JSON（默认 data/wordy_synonyms.json）")
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args(argv)
    if not os.path.isdir(args.locale_dir):
        print(f"QDTEXT WORDY ERROR 目录不存在: {args.locale_dir}", file=sys.stderr)
        return 2
    cfg = C.load_config(args.config, DEFAULT_CONFIG)
    screens = C.read_json(args.screens) if args.screens else None
    line, code, _ = run(args.locale_dir, args.out, cfg, screens, load_tables(args.tables), args.strict)
    print(line)
    print(f"report: {os.path.join(args.out, 'wordy.md')}  json: {os.path.join(args.out, 'wordy.json')}")
    return code


if __name__ == "__main__":
    sys.exit(main())
