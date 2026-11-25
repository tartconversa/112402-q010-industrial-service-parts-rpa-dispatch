"""门户预留：幂等、结果不明先对账、取消释放、已发货保护。"""

import os
import tempfile
import threading
import unittest
from datetime import timedelta

from support import CS, Harness


def ready(h, ticket="T1"):
    h.load_samples()
    h.appointment(ticket)
    h.confirm_all(ticket)
    return ticket


class ReserveFlow(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.t = ready(self.h)

    def test_reserve_returns_receipt_and_blocks_duplicate(self):
        first = self.h.service.reserve(CS, self.t)
        self.assertEqual(first["state"], "held")
        self.assertEqual(first["portal_ref"], "H-0001")
        second = self.h.service.reserve(CS, self.t)
        self.assertEqual(second["state"], "held")
        self.assertIn("未重复请求", second["note"])
        reserve_calls = [c for c in self.h.portal.calls if c[0] == "reserve"]
        self.assertEqual(len(reserve_calls), 1)

    def test_dispatch_requires_held_receipt(self):
        check = self.h.service.evaluate(self.t, persist=False)
        self.assertEqual(check["verdict"], "reserve_needed")
        self.assertFalse(check["can_dispatch"])
        with self.assertRaises(Exception) as ctx:
            self.h.service.dispatch(CS, self.t)
        self.assertEqual(ctx.exception.code, "dispatch_blocked")
        # 预留之后才能派单
        self.h.service.reserve(CS, self.t)
        self.assertEqual(self.h.service.dispatch(CS, self.t)["state"], "dispatched")

    def test_conflict_reported_as_failure(self):
        self.h.portal.conflict_on_hold = True
        result = self.h.service.reserve(CS, self.t)
        self.assertEqual(result["state"], "failed")
        self.assertIn("库存已被其他订单占用", result["response"]["reason"])
        check = self.h.service.evaluate(self.t, persist=False)
        self.assertFalse(check["can_dispatch"])


class AmbiguousReservation(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.t = ready(self.h)

    def test_timeout_is_pending_then_reconciled_before_retry(self):
        self.h.portal.fail_reserve_once = True
        first = self.h.service.reserve(CS, self.t)
        self.assertEqual(first["state"], "pending")
        self.assertEqual([a["outcome"] for a in first["attempts"]], ["pending"])
        self.assertFalse(self.h.service.evaluate(self.t, persist=False)["can_dispatch"])

        # 重试必须先查询；门户此时没有这笔预留，确认后才重发
        retried = self.h.service.retry_reservation(
            CS, self.t, partner="D-1", item="BEAR-1"
        )
        self.assertEqual(retried["state"], "held")
        outcomes = [a["outcome"] for a in retried["attempts"]]
        self.assertEqual(outcomes, ["pending", "reconciled", "held"])
        calls = [c[0] for c in self.h.portal.calls]
        self.assertEqual(calls, ["reserve", "query", "reserve"])

    def test_lost_receipt_does_not_double_book(self):
        # 门户成功但回执丢失：本地为 pending
        self.h.portal.ambiguous_reserve = True
        first = self.h.service.reserve(CS, self.t)
        self.assertEqual(first["state"], "pending")

        # 对账查到了这笔预留，不能重发
        reconciled = self.h.service.reconcile_reservation(CS, first["reservation_id"])
        self.assertEqual(reconciled["state"], "held")
        self.assertEqual(reconciled["portal_ref"], "H-0001")
        reserve_calls = [c for c in self.h.portal.calls if c[0] == "reserve"]
        self.assertEqual(len(reserve_calls), 1)
        # 库存只被扣了一次
        self.assertEqual(self.h.portal.stock[("D-1", "BEAR-1")], 2)

    def test_retry_after_lost_receipt_returns_held_not_second_booking(self):
        self.h.portal.ambiguous_reserve = True
        self.h.service.reserve(CS, self.t)
        retried = self.h.service.retry_reservation(CS, self.t, partner="D-1", item="BEAR-1")
        self.assertEqual(retried["state"], "held")
        reserve_calls = [c for c in self.h.portal.calls if c[0] == "reserve"]
        self.assertEqual(len(reserve_calls), 1)


class CancelAndRelease(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.t = ready(self.h)
        self.held = self.h.service.reserve(CS, self.t)

    def test_cancel_releases_stock(self):
        result = self.h.service.release_on_cancel(CS, self.t, reason="客户取消")
        self.assertEqual(result[0]["state"], "released")
        self.assertEqual(self.h.portal.stock[("D-1", "BEAR-1")], 3)
        row = self.h.service.tickets.get(self.t)
        self.assertEqual(row["dispatch_state"], "cancelled")

    def test_ambiguous_release_reconciles_before_retry(self):
        self.h.portal.ambiguous_release = True
        result = self.h.service.release_on_cancel(CS, self.t, reason="客户取消")
        self.assertEqual(result[0]["state"], "releasing")
        self.assertIn("先查询", result[0]["note"])
        retried = self.h.service.reservations.retry_release(CS, result[0]["reservation_id"])
        self.assertEqual(retried["state"], "released")
        self.assertEqual(self.h.portal.stock[("D-1", "BEAR-1")], 3)

    def test_shipped_part_cannot_be_released_even_without_local_receipt(self):
        # 只把门户标为已发货，本地仍认为 held；取消时必须以门户发货记录为准
        self.h.portal.mark_shipped("H-0001")
        with self.assertRaises(Exception) as ctx:
            self.h.service.release_on_cancel(CS, self.t, reason="客户取消")
        self.assertEqual(ctx.exception.code, "already_shipped")
        self.assertEqual(self.h.portal.stock[("D-1", "BEAR-1")], 2)
        row = self.h.db.one(
            "SELECT state FROM reservations WHERE ticket = ?", (self.t,)
        )
        self.assertEqual(row["state"], "consumed")

    def test_pending_release_reconciles_first(self):
        # 预留结果是 pending（回执丢失），取消时先对账发现确实已预占，再释放
        self.h.portal.ambiguous_reserve = True
        self.h.service.tickets.create("T-pending", asset="A-77", model="P-4", part="BEAR-1",
                                      firmware="3.4", fault="bearing", warranty="valid")
        self.h.appointment("T-pending")
        self.h.confirm_all("T-pending", serial="A-77")
        pending = self.h.service.reserve(CS, "T-pending")
        self.assertEqual(pending["state"], "pending")
        released = self.h.service.release_on_cancel(CS, "T-pending", reason="客户取消")
        self.assertEqual(released[0]["state"], "released")


class Concurrency(unittest.TestCase):
    def test_parallel_reserve_books_once(self):
        h = Harness(db_path=os.path.join(tempfile.mkdtemp(), "concurrent.db"))
        ready(h)
        outcomes = []

        def attempt():
            try:
                outcomes.append(h.service.reserve(CS, "T1")["state"])
            except Exception as exc:  # pragma: no cover - 失败时便于定位
                outcomes.append(type(exc).__name__)

        threads = [threading.Thread(target=attempt) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(outcomes, ["held"] * 4)
        self.assertEqual(h.db.one("SELECT COUNT(*) AS n FROM reservations")["n"], 1)
        self.assertEqual(h.portal.stock[("D-1", "BEAR-1")], 2)
        reserve_calls = [c for c in h.portal.calls if c[0] == "reserve"]
        self.assertEqual(len(reserve_calls), 1)


class ShippedStockNotAvailable(unittest.TestCase):
    def test_catalog_update_cannot_reuse_shipped_stock(self):
        h = Harness()
        ready(h)
        held = h.service.reserve(CS, "T1")
        h.portal.mark_shipped(held["portal_ref"])
        h.service.reservations.mark_shipped(CS, held["reservation_id"])

        # 目录更新后重算：已发货的 1 件仍计入占用，不会被当成可用库存
        h.catalog([
            ("BEAR-1", "P-4", "3.0", "3.9", "", "2", "active"),
            ("BEAR-2", "P-4", "4.0", "4.4", "BEAR-1", "2", "active"),
            ("CTRL-4", "C-8", "1.7", "2.2", "", "1", "active"),
        ])
        best, candidates, _ = h.service.inventory.select("BEAR-1")
        self.assertEqual(candidates[0]["later_holds"], 1)
        self.assertEqual(candidates[0]["available_after_holds"], 2)
        # 已发货的预留不能被释放回库存：不发出释放请求，且结果里明确标注未释放
        before = [c for c in h.portal.calls if c[0] == "release"]
        with self.assertRaises(Exception) as ctx:
            h.service.release_on_cancel(CS, "T1", reason="客户取消")
        self.assertEqual(ctx.exception.code, "already_shipped")
        self.assertFalse(ctx.exception.details["releases"][0]["released"])
        after = [c for c in h.portal.calls if c[0] == "release"]
        self.assertEqual(len(before), len(after))
        self.assertEqual(h.portal.stock[("D-1", "BEAR-1")], 2)


if __name__ == "__main__":
    unittest.main()
