"""G4 手机版式体检：本地静态站 + 页面路径，在 375×812 / 414×896 下用无头 Chromium 取所有可见文字盒子并判定。

    python3.12 -m qdtext.mobile_layout <站点目录> --pages index.html overlap.html [--sizes 375x812 414x896]
                                       [--out qdtext/results/mobile_layout] [--snapshot-out boxes.json] [--config cfg.json]
    python3.12 -m qdtext.mobile_layout --snapshot boxes.json [--out ...]        # 离线：不起浏览器，直接吃盒子快照

判定（规则 / 级别）：
- text-overlap 红：不同文本节点的行盒子两两相交（相交宽高都 > 1px），报两段文字和坐标；
- horizontal-overflow 红：document.scrollWidth > 设备宽度（横向滚动；手机模拟下 Chrome 会把 layout viewport 撑大并整体缩小，
  所以不能拿 innerWidth 比）；
- touch-target 黄：button / a[href] / [role=button] / input[type=button|submit] 的盒子 < 44×44（正文里的行内链接默认不算，
  与 WCAG 2.5.8 的例外一致，可配 skip_inline_links=false）；
- first-screen-text 黄：首屏（y < innerHeight）可见文字字宽总和（CJK 1、其余 0.5）> 预算（默认 120），列出最长 5 段；
- no-viewport-meta 黄：没有 viewport meta，页面按桌面宽度排版再缩放。
找不到 Chromium：stdout 第一行 `QDTEXT MOBILE SKIP reason=...`，退出码 3（不是通过）。快照 JSON 格式见 snapshot_page 的返回值。
"""
import argparse
import os
import sys
import time

from qdtext import cdp
from qdtext import common as C

DEFAULT_CONFIG = {
    "sizes": [[375, 812], [414, 896]],
    "overlap_tolerance_px": 1.0,
    "touch_min_px": 44,
    "first_screen_budget": 120,
    "first_screen_top_n": 5,
    "skip_inline_links": True,
}
EXTRACT_JS = r"""
(() => {
  const skip = new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','HEAD','TITLE','META','LINK','SVG']);
  const path = el => { const parts = []; while (el && el.nodeType === 1 && parts.length < 5) {
      let s = el.tagName.toLowerCase(); if (el.id) s += '#' + el.id; else if (el.classList.length) s += '.' + [...el.classList].slice(0, 2).join('.');
      parts.unshift(s); el = el.parentElement; } return parts.join(' > '); };
  const visible = el => { for (let e = el; e && e.nodeType === 1; e = e.parentElement) { const cs = getComputedStyle(e);
      if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) === 0) return false; } return true; };
  const r4 = r => [Math.round(r.left * 100) / 100, Math.round(r.top * 100) / 100, Math.round(r.width * 100) / 100, Math.round(r.height * 100) / 100];
  const texts = []; const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT); let n, id = 0;
  while ((n = walker.nextNode())) { const t = n.nodeValue.replace(/\s+/g, ' ').trim(); if (!t) continue; const p = n.parentElement;
      if (!p || skip.has(p.tagName) || !visible(p)) continue; const range = document.createRange(); range.selectNodeContents(n);
      const rects = [...range.getClientRects()].filter(r => r.width > 0 && r.height > 0).map(r4); if (!rects.length) continue;
      texts.push({id: id++, text: t.slice(0, 120), path: path(p), rects}); }
  const clickables = [];
  document.querySelectorAll('button, a[href], [role=button], input[type=button], input[type=submit], [onclick]').forEach(el => {
      if (!visible(el)) return; const r = el.getBoundingClientRect(); if (r.width <= 0 || r.height <= 0) return; const cs = getComputedStyle(el);
      const own = (el.innerText || el.value || el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
      const inline = el.tagName === 'A' && cs.display === 'inline' && !!el.parentElement && el.parentElement.innerText.replace(/\s+/g, ' ').trim().length > own.length;
      clickables.push({tag: el.tagName.toLowerCase(), text: own.slice(0, 60), path: path(el), rect: r4(r), inline}); });
  const de = document.documentElement;
  return {inner: {width: innerWidth, height: innerHeight}, scroll: {width: Math.max(de.scrollWidth, document.body.scrollWidth),
          height: Math.max(de.scrollHeight, document.body.scrollHeight)}, viewport_meta: !!document.querySelector('meta[name=viewport]'),
          title: document.title, texts, clickables};
})()
"""


# ---- 判定（纯函数，离线可测） ----------------------------------------------------------------
def intersect(a, b, tol):
    """两个 [x, y, w, h] 的相交区域宽高都 > tol 才算重叠。"""
    x = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    y = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    return x > tol and y > tol


def overlaps(snap, cfg):
    out, texts = [], snap["texts"]
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            a, b = texts[i], texts[j]
            hit = next(((ra, rb) for ra in a["rects"] for rb in b["rects"] if intersect(ra, rb, cfg["overlap_tolerance_px"])), None)
            if hit:
                out.append({"a": a, "b": b, "rect_a": hit[0], "rect_b": hit[1]})
    return out


def first_screen(snap, cfg):
    """首屏文字：每个文本节点按落在视口内的行数比例计宽；→ (总宽, 最长 N 段)。"""
    h = snap["inner"]["height"]
    total, segs = 0.0, []
    for t in snap["texts"]:
        inside = sum(1 for r in t["rects"] if r[1] < h and r[1] + r[3] > 0)
        if inside:
            w = C.width(t["text"]) * inside / len(t["rects"])
            total += w
            segs.append((w, t))
    segs.sort(key=lambda s: -s[0])
    return total, segs[:cfg["first_screen_top_n"]]


def judge(snap, cfg):
    """一个页面 × 一个尺寸的快照 → 发现列表。"""
    page, size = snap["page"], f"{snap['viewport']['width']}x{snap['viewport']['height']}"
    out = []
    for o in overlaps(snap, cfg):
        out.append(C.finding("text-overlap", "red", page, size, "-", f"「{o['a']['text'][:40]}」 × 「{o['b']['text'][:40]}」",
                             f"文字重叠：A {o['a']['path']} @ {o['rect_a']}，B {o['b']['path']} @ {o['rect_b']}",
                             rect_a=o["rect_a"], rect_b=o["rect_b"], path_a=o["a"]["path"], path_b=o["b"]["path"]))
    sw, iw, dw = snap["scroll"]["width"], snap["inner"]["width"], snap["viewport"]["width"]
    if sw > dw + 1:
        how = "手机会横向滚动" if sw > iw + 1 else "手机会整体缩小显示（layout viewport 被内容撑到 %dpx）" % iw
        out.append(C.finding("horizontal-overflow", "red", page, size, "-", f"scrollWidth {sw} > 设备宽 {dw}",
                             f"页面横向溢出 {sw - dw}px，{how}；找宽度写死 / 不换行的元素", scroll_width=sw, inner_width=iw,
                             device_width=dw))
    if not snap.get("viewport_meta", True):
        out.append(C.finding("no-viewport-meta", "yellow", page, size, "-", f"innerWidth {iw}，设备宽 {dw}",
                             "缺 <meta name=viewport content=\"width=device-width\">，页面按桌面宽度排版再缩放"))
    m = cfg["touch_min_px"]
    for c in snap["clickables"]:
        if cfg["skip_inline_links"] and c.get("inline"):
            continue
        w, h = c["rect"][2], c["rect"][3]
        if w < m or h < m:
            out.append(C.finding("touch-target", "yellow", page, size, "-", f"<{c['tag']}> 「{c['text'][:30]}」 {w:g}×{h:g}",
                                 f"触控区小于 {m}×{m}：{c['path']} @ {c['rect']}", rect=c["rect"], path=c["path"]))
    total, top = first_screen(snap, cfg)
    if total > cfg["first_screen_budget"]:
        longest = "；".join(f"「{t['text'][:30]}」({w:g})" for w, t in top)
        out.append(C.finding("first-screen-text", "yellow", page, size, "-", f"首屏文字 {total:g} 字宽 > 预算 {cfg['first_screen_budget']}",
                             f"最长 {len(top)} 段：{longest}", total=round(total, 1), longest=[t["text"] for _w, t in top]))
    return out


# ---- 采集 ------------------------------------------------------------------------------------
def snapshot_page(browser, session, url, page, width, height):
    browser.navigate(session, url, width, height)
    data = browser.evaluate(session, EXTRACT_JS)
    data.update({"page": page, "url": url, "viewport": {"width": width, "height": height}})
    return data


def collect(site_dir, pages, sizes, exe=None):
    """→ (快照列表, 浏览器版本)。每页每尺寸一个快照；页面用 file:// 打开。"""
    snaps = []
    with cdp.Browser(exe) as browser:
        target, session = browser.new_page()
        for page in pages:
            path = os.path.abspath(os.path.join(site_dir, page))
            if not os.path.isfile(path):
                raise FileNotFoundError(path)
            for w, h in sizes:
                snaps.append(snapshot_page(browser, session, "file://" + path, page, w, h))
        browser.close_page(target)
        return snaps, browser.version


def run(snaps, out_dir, cfg, meta, strict=False):
    findings = []
    for s in snaps:
        findings.extend(judge(s, cfg))
    findings = C.sort_findings(findings)
    line, code = C.status_line("MOBILE", findings, strict)
    C.write_json(os.path.join(out_dir, "mobile_layout.json"),
                 {"tool": "mobile_layout", "status": line, "counts": C.count_by_severity(findings), "meta": meta, "findings": findings})
    doc = C.findings_markdown("QDTEXT 手机版式体检", findings)
    doc += "\n| 页面 | 尺寸 | 文本节点 | 可点元素 | scrollWidth | 首屏字宽 |\n|---|---|---|---|---|---|\n"
    for s in snaps:
        doc += (f"| {s['page']} | {s['viewport']['width']}x{s['viewport']['height']} | {len(s['texts'])} | {len(s['clickables'])} | "
                f"{s['scroll']['width']} | {first_screen(s, cfg)[0]:g} |\n")
    doc += (f"\n模式：{meta.get('mode')}；浏览器：{meta.get('browser', '-')}；页面 {meta.get('pages')}；尺寸 {meta.get('sizes')}"
            + (f"；快照 {meta['snapshot']}" if meta.get("snapshot") else "") + "\n")
    C.write_text(os.path.join(out_dir, "mobile_layout.md"), doc)
    return line, code, findings


def parse_sizes(items):
    out = []
    for s in items:
        w, h = s.lower().split("x")
        out.append([int(w), int(h)])
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("site_dir", nargs="?")
    ap.add_argument("--pages", nargs="*", default=["index.html"])
    ap.add_argument("--sizes", nargs="*", default=None, help="如 375x812 414x896")
    ap.add_argument("--snapshot", default=None, help="离线：读盒子快照 JSON，不起浏览器")
    ap.add_argument("--snapshot-out", default=None, help="把采到的盒子快照写到这个 JSON")
    ap.add_argument("--out", default=os.path.join(C.RESULTS, "mobile_layout"))
    ap.add_argument("--config", default=None)
    ap.add_argument("--chrome", default=None, help="Chromium 可执行文件（默认自动查找 / QDTEXT_CHROME）")
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args(argv)
    cfg = C.load_config(args.config, DEFAULT_CONFIG)
    sizes = parse_sizes(args.sizes) if args.sizes else cfg["sizes"]
    if args.snapshot:
        doc = C.read_json(args.snapshot)
        snaps, meta = doc["snapshots"], dict(doc.get("meta", {}), mode="offline", snapshot=os.path.abspath(args.snapshot))
    else:
        if not args.site_dir or not os.path.isdir(args.site_dir):
            print(f"QDTEXT MOBILE ERROR 站点目录不存在: {args.site_dir}", file=sys.stderr)
            return 2
        exe = args.chrome or cdp.find_chrome()
        if not exe:
            print("QDTEXT MOBILE SKIP reason=no chromium/chrome executable found (set QDTEXT_CHROME)")
            return 3
        t0 = time.perf_counter()
        snaps, version = collect(args.site_dir, args.pages, sizes, exe)
        meta = {"mode": "chromium", "browser": version, "chrome": exe, "site": os.path.abspath(args.site_dir), "pages": args.pages,
                "sizes": sizes, "seconds": round(time.perf_counter() - t0, 2)}
        if args.snapshot_out:
            C.write_json(args.snapshot_out, {"meta": meta, "snapshots": snaps})
    line, code, _ = run(snaps, args.out, cfg, meta, args.strict)
    print(line)
    print(f"report: {os.path.join(args.out, 'mobile_layout.md')}  json: {os.path.join(args.out, 'mobile_layout.json')}")
    return code


if __name__ == "__main__":
    sys.exit(main())
