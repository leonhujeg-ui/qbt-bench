"""qdtext 公共部分：路径、语言包读取（`<命名空间>.<语言>.json`）、键类型判定、字宽、发现（finding）、报告输出、审计钩子。

所有工具共享同一套约定，便于搬到真实前端仓时只改这里：
- 十种语言 LANGS，基准 zh；CJK 语种（zh / zh-TW / ja / ko）按「中文预算」，其余按「拉丁预算」；
- 键类型由键名最后一段判定（含 btn/button/action → button；title/label/tab → label；chip/status/badge → status；
  hint/note/desc/intro/tip/help/disclaimer → desc；其余 → body），可用 JSON 配置覆盖（见 DEFAULT_KEY_RULES）；
- 字宽：CJK（东亚宽度 W/F）计 1，其余可见字符计 0.5，空白 / 组合记号 / 零宽字符计 0（拉丁预算的「字符数」不含空格）。
"""
import json
import os
import re
import sys
import tempfile
import unicodedata

PKG = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(PKG)
RESULTS = os.path.join(PKG, "results")
DATA = os.path.join(PKG, "data")
FIXTURES = os.path.join(PKG, "fixtures")

LANGS = ("zh", "zh-TW", "en", "vi", "th", "ja", "ko", "ar", "fr", "de")
BASE_LANG = "zh"
CJK_LANGS = frozenset({"zh", "zh-TW", "ja", "ko"})
HAN_ALLOWED_LANGS = frozenset({"zh", "zh-TW", "ja"})          # 其余语种出现汉字 = 残留

# (类型, 子串列表)，按顺序匹配键名最后一段（小写）；先命中先得
DEFAULT_KEY_RULES = [
    ["button", ["btn", "button", "action"]],
    ["label", ["title", "label", "tab"]],
    ["status", ["chip", "status", "badge"]],
    ["desc", ["hint", "note", "desc", "intro", "tip", "help", "disclaimer"]],
]
KEY_TYPES = ("button", "label", "status", "desc", "body")
# 类型 → [CJK 语种预算（字宽单位）, 拉丁语种预算（字符数）]；body 不设预算
DEFAULT_BUDGETS = {"button": [4, 14], "status": [4, 12], "label": [8, 24], "desc": [24, 60]}

PLACEHOLDER_RE = re.compile(r"\{\s*([A-Za-z_][\w.]*|\d+)\s*\}")
PIPE_RE = re.compile(r"\s*\|\s*")
SEVERITY_ORDER = {"red": 0, "yellow": 1, "info": 2}


# ---- 文本度量 -------------------------------------------------------------------------
def is_han(ch):
    cp = ord(ch)
    return (0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or 0xF900 <= cp <= 0xFAFF
            or 0x20000 <= cp <= 0x2FFFF)


def char_width(ch):
    """CJK 1、其余可见字符 0.5、空白 / 组合记号 / 零宽字符 0。"""
    if ch.isspace() or unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0.0
    return 1.0 if unicodedata.east_asian_width(ch) in ("W", "F") else 0.5


def width(text):
    return sum(char_width(c) for c in text)


def budget_units(lang, budget_pair):
    """某语种的预算换成字宽单位：CJK 语种取第一列；拉丁语种第二列是字符数，每字符 0.5。"""
    return float(budget_pair[0]) if lang in CJK_LANGS else budget_pair[1] * 0.5


def width_label(lang, units):
    """给人看的宽度：CJK 语种报「字」，其余报「字符」（单位 ×2）。"""
    return f"{units:g} 字" if lang in CJK_LANGS else f"{units * 2:g} 字符"


def placeholders(text):
    return sorted(set(PLACEHOLDER_RE.findall(text)))


def pipe_segments(text):
    return PIPE_RE.split(text) if "|" in text else [text]


# ---- 键类型 ----------------------------------------------------------------------------
def classify_key(key, rules=None):
    last = key.rsplit(".", 1)[-1].lower()
    for ktype, needles in (rules or DEFAULT_KEY_RULES):
        if any(n in last for n in needles):
            return ktype
    return "body"


# ---- 语言包读取 ------------------------------------------------------------------------
def flatten(obj, prefix=""):
    """嵌套 JSON → {扁平键: 叶子}；数组下标也当一段。叶子原样返回（非字符串由调用方判定）。"""
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(flatten(v, f"{prefix}.{i}" if prefix else str(i)))
    else:
        out[prefix] = obj
    return out


def parse_locale_filename(name):
    """'common.zh-TW.json' → ('common', 'zh-TW')；不符合约定返回 None。"""
    if not name.endswith(".json"):
        return None
    parts = name[:-5].rsplit(".", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1]


def load_locale_dir(path, langs=LANGS):
    """目录 → {命名空间: {语言: {扁平键: 叶子}}}；只读文件名符合 `<ns>.<lang>.json` 且语言在 langs 内的文件。"""
    packs = {}
    for name in sorted(os.listdir(path)):
        parsed = parse_locale_filename(name)
        if parsed is None or parsed[1] not in langs:
            continue
        ns, lang = parsed
        with open(os.path.join(path, name), encoding="utf-8") as f:
            packs.setdefault(ns, {})[lang] = flatten(json.load(f))
    return packs


def write_locale_dir(path, packs):
    """{ns: {lang: {扁平键: str}}} → `<ns>.<lang>.json`（嵌套回去，键排序）。"""
    os.makedirs(path, exist_ok=True)
    for ns, by_lang in packs.items():
        for lang, flat in by_lang.items():
            write_json(os.path.join(path, f"{ns}.{lang}.json"), unflatten(flat))


def unflatten(flat):
    root = {}
    for key in sorted(flat):
        node = root
        parts = key.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = flat[key]
    return root


# ---- 配置 / JSON ------------------------------------------------------------------------
def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")


def write_text(path, text):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def load_config(path, defaults):
    """JSON 配置覆盖默认值（顶层键逐个替换，不做深合并）；path 为空 → 默认值副本。"""
    cfg = json.loads(json.dumps(defaults))
    if path:
        cfg.update(read_json(path))
    return cfg


def data_file(name):
    return read_json(os.path.join(DATA, name))


def mkdtemp(suffix=""):
    return tempfile.mkdtemp(prefix="qdtext-" + suffix)


# ---- 发现与报告 ------------------------------------------------------------------------
def finding(rule, severity, namespace, key, lang, text, suggestion, **extra):
    d = {"namespace": namespace, "key": key, "lang": lang, "rule": rule, "severity": severity,
         "text": text, "suggestion": suggestion}
    d.update(extra)
    return d


def count_by_severity(findings):
    c = {"red": 0, "yellow": 0, "info": 0}
    for f in findings:
        c[f["severity"]] = c.get(f["severity"], 0) + 1
    return c


def status_line(tool, findings, strict=False):
    """stdout 第一行：`QDTEXT <TOOL> OK|FAIL n=<总数> red=<红> yellow=<黄>`；--strict 时黄也算 FAIL。"""
    c = count_by_severity(findings)
    fail = c["red"] > 0 or (strict and c["yellow"] > 0)
    return (f"QDTEXT {tool} {'FAIL' if fail else 'OK'} n={len(findings)} red={c['red']} yellow={c['yellow']}",
            1 if fail else 0)


def sort_findings(findings):
    return sorted(findings, key=lambda f: (SEVERITY_ORDER.get(f["severity"], 9), f["namespace"], f["key"],
                                           f["lang"] or "", f["rule"]))


def md_escape(s):
    return str(s).replace("|", "\\|").replace("\n", "⏎")


def md_table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(md_escape(c) for c in row) + " |" for row in rows]
    return "\n".join(out) + "\n"


def findings_markdown(title, findings, extra_cols=()):
    lines = [f"# {title}", ""]
    c = count_by_severity(findings)
    lines.append(f"共 {len(findings)} 条：红 {c['red']}，黄 {c['yellow']}，提示 {c['info']}。")
    lines.append("")
    headers = ["级别", "命名空间", "键", "语言", "规则", "原文", "建议", *extra_cols]
    rows = [[f["severity"], f["namespace"], f["key"], f["lang"] or "-", f["rule"], f["text"], f["suggestion"],
             *[f.get(c, "") for c in extra_cols]] for f in sort_findings(findings)]
    lines.append(md_table(headers, rows) if rows else "无发现。\n")
    return "\n".join(lines)


def emit(tool, findings, out_dir, title, extra_cols=(), strict=False, meta=None):
    """写 <out_dir>/<tool>.json + <tool>.md，打印首行与摘要，返回退出码。"""
    findings = sort_findings(findings)
    line, code = status_line(tool.upper(), findings, strict)
    doc = {"tool": tool, "status": line, "counts": count_by_severity(findings), "findings": findings}
    if meta:
        doc["meta"] = meta
    write_json(os.path.join(out_dir, f"{tool}.json"), doc)
    write_text(os.path.join(out_dir, f"{tool}.md"), findings_markdown(title, findings, extra_cols))
    print(line)
    print(f"report: {os.path.join(out_dir, tool + '.md')}  json: {os.path.join(out_dir, tool + '.json')}")
    return code


# ---- 审计钩子（零出网 / 零写仓外 判据用） ------------------------------------------------
def _fd_path(fd):
    """目录 fd → 路径（Linux 走 /proc；解析不了就标出来，审计判据按「不放行」处理）。"""
    try:
        return os.readlink(f"/proc/self/fd/{int(fd)}")
    except (OSError, ValueError):
        return f"<unresolved dir_fd={fd}>"


def install_audit_hook():
    """QDTEXT_AUDIT_LOG 指向文件时，把 open(写) / rename / remove / mkdir / socket.connect 事件按行写进去（JSON）。
    子进程通过环境变量继承。日志用 os.write 直写，避免再触发 open 事件。"""
    log = os.environ.get("QDTEXT_AUDIT_LOG")
    if not log:
        return False
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    pid = os.getpid()

    def hook(event, args):
        target = None
        if event == "open":
            mode = args[1] or ""
            if any(ch in mode for ch in "wax+") and isinstance(args[0], (str, bytes)):
                target = os.fsdecode(args[0])
        elif event in ("os.rename", "os.remove", "os.rmdir", "os.mkdir", "os.unlink", "shutil.rmtree"):
            target = os.fsdecode(args[0]) if isinstance(args[0], (str, bytes)) else repr(args[0])
            dir_fd = {"os.rename": 2, "os.mkdir": 2, "os.remove": 1, "os.rmdir": 1, "os.unlink": 1, "shutil.rmtree": 1}[event]
            if len(args) > dir_fd and args[dir_fd] is not None and not os.path.isabs(target):
                target = os.path.join(_fd_path(args[dir_fd]), target)             # shutil.rmtree 用 dir_fd 传相对名
        elif event == "socket.connect":
            addr = args[1]
            target = list(addr) if isinstance(addr, tuple) else str(addr)
        if target is not None:
            rec = json.dumps({"pid": pid, "event": "write" if event == "open" else event, "target": target},
                             ensure_ascii=False)
            os.write(fd, (rec + "\n").encode("utf-8"))

    sys.addaudithook(hook)
    return True


install_audit_hook()
