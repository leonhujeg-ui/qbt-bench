"""G1 语言包体检：十语对齐、占位符 / 管道段数一致、残留（汉字 / 全角标点 / 阿拉伯语裸 LTR 片段）、字数预算。

    python3.12 -m qdtext.locale_lint <语言包目录> [--out qdtext/results/locale_lint] [--config cfg.json] [--strict]

输入：目录下 `<命名空间>.<语言>.json`（嵌套 JSON，叶子是字符串）。输出：<out>/locale_lint.md + locale_lint.json
（逐条：命名空间、键、语言、规则、级别、原文、建议）。stdout 第一行 `QDTEXT LOCALE OK|FAIL n=<总数> red=<红> yellow=<黄>`；
有红项退出码 1（--strict 时黄也算），输入目录不存在 / 配置不合法退出码 2。

规则（级别）：missing-file 红、missing-key 红、empty-value 红、extra-key 黄、leaf-type 黄、placeholder-mismatch 红、
pipe-mismatch 红、han-residue 红、fullwidth-punct 红、rtl-unwrapped-ltr 黄（只报不改）、over-budget 黄（可配成红）。
配置 JSON 可覆盖：langs / base_lang / key_rules / budgets / han_allowed_langs / fullwidth_langs / rtl_langs / budget_severity。
"""
import argparse
import os
import re
import sys

from qdtext import common as C

DEFAULT_CONFIG = {
    "langs": list(C.LANGS),
    "base_lang": C.BASE_LANG,
    "key_rules": C.DEFAULT_KEY_RULES,
    "budgets": C.DEFAULT_BUDGETS,
    "han_allowed_langs": sorted(C.HAN_ALLOWED_LANGS),
    "fullwidth_langs": ["en"],
    "rtl_langs": ["ar"],
    "budget_severity": "yellow",
}
FULLWIDTH_PUNCT = set("，。、；：？！（）【】《》〈〉「」『』～　")
LTR_RUN_RE = re.compile(r"[A-Za-z][A-Za-z0-9._-]+")
WRAP_OPEN = set("‎‏‪‫‭‮⁦⁧⁨")
WRAP_CLOSE = set("‎‏‬⁩")


def check_pack(ns, by_lang, cfg):
    """一个命名空间的十语字典 → 发现列表。"""
    base_lang, langs = cfg["base_lang"], cfg["langs"]
    out = []
    base = by_lang.get(base_lang)
    if base is None:
        return [C.finding("missing-file", "red", ns, "*", base_lang, "", f"缺基准语言文件 {ns}.{base_lang}.json")]
    base_str = {k: v for k, v in base.items() if isinstance(v, str)}
    for lang in langs:
        flat = by_lang.get(lang)
        if flat is None:
            out.append(C.finding("missing-file", "red", ns, "*", lang, "", f"缺文件 {ns}.{lang}.json（{len(base)} 键全缺）"))
            continue
        for key, val in flat.items():
            if not isinstance(val, str):
                out.append(C.finding("leaf-type", "yellow", ns, key, lang, repr(val), "叶子必须是字符串"))
            elif not val.strip():
                out.append(C.finding("empty-value", "red", ns, key, lang, val, "补上译文"))
            if key not in base:
                out.append(C.finding("extra-key", "yellow", ns, key, lang, str(val), f"基准 {base_lang} 里没有这个键，删掉或补到基准"))
        if lang == base_lang:
            continue
        for key, src in base_str.items():
            if key not in flat:
                out.append(C.finding("missing-key", "red", ns, key, lang, src, "补上译文"))
                continue
            val = flat[key]
            if isinstance(val, str) and val.strip():
                out.extend(check_pair(ns, key, lang, src, val))
    for lang in langs:
        for key, val in (by_lang.get(lang) or {}).items():
            if isinstance(val, str) and val.strip():
                out.extend(check_text(ns, key, lang, val, cfg))
    return out


def check_pair(ns, key, lang, src, val):
    """与基准逐键比：占位符集合、管道段数。"""
    out = []
    ps, pv = C.placeholders(src), C.placeholders(val)
    if ps != pv:
        out.append(C.finding("placeholder-mismatch", "red", ns, key, lang, val,
                             f"占位符应为 {{{'}, {'.join(ps)}}}，实际 {{{'}, {'.join(pv)}}}" if pv else f"占位符应为 {ps}，实际无",
                             expected=ps, actual=pv))
    ns_, nv = len(C.pipe_segments(src)), len(C.pipe_segments(val))
    if ns_ != nv:
        out.append(C.finding("pipe-mismatch", "red", ns, key, lang, val, f"管道段数应为 {ns_}（基准），实际 {nv}",
                             expected=ns_, actual=nv))
    return out


def check_text(ns, key, lang, val, cfg):
    """单条文本自身的规则：残留与预算。"""
    out = []
    if lang not in cfg["han_allowed_langs"]:
        han = sorted({c for c in val if C.is_han(c)})
        if han:
            out.append(C.finding("han-residue", "red", ns, key, lang, val, f"残留汉字 {''.join(han)}，应翻译"))
    if lang in cfg["fullwidth_langs"]:
        fw = sorted({c for c in val if c in FULLWIDTH_PUNCT})
        if fw:
            out.append(C.finding("fullwidth-punct", "red", ns, key, lang, val, f"全角标点 {' '.join(fw)} 应换成半角"))
    if lang in cfg["rtl_langs"]:
        runs = unwrapped_ltr_runs(val)
        if runs:
            out.append(C.finding("rtl-unwrapped-ltr", "yellow", ns, key, lang, val,
                                 f"LTR 片段 {', '.join(runs)} 未用 LRM/RLM 或 isolate（U+2066..2069）包裹，可能错位；只报不改"))
    ktype = C.classify_key(key, cfg["key_rules"])
    pair = cfg["budgets"].get(ktype)
    if pair:
        limit, w = C.budget_units(lang, pair), C.width(val)
        if w > limit:
            out.append(C.finding("over-budget", cfg["budget_severity"], ns, key, lang, val,
                                 f"{ktype} 预算 {C.width_label(lang, limit)}，现在 {C.width_label(lang, w)}，缩短 {C.width_label(lang, w - limit)}",
                                 key_type=ktype, width=w, limit=limit, over=round(w - limit, 2)))
    return out


def unwrapped_ltr_runs(text):
    """阿拉伯语字符串里未被方向控制符包裹的拉丁片段（占位符 {name} 本身不算）。"""
    masked = C.PLACEHOLDER_RE.sub(lambda m: " " * len(m.group(0)), text)
    runs = []
    for m in LTR_RUN_RE.finditer(masked):
        before = masked[m.start() - 1] if m.start() > 0 else ""
        after = masked[m.end()] if m.end() < len(masked) else ""
        if not (before in WRAP_OPEN and after in WRAP_CLOSE):
            runs.append(m.group(0))
    return runs


def lint(packs, cfg):
    out = []
    for ns in sorted(packs):
        out.extend(check_pack(ns, packs[ns], cfg))
    return C.sort_findings(out)


def markdown(findings, meta):
    doc = C.findings_markdown("QDTEXT 语言包体检", findings)
    over = sorted((f for f in findings if f["rule"] == "over-budget"), key=lambda f: -f["over"])
    doc += "\n## 超预算排行（按超出量）\n\n"
    if over:
        rows = [[f["namespace"], f["key"], f["lang"], f["key_type"], C.width_label(f["lang"], f["width"]),
                 C.width_label(f["lang"], f["limit"]), C.width_label(f["lang"], f["over"]), f["text"]] for f in over]
        doc += C.md_table(["命名空间", "键", "语言", "类型", "宽度", "预算", "超出", "原文"], rows)
    else:
        doc += "无。\n"
    by_rule = {}
    for f in findings:
        by_rule[f["rule"]] = by_rule.get(f["rule"], 0) + 1
    doc += "\n## 按规则\n\n" + C.md_table(["规则", "条数"], sorted(by_rule.items()))
    doc += f"\n输入：{meta['dir']}；命名空间 {meta['namespaces']}；基准键 {meta['base_keys']}；语言 {', '.join(meta['langs'])}\n"
    return doc


def run(locale_dir, out_dir, cfg, strict=False):
    packs = C.load_locale_dir(locale_dir, cfg["langs"])
    findings = lint(packs, cfg)
    meta = {"dir": os.path.abspath(locale_dir), "namespaces": len(packs), "langs": cfg["langs"],
            "base_keys": sum(len(b.get(cfg["base_lang"], {})) for b in packs.values())}
    line, code = C.status_line("LOCALE", findings, strict)
    C.write_json(os.path.join(out_dir, "locale_lint.json"),
                 {"tool": "locale_lint", "status": line, "counts": C.count_by_severity(findings), "meta": meta,
                  "findings": findings})
    C.write_text(os.path.join(out_dir, "locale_lint.md"), markdown(findings, meta))
    return line, code, findings


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("locale_dir")
    ap.add_argument("--out", default=os.path.join(C.RESULTS, "locale_lint"))
    ap.add_argument("--config", default=None, help="JSON：覆盖 langs / key_rules / budgets 等")
    ap.add_argument("--strict", action="store_true", help="黄项也算 FAIL")
    args = ap.parse_args(argv)
    if not os.path.isdir(args.locale_dir):
        print(f"QDTEXT LOCALE ERROR 目录不存在: {args.locale_dir}", file=sys.stderr)
        return 2
    try:
        cfg = C.load_config(args.config, DEFAULT_CONFIG)
        if not isinstance(cfg["budgets"], dict) or cfg["base_lang"] not in cfg["langs"]:
            raise ValueError("budgets 必须是对象且 base_lang 必须在 langs 内")
    except (OSError, ValueError, KeyError) as e:
        print(f"QDTEXT LOCALE ERROR 配置不合法: {e}", file=sys.stderr)
        return 2
    line, code, _ = run(args.locale_dir, args.out, cfg, args.strict)
    print(line)
    print(f"report: {os.path.join(args.out, 'locale_lint.md')}  json: {os.path.join(args.out, 'locale_lint.json')}")
    return code


if __name__ == "__main__":
    sys.exit(main())
