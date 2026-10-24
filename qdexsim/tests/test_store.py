import os
import unittest

from qdexsim import store as store_mod
from qdexsim.tests._util import tmpdir


def order(i, cid=None, status="NEW"):
    return {"id": i, "cid": cid, "symbol": "BTCUSDT", "status": status, "created": 1000 + i, "x": i}


class TestStore(unittest.TestCase):
    def test_roundtrip_and_restart(self):
        d = tmpdir(self)
        path = os.path.join(d, "s.db")
        st = store_mod.Store(path)
        st.save_order(order(1, "a"))
        st.save_order(order(2))
        st.save_fill({"id": 3, "order_id": 1, "symbol": "BTCUSDT", "ts": 5, "qty": 1})
        st.save_position({"symbol": "BTCUSDT", "qty": 1.5})
        st.set_kv("meta", {"wallet": 9})
        st.add_event(7, "k", {"a": 1})
        st.add_event(8, "other", {"b": 2})
        st.close()
        st2 = store_mod.Store(path)
        self.assertEqual([o["id"] for o in st2.load_orders()], [1, 2])
        self.assertEqual(st2.load_fills()[0]["qty"], 1)
        self.assertEqual(st2.load_positions()[0]["qty"], 1.5)
        self.assertEqual(st2.get_kv("meta")["wallet"], 9)
        self.assertIsNone(st2.get_kv("nope"))
        self.assertEqual(st2.get_kv("nope", 3), 3)
        ev = st2.events()
        self.assertEqual([e["kind"] for e in ev], ["k", "other"])
        self.assertEqual(ev[0]["seq"], 1)
        self.assertEqual(ev[0]["ts"], 7)
        self.assertEqual([e["seq"] for e in st2.events(since_seq=1)], [2])
        self.assertEqual([e["b"] for e in st2.events(kind="other")], [2])
        self.assertEqual(st2.counts(), {"orders": 2, "fills": 1, "positions": 1, "events": 2})
        st2.close()

    def test_duplicate_cid_is_rejected_by_database(self):
        st = store_mod.Store()
        st.save_order(order(1, "same"))
        with self.assertRaises(store_mod.DuplicateClientId):
            st.save_order(order(2, "same"))
        st.save_order(order(1, "same", status="FILLED"))           # 同一 id 更新不是重复
        self.assertEqual(st.load_orders()[0]["status"], "FILLED")
        st.save_order(order(3))
        st.save_order(order(4))                                   # cid None 可以多张

    def test_transaction_rollback(self):
        st = store_mod.Store()
        with self.assertRaises(RuntimeError):
            with st.transaction():
                st.save_order(order(1))
                raise RuntimeError("boom")
        self.assertEqual(st.load_orders(), [])
        with st.transaction():
            st.save_order(order(2))
        self.assertEqual(len(st.load_orders()), 1)

    def test_memory_store_has_no_wal(self):
        st = store_mod.Store(":memory:")
        self.assertEqual(st.conn.execute("PRAGMA journal_mode").fetchone()[0], "memory")
        st.close()


if __name__ == "__main__":
    unittest.main()
