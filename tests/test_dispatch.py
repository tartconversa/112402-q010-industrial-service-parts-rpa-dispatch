"""派单四项门禁：保修、兼容、库存新鲜度、预约时段。"""

import unittest
from datetime import timedelta

from support import ASSET, CS, ENGINEER, SUPERVISOR, WARRANTY, Harness


class FourGateDispatch(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.h.load_samples()
        self.t = "T1"

    def gates(self):
        return self.h.service.evaluate(self.t, persist=False)["gates"]

    def test_all_four_gates_required(self):
        # 只有回执里的 valid/held/queued，不能直接派单
        result = self.h.service.evaluate(self.t, persist=False)
        self.assertFalse(result["can_dispatch"])
        self.assertEqual(result["gates"]["warranty"]["status"], "pending")
        self.assertEqual(result["gates"]["compatibility"]["status"], "pending")
        self.assertEqual(result["gates"]["inventory"]["status"], "pending")
        self.assertEqual(result["gates"]["appointment"]["status"], "pending")

    def test_warranty_expired_blocks(self):
        self.h.appointment(self.t)
        self.h.confirm_all(self.t, covered=False, warranty_state="expired")
        gate = self.gates()["warranty"]
        self.assertEqual(gate["status"], "blocked")
        self.assertIn("未确认", gate["summary"])

    def test_warranty_review_is_not_valid(self):
        self.h.appointment(self.t)
        self.h.confirm_all(self.t, covered=False, warranty_state="review")
        self.assertEqual(self.gates()["warranty"]["status"], "blocked")

    def test_stale_inventory_blocks_dispatch(self):
        self.h.appointment(self.t)
        self.h.confirm_all(self.t)
        self.h.clock.advance(int(timedelta(minutes=31).total_seconds()))
        gate = self.gates()["inventory"]
        self.assertEqual(gate["status"], "blocked")
        self.assertIn("过期", gate["summary"])
        result = self.h.service.evaluate(self.t, persist=False)
        # 其余检查都通过时，结论应是"需刷新库存"，不是笼统阻断
        self.assertEqual(result["verdict"], "stale")
        self.assertTrue(result["needs_fresh_inventory"])
        self.assertFalse(result["can_dispatch"])

    def test_stale_inventory_can_be_recovered_by_new_snapshot(self):
        self.h.appointment(self.t)
        self.h.confirm_all(self.t)
        self.h.clock.advance(int(timedelta(minutes=31).total_seconds()))
        self.h.inventory([("D-1", "BEAR-1", 3, "2026-10-07T09:30+08:00")],
                         source="dealer_portal")
        result = self.h.service.evaluate(self.t, persist=False)
        self.assertEqual(result["verdict"], "reserve_needed")

    def test_stale_snapshot_does_not_invalidate_held_receipt(self):
        self.h.appointment(self.t)
        self.h.confirm_all(self.t)
        self.h.service.reserve(CS, self.t)
        self.h.clock.advance(int(timedelta(minutes=31).total_seconds()))
        result = self.h.service.evaluate(self.t, persist=False)
        self.assertEqual(result["verdict"], "stale")
        self.assertEqual(result["gates"]["reservation"]["status"], "pass")
        self.assertFalse(result["can_dispatch"])
        self.assertIn("过期", result["gates"]["inventory"]["summary"])

    def test_missing_appointment_blocks_dispatch(self):
        self.h.confirm_all(self.t)
        self.assertEqual(self.gates()["appointment"]["status"], "pending")
        self.h.appointment(self.t)
        self.assertEqual(self.gates()["appointment"]["status"], "pass")

    def test_past_appointment_blocks(self):
        self.h.confirm_all(self.t)
        self.h.service.set_appointment(CS, self.t,
                                       start="2026-10-06T09:00:00+08:00",
                                       end="2026-10-06T11:00:00+08:00")
        self.assertEqual(self.gates()["appointment"]["status"], "blocked")

    def test_everything_green_then_reserve_then_dispatch(self):
        self.h.appointment(self.t)
        self.h.confirm_all(self.t)
        result = self.h.service.evaluate(self.t, persist=False)
        self.assertTrue(result["can_reserve"])
        self.assertFalse(result["can_dispatch"])
        self.h.service.reserve(CS, self.t)
        final = self.h.service.evaluate(self.t, persist=False)
        self.assertTrue(final["can_dispatch"])
        self.assertEqual(final["verdict"], "ready")

    def test_dispatch_records_evidence(self):
        self.h.appointment(self.t)
        self.h.confirm_all(self.t)
        self.h.service.reserve(CS, self.t)
        record = self.h.service.dispatch(CS, self.t)
        self.assertEqual(record["state"], "dispatched")
        self.assertEqual(record["reservation"]["portal_ref"], "H-0001")
        self.assertEqual(record["stock"]["checked_at"], "2026-10-07T01:00:00+00:00")
        # 历史里保留了四项检查与预留回执
        events = self.h.service.explain(self.t)["history"]
        kinds = [e["kind"] for e in events]
        self.assertIn("dispatched", kinds)
        self.assertIn("reservation_held", kinds)
        self.assertIn("asset_confirmation", kinds)
        self.assertIn("part_confirmation", kinds)

    def test_confirmation_requires_right_role(self):
        with self.assertRaises(Exception) as ctx:
            self.h.service.confirm_warranty(CS, self.t, covered=True, warranty_state="valid")
        self.assertEqual(ctx.exception.code, "permission_denied")

    def test_warranty_confirmation_must_be_explicit(self):
        with self.assertRaises(Exception) as ctx:
            self.h.service.confirm_warranty(WARRANTY, self.t, covered=None,
                                            warranty_state="valid")
        self.assertEqual(ctx.exception.code, "incomplete_warranty_confirmation")


class ChangeRecheck(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.h.load_samples()
        self.t = "T1"
        self.h.appointment(self.t)
        self.h.confirm_all(self.t)
        self.h.service.reserve(CS, self.t)

    def test_catalog_change_on_dispatched_goes_to_supervisor(self):
        self.h.service.dispatch(CS, self.t)
        self.h.catalog([("BEAR-1", "P-4", "3.0", "3.3", "", "2", "active")])
        row = self.h.service.tickets.get(self.t)
        self.assertEqual(row["dispatch_state"], "supervisor_review")
        events = [e["kind"] for e in self.h.service.explain(self.t)["history"]]
        self.assertIn("supervisor_review_required", events)
        queue = self.h.service.supervisor_queue()
        self.assertEqual(queue[0]["ticket"], self.t)
        self.assertEqual(queue[0]["reservations"][0]["portal_ref"], "H-0001")
        self.assertIn("目录", queue[0]["reason"])

    def test_same_content_revision_bump_does_not_alert_supervisor(self):
        self.h.service.dispatch(CS, self.t)
        self.h.catalog([("BEAR-1", "P-4", "3.0", "3.9", "", "2", "active")])
        self.assertEqual(self.h.service.tickets.get(self.t)["dispatch_state"], "dispatched")
        self.assertEqual(self.h.service.supervisor_queue(), [])

    def test_catalog_change_unrelated_part_does_not_alert_supervisor(self):
        self.h.service.dispatch(CS, self.t)
        self.h.catalog([
            ("BEAR-1", "P-4", "3.0", "3.9", "", "1", "active"),
            ("BEAR-2", "P-4", "4.0", "4.4", "BEAR-1", "3", "active"),
            ("CTRL-4", "C-8", "1.7", "2.2", "", "2", "active"),
        ])
        row = self.h.service.tickets.get(self.t)
        self.assertEqual(row["dispatch_state"], "dispatched")
        self.assertEqual(self.h.service.supervisor_queue(), [])

    def test_catalog_change_on_undispatched_rechecks(self):
        # 未派单、已预留；目录把固件上限收窄到 3.3 后，现场 3.4 不再兼容
        self.h.catalog([("BEAR-1", "P-4", "3.0", "3.3", "", "2", "active")])
        result = self.h.service.evaluate(self.t, persist=False)
        self.assertEqual(result["gates"]["compatibility"]["status"], "blocked")
        self.assertIn("不覆盖现场固件", result["gates"]["compatibility"]["summary"])
        self.assertFalse(result["can_dispatch"])

    def test_replacement_withdrawal_rechecks_open_tickets(self):
        self.h.service.tickets.create("T-sub2", asset="A-55", model="P-4", part="BEAR-2",
                                      firmware="4.1", fault="bearing", warranty="valid")
        self.h.appointment("T-sub2")
        self.h.confirm_all("T-sub2", model="P-4", firmware="4.1", part="BEAR-1",
                           low="4.0", high="4.4", replacement="BEAR-2")
        before = self.h.service.evaluate("T-sub2", persist=False)
        self.assertEqual(before["gates"]["compatibility"]["status"], "pass")

        result = self.h.service.ingest_catalog
        self.h.service.catalog.retract_replacement("BEAR-2", "P-4", reason="厂商撤回")
        after = self.h.service.evaluate("T-sub2", persist=False)
        self.assertEqual(after["gates"]["compatibility"]["status"], "blocked")
        self.assertIn("厂商目录不支持", after["gates"]["compatibility"]["summary"])

    def test_site_config_correction_after_dispatch_alerts_supervisor(self):
        self.h.service.dispatch(CS, self.t)
        self.h.service.set_site_config(ASSET, self.t, firmware="3.8")
        row = self.h.service.tickets.get(self.t)
        self.assertEqual(row["dispatch_state"], "supervisor_review")

    def test_site_config_correction_invalidates_asset_confirmation(self):
        check = self.h.service.evaluate(self.t, persist=False)
        self.assertTrue(check["gates"]["compatibility"]["status"] == "pass")
        self.h.service.set_site_config(ASSET, self.t, firmware="3.8")
        after = self.h.service.evaluate(self.t, persist=False)
        self.assertEqual(after["gates"]["compatibility"]["status"], "pending")
        self.assertIn("现场配置", after["gates"]["compatibility"]["summary"])

    def test_recheck_clears_once_checks_pass_again(self):
        self.h.catalog([("BEAR-1", "P-4", "3.0", "3.3", "", "2", "active")])
        self.assertEqual(self.h.service.tickets.get(self.t)["dispatch_state"], "needs_recheck")
        # 目录恢复覆盖 3.4，并重新做一次备件确认后，应重新可派单
        self.h.catalog([("BEAR-1", "P-4", "3.0", "3.9", "", "3", "active")])
        self.h.service.confirm_part(ENGINEER, self.t, part="BEAR-1", model="P-4",
                                    firmware_from="3.0", firmware_to="3.9")
        result = self.h.service.evaluate(self.t, persist=False)
        self.assertTrue(result["can_dispatch"])
        self.h.service.evaluate(self.t, persist=True, actor=CS)
        self.assertEqual(self.h.service.tickets.get(self.t)["dispatch_state"], "queued")

    def test_undispatched_recheck_state_is_recorded(self):
        # 目录收窄到不覆盖 3.4，已通过检查但未派出的工单必须留下待复核标记
        self.h.catalog([("BEAR-1", "P-4", "3.0", "3.3", "", "2", "active")])
        row = self.h.service.tickets.get(self.t)
        self.assertEqual(row["dispatch_state"], "needs_recheck")
        events = [e["kind"] for e in self.h.service.explain(self.t)["history"]]
        self.assertIn("recheck_required", events)


if __name__ == "__main__":
    unittest.main()
