"""库存快照：查询时间、来源、新鲜度与占用。"""

import unittest
from datetime import timedelta

from support import NOW, Harness


class InventoryFreshness(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.h.load_samples()

    def test_snapshot_carries_source_and_checked_time(self):
        best, candidates, _ = self.h.service.inventory.select("BEAR-1")
        self.assertEqual(best["partner"], "D-1")
        self.assertEqual(best["checked_at"], "2026-10-07T01:00:00+00:00")
        self.assertEqual(best["source"], "dealer_portal")
        self.assertTrue(all(c["snapshot_id"] for c in candidates))

    def test_stale_snapshot_cannot_be_used(self):
        self.h.clock.advance(int(timedelta(hours=2).total_seconds()))
        best, candidates, _ = self.h.service.inventory.select("BEAR-1")
        self.assertIsNone(best)
        self.assertIn("超过允许", " ".join(candidates[0]["reasons"]))

    def test_future_snapshot_rejected(self):
        self.h.clock.advance(-int(timedelta(hours=3).total_seconds()))
        best, candidates, _ = self.h.service.inventory.select("BEAR-1")
        self.assertIsNone(best)
        self.assertEqual(candidates[0]["freshness"]["state"], "future")

    def test_quantity_must_cover_request(self):
        best, candidates, _ = self.h.service.inventory.select("BEAR-1", quantity=4)
        self.assertIsNone(best)
        self.assertIn("少于所需", " ".join(candidates[0]["reasons"]))

    def test_newer_snapshot_supersedes_older_for_same_partner(self):
        self.h.inventory([
            ("D-1", "BEAR-1", 0, "2026-10-07T09:06+08:00"),
            ("D-2", "BEAR-1", 2, "2026-10-07T09:02+08:00"),
        ])
        best, candidates, context = self.h.service.inventory.select("BEAR-1")
        # D-1 最新快照为 0，不能拿更早的 3 件当依据
        self.assertEqual(best["partner"], "D-2")
        d1 = [c for c in candidates if c["partner"] == "D-1"][0]
        self.assertEqual(d1["available_after_holds"], 0)
        self.assertEqual(len(context["superseded"]), 1)

    def test_holds_after_snapshot_reduce_availability(self):
        self.h.ticket("T-hold")
        self.h.appointment("T-hold")
        self.h.confirm_all("T-hold")
        self.h.clock.advance(60)
        self.h.service.reserve({"name": "cs", "role": "customer_service"}, "T-hold")
        # 该预留发生在快照之后，应从可用数量中扣除
        best, candidates, _ = self.h.service.inventory.select("BEAR-1")
        self.assertEqual(candidates[0]["later_holds"], 1)
        self.assertLess(candidates[0]["available_after_holds"], candidates[0]["quantity"])

    def test_inventory_requires_checked_time(self):
        path = self.h.db_dir() / "no-checked.csv"
        path.write_text("stock,partner,item,quantity,checked\nS1,D-1,BEAR-1,3,\n", encoding="utf-8")
        with self.assertRaises(Exception) as ctx:
            self.h.service.ingest_inventory(path)
        self.assertIn("查询时间", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
