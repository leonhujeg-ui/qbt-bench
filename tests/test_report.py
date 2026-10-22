"""报告判据：文本报告内容、SVG 结构（可解析 XML、点数、回撤面积、坐标轴）、文件写出、确定性。"""
import json
import math
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET

from qbt.broker import BrokerConfig
from qbt.data import generate
from qbt.engine import run_backtest
from qbt import metrics as M
from qbt import report as R
from strategies import REGISTRY
from tests._helpers import QBTTestCase, scale

SVG_NS = "{http://www.w3.org/2000/svg}"


class TestTextReport(QBTTestCase):
    def setUp(self):
        d = generate(n=scale(3000, 800), seed=2, bar_seconds=3600)
        self.r = run_backtest(d, REGISTRY["ma_cross"](fast=5, slow=20), BrokerConfig(max_participation=1.0))
        self.m = M.compute(self.r.equity, self.r.ts, self.r.bar_seconds, self.r.trades)

    def test_fmt(self):
        self.assertEqual(R.fmt(None, "pct"), "n/a")
        self.assertEqual(R.fmt(math.nan, "f2"), "n/a")
        self.assertEqual(R.fmt(math.inf, "int"), "n/a")
        self.assertEqual(R.fmt(0.1234, "pct"), "12.34%")
        self.assertEqual(R.fmt(7.0, "int"), "7")
        self.assertEqual(R.fmt(1.25, "f1"), "1.2")
        self.assertEqual(R.fmt(1.256, "f2"), "1.26")

    def test_text_report_content(self):
        txt = R.text_report(self.r, self.m, title="T")
        self.assertTrue(txt.startswith("T\n=\n"))
        self.assertIn('"fast": 5', txt)
        self.assertIn("期末权益", txt)
        from datetime import datetime, timezone
        d0, d1 = (datetime.fromtimestamp(x, tz=timezone.utc).strftime("%Y-%m-%d") for x in (self.r.ts[0], self.r.ts[-1]))
        self.assertIn(f"区间: {d0} .. {d1}", txt)
        daily = run_backtest(generate(n=40, seed=4, bar_seconds=86400), REGISTRY["ma_cross"](fast=3, slow=8),
                             BrokerConfig(max_participation=1.0))
        e0, e1 = (datetime.fromtimestamp(x, tz=timezone.utc).strftime("%Y-%m-%d") for x in (daily.ts[0], daily.ts[-1]))
        self.assertIn(f"区间: {e0} .. {e1}", R.text_report(daily))             # 日线：ts[1] 已是另一天，必须用 ts[0]
        self.assertIn(f"{self.r.final_equity:,.2f}", txt)
        for label, key, kind in R._METRIC_ROWS:
            self.assertIn(label, txt)
            self.assertIn(R.fmt(self.m.get(key), kind), txt)
        self.assertIn("月度收益", txt)
        self.assertEqual(txt.count("-" * 40), 2)
        self.assertNotIn("-" * 41, txt)
        self.assertIn(str(self.m["monthly"][0]["year"]), txt)
        self.assertEqual(R.text_report(self.r), R.text_report(self.r))         # 确定性
        auto = R.text_report(self.r)
        self.assertIn("qbt backtest report", auto)

    def test_monthly_table_layout(self):
        rows = [{"year": 2020, "month": 11, "ret": 0.1}, {"year": 2020, "month": 12, "ret": -0.05},
                {"year": 2021, "month": 1, "ret": 0.02}]
        table = R._monthly_table(rows)
        lines = table.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0].split()[1:], [str(k) for k in range(1, 13)])       # 表头恰为 1..12 月
        self.assertEqual(len(lines[1]), 6 + 12 * 7 + 11)                               # 12 个 7 宽单元格 + 11 个空格
        self.assertIn("  10.00", lines[1])
        self.assertIn("  -5.00", lines[1])
        self.assertTrue(lines[2].startswith("2021"))
        self.assertIn("   2.00", lines[2])
        self.assertEqual(R._monthly_table([]), "(无)")


class TestSvg(QBTTestCase):
    def _parse(self, svg):
        root = ET.fromstring(svg)
        self.assertEqual(root.tag, f"{SVG_NS}svg")
        return root

    def test_svg_structure(self):
        eq = [100.0, 110.0, 90.0, 120.0, 95.0, 130.0]
        ts = [1_600_000_000 + i * 86400 for i in range(6)]
        svg = R.equity_svg(eq, ts, width=500, height=300, title="demo")
        root = self._parse(svg)
        self.assertEqual((root.get("width"), root.get("height")), ("500", "300"))
        polyline = root.find(f"{SVG_NS}polyline")
        self.assertEqual(len(polyline.get("points").split()), 6)
        polygon = root.find(f"{SVG_NS}polygon")
        self.assertEqual(len(polygon.get("points").split()), 8)            # 两端闭合点 + 6
        texts = [t.text for t in root.iter(f"{SVG_NS}text")]
        self.assertIn("demo", texts)
        self.assertIn("2020-09-13", texts)
        self.assertTrue(any(t.startswith("-") and t.endswith("%") for t in texts))   # 最大回撤刻度
        self.assertTrue(any("130" in t for t in texts))
        self.assertEqual(svg, R.equity_svg(eq, ts, width=500, height=300, title="demo"))
        # 精确几何：pad_l=70, pad_r=20, pad_t=30, pad_b=30 => w=410, h=240, h_eq=int(240*0.68)=163；lo=90, hi=130
        pts = polyline.get("points").split()
        self.assertEqual(pts[0], "70.0,152.2")                   # x=70, y=30+(130-100)/40*163=152.25 -> .1f
        self.assertEqual(pts[2], "234.0,193.0")                  # x=70+2*82, y=30+163
        self.assertEqual(pts[-1], "480.0,30.0")                  # x=70+5*82, y=30
        self.assertEqual(len(root.findall(f"{SVG_NS}line")), 2 + 5 + 6)   # 两条轴 + 5 个 y 刻度 + 6 个 x 刻度
        area = polygon.get("points").split()                     # y0 = 30+163+10 = 203, h_dd = 240-163-10 = 67
        self.assertEqual((area[0], area[-1]), ("70.0,203.0", "480.0,203.0"))
        self.assertEqual(area[5], "398.0,270.0")                 # 最大回撤点（120->95）落在面积底部：203+67
        ticks = [t for t in root.iter(f"{SVG_NS}text") if t.text == "90"]
        self.assertEqual((ticks[0].get("x"), ticks[0].get("y")), ("64", "197.0"))   # y 刻度文字：x=70-6, y=30+163+4
        labels = [t.text for t in root.iter(f"{SVG_NS}text")]
        self.assertEqual(labels[1:6], ["90", "100", "110", "120", "130"])             # 5 个 y 刻度标签等分 [lo, hi]
        self.assertIn("-20.8%", labels)                                              # 最大回撤 1-95/120
        dd_label = next(t for t in root.iter(f"{SVG_NS}text") if t.text == "-20.8%")
        self.assertEqual((dd_label.get("x"), dd_label.get("y")), ("64", "274"))     # y0 + h_dd + 4 = 203 + 67 + 4
        yt = [(l.get("x1"), l.get("x2")) for l in root.findall(f"{SVG_NS}line")[2:7]]
        self.assertEqual(yt, [("67", "70")] * 5)                                     # y 刻度短线 3px
        dates = [t for t in root.iter(f"{SVG_NS}text") if t.text and t.text.startswith("2020-")]
        self.assertEqual({t.get("y") for t in dates}, {"208"})                        # x 刻度文字在 x 轴下 15px
        xt = [(l.get("y1"), l.get("y2")) for l in root.findall(f"{SVG_NS}line")[7:]]
        self.assertEqual(xt, [("193", "197")] * 6)                                   # x 刻度线从 x 轴向下 4px
        self.assertEqual([l.get("x1") for l in root.findall(f"{SVG_NS}line")[7:]],
                         ["70.0", "152.0", "234.0", "316.0", "398.0", "480.0"])      # 6 个刻度均分到最后一根
        axes = [(l.get("x1"), l.get("y1"), l.get("x2"), l.get("y2")) for l in root.findall(f"{SVG_NS}line")[:2]]
        self.assertEqual(axes, [("70", "30", "70", "270"), ("70", "193", "480", "193")])   # y 轴到 y0+h_dd=270，x 轴在 193
        # 折线 y 坐标：权益越高 y 越小
        ys = [float(p.split(",")[1]) for p in polyline.get("points").split()]
        self.assertLess(ys[5], ys[0])
        self.assertGreater(ys[2], ys[1])

    def test_svg_edge_cases(self):
        default_root = self._parse(R.equity_svg([1.0, 2.0]))
        self.assertEqual((default_root.get("width"), default_root.get("height")), ("960", "420"))   # 默认画布
        root = self._parse(R.equity_svg([], None))
        self.assertIn("no data", [t.text for t in root.iter(f"{SVG_NS}text")])
        neg = self._parse(R.equity_svg([-5.0, -3.0, -4.0], None, width=500, height=300))
        ys = {p.split(",")[1] for p in neg.find(f"{SVG_NS}polygon").get("points").split()}
        self.assertEqual(ys, {"203.0"})                          # 权益非正：回撤定义为 0，面积贴在基线上
        tiny = self._parse(R.equity_svg([0.5, 0.4], None, width=500, height=300))
        self.assertIn("480.0,270.0", tiny.find(f"{SVG_NS}polygon").get("points").split())   # 峰值 0.5 (>0 但 <1) 的回撤 20% 仍画出
        flat = self._parse(R.equity_svg([5.0, 5.0, 5.0]))
        self.assertEqual(len(flat.find(f"{SVG_NS}polyline").get("points").split()), 3)
        one = self._parse(R.equity_svg([7.0], [0]))
        self.assertEqual(len(one.find(f"{SVG_NS}polyline").get("points").split()), 1)
        texts = [t.text for t in self._parse(R.equity_svg([1.0, 2.0, 3.0])).iter(f"{SVG_NS}text")]
        self.assertIn("2", texts)                                             # 无 ts 时用下标作刻度

    def test_write_report_files(self):
        d = generate(n=400, seed=3)
        r = run_backtest(d, REGISTRY["rsi_reversion"](), BrokerConfig(max_participation=1.0))
        with tempfile.TemporaryDirectory() as tmp:
            paths = R.write_report(r, os.path.join(tmp, "sub"), "rsi")
            self.assertEqual(sorted(paths), ["json", "svg", "txt"])
            for p in paths.values():
                self.assertGreater(os.path.getsize(p), 100)
            with open(paths["json"]) as f:
                j = json.load(f)
            self.assertEqual(j["summary"]["fills"], len(r.fills))
            self.assertIn("sharpe", j["metrics"])
            with open(paths["svg"]) as f:
                svg_root = self._parse(f.read())
            self.assertEqual(len(svg_root.findall(f"{SVG_NS}line")), 13)      # 400 根也只画 6 个 x 刻度
            with open(paths["txt"], encoding="utf-8") as f:
                self.assertIn("qbt report: rsi", f.read())


if __name__ == "__main__":
    unittest.main()
