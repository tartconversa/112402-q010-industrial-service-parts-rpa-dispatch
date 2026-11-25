"""厂商目录修订与替代件核验。"""

import unittest

from support import Harness


class CatalogRevisions(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.h.load_samples()

    def test_every_revision_is_kept(self):
        self.h.catalog([
            ("BEAR-2", "P-4", "4.0", "4.4", "BEAR-1", "3", "active"),
        ])
        history = self.h.service.catalog.history("BEAR-2", "P-4")
        self.assertEqual([r.revision for r in history], ["2", "3"])
        active = [r for r in history if r.valid_to is None]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].revision, "3")

    def test_same_revision_different_content_rejected(self):
        with self.assertRaises(Exception) as ctx:
            self.h.catalog([
                ("BEAR-1", "P-4", "3.0", "3.5", "", "1", "active"),
            ])
        self.assertIn("修订", str(ctx.exception))

    def test_older_revision_does_not_roll_back(self):
        result = self.h.catalog([
            ("BEAR-1", "P-4", "3.0", "3.9", "", "0", "active"),
        ])
        self.assertTrue(any(c["kind"] == "older_revision_ignored" for c in result["changed"]))
        effective = self.h.service.catalog.effective("BEAR-1", "P-4")
        self.assertEqual(effective.revision, "1")

    def test_revision_compared_numerically(self):
        self.h.catalog([("BEAR-1", "P-4", "3.0", "3.9", "", "10", "active")])
        effective = self.h.service.catalog.effective("BEAR-1", "P-4")
        self.assertEqual(effective.revision, "10")

    def test_manual_replacement_withdrawal_keeps_history(self):
        self.h.catalog([("BEAR-2", "P-4", "4.0", "4.4", "BEAR-1", "3", "active")])
        result = self.h.service.catalog.retract_replacement(
            "BEAR-2", "P-4", reason="厂商通知撤回替代"
        )
        self.assertEqual(result["kind"], "replacement_withdrawn")
        effective = self.h.service.catalog.effective("BEAR-2", "P-4")
        self.assertIsNone(effective.replacement)
        self.assertEqual(len(self.h.service.catalog.history("BEAR-2", "P-4")), 3)

    def test_complete_snapshot_retracts_missing_entries(self):
        result = self.h.catalog([("BEAR-1", "P-4", "3.0", "3.9", "", "1", "active")],
                                complete=True)
        removed = [c for c in result["changed"] if c["kind"] == "entry_removed"]
        self.assertEqual([c["part"] for c in removed], ["BEAR-2", "CTRL-4"])
        self.assertIsNone(self.h.service.catalog.effective("CTRL-4", "C-8"))

    def test_partial_snapshot_does_not_retract(self):
        self.h.catalog([("BEAR-1", "P-4", "3.0", "3.9", "", "1", "active")], complete=False)
        self.assertIsNotNone(self.h.service.catalog.effective("CTRL-4", "C-8"))

    def test_firmware_range_unparseable_rejected(self):
        with self.assertRaises(Exception) as ctx:
            self.h.catalog([("X-1", "P-4", "v-next", "v-later", "", "1", "active")])
        self.assertIn("不是可比较的版本号", str(ctx.exception))


class CompatibilityRules(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.h.load_samples()
        self.h.ticket("T-c")
        self.h.appointment("T-c")

    def grades(self):
        return self.h.service.evaluate("T-c", persist=False)["gates"]

    def test_no_part_confirmation_means_pending_not_compatible(self):
        self.h.service.confirm_asset({"name": "a", "role": "asset_admin"}, "T-c",
                                     serial="A-99", model="P-4", firmware="3.4")
        gate = self.grades()["compatibility"]
        self.assertEqual(gate["status"], "pending")
        self.assertIn("尚无备件工程师", gate["summary"])
        parts = [o["part"] for o in gate["evidence"]["options"]]
        self.assertIn("BEAR-1", parts)
        self.assertNotIn("BEAR-2", parts)  # 备件号是 BEAR-2，不是 BEAR-1 的替代件
        self.assertIn("不代表已确认兼容", gate["evidence"]["options"][0]["note"])

    def test_part_confirmation_without_firmware_scope_rejected(self):
        self.h.service.confirm_asset({"name": "a", "role": "asset_admin"}, "T-c",
                                     serial="A-99", model="P-4", firmware="3.4")
        with self.assertRaises(Exception) as ctx:
            self.h.service.confirm_part({"name": "p", "role": "part_engineer"}, "T-c",
                                        part="BEAR-1", model="P-4",
                                        firmware_from=None, firmware_to=None)
        self.assertIn("固件范围", str(ctx.exception))

    def test_firmware_outside_confirmed_scope_blocks(self):
        self.h.confirm_all("T-c", firmware="3.4", low="4.0", high="4.4")
        gate = self.grades()["compatibility"]
        self.assertEqual(gate["status"], "blocked")
        self.assertIn("不在备件确认的适用范围", gate["summary"])

    def test_substitution_needs_catalog_support(self):
        # 目录里 BEAR-1 没有替代件，BEAR-2 -> BEAR-1 才是厂商方向
        self.h.confirm_all("T-c", firmware="3.6", part="BEAR-2", low="3.0",
                           high="3.9", replacement="BEAR-1")
        gate = self.grades()["compatibility"]
        self.assertEqual(gate["status"], "blocked")
        self.assertIn("厂商目录不支持", gate["summary"])

    def test_substitution_supported_by_catalog_passes_compat(self):
        # 目录：BEAR-2 由 BEAR-1 替代，固件 4.0–4.4
        self.h.service.tickets.create("T-sub", asset="A-99", model="P-4", part="BEAR-2",
                                      firmware="4.1", fault="bearing", warranty="valid")
        self.h.appointment("T-sub")
        self.h.confirm_all("T-sub", firmware="4.1", part="BEAR-1", low="4.0",
                           high="4.4", replacement="BEAR-2")
        gate = self.h.service.evaluate("T-sub", persist=False)["gates"]["compatibility"]
        self.assertEqual(gate["status"], "pass")
        self.assertIn("按厂商目录", gate["evidence"]["catalog_summary"])

    def test_discontinued_part_cannot_be_installed(self):
        self.h.catalog([("BEAR-1", "P-4", "3.0", "3.9", "", "2", "discontinued")])
        self.h.confirm_all("T-c", firmware="3.4", part="BEAR-1", low="3.0", high="3.9")
        gate = self.grades()["compatibility"]
        self.assertEqual(gate["status"], "blocked")
        self.assertIn("已停产", gate["summary"])

    def test_missing_asset_confirmation_uses_reported_config_only_as_hint(self):
        gate = self.grades()["compatibility"]
        self.assertEqual(gate["status"], "pending")
        self.assertIn("资产管理员", gate["summary"])
        self.assertEqual(gate["evidence"]["reported_config"], {})

    def test_wrong_role_cannot_confirm(self):
        self.h.service.set_site_config({"name": "field", "role": "asset_admin"}, "T-c",
                                       model="P-4", firmware="3.4")
        with self.assertRaises(Exception) as ctx:
            self.h.service.confirm_part({"name": "cs", "role": "customer_service"}, "T-c",
                                        part="BEAR-1", model="P-4",
                                        firmware_from="3.0", firmware_to="3.9")
        self.assertIn("权限", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
