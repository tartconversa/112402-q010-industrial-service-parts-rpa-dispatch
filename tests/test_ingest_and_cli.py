"""采集：工单回执、调度器、CLI 与门户适配。"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path

from support import CS, Harness

from spare_service import cli
from spare_service.scheduler import Scheduler, Source


class TicketReceipts(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.h.load_samples()

    def test_receipt_does_not_make_ticket_dispatchable(self):
        row = self.h.service.tickets.get("T1")
        self.assertEqual(row["warranty"], "valid")
        self.assertEqual(row["source_reservation"], "held")
        self.assertEqual(row["source_dispatch"], "queued")
        # 回执原样保留，但内部状态仍是草稿
        self.assertEqual(row["dispatch_state"], "draft")
        result = self.h.service.evaluate("T1", persist=False)
        self.assertFalse(result["can_dispatch"])

    def test_receipt_conflict_keeps_system_value(self):
        path = self.h.db_dir() / "receipt-conflict.csv"
        path.write_text(
            "ticket,asset,model,part,firmware,fault,warranty,reservation,dispatch\n"
            "T1,A-99,P-4,BEAR-1,3.4,bearing,expired,none,queued\n",
            encoding="utf-8",
        )
        receipt = self.h.service.ingest_tickets(path)
        self.assertEqual(receipt.conflicts[0]["ticket"], "T1")
        self.assertEqual(receipt.conflicts[0]["fields"]["asset"]["current"], "A-11")
        self.assertEqual(receipt.conflicts[0]["fields"]["warranty"]["incoming"], "expired")
        row = self.h.service.tickets.get("T1")
        self.assertEqual(row["asset"], "A-11")
        self.assertEqual(row["warranty"], "valid")
        events = [e["kind"] for e in self.h.service.explain("T1")["history"]]
        self.assertIn("receipt_conflict", events)

    def test_receipt_fills_missing_fields_only(self):
        first = self.h.db_dir() / "receipt-new.csv"
        first.write_text(
            "ticket,asset,model,part,firmware,fault,warranty,reservation,dispatch\n"
            "T7,,P-4,BEAR-1,3.4,bearing,valid,unknown,queued\n",
            encoding="utf-8",
        )
        created = self.h.service.ingest_tickets(first)
        self.assertIn("T7", created.created)
        self.assertIsNone(self.h.service.tickets.get("T7")["asset"])
        second = self.h.db_dir() / "receipt-fill.csv"
        second.write_text(
            "ticket,asset,model,part,firmware,fault,warranty,reservation,dispatch\n"
            "T7,A-77,P-4,BEAR-1,3.4,bearing,valid,held,queued\n",
            encoding="utf-8",
        )
        receipt = self.h.service.ingest_tickets(second)
        self.assertEqual(receipt.updated[0]["filled"], ["asset"])
        self.assertEqual(self.h.service.tickets.get("T7")["asset"], "A-77")


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.h.load_samples()

    def test_failed_source_does_not_stop_others(self):
        config = self.h.db_dir() / "sources.json"
        config.write_text(json.dumps({"sources": [
            {"name": "inventory", "kind": "inventory",
             "path": str(self.h.samples / "inventory.csv"), "options": {}},
            {"name": "catalog", "kind": "catalog",
             "path": str(self.h.db_dir() / "missing.csv"), "options": {}},
            {"name": "tickets", "kind": "tickets",
             "path": str(self.h.samples / "tickets.csv"), "options": {}},
        ]}), encoding="utf-8")
        sources = [Source(**s) for s in json.loads(config.read_text())["sources"]]
        logs = []
        report = Scheduler(self.h.service, sources, logger=logs.append).run_once()
        self.assertEqual([r["ok"] for r in report], [True, False, True])
        self.assertIsNotNone(self.h.service.db.get_meta("last_success:inventory"))
        self.assertIsNone(self.h.service.db.get_meta("last_success:catalog"))
        self.assertTrue(any("来源文件不存在" in line for line in logs))

    def test_run_forever_bounded(self):
        calls = []
        scheduler = Scheduler(self.h.service, [], sleep=calls.append, logger=lambda _: None)
        done = scheduler.run_forever(interval=5, iterations=3)
        self.assertEqual(done, 3)
        self.assertEqual(calls, [5, 5])


class PortalAdapters(unittest.TestCase):
    def test_unknown_response_is_pending_not_success(self):
        from spare_service.portal import ScriptedPortal

        h = Harness()
        h.load_samples()
        h.appointment("T1")
        h.confirm_all("T1")
        scripted = ScriptedPortal(reserve_responses=[{"state": "???"}])
        h.service.set_portal(scripted)
        result = h.service.reserve(CS, "T1")
        self.assertEqual(result["state"], "pending")
        self.assertFalse(h.service.evaluate("T1", persist=False)["can_dispatch"])

    def test_scripted_conflict(self):
        from spare_service.portal import ScriptedPortal

        h = Harness()
        h.load_samples()
        h.appointment("T1")
        h.confirm_all("T1")
        h.service.set_portal(ScriptedPortal(
            reserve_responses=[{"state": "conflict", "reason": "被占用"}]
        ))
        self.assertEqual(h.service.reserve(CS, "T1")["state"], "failed")

    def test_demo_portal_deduplicates_by_request_key(self):
        from spare_service.portal import DemoPortal

        h = Harness()
        portal = h.portal
        first = portal.reserve(request_key="k1", partner="D-1", item="BEAR-1",
                               quantity=1, ticket="T1")
        second = portal.reserve(request_key="k1", partner="D-1", item="BEAR-1",
                                quantity=1, ticket="T1")
        self.assertEqual(first["portal_ref"], second["portal_ref"])
        self.assertTrue(second.get("deduplicated"))
        self.assertEqual(portal.stock[("D-1", "BEAR-1")], 2)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db_path = str(self.tmp / "cli.db")
        self.h = Harness(db_path=self.db_path)
        self.h.load_samples()

    def run_cli(self, *argv):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = cli.main(["--db", self.db_path, *argv])
        return code, buffer.getvalue()

    def test_board_lists_tickets(self):
        code, out = self.run_cli("board", "--json")
        self.assertEqual(code, 0)
        rows = json.loads(out)
        self.assertEqual({r["ticket"] for r in rows}, {"T1", "T2", "T3"})

    def test_explain_shows_exclusion_reasons(self):
        code, out = self.run_cli("explain", "T1")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["verdict"], "pending")
        text = " ".join(payload["explanation"])
        self.assertIn("保修", text)
        self.assertIn("门户预留", text)

    def test_dispatch_via_cli_reports_error_code(self):
        code, out = self.run_cli("dispatch", "T1")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["error"]["code"], "dispatch_blocked")


if __name__ == "__main__":
    unittest.main()
