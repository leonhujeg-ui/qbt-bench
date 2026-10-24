import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout

from qdtext import cdp
from qdtext import common as C
from qdtext import mobile_layout as M
from qdtext.tests._util import tmpdir

CFG = C.load_config(None, M.DEFAULT_CONFIG)
SNAP_FILE = os.path.join(C.FIXTURES, "snapshots", "site.json")


def snap(texts=(), clickables=(), inner=(375, 812), scroll=(375, 1200), viewport=(375, 812), meta=True, page="p.html"):
    return {"page": page, "url": "file:///p.html", "viewport": {"width": viewport[0], "height": viewport[1]},
            "inner": {"width": inner[0], "height": inner[1]}, "scroll": {"width": scroll[0], "height": scroll[1]},
            "viewport_meta": meta, "texts": [dict(t) for t in texts], "clickables": [dict(c) for c in clickables]}


def text(id_, s, *rects, path="p"):
    return {"id": id_, "text": s, "path": path, "rects": [list(r) for r in rects]}


class TestGeometry(unittest.TestCase):
    def test_intersect(self):
        self.assertTrue(M.intersect([0, 0, 10, 10], [5, 5, 10, 10], 1.0))
        self.assertFalse(M.intersect([0, 0, 10, 10], [10, 0, 10, 10], 1.0))              # 贴边
        self.assertFalse(M.intersect([0, 0, 10, 10], [9.5, 0, 10, 10], 1.0))             # 0.5px 在容差内
        self.assertTrue(M.intersect([0, 0, 10, 10], [9.5, 0, 10, 10], 0.0))
        self.assertFalse(M.intersect([0, 0, 10, 10], [0, 12, 10, 10], 1.0))

    def test_overlaps_only_between_different_nodes(self):
        a = text(0, "A", (0, 0, 50, 20), (0, 20, 50, 20))                                 # 同节点两行
        b = text(1, "B", (40, 10, 50, 20))
        c = text(2, "C", (200, 0, 50, 20))
        o = M.overlaps(snap([a, b, c]), CFG)
        self.assertEqual([(x["a"]["id"], x["b"]["id"]) for x in o], [(0, 1)])
        self.assertEqual(o[0]["rect_a"], [0, 0, 50, 20])

    def test_first_screen(self):
        t1 = text(0, "一二三四", (0, 0, 10, 10), (0, 900, 10, 10))                         # 两行只有一行在首屏 → 计一半
        t2 = text(1, "abcd", (0, 100, 10, 10))
        t3 = text(2, "看不见", (0, 2000, 10, 10))
        total, top = M.first_screen(snap([t1, t2, t3]), dict(CFG, first_screen_top_n=1))
        self.assertEqual(total, 4.0)
        self.assertEqual([(w, t["id"]) for w, t in top], [(2.0, 0)])


class TestJudge(unittest.TestCase):
    def test_overlap_finding(self):
        s = snap([text(0, "63,412.50", (32, 116, 111, 26), path="span.num"), text(1, "执行异常", (48, 120, 120, 15), path="span.chip")])
        f = M.judge(s, CFG)
        self.assertEqual([(x["rule"], x["severity"], x["namespace"], x["key"]) for x in f], [("text-overlap", "red", "p.html", "375x812")])
        self.assertEqual(f[0]["text"], "「63,412.50」 × 「执行异常」")
        self.assertIn("span.num @ [32, 116, 111, 26]", f[0]["suggestion"])
        self.assertEqual(f[0]["rect_b"], [48, 120, 120, 15])

    def test_overflow_variants(self):
        f = M.judge(snap(scroll=(576, 900)), CFG)
        self.assertEqual([(x["rule"], x["severity"]) for x in f], [("horizontal-overflow", "red")])
        self.assertIn("横向滚动", f[0]["suggestion"])
        self.assertEqual((f[0]["scroll_width"], f[0]["device_width"]), (576, 375))
        f = M.judge(snap(scroll=(576, 900), inner=(576, 1247)), CFG)
        self.assertEqual([x["rule"] for x in f], ["horizontal-overflow"])
        self.assertIn("整体缩小显示", f[0]["suggestion"])
        self.assertEqual(M.judge(snap(scroll=(376, 900)), CFG), [])                        # 1px 容差
        self.assertEqual([x["rule"] for x in M.judge(snap(scroll=(377, 900)), CFG)], ["horizontal-overflow"])

    def test_viewport_meta(self):
        f = M.judge(snap(meta=False, inner=(980, 2121)), CFG)
        self.assertEqual([(x["rule"], x["severity"]) for x in f], [("no-viewport-meta", "yellow")])
        self.assertIn("innerWidth 980", f[0]["text"])
        self.assertEqual(M.judge(snap(meta=True), CFG), [])

    def test_touch_targets(self):
        cl = [{"tag": "button", "text": "筛", "path": "button", "rect": [0, 0, 28, 28], "inline": False},
              {"tag": "a", "text": "详情", "path": "a", "rect": [0, 0, 60, 14], "inline": True},
              {"tag": "a", "text": "大", "path": "a.btn", "rect": [0, 0, 44, 44], "inline": False},
              {"tag": "button", "text": "宽扁", "path": "b", "rect": [0, 0, 100, 43.5], "inline": False}]
        f = M.judge(snap(clickables=cl), CFG)
        self.assertEqual([(x["rule"], x["severity"], x["text"]) for x in f],
                         [("touch-target", "yellow", "<button> 「筛」 28×28"), ("touch-target", "yellow", "<button> 「宽扁」 100×43.5")])
        f = M.judge(snap(clickables=cl), dict(CFG, skip_inline_links=False))
        self.assertEqual(len(f), 3)
        f = M.judge(snap(clickables=cl), dict(CFG, touch_min_px=28))
        self.assertEqual(len(f), 0)

    def test_first_screen_budget(self):
        texts = [text(i, "字" * 30, (0, i * 40, 300, 30)) for i in range(5)]               # 150 字宽
        f = M.judge(snap(texts), CFG)
        self.assertEqual([(x["rule"], x["severity"], x["total"]) for x in f], [("first-screen-text", "yellow", 150.0)])
        self.assertEqual(len(f[0]["longest"]), 5)
        self.assertIn("首屏文字 150 字宽 > 预算 120", f[0]["text"])
        self.assertEqual(M.judge(snap(texts[:4]), CFG), [])                                 # 120 不超
        self.assertEqual(len(M.judge(snap(texts), dict(CFG, first_screen_budget=149, first_screen_top_n=2))[0]["longest"]), 2)


class TestOfflineFixture(unittest.TestCase):
    def test_fixture_snapshot_expectations(self):
        doc = C.read_json(SNAP_FILE)
        self.assertEqual(len(doc["snapshots"]), 6)
        by = {}
        for s in doc["snapshots"]:
            by[(s["page"], s["viewport"]["width"])] = {f["rule"] for f in M.judge(s, CFG)}
        for w in (375, 414):
            self.assertEqual(by[("index.html", w)], set())
            self.assertEqual(by[("overlap.html", w)], {"text-overlap"})
            self.assertEqual(by[("overflow.html", w)], {"horizontal-overflow", "touch-target"})

    def test_run_and_main_offline(self):
        d = tmpdir(self)
        with redirect_stdout(io.StringIO()) as out:
            code = M.main(["--snapshot", SNAP_FILE, "--out", d])
        self.assertEqual(code, 1)
        self.assertTrue(out.getvalue().startswith("QDTEXT MOBILE FAIL n=8 red=4 yellow=4\n"), out.getvalue())
        doc = C.read_json(os.path.join(d, "mobile_layout.json"))
        self.assertEqual(doc["meta"]["mode"], "offline")
        with open(os.path.join(d, "mobile_layout.md"), encoding="utf-8") as f:
            md = f.read()
        self.assertIn("| overflow.html | 375x812 |", md)
        self.assertIn("模式：offline", md)
        self.assertIn("快照 ", md)
        self.assertEqual(M.parse_sizes(["375x812", "414X896"]), [[375, 812], [414, 896]])
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(M.main([os.path.join(d, "nosite")]), 2)
        self.assertIn("站点目录不存在", err.getvalue())

    def test_main_skip_without_chrome(self):
        old = os.environ.get(cdp.CHROME_ENV)
        os.environ[cdp.CHROME_ENV] = "/nonexistent/chrome"
        try:
            with redirect_stdout(io.StringIO()) as out:
                code = M.main([os.path.join(C.FIXTURES, "site"), "--out", tmpdir(self)])
        finally:
            if old is None:
                del os.environ[cdp.CHROME_ENV]
            else:
                os.environ[cdp.CHROME_ENV] = old
        self.assertEqual(code, 3)
        self.assertTrue(out.getvalue().startswith("QDTEXT MOBILE SKIP reason=no chromium"))


@unittest.skipUnless(cdp.find_chrome(), "no chromium executable")
class TestRealBrowser(unittest.TestCase):
    def test_collect_fixture_site(self):
        d = tmpdir(self)
        with redirect_stdout(io.StringIO()) as out:
            code = M.main([os.path.join(C.FIXTURES, "site"), "--pages", "index.html", "overlap.html", "overflow.html", "--sizes", "375x812",
                           "--out", d, "--snapshot-out", os.path.join(d, "snap.json")])
        self.assertEqual(code, 1)
        self.assertTrue(out.getvalue().startswith("QDTEXT MOBILE FAIL n=4 red=2 yellow=2\n"), out.getvalue())
        doc = C.read_json(os.path.join(d, "snap.json"))
        self.assertEqual(doc["meta"]["mode"], "chromium")
        self.assertTrue(doc["meta"]["browser"].startswith(("Chrome/", "HeadlessChrome/")))
        pages = {s["page"]: s for s in doc["snapshots"]}
        self.assertEqual(pages["index.html"]["inner"], {"width": 375, "height": 812})
        self.assertGreater(pages["overflow.html"]["scroll"]["width"], 500)
        self.assertTrue(any(t["text"] == "63,412.50" for t in pages["overlap.html"]["texts"]))
        rules = {(f["namespace"], f["rule"]) for f in C.read_json(os.path.join(d, "mobile_layout.json"))["findings"]}
        self.assertEqual(rules, {("overlap.html", "text-overlap"), ("overflow.html", "horizontal-overflow"), ("overflow.html", "touch-target")})

    def test_missing_page_raises(self):
        with self.assertRaises(FileNotFoundError):
            M.collect(os.path.join(C.FIXTURES, "site"), ["nope.html"], [[375, 812]])


if __name__ == "__main__":
    unittest.main()
