"""服务工单回执采集。

回执只提供事实（资产、型号、故障、预约时段、门户预留/派单原始状态）。
回执里的 "valid"/"held"/"queued" 不会直接让内部状态变成可派单——
保修、资产、备件三项确认与预留回执仍需各自角色在系统中完成。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .db import to_iso
from .ingest import parse_tickets
from .models import DispatchState


@dataclass
class TicketReceipt:
    created: list
    updated: list
    unchanged: list
    conflicts: list


class TicketIngest:
    def __init__(self, db, clock):
        self.db = db
        self.clock = clock

    def ingest(self, path, *, source="service_desk", run_id=None):
        rows = parse_tickets(path)
        now = self.clock.now()
        created, updated, unchanged, conflicts = [], [], [], []
        with self.db.tx():
            for row in rows:
                existing = self.db.one("SELECT * FROM tickets WHERE ticket = ?", (row.ticket,))
                if existing is None:
                    self.db.insert(
                        "tickets",
                        {
                            "ticket": row.ticket,
                            "asset": row.asset,
                            "model": row.model,
                            "part": row.part,
                            "firmware": row.firmware,
                            "fault": row.fault,
                            "warranty": row.warranty,
                            "source_reservation": row.reservation,
                            "source_dispatch": row.dispatch,
                            "dispatch_state": DispatchState.DRAFT.value,
                            "created_at": to_iso(now),
                            "updated_at": to_iso(now),
                        },
                    )
                    created.append(row.ticket)
                    continue

                incoming = {
                    "asset": row.asset, "model": row.model, "part": row.part,
                    "firmware": row.firmware, "fault": row.fault,
                    "warranty": row.warranty,
                }
                mismatch = {
                    field: {"current": existing[field], "incoming": value}
                    for field, value in incoming.items()
                    if value is not None and existing[field] not in (None, value)
                }
                changes = {
                    field: value for field, value in incoming.items()
                    if value is not None and existing[field] is None
                }
                source_changes = {}
                if row.reservation is not None and row.reservation != existing["source_reservation"]:
                    source_changes["source_reservation"] = row.reservation
                if row.dispatch is not None and row.dispatch != existing["source_dispatch"]:
                    source_changes["source_dispatch"] = row.dispatch

                if mismatch:
                    conflicts.append({"ticket": row.ticket, "fields": mismatch,
                                      "action": "保留系统已有值，交人工核对"})
                if changes or source_changes:
                    self.db.update("tickets", "ticket", row.ticket,
                                   {**changes, **source_changes, "updated_at": to_iso(now)})
                    updated.append({"ticket": row.ticket,
                                    "filled": sorted(changes), "source_changed": sorted(source_changes)})
                elif not mismatch:
                    unchanged.append(row.ticket)
                if mismatch:
                    self.db.event(ticket=row.ticket, at=now, kind="receipt_conflict",
                                  message="服务工单回执与系统记录不一致，已保留系统值",
                                  actor="system", data={"mismatch": mismatch})
            self.db.insert(
                "ingest_log",
                {
                    "job": "tickets", "started_at": to_iso(now), "finished_at": to_iso(now),
                    "ok": 1,
                    "detail": json.dumps({"source": source, "created": created,
                                          "updated": updated, "unchanged": len(unchanged),
                                          "conflicts": conflicts}, ensure_ascii=False),
                },
            )
        return TicketReceipt(created, updated, unchanged, conflicts)
