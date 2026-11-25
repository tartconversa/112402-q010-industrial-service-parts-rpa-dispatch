"""测试夹具：统一的时钟、门户和样例数据构造。"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from spare_service import Service  # noqa: E402
from spare_service.clock import FixedClock  # noqa: E402
from spare_service.portal import DemoPortal  # noqa: E402

NOW = "2026-10-07T09:10:00+08:00"
CS = {"name": "cs-1", "role": "customer_service"}
ASSET = {"name": "asset-admin", "role": "asset_admin"}
WARRANTY = {"name": "warranty-1", "role": "warranty_specialist"}
ENGINEER = {"name": "part-1", "role": "part_engineer"}
SUPERVISOR = {"name": "supervisor-1", "role": "service_supervisor"}


class Harness:
    """一个默认把 T1 修到"可预留"状态的服务实例。"""

    def __init__(self, *, now=NOW, stock=None, max_age=30, portal=None, db_path=":memory:"):
        self.clock = FixedClock(now, max_inventory_age=timedelta(minutes=max_age))
        self.portal = portal or DemoPortal(
            self.clock,
            stock=stock if stock is not None else {("D-1", "BEAR-1"): 3, ("D-2", "BEAR-2"): 1},
        )
        self.service = Service(clock=self.clock, portal=self.portal, db_path=db_path)
        self.db = self.service.db
        self.samples = Path(__file__).resolve().parent.parent / "samples"

    # -- 采集 ------------------------------------------------------------
    def load_samples(self):
        self.service.ingest_inventory(self.samples / "inventory.csv")
        self.service.ingest_catalog(self.samples / "compatibility.csv")
        self.service.ingest_tickets(self.samples / "tickets.csv")
        return self.service

    def ticket(self, ticket="T9", *, model="P-4", part="BEAR-1", firmware="3.4",
               fault="bearing", warranty="valid"):
        self.service.tickets.create(
            ticket, asset=f"A-99", model=model, part=part, firmware=firmware,
            fault=fault, warranty=warranty,
        )
        return ticket

    def inventory(self, rows, *, source="dealer_portal"):
        path = self.db_dir() / f"inventory-{_digest(rows)}.csv"
        path.write_text(
            "stock,partner,item,quantity,checked\n"
            + "\n".join(f"S{i},{p},{item},{qty},{checked}" for i, (p, item, qty, checked) in enumerate(rows))
            + "\n",
            encoding="utf-8",
        )
        return self.service.ingest_inventory(path, source=source)

    def catalog(self, rows, *, complete=True):
        path = self.db_dir() / f"catalog-{_digest(rows)}.csv"
        path.write_text(
            "part,model,firmware_from,firmware_to,replacement,revision,status\n"
            + "\n".join(
                f"{part},{model},{low},{high},{repl},{rev},{status}"
                for part, model, low, high, repl, rev, status in rows
            )
            + "\n",
            encoding="utf-8",
        )
        return self.service.ingest_catalog(path, complete=complete)

    def db_dir(self):
        folder = Path(__file__).resolve().parent / "_tmp"
        folder.mkdir(exist_ok=True)
        return folder

    # -- 确认 ------------------------------------------------------------
    def confirm_all(self, ticket="T1", *, model="P-4", firmware="3.4", part="BEAR-1",
                    low="3.0", high="3.9", replacement=None, covered=True,
                    warranty_state="valid", serial=None):
        service = self.service
        service.confirm_asset(ASSET, ticket, serial=serial or _serial_for(ticket, self.service),
                              model=model, firmware=firmware)
        service.confirm_warranty(WARRANTY, ticket, covered=covered,
                                 warranty_state=warranty_state)
        service.confirm_part(ENGINEER, ticket, part=part, model=model,
                             firmware_from=low, firmware_to=high, replacement=replacement)
        return service

    def appointment(self, ticket, *, start="2026-10-07T11:00:00+08:00",
                    end="2026-10-07T13:00:00+08:00"):
        return self.service.set_appointment(CS, ticket, start=start, end=end)

    def ready_ticket(self, ticket="T1", *, part="BEAR-1", model="P-4", firmware="3.4",
                     low="3.0", high="3.9", replacement=None):
        self.appointment(ticket)
        self.confirm_all(ticket, model=model, firmware=firmware, part=part,
                         low=low, high=high, replacement=replacement)
        return self.service.evaluate(ticket, persist=False)


def _serial_for(ticket, service):
    row = service.tickets.get(ticket)
    return row["asset"] or "A-99"


def _digest(rows):
    import hashlib

    return hashlib.sha256(repr(rows).encode()).hexdigest()[:12]


def grades(harness, ticket):
    result = harness.service.evaluate(ticket, persist=False)
    return {name: gate["status"] for name, gate in result["gates"].items()}
