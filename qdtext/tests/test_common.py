import io
import os
import unittest
from contextlib import redirect_stdout

from qdtext import common as C
from qdtext.tests._util import tmpdir


class TestWidth(unittest.TestCase):
    def test_char_width_classes(self):
        self.assertEqual(C.char_width("中"), 1.0)
        self.assertEqual(C.char_width("ａ"), 1.0)                    # 全角拉丁也算宽
        self.assertEqual(C.char_width("a"), 0.5)
        self.assertEqual(C.char_width(" "), 0.0)
        self.assertEqual(C.char_width("́"), 0.0)                # 组合记号
        self.assertEqual(C.char_width("‎"), 0.0)                # 零宽（Cf）
        self.assertEqual(C.char_width("ั"), 0.0)                # 泰文上标元音（Mn）

    def test_width_mixed(self):
        self.assertEqual(C.width("下单 BTC"), 3.5)
        self.assertEqual(C.width("Create bot"), 4.5)
        self.assertEqual(C.width(""), 0.0)
        self.assertEqual(C.width("สวัสดี"), 2.0)                    # 6 码位，2 个是组合记号 → 4 × 0.5

    def test_budget_units_and_label(self):
        self.assertEqual(C.budget_units("zh", [4, 14]), 4.0)
        self.assertEqual(C.budget_units("ja", [4, 14]), 4.0)
        self.assertEqual(C.budget_units("en", [4, 14]), 7.0)
        self.assertEqual(C.width_label("zh", 4.5), "4.5 字")
        self.assertEqual(C.width_label("en", 7.0), "14 字符")

    def test_is_han(self):
        self.assertTrue(C.is_han("中"))
        self.assertTrue(C.is_han("㐀"))
        self.assertTrue(C.is_han("豈"))                              # 兼容区
        self.assertTrue(C.is_han("\U00020000"))
        for ch in ("\u4e00", "\u9fff", "\u3400", "\u4dbf", "\uf900", "\ufaff"):               # 区段两端
            self.assertTrue(C.is_han(ch), hex(ord(ch)))
        for ch in ("\u4dff", "\u33ff", "\ua000", "\uf8ff", "\ufb00"):
            self.assertFalse(C.is_han(ch), hex(ord(ch)))
        self.assertFalse(C.is_han("、"))
        self.assertFalse(C.is_han("あ"))
        self.assertFalse(C.is_han("한"))
        self.assertFalse(C.is_han("a"))


class TestPlaceholders(unittest.TestCase):
    def test_placeholders_sorted_unique(self):
        self.assertEqual(C.placeholders("{b} {a} {a} {0} { c } {x.y}"), ["0", "a", "b", "c", "x.y"])
        self.assertEqual(C.placeholders("{} {-} 无"), [])

    def test_pipe_segments(self):
        self.assertEqual(C.pipe_segments("one | many"), ["one", "many"])
        self.assertEqual(C.pipe_segments("a|b|c"), ["a", "b", "c"])
        self.assertEqual(C.pipe_segments("none"), ["none"])


class TestClassify(unittest.TestCase):
    def test_default_rules(self):
        cases = {"a.submitBtn": "button", "x.okButton": "button", "x.primaryAction": "button", "x.pageTitle": "label",
                 "x.nameLabel": "label", "x.allTab": "label", "x.runningChip": "status", "x.status": "status", "x.newBadge": "status",
                 "x.feeHint": "desc", "x.note": "desc", "x.desc": "desc", "x.intro": "desc", "x.tip": "desc", "x.help": "desc",
                 "x.disclaimer": "desc", "x.msg": "body", "plain": "body", "x.count": "body"}
        for key, want in cases.items():
            self.assertEqual(C.classify_key(key), want, key)

    def test_precedence_and_last_segment_only(self):
        self.assertEqual(C.classify_key("btn.group.title"), "label")          # 只看最后一段
        self.assertEqual(C.classify_key("x.statusHint"), "status")            # 默认顺序 status 先于 desc
        self.assertEqual(C.classify_key("x.statusHint", [["desc", ["hint"]], ["status", ["status"]]]), "desc")
        self.assertEqual(C.classify_key("x.anything", [["desc", ["hint"]]]), "body")


class TestFlatten(unittest.TestCase):
    def test_roundtrip_nested(self):
        nested = {"a": {"b": "1", "c": {"d": "2"}}, "e": "3"}
        flat = C.flatten(nested)
        self.assertEqual(flat, {"a.b": "1", "a.c.d": "2", "e": "3"})
        self.assertEqual(C.unflatten(flat), nested)

    def test_arrays_and_scalars(self):
        self.assertEqual(C.flatten({"a": ["x", "y"], "n": 3, "t": True}), {"a.0": "x", "a.1": "y", "n": 3, "t": True})
        self.assertEqual(C.flatten(["x"]), {"0": "x"})
        self.assertEqual(C.flatten("leaf"), {"": "leaf"})

    def test_parse_locale_filename(self):
        self.assertEqual(C.parse_locale_filename("common.zh-TW.json"), ("common", "zh-TW"))
        self.assertEqual(C.parse_locale_filename("a.b.en.json"), ("a.b", "en"))
        self.assertIsNone(C.parse_locale_filename("en.json"))
        self.assertIsNone(C.parse_locale_filename("common.en.txt"))
        self.assertIsNone(C.parse_locale_filename(".en.json"))
        self.assertIsNone(C.parse_locale_filename("x..json"))

    def test_load_and_write_locale_dir(self):
        d = tmpdir(self)
        packs = {"app": {"zh": {"a.b": "你好", "c": "再见"}, "en": {"a.b": "hi", "c": "bye"}, "xx": {"a.b": "?"}},
                 "bot": {"zh": {"k": "v"}}}
        C.write_locale_dir(d, packs)
        with open(os.path.join(d, "notes.txt"), "w") as f:
            f.write("ignored")
        loaded = C.load_locale_dir(d)
        self.assertEqual(loaded, {"app": {"zh": {"a.b": "你好", "c": "再见"}, "en": {"a.b": "hi", "c": "bye"}}, "bot": {"zh": {"k": "v"}}})
        self.assertEqual(C.load_locale_dir(d, ("en",)), {"app": {"en": {"a.b": "hi", "c": "bye"}}})
        with open(os.path.join(d, "app.zh.json"), encoding="utf-8") as f:
            self.assertIn('"a": {', f.read())                                   # 写回去是嵌套的


class TestConfigAndReport(unittest.TestCase):
    def test_load_config_override(self):
        d = tmpdir(self)
        path = os.path.join(d, "cfg.json")
        C.write_json(path, {"b": 2, "c": {"x": 1}})
        cfg = C.load_config(path, {"a": 1, "b": 1, "c": {"y": 2}})
        self.assertEqual(cfg, {"a": 1, "b": 2, "c": {"x": 1}})
        defaults = {"a": [1]}
        cfg2 = C.load_config(None, defaults)
        cfg2["a"].append(2)
        self.assertEqual(defaults, {"a": [1]})                               # 默认值不被改

    def test_status_line(self):
        f = [C.finding("r", "red", "ns", "k", "en", "t", "s"), C.finding("r", "yellow", "ns", "k", "en", "t", "s")]
        self.assertEqual(C.status_line("LOCALE", f), ("QDTEXT LOCALE FAIL n=2 red=1 yellow=1", 1))
        self.assertEqual(C.status_line("LOCALE", f[1:]), ("QDTEXT LOCALE OK n=1 red=0 yellow=1", 0))
        self.assertEqual(C.status_line("LOCALE", f[1:], strict=True), ("QDTEXT LOCALE FAIL n=1 red=0 yellow=1", 1))
        self.assertEqual(C.status_line("X", []), ("QDTEXT X OK n=0 red=0 yellow=0", 0))

    def test_sort_and_counts(self):
        f = [C.finding("b", "yellow", "ns", "k2", "en", "t", "s"), C.finding("a", "red", "ns", "k9", None, "t", "s"),
             C.finding("c", "info", "ns", "k1", "en", "t", "s"), C.finding("a", "red", "ns", "k1", "zh", "t", "s")]
        s = C.sort_findings(f)
        self.assertEqual([(x["severity"], x["key"]) for x in s], [("red", "k1"), ("red", "k9"), ("yellow", "k2"), ("info", "k1")])
        self.assertEqual(C.count_by_severity(f), {"red": 2, "yellow": 1, "info": 1})
        g = [C.finding("a", "red", "ns", "k", "zh", "t", "s"), C.finding("a", "red", "ns", "k", "en", "t", "s"),
             C.finding("a", "red", "ns", "k", None, "t", "s")]
        self.assertEqual([x["lang"] for x in C.sort_findings(g)], [None, "en", "zh"])        # 同键按语言排，None 当空串

    def test_markdown(self):
        rows = [["a|b", "x\ny"]]
        md = C.md_table(["h1", "h2"], rows)
        self.assertIn("| a\\|b | x⏎y |", md)
        self.assertTrue(md.startswith("| h1 | h2 |\n|---|---|\n"))
        f = [C.finding("r", "red", "ns", "k", None, "t", "s", screen="S")]
        doc = C.findings_markdown("标题", f, extra_cols=("screen",))
        self.assertIn("# 标题", doc)
        self.assertIn("| red | ns | k | - | r | t | s | S |", doc)
        self.assertIn("无发现", C.findings_markdown("t", []))

    def test_emit_writes_files(self):
        d = tmpdir(self)
        with redirect_stdout(io.StringIO()) as out:
            code = C.emit("demo", [C.finding("r", "yellow", "ns", "k", "en", "t", "s")], d, "标题", meta={"x": 1})
        self.assertEqual(code, 0)
        self.assertTrue(out.getvalue().startswith("QDTEXT DEMO OK n=1 red=0 yellow=1\nreport: "))
        doc = C.read_json(os.path.join(d, "demo.json"))
        self.assertEqual(doc["counts"]["yellow"], 1)
        self.assertEqual(doc["meta"], {"x": 1})
        self.assertTrue(os.path.exists(os.path.join(d, "demo.md")))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(C.emit("demo", [C.finding("r", "red", "ns", "k", "en", "t", "s")], d, "标题"), 1)

    def test_audit_hook_off_without_env(self):
        self.assertFalse(C.install_audit_hook()) if "QDTEXT_AUDIT_LOG" not in os.environ else None

    def test_mkdtemp_prefix(self):
        d = C.mkdtemp("x-")
        self.addCleanup(os.rmdir, d)
        self.assertIn("qdtext-x-", os.path.basename(d))


if __name__ == "__main__":
    unittest.main()


class TestAuditHookInProcess(unittest.TestCase):
    """钩子装上后（本进程此后一直带着它，只多写日志），写 / 建目录 / 删 / 改名事件都应落到日志，含 dir_fd 的相对名也能还原。"""

    def test_events_logged(self):
        import json
        import shutil
        d = tmpdir(self)
        log = os.path.join(d, "audit.log")
        os.environ["QDTEXT_AUDIT_LOG"] = log
        try:
            self.assertTrue(C.install_audit_hook())
        finally:
            del os.environ["QDTEXT_AUDIT_LOG"]
        sub = os.path.join(d, "sub")
        os.mkdir(sub)
        with open(os.path.join(sub, "a.txt"), "w") as f:
            f.write("x")
        with open(os.path.join(sub, "a.txt")) as f:                       # 只读不记
            f.read()
        os.rename(os.path.join(sub, "a.txt"), os.path.join(sub, "b.txt"))
        shutil.rmtree(sub)                                                 # 走 dir_fd 的 unlink / rmdir
        with open(log, encoding="utf-8") as f:
            events = [json.loads(line) for line in f]
        kinds = {e["event"] for e in events}
        self.assertTrue({"write", "os.mkdir", "os.rename"} <= kinds, kinds)
        self.assertTrue(any(e["event"] in ("os.rmdir", "os.unlink", "os.remove", "shutil.rmtree") for e in events))
        for e in events:
            self.assertTrue(os.path.isabs(e["target"]) or e["target"].startswith("<unresolved"), e)
        self.assertTrue(all(e["pid"] == os.getpid() for e in events))
        self.assertEqual(C._fd_path(-1)[:11], "<unresolved")
