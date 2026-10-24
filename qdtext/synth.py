"""G5 合成语言包生成器：十语、≥ 400 键、六种键名族（btn / title+tab / label / chip / hint / 正文），按种子可复现地埋入已知问题并输出答案表。

    python3.12 -m qdtext.synth --out <目录> [--seed 7]

输出：<目录>/locales/<ns>.<lang>.json（5 个命名空间 × 10 语）、<目录>/screens.json（同屏键组：10 组重复 + 8 组好屏）、
<目录>/answers.json（答案表：lint 埋点逐条、wordy 的 30 条说明句键与 30 条好文案键、dup 组、zh_pair 的 20 个形近错字与 50 条意译）。

词库在 data/synth_vocab.json；组合规则：按钮 = 动词 × 名词（144）、标签 = 名词 × 属性（96）、计数（12，带管道）、确认句（36）、
状态（12 × 5 命名空间）、页标题 + 标签页（20）、固定句 73。干净版本（不埋问题）应让四个工具零红——这也是判据的一部分。
"""
import argparse
import os
import random
import re
import sys

from qdtext import common as C

DEFAULT_SEED = 7
NOUN_NS = {"order": "trade", "strategy": "bot", "asset": "wallet", "position": "trade", "account": "account", "wallet": "wallet",
           "grid": "bot", "contract": "trade", "market": "trade", "profit": "wallet", "record": "account", "notice": "account"}
LINT_PLAN = {"missing-key": 8, "empty-value": 4, "placeholder-mismatch": 6, "pipe-mismatch": 3, "han-residue": 5,
             "fullwidth-punct": 3, "rtl-unwrapped-ltr": 3, "over-budget": 6, "extra-key": 2, "leaf-type": 1}
LTR_RUN = re.compile(r"[A-Za-z][A-Za-z0-9._-]+")
LONG_BUTTON = {"zh": "立即创建网格机器人", "zh-TW": "立即建立網格機器人", "en": "Create a brand new grid bot now", "vi": "Tạo ngay một bot lưới hoàn toàn mới",
               "th": "สร้างบอทกริดใหม่ทั้งหมดเดี๋ยวนี้", "ja": "新しいグリッドボットを今すぐ作成する", "ko": "완전히 새로운 그리드 봇을 지금 생성하기",
               "ar": "إنشاء روبوت شبكة جديد تماماً الآن", "fr": "Créer tout de suite un nouveau robot de grille", "de": "Jetzt einen komplett neuen Grid-Bot anlegen"}


def load_vocab():
    return C.data_file("synth_vocab.json")


def fill(template, lang, **parts):
    """模板填充：{v}/{n}/{a} 原样，{V}/{N}/{A} 首字母大写；{n_of} 是法语的 de+名词。"""
    out = template
    for k, v in parts.items():
        out = out.replace("{" + k + "}", v).replace("{" + k.upper() + "}", v[:1].upper() + v[1:])
    if lang in ("en", "vi", "fr") and out:
        out = out[:1].upper() + out[1:]
    return out


def wrap_ltr(text):
    """阿拉伯语里的拉丁片段用 U+2066 LRI … U+2069 PDI 包裹（占位符不包）。"""
    def repl(m):
        return "⁦" + m.group(0) + "⁩"
    masked = C.PLACEHOLDER_RE.sub(lambda m: "\0" * len(m.group(0)), text)
    out, i = [], 0
    for m in LTR_RUN.finditer(masked):
        out.append(text[i:m.start()])
        out.append(repl(m) if "\0" not in m.group(0) else text[m.start():m.end()])
        i = m.end()
    out.append(text[i:])
    return "".join(out)


def build_clean(vocab):
    """→ {ns: {lang: {key: text}}}，不含任何埋点。"""
    langs, T = vocab["langs"], vocab["templates"]
    packs = {ns: {lang: {} for lang in langs} for ns in vocab["namespaces"]}
    for i, ns in enumerate(vocab["namespaces"]):
        for lang in langs:
            packs[ns][lang]["pageTitle"] = vocab["ns_titles"][lang][i]
            for j, tk in enumerate(vocab["tabs"]["keys"]):
                packs[ns][lang][f"tabs.{tk}"] = vocab["tabs"][lang][j]
            for j, sk in enumerate(vocab["status_keys"]):
                packs[ns][lang][f"status.{sk}Chip"] = vocab["statuses"][lang][j]
    for ni, nk in enumerate(vocab["noun_keys"]):
        ns = NOUN_NS[nk]
        for lang in langs:
            n = vocab["nouns"][lang][ni]
            n_of = vocab["nouns"]["fr_of"][ni] if lang == "fr" else n
            flat = packs[ns][lang]
            for vi, vk in enumerate(vocab["verb_keys"]):
                v = vocab["verbs"][lang][vi]
                flat[f"{nk}.{vk}Btn"] = fill(T["button"][lang], lang, v=v, n=n)
                if vk in vocab["confirm_verbs"]:
                    flat[f"{nk}.{vk}Confirm"] = fill(T["confirm"][lang], lang, v=v.lower() if lang in ("en", "fr") else v, n=n)
            for ai, ak in enumerate(vocab["attr_keys"]):
                flat[f"{nk}.{ak}Label"] = fill(T["label"][lang], lang, n=n, a=vocab["attrs"][lang][ai], n_of=n_of)
            flat[f"{nk}.count"] = fill(T["count"][lang], lang, n=n).replace("{x}", "{n}")
    for s in vocab["sentences"]:
        for lang in langs:
            packs[s["ns"]][lang][s["key"]] = s[lang]
    for ns in packs:
        packs[ns]["ar"] = {k: wrap_ltr(v) for k, v in packs[ns]["ar"].items()}
    return packs


# ---- 埋点 ----------------------------------------------------------------------------------
def _pick(rng, items, n):
    items = sorted(items)
    rng.shuffle(items)
    return items[:n]


def plant_lint(packs, vocab, rng, plan):
    """按 plan 埋 G1 问题（不碰 zh-TW，免得干扰 G3 判据）；→ 答案列表 [{rule, namespace, key, lang, ...}]。
    埋点会连带触发的第二条规则（砍掉管道段后占位符也没了）一起写进答案。"""
    langs = [l for l in vocab["langs"] if l not in ("zh", "zh-TW")]
    used, answers = set(), []

    def choose(rule, n, lang_pool, pred):
        cands = [(ns, k, lang) for ns in packs for k in packs[ns]["zh"] for lang in lang_pool
                 if (ns, k) not in used and pred(ns, k, packs[ns]["zh"][k])]
        out = []
        for ns, k, lang in _pick(rng, cands, n):
            used.add((ns, k))
            out.append({"rule": rule, "namespace": ns, "key": k, "lang": lang})
        answers.extend(out)
        return out

    body_only = lambda ns, k, z: "|" not in z and C.classify_key(k) == "body"        # noqa: E731  不连带超预算
    for a in choose("missing-key", plan["missing-key"], langs, lambda *_: True):
        del packs[a["namespace"]][a["lang"]][a["key"]]
    for a in choose("empty-value", plan["empty-value"], langs, lambda *_: True):
        packs[a["namespace"]][a["lang"]][a["key"]] = "  "
    for a in choose("placeholder-mismatch", plan["placeholder-mismatch"], langs, lambda ns, k, z: bool(C.placeholders(z))):
        flat = packs[a["namespace"]][a["lang"]]
        flat[a["key"]] = C.PLACEHOLDER_RE.sub(lambda m: "{" + m.group(1) + "_x}", flat[a["key"]], count=1)
    for a in choose("pipe-mismatch", plan["pipe-mismatch"], langs, lambda ns, k, z: "|" in z):
        flat = packs[a["namespace"]][a["lang"]]
        flat[a["key"]] = C.pipe_segments(flat[a["key"]])[0]
        if C.placeholders(flat[a["key"]]) != C.placeholders(packs[a["namespace"]]["zh"][a["key"]]):
            answers.append(dict(a, rule="placeholder-mismatch", secondary=True))
    for a in choose("han-residue", plan["han-residue"], [l for l in langs if l != "ja"], body_only):
        packs[a["namespace"]][a["lang"]][a["key"]] += " " + packs[a["namespace"]]["zh"][a["key"]]
    for a in choose("fullwidth-punct", plan["fullwidth-punct"], ["en"], body_only):
        packs[a["namespace"]][a["lang"]][a["key"]] = packs[a["namespace"]][a["lang"]][a["key"]].rstrip(".") + "。"
    for a in choose("rtl-unwrapped-ltr", plan["rtl-unwrapped-ltr"], ["ar"], body_only):
        packs[a["namespace"]][a["lang"]][a["key"]] += " BTC"
    for a in choose("over-budget", plan["over-budget"], langs, lambda ns, k, z: k.endswith("Btn")):
        for lg in vocab["langs"]:                                          # 全语种一起改长：zh/zh-TW 保持一致，G3 不受影响
            packs[a["namespace"]][lg][a["key"]] = LONG_BUTTON[lg]
        a["langs_all"] = True
    for a in choose("extra-key", plan["extra-key"], langs, lambda *_: True):
        packs[a["namespace"]][a["lang"]][a["key"] + "Extra"] = packs[a["namespace"]][a["lang"]][a["key"]]
        a["key"] += "Extra"
    for a in choose("leaf-type", plan["leaf-type"], langs, lambda *_: True):
        packs[a["namespace"]][a["lang"]][a["key"]] = 123
    return answers


def plant_zh_pair(packs, vocab, rng, n_para=50, n_typo=20):
    """zh-TW 侧：50 条正确意译（8 条整句改写 + 42 条词级换写）、20 个形近错字；→ (意译键, 错字答案)。"""
    paraphrases, used = [], set()
    for s in vocab["sentences"]:
        if "tw_alt" in s:
            packs[s["ns"]]["zh-TW"][s["key"]] = s["tw_alt"]
            paraphrases.append({"namespace": s["ns"], "key": s["key"], "kind": "rewrite"})
            used.add((s["ns"], s["key"]))
    from qdtext import zh_pair as Z
    T = Z.load_tables()
    swaps = list(vocab["term_swaps"])
    cands = []
    for zh_word, tw_word in swaps:
        default = T.words[zh_word][0]
        for ns in sorted(packs):
            for k, z in sorted(packs[ns]["zh"].items()):
                tw = packs[ns]["zh-TW"].get(k, "")
                if zh_word in z and default in tw and (ns, k) not in used and tw_word != default:
                    cands.append((ns, k, default, tw_word, zh_word))
    rng.shuffle(cands)
    for ns, k, default, tw_word, zh_word in cands:
        if len(paraphrases) >= n_para:
            break
        if (ns, k) in used:
            continue
        packs[ns]["zh-TW"][k] = packs[ns]["zh-TW"][k].replace(default, tw_word, 1)
        paraphrases.append({"namespace": ns, "key": k, "kind": "term-swap", "from": default, "to": tw_word})
        used.add((ns, k))
    conf = C.data_file("zh_confusable.json")["pairs"]
    pair_of = {}
    for a, b in conf:
        pair_of.setdefault(a, b)
        pair_of.setdefault(b, a)
    typo_cands = [(ns, k) for ns in sorted(packs) for k in sorted(packs[ns]["zh-TW"])
                  if (ns, k) not in used and isinstance(packs[ns]["zh-TW"][k], str) and any(c in pair_of for c in packs[ns]["zh-TW"][k])]
    typos = []
    for ns, k in _pick(rng, typo_cands, n_typo):
        tw = packs[ns]["zh-TW"][k]
        pos = next(i for i, c in enumerate(tw) if c in pair_of)
        wrong = pair_of[tw[pos]]
        packs[ns]["zh-TW"][k] = tw[:pos] + wrong + tw[pos + 1:]
        typos.append({"namespace": ns, "key": k, "pos": pos, "expected": tw[pos], "actual": wrong, "text": packs[ns]["zh-TW"][k]})
    return paraphrases, typos


def screens(vocab):
    out = {g["screen"]: g["keys"] for g in vocab["dup_groups"]}
    out.update({g["screen"]: g["keys"] for g in vocab["good_screens"]})
    return out


def generate(seed=DEFAULT_SEED, plant=True):
    """→ (packs, screens, answers)。plant=False 给干净版本。"""
    vocab = load_vocab()
    rng = random.Random(seed)
    packs = build_clean(vocab)
    answers = {"seed": seed, "planted": plant, "keys": sum(len(v["zh"]) for v in packs.values()), "namespaces": sorted(packs),
               "langs": vocab["langs"],
               "wordy": {"wordy_keys": [f"{s['ns']}:{s['key']}" for s in vocab["sentences"] if s["tag"] == "wordy"],
                         "good_keys": [f"{s['ns']}:{s['key']}" for s in vocab["sentences"] if s["tag"] == "good"],
                         "dup_groups": vocab["dup_groups"], "good_screens": vocab["good_screens"]},
               "lint": [], "zh_pair": {"paraphrases": [], "typos": []}}
    if plant:
        paraphrases, typos = plant_zh_pair(packs, vocab, rng)
        answers["zh_pair"] = {"paraphrases": paraphrases, "typos": typos}
        answers["lint"] = plant_lint(packs, vocab, rng, LINT_PLAN)
    return packs, screens(vocab), answers


def write(out_dir, packs, scr, answers):
    C.write_locale_dir(os.path.join(out_dir, "locales"), packs)
    C.write_json(os.path.join(out_dir, "screens.json"), scr)
    C.write_json(os.path.join(out_dir, "answers.json"), answers)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--clean", action="store_true", help="不埋问题")
    args = ap.parse_args(argv)
    packs, scr, answers = generate(args.seed, plant=not args.clean)
    write(args.out, packs, scr, answers)
    print(f"QDTEXT SYNTH OK keys={answers['keys']} namespaces={len(packs)} langs={len(answers['langs'])} "
          f"lint_planted={len(answers['lint'])} typos={len(answers['zh_pair']['typos'])} paraphrases={len(answers['zh_pair']['paraphrases'])} "
          f"out={os.path.abspath(args.out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
