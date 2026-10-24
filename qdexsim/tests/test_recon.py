import os
import unittest

from qdexsim import common, recon
from qdexsim.tests._util import tmpdir


class TestPieces(unittest.TestCase):
    def test_normalize_mapping(self):
        rows = [{"order_id": "7", "client_oid": "", "sym": "BTCUSDT", "dir": "b", "amount": "0.5", "px": "1.5", "dealt": "", "state": "open",
                 "created_at": "1700000000000"}]
        n = recon.normalize(rows, recon.SYNTH_MAP)[0]
        self.assertEqual((n["id"], n["cid"], n["side"], n["qty"], n["price"], n["filled"], n["status"], n["ts"]),
                         ("7", None, "BUY", 0.5, 1.5, None, "NEW", 1700000000000))
        n = recon.normalize([{"id": 3, "status": "filled", "side": "sell"}])[0]
        self.assertEqual((n["id"], n["status"], n["side"], n["qty"]), ("3", "FILLED", "SELL", None))

    def test_aggregate_split_orders(self):
        rows = recon.normalize([{"cid": "a", "qty": 0.4, "filled": 0.4, "price": 100, "status": "FILLED", "ts": 5, "id": ""},
                                {"cid": "a", "qty": 0.6, "filled": 0.6, "price": 110, "status": "FILLED", "ts": 3, "id": "9"},
                                {"cid": None, "qty": 1, "filled": 0, "price": 1, "status": "NEW", "ts": None, "id": "x"},
                                {"cid": None, "qty": 1, "filled": 0, "price": 1, "status": "NEW", "ts": None, "id": "y"}])
        agg = recon.aggregate(rows)
        a = next(r for r in agg if r["cid"] == "a")
        self.assertEqual((a["qty"], a["filled"], a["parts"], a["ts"], a["id"], a["status"]), (1.0, 1.0, 2, 3, "9", "FILLED"))
        self.assertAlmostEqual(a["price"], 106.0)
        self.assertEqual(len(agg), 3)
        self.assertEqual(recon._combine_status(["NEW", "FILLED"]), "PARTIALLY_FILLED")
        self.assertEqual(recon._combine_status(["NEW", "NEW"]), "NEW")
        self.assertEqual(recon._combine_status(["CANCELED", "FILLED"]), "FILLED")

    def test_match_precedence(self):
        local = recon.normalize([{"id": "1", "cid": "a"}, {"id": "2", "cid": None}, {"id": "9", "cid": "zz"}, {"id": "1", "cid": "dup"}])
        remote = recon.normalize([{"id": "1", "cid": "a"}, {"id": "2", "cid": "b"}, {"id": "3", "cid": None}])
        pairs, only_l, only_r = recon.match(local, remote)
        self.assertEqual([(l["id"], r["id"]) for l, r in pairs], [("1", "1"), ("2", "2")])
        self.assertEqual([l["cid"] for l in only_l], ["zz", "dup"])
        self.assertEqual([r["id"] for r in only_r], ["3"])

    def test_detect_ts_offset(self):
        mk = lambda d: [({"ts": 1000 + d}, {"ts": 1000}) for _ in range(5)]        # noqa: E731
        self.assertEqual(recon.detect_ts_offset(mk(8 * 3600000)), 8 * 3600000)
        self.assertEqual(recon.detect_ts_offset(mk(-5.5 * 3600000)), -int(5.5 * 3600000))
        self.assertEqual(recon.detect_ts_offset(mk(1000)), 0)                                    # 太小
        self.assertEqual(recon.detect_ts_offset(mk(3600000 + 60000)), 0)                          # 不是 15 分钟整数倍
        self.assertEqual(recon.detect_ts_offset(mk(3600000)[:2]), 0)                              # 样本太少
        self.assertEqual(recon.detect_ts_offset([({"ts": None}, {"ts": 1})] * 5), 0)

    def test_compare_and_actions(self):
        l = {"qty": 1.0, "price": 2.0, "filled": 0.0, "status": "NEW", "ts": 1000 + 3600000}
        r = {"qty": 1.0, "price": 2.0, "filled": 1.0, "status": "FILLED", "ts": 1000}
        self.assertEqual(recon.compare_pair(l, r, 3600000, 5000), ["filled", "status"])
        self.assertEqual(recon.compare_pair(l, r, 0, 5000), ["filled", "status", "ts"])
        self.assertEqual(recon.compare_pair(dict(l, qty=None, status=None, ts=None), r, 0, 5000), ["filled"])
        self.assertEqual(recon.action_for("missing_local", [], None, r), "补记")
        self.assertEqual(recon.action_for("phantom", [], {"status": "NEW"}, None), "标失败")
        self.assertEqual(recon.action_for("phantom", [], {"status": None}, None), "标失败")
        self.assertEqual(recon.action_for("phantom", [], {"status": "FILLED"}, None), "人工看")
        self.assertEqual(recon.action_for("mismatch", ["status"], {"status": "NEW"}, {"status": "FILLED"}), "补记")
        self.assertEqual(recon.action_for("mismatch", ["status"], {"status": "FILLED"}, {"status": "NEW"}), "人工看")
        self.assertEqual(recon.action_for("mismatch", ["filled", "status"], {"status": "NEW"}, {"status": "PARTIALLY_FILLED"}), "补记")
        self.assertEqual(recon.action_for("mismatch", ["filled", "status"], {"status": "FILLED"}, {"status": "NEW"}), "人工看")
        self.assertEqual(recon.action_for("mismatch", ["price"], {"status": "NEW"}, {"status": "NEW"}), "人工看")


class TestSelfcheck(unittest.TestCase):
    def test_selfcheck_finds_all_50_zero_false_positives(self):
        res = recon.selfcheck()
        self.assertTrue(res["ok"], res)
        self.assertEqual((res["planted"], res["found"], res["missed"], res["false_positives"]), (50, 50, {}, {}))
        s = res["summary"]
        self.assertEqual((s["missing_local"], s["phantom"], s["mismatch"], s["ts_offset_ms"]), (15, 15, 20, 28800000))
        kinds = {}
        local, remote, planted = recon.synth(7)
        rep = recon.reconcile(local, remote["orders"], recon.SYNTH_MAP, recon.REMOTE_MAP)
        for d in rep["diffs"]:
            kinds.setdefault(tuple(d["fields"]), 0)
            kinds[tuple(d["fields"])] += 1
        self.assertEqual(kinds[("qty",)], 5)
        self.assertEqual(kinds[("price",)], 5)
        self.assertEqual(kinds[("status",)], 7)
        self.assertEqual(kinds[("filled", "status")], 3)
        self.assertTrue(any(l for l in rep["diffs"] if l["action"] == "补记"))
        self.assertTrue(any(l for l in rep["diffs"] if l["action"] == "标失败"))
        self.assertTrue(any(l for l in rep["diffs"] if l["action"] == "人工看"))
        splits = [r for r in recon.aggregate(recon.normalize(local, recon.SYNTH_MAP)) if r["parts"] > 1]
        self.assertEqual(len(splits), 10)                                                          # 拆单真的存在且没误报
        self.assertTrue(all(len(recon.synth(s)[2]) == 50 for s in (1, 2, 3)))

    def test_without_status_compare_would_miss(self):
        """三刀之一：对账漏比状态字段必须被抓住——这里用同一数据证明只比数量 / 价格会漏 7 + 3 条。"""
        local, remote, planted = recon.synth(7)
        rep = recon.reconcile(local, remote["orders"], recon.SYNTH_MAP, recon.REMOTE_MAP)
        status_only = [d for d in rep["diffs"] if d["fields"] in (["status"], ["filled", "status"])]
        self.assertEqual(len(status_only), 10)

    def test_ts_offset_given_disables_detection(self):
        local, remote, _p = recon.synth(7)
        rep = recon.reconcile(local, remote["orders"], dict(recon.SYNTH_MAP, ts_offset_ms=0), recon.REMOTE_MAP)
        self.assertEqual(rep["summary"]["ts_offset_ms"], 0)
        self.assertGreater(rep["summary"]["mismatch"], 100)                                       # 全部时间戳都差 8 小时


class TestCli(unittest.TestCase):
    def test_files_and_main(self):
        d = tmpdir(self)
        local, remote, _p = recon.synth(3)
        lp, rp, mp, rmp, out = (os.path.join(d, n) for n in ("local.csv", "remote.json", "map.json", "rmap.json", "report.json"))
        import csv
        with open(lp, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(local[0]))
            w.writeheader()
            w.writerows(local)
        common.write_json(rp, remote)
        common.write_json(mp, recon.SYNTH_MAP)
        common.write_json(rmp, recon.REMOTE_MAP)
        self.assertEqual(len(recon.load_table(lp)), len(local))
        self.assertEqual(len(recon.load_table(rp)), 200)
        common.write_json(os.path.join(d, "rows.json"), {"rows": [{"id": 1}]})
        self.assertEqual(recon.load_table(os.path.join(d, "rows.json")), [{"id": 1}])
        common.write_json(os.path.join(d, "list.json"), [{"id": 2}])
        self.assertEqual(recon.load_table(os.path.join(d, "list.json")), [{"id": 2}])
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = recon.main(["--local", lp, "--remote", rp, "--map", mp, "--remote-map", rmp, "--out", out])
        self.assertEqual(rc, 1)
        rep = common.read_json(out)
        self.assertEqual(rep["summary"]["total"], 50)
        text = buf.getvalue()
        self.assertIn("missing_local 15", text)
        self.assertIn("人工看", text)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(recon.main(["--selfcheck"]), 0)
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                recon.main(["--local", lp])
        common.write_json(rp, {"orders": []})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(recon.main(["--local", rp, "--remote", rp]), 0)                      # 空对空 → 0


if __name__ == "__main__":
    unittest.main()
