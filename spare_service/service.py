"""面向客服的服务门面。

把采集、确认、评估、预留、派单串成一个入口，并在每次状态变化后记录事件，
使客服可以追溯"为何选用或排除某备件、库存依据、门户预留回执、工单后续状态"。
"""

from __future__ import annotations

import csv
import json
from datetime import timedelta
from pathlib import Path

from .catalog import Catalog
from .clock import BusinessClock
from .db import Database, to_iso
from .errors import Conflict, ValidationError
from .evaluation import DispatchEvaluator
from .inventory import Inventory
from .models import (
    DispatchState,
    Role,
    actor_name,
    require_role,
)
from .receipts import TicketIngest
from .reservations import Reservations
from .tickets import Tickets


class Service:
    def __init__(self, db=None, *, clock=None, portal=None, max_inventory_age=timedelta(minutes=30),
                 db_path=":memory:"):
        self.db = db or Database(db_path)
        self.clock = clock or BusinessClock(max_inventory_age=max_inventory_age)
        if getattr(self.clock, "max_inventory_age", None) is None:
            self.clock.max_inventory_age = max_inventory_age
        self.tickets = Tickets(self.db, self.clock)
        self.catalog = Catalog(self.db, self.clock)
        self.inventory = Inventory(self.db, self.clock)
        self.evaluator = DispatchEvaluator(self.db, self.clock)
        self.receipts = TicketIngest(self.db, self.clock)
        self._portal = portal
        self.reservations = Reservations(self.db, self.clock, portal, self.inventory)

    @property
    def portal(self):
        return self._portal

    def set_portal(self, portal):
        self._portal = portal
        self.reservations.portal = portal

    # ------------------------------------------------------------------ 采集
    def ingest_inventory(self, path, *, source="dealer_portal"):
        result = self.inventory.ingest(path, source=source)
        self._log_ingest("inventory", result)
        self._recheck_after_ingest("库存更新")
        return result

    def ingest_catalog(self, path, *, source="manufacturer_catalog", complete=True):
        result = self.catalog.ingest(path, source=source, complete=complete)
        self._log_ingest("catalog", result)
        if result.get("changed"):
            self._recheck_catalog_changes(result)
        return result

    def ingest_tickets(self, path, *, source="service_desk"):
        result = self.receipts.ingest(path, source=source)
        self._log_ingest("tickets", {
            "created": result.created, "updated": len(result.updated),
            "conflicts": result.conflicts,
        })
        for ticket in result.created:
            self.evaluator.evaluate(ticket, persist=True, actor="ingest")
        return result

    def _log_ingest(self, job, detail):
        now = self.clock.now()
        self.db.insert(
            "ingest_log",
            {
                "job": job,
                "started_at": to_iso(now),
                "finished_at": to_iso(now),
                "ok": 1,
                "detail": json.dumps(detail, ensure_ascii=False, default=str),
            },
        )

    # ------------------------------------------------------------------ 复核
    def _open_tickets(self):
        rows = self.db.query("SELECT * FROM tickets ORDER BY ticket")
        return [r for r in rows if r["dispatch_state"] not in {
            DispatchState.DISPATCHED.value, DispatchState.CANCELLED.value,
            DispatchState.SUPERVISOR_REVIEW.value,
        }]

    def _recheck_after_ingest(self, reason):
        """库存更新不会改变兼容性结论，只刷新库存与可派单状态。"""
        for row in self._open_tickets():
            self.evaluator.evaluate(row["ticket"], persist=True, actor="ingest")
        return len(self._open_tickets())

    def _recheck_catalog_changes(self, result):
        """目录发生变化时：
        - 未派出的工单重新核验，不兼容则退回待核选项；
        - 已派出的工单提示服务主管，不自动改派。
        """
        affected = []
        for row in self.db.query("SELECT * FROM tickets ORDER BY ticket"):
            if row["dispatch_state"] in {DispatchState.CANCELLED.value}:
                continue
            if row["dispatch_state"] not in {
                DispatchState.DISPATCHED.value, DispatchState.SUPERVISOR_REVIEW.value
            } and row["source_dispatch"] in {"closed", "cancelled"}:
                continue
            if row["dispatch_state"] in {
                DispatchState.DISPATCHED.value, DispatchState.SUPERVISOR_REVIEW.value
            }:
                if self._ticket_touched_by_catalog(row, result["changed"]):
                    self.tickets._refresh_after_change(
                        row["ticket"], reason="厂商目录更新影响已派出工单，交服务主管处理"
                    )
                    affected.append(row["ticket"])
                continue
            before = row["dispatch_state"]
            after = self.evaluator.evaluate(row["ticket"], persist=True, actor="ingest")
            if before == DispatchState.QUEUED.value and not after["can_dispatch"]:
                # 评估会把状态落成 blocked/draft；这里显式保留"待复核"标记。
                self._mark_recheck(row["ticket"],
                                   reason="厂商目录更新后四项检查不再通过，需重新核验")
                affected.append(row["ticket"])
            elif not after["can_dispatch"]:
                affected.append(row["ticket"])
        return affected

    def _mark_recheck(self, ticket, *, reason):
        now = self.clock.now()
        with self.db.tx():
            self.db.update("tickets", "ticket", ticket,
                           {"dispatch_state": DispatchState.NEEDS_RECHECK.value,
                            "status_note": reason, "updated_at": to_iso(now)})
            self.db.event(ticket=ticket, at=now, kind="recheck_required",
                          message=f"未派出工单需要重新核验：{reason}", actor="system",
                          data={"reason": reason})

    def _ticket_touched_by_catalog(self, row, changes):
        """目录变更是否真的影响这张工单。

        只看这张工单用到的备件（原申请件、已预留件、工程师确认安装件），
        并且只有替代关系、固件范围、停产状态这类实质性变化才算影响；
        单纯修订号递进不惊动服务主管。
        """
        parts = {row["part"], row["reserved_part"]}
        model = row["model"]
        confirmation = self.tickets.latest_confirmation(row["ticket"], "part")
        if confirmation:
            parts.add(confirmation["details"].get("part"))
            parts.add(confirmation["details"].get("replacement"))
            model = confirmation["details"].get("model") or model
        parts.discard(None)

        substantive = {"replacement", "firmware_range", "status"}
        for change in changes:
            if change.get("part") not in parts:
                continue
            if change.get("model") not in (None, model):
                continue
            kind = change.get("kind")
            if kind in {"entry_removed", "replacement_withdrawn"}:
                return True
            if kind == "revision_added":
                return True
            if kind == "revision_replaced":
                if substantive & set(change.get("changes") or []):
                    return True
        return False

    # ------------------------------------------------------------------ 确认
    def confirm_asset(self, actor, ticket, **kwargs):
        result = self.tickets.confirm_asset(actor, ticket, **kwargs)
        self.evaluator.evaluate(ticket, persist=True, actor=actor)
        return result

    def confirm_warranty(self, actor, ticket, **kwargs):
        result = self.tickets.confirm_warranty(actor, ticket, **kwargs)
        self.evaluator.evaluate(ticket, persist=True, actor=actor)
        return result

    def confirm_part(self, actor, ticket, **kwargs):
        result = self.tickets.confirm_part(actor, ticket, **kwargs)
        self.evaluator.evaluate(ticket, persist=True, actor=actor)
        return result

    def set_site_config(self, actor, ticket, **kwargs):
        result = self.tickets.set_site_config(actor, ticket, **kwargs)
        # 现场配置变化会让此前的资产/备件确认失效。
        # 已派出的交服务主管；未派出的标为待复核并重新评估。
        self.tickets._refresh_after_change(ticket, reason="现场配置回填更新")
        self.evaluator.evaluate(ticket, persist=True, actor=actor)
        if self.tickets.get(ticket)["dispatch_state"] == DispatchState.NEEDS_RECHECK.value:
            self._mark_recheck(ticket, reason="现场配置回填更新，需要重新核验")
        return result

    def set_appointment(self, actor, ticket, *, start, end=None, source="manual"):
        require_role(actor, [Role.CS, Role.SUPERVISOR])
        self.tickets.get(ticket)
        if start is None:
            raise ValidationError("预约开始时间不能为空", code="bad_appointment")
        from .db import parse_time

        start_iso = to_iso(parse_time(start))
        end_iso = to_iso(parse_time(end)) if end else None
        with self.db.tx():
            self.db.update("tickets", "ticket", ticket, {
                "appointment_start": start_iso, "appointment_end": end_iso,
                "appointment_source": source, "updated_at": to_iso(self.clock.now()),
            })
        self.db.event(ticket=ticket, at=self.clock.now(), kind="appointment_set",
                      message=f"预约时段更新为 {start_iso} 至 {end_iso or '未指定'}",
                      actor=actor_name(actor), role=Role.CS.value)
        self.tickets._refresh_after_change(ticket, reason="预约时段变更")
        return self.evaluator.evaluate(ticket, persist=True, actor=actor)

    # ------------------------------------------------------------------ 预留/派单
    def reserve(self, actor, ticket, *, partner=None, item=None, quantity=1):
        require_role(actor, [Role.CS])
        check = self.evaluator.evaluate(ticket, persist=True, actor=actor)
        if not check["can_reserve"]:
            raise Conflict(
                "四项检查未通过，不能发起预留",
                code="reservation_not_allowed",
                details={"reasons": check["pending_or_blocking"]},
            )
        if item is None:
            item = check["selected_part"]
        if partner is None:
            selected = check["gates"]["inventory"]["evidence"].get("selected")
            if selected is None:
                raise Conflict("没有可用的库存来源", code="no_stock")
            partner = selected["partner"]
        result = self.reservations.reserve(actor, ticket, partner=partner, item=item,
                                           quantity=quantity)
        # 预留成功后重新评估，使工单从"可预留"进入"可派单"，而不是停留在草稿。
        self.evaluator.evaluate(ticket, persist=True, actor=actor)
        return result

    def reconcile_reservation(self, actor, reservation_id, *, reason="客服查询现有预留"):
        return self.reservations.reconcile(actor, reservation_id, reason=reason)

    def retry_reservation(self, actor, ticket, *, partner, item, quantity=1):
        return self.reservations.retry(actor, ticket, partner=partner, item=item, quantity=quantity)

    def release_on_cancel(self, actor, ticket, *, reason="订单取消"):
        return self.reservations.release_on_cancel(actor, ticket, reason=reason)

    def dispatch(self, actor, ticket):
        """派单前的最终门禁：保修、兼容、库存新鲜度、预约、门户预留回执必须同时通过。"""
        require_role(actor, [Role.CS])
        check = self.evaluator.evaluate(ticket, persist=True, actor=actor)
        if not check["can_dispatch"]:
            raise Conflict(
                "派单检查未通过",
                code="dispatch_blocked",
                details={"reasons": check["pending_or_blocking"], "verdict": check["verdict"]},
            )
        now = self.clock.now()
        result = {
            "ticket": ticket,
            "state": DispatchState.DISPATCHED.value,
            "checked_at": to_iso(now),
            "reservation": check["gates"]["reservation"]["evidence"],
            "stock": check["gates"]["inventory"]["evidence"].get("selected"),
            "selected_part": check["selected_part"],
        }
        with self.db.tx():
            current = self.tickets.get(ticket)
            if current["dispatch_state"] in {
                DispatchState.DISPATCHED.value, DispatchState.CANCELLED.value
            }:
                raise Conflict(f"工单当前状态为 {current['dispatch_state']}，不能重复派单",
                               code="bad_dispatch_state")
            self.db.update(
                "tickets", "ticket", ticket,
                {"dispatch_state": DispatchState.DISPATCHED.value,
                 "status_note": "已派单", "updated_at": to_iso(now)},
            )
            self.db.event(
                ticket=ticket, at=now, kind="dispatched",
                message=f"派单：{result['selected_part']}，预留回执 "
                        f"{result['reservation'].get('portal_ref')}",
                actor=actor_name(actor), role=Role.CS.value, data=result,
            )
        return result

    # ------------------------------------------------------------------ 查看
    def evaluate(self, ticket, **kwargs):
        return self.evaluator.evaluate(ticket, **kwargs)

    def explain(self, ticket):
        return self.evaluator.explain(ticket)

    def board(self):
        """客服工作台：每张工单的结论、缺失确认与后续状态。"""
        out = []
        for row in self.db.query("SELECT * FROM tickets ORDER BY ticket"):
            result = self.evaluator.evaluate(row["ticket"], persist=False)
            out.append({
                "ticket": row["ticket"],
                "asset": row["asset"],
                "model": row["model"],
                "part": row["part"],
                "state": row["dispatch_state"],
                "verdict": result["verdict"],
                "can_reserve": result["can_reserve"],
                "can_dispatch": result["can_dispatch"],
                "blockers": result["pending_or_blocking"],
                "selected_part": result["selected_part"],
                "source_warranty": row["warranty"],
                "source_reservation": row["source_reservation"],
                "source_dispatch": row["source_dispatch"],
            })
        return out

    def supervisor_queue(self):
        """服务主管待办：已派出但受目录/配置变更影响的工单，附变更原因与预留回执。"""
        out = []
        for row in self.db.query(
            "SELECT * FROM tickets WHERE dispatch_state = ? ORDER BY ticket",
            (DispatchState.SUPERVISOR_REVIEW.value,),
        ):
            reservations = self.db.query(
                "SELECT id, partner, item, state, portal_ref FROM reservations "
                "WHERE ticket = ? ORDER BY id DESC", (row["ticket"],),
            )
            out.append({
                "ticket": row["ticket"],
                "asset": row["asset"],
                "model": row["model"],
                "part": row["part"],
                "reserved_part": row["reserved_part"],
                "reason": row["status_note"],
                "source_dispatch": row["source_dispatch"],
                "reservations": [dict(r) for r in reservations],
            })
        return out

    def export_board(self, path):
        rows = self.board()
        fields = ["ticket", "asset", "model", "part", "state", "verdict", "can_reserve",
                  "can_dispatch", "selected_part", "source_warranty", "source_reservation",
                  "source_dispatch", "blockers"]
        with Path(path).open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow({**row, "blockers": " | ".join(row["blockers"])})
        return path

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
