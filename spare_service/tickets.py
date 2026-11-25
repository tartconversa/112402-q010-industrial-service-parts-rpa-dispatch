"""工单、现场配置与三类确认。

确认为"追加记录"，不覆盖历史；每条确认记录当时依据的目录修订或配置版本，
后续目录或现场配置变化时，旧的确认会失效，需要重新核验。

没有确认就没有结论：系统只会把它标成待核选项。
"""

from __future__ import annotations

import json

from .db import to_iso
from .errors import Conflict, NotFound, NotConfirmed, ValidationError
from .models import (
    DispatchState,
    Role,
    WarrantyState,
    actor_name,
    actor_roles,
    require_role,
)
from .catalog import Catalog
from .versions import firmware_in_range


class Tickets:
    def __init__(self, db, clock):
        self.db = db
        self.clock = clock
        self.catalog = Catalog(db, clock)

    # ------------------------------------------------------------------ 基础
    def get(self, ticket):
        row = self.db.one("SELECT * FROM tickets WHERE ticket = ?", (ticket,))
        if row is None:
            raise NotFound(f"没有工单 {ticket}", code="ticket_not_found")
        return row

    def all(self):
        return self.db.query("SELECT * FROM tickets ORDER BY ticket")

    def create(self, ticket, *, asset=None, model=None, part=None, firmware=None,
               fault=None, warranty=None, source_reservation=None, source_dispatch=None):
        if self.db.one("SELECT 1 FROM tickets WHERE ticket = ?", (ticket,)):
            raise Conflict(f"工单 {ticket} 已存在", code="ticket_exists")
        now = to_iso(self.clock.now())
        self.db.insert(
            "tickets",
            {
                "ticket": ticket,
                "asset": asset,
                "model": model,
                "part": part,
                "firmware": firmware,
                "fault": fault,
                "warranty": warranty,
                "source_reservation": source_reservation,
                "source_dispatch": source_dispatch,
                "dispatch_state": DispatchState.DRAFT.value,
                "created_at": now,
                "updated_at": now,
            },
        )
        self.db.event(ticket=ticket, at=self.clock.now(), kind="ticket_created",
                      message=f"工单 {ticket} 已建立", actor="system")

    # ------------------------------------------------------------------ 回填现场配置
    def set_site_config(self, actor, ticket, *, asset=None, model=None, firmware=None,
                        config=None, note=None):
        """服务人员回填现场配置。资产管理员确认前，这只是"上报值"，不能直接用于派单。"""
        require_role(actor, [Role.ASSET])
        row = self.get(ticket)
        config = dict(config or {})
        if asset:
            config["asset"] = asset
        if model:
            config["model"] = model
        if firmware:
            config["firmware"] = firmware
        if not any(k in config for k in ("asset", "model", "firmware")):
            raise ValidationError("现场配置至少需要资产编号、型号或固件版本之一", code="empty_config")
        now = self.clock.now()
        with self.db.tx():
            revision = row["config_revision"] + 1
            # 回填值合并到当前配置之上；资产管理员确认的是合并后的整体。
            merged = dict(json.loads(row["site_config"] or "{}"))
            merged.update(config)
            self.db.update(
                "tickets", "ticket", ticket,
                {
                    "site_config": json.dumps(merged, ensure_ascii=False, sort_keys=True),
                    "config_source": note or actor_name(actor),
                    "config_revision": revision,
                    "updated_at": to_iso(now),
                },
            )
        self.db.event(
            ticket=ticket, at=now, kind="site_config_updated",
            message=f"现场配置更新为第 {revision} 版",
            actor=actor_name(actor), role=Role.ASSET.value,
            data={"config": merged, "note": note},
        )
        return {"revision": revision, "config": merged}

    # ------------------------------------------------------------------ 确认
    def _add_confirmation(self, actor, ticket, kind, status, details, note, roles):
        require_role(actor, roles)
        self.get(ticket)
        roles_set = actor_roles(actor)
        role = next((r for r in roles_set if r in {x.value for x in roles}), sorted(roles_set)[0] if roles_set else "unknown")
        now = self.clock.now()
        with self.db.tx():
            self._maybe_invalidate(ticket, kind, now)
            self.db.insert(
                "confirmations",
                {
                    "ticket": ticket,
                    "kind": kind,
                    "status": status,
                    "actor": actor_name(actor),
                    "role": role,
                    "decided_at": to_iso(now),
                    "details": json.dumps(details, ensure_ascii=False, sort_keys=True),
                    "note": note,
                },
            )
            if kind == "asset":
                row = self.get(ticket)
                self.db.update(
                    "tickets", "ticket", ticket,
                    {"confirmed_config_revision": row["config_revision"],
                     "confirmed_config_at": to_iso(now)},
                )
                # 确认值同时作为现场配置的事实来源；回填只是上报值。
                confirmed = {
                    "asset": details.get("serial"),
                    "model": details.get("model"),
                    "firmware": details.get("firmware"),
                    **dict(details.get("config") or {}),
                }
                self.db.update(
                    "tickets", "ticket", ticket,
                    {"site_config": json.dumps(confirmed, ensure_ascii=False, sort_keys=True)},
                )
        labels = {"asset": "资产与现场配置", "warranty": "故障与保修范围", "part": "备件与替代关系"}
        states = {"confirmed": "已确认", "rejected": "未通过", "review": "待复核"}
        self.db.event(
            ticket=ticket, at=now, kind=f"{kind}_confirmation",
            message=f"{labels.get(kind, kind)}{states.get(status, status)}",
            actor=actor_name(actor), role=role,
            data={"status": status, "details": details, "note": note},
        )
        # 确认变化后，未派出的工单立即按新状态重算；已派出的交服务主管。
        return self._refresh_after_change(ticket, reason=f"{kind} 确认更新")

    def confirm_asset(self, actor, ticket, *, serial, model, firmware, config=None, note=None):
        """资产管理员确认设备序列号与现场配置。"""
        if not serial or not model or not firmware:
            raise NotConfirmed(
                "资产确认必须提供序列号、型号和固件版本", code="incomplete_asset_confirmation"
            )
        details = {
            "serial": serial.strip(),
            "model": model.strip(),
            "firmware": firmware.strip(),
            "config": dict(config or {}),
        }
        # 固件必须可解析，避免把 "最新" 这类模糊值当成版本比较
        firmware_in_range(details["firmware"], None, None)
        row = self.get(ticket)
        if row["asset"] and row["asset"] != details["serial"]:
            raise Conflict(
                f"资产序列号不一致：工单为 {row['asset']}，确认为 {details['serial']}",
                code="asset_mismatch",
            )
        if not row["asset"]:
            # 回执没有资产序列号时，以资产管理员确认值为准，留下变更记录。
            self.db.update("tickets", "ticket", ticket,
                           {"asset": details["serial"], "updated_at": to_iso(self.clock.now())})
        return self._add_confirmation(
            actor, ticket, "asset", "confirmed", details, note, [Role.ASSET]
        )

    def confirm_warranty(self, actor, ticket, *, covered, warranty_state, fault=None, note=None):
        """保修专员确认故障与保修范围。covered 必须是明确布尔值。"""
        if covered not in (True, False):
            raise NotConfirmed("保修确认必须明确覆盖/不覆盖，不能用未知值代替", code="incomplete_warranty_confirmation")
        if warranty_state not in {s.value for s in WarrantyState}:
            raise ValidationError(f"未知的保修状态：{warranty_state!r}", code="bad_warranty_state")
        details = {
            "covered": bool(covered),
            "warranty_state": warranty_state,
            "fault": fault or self.get(ticket)["fault"],
        }
        status = "confirmed" if covered and warranty_state == WarrantyState.VALID.value else "rejected"
        if warranty_state == WarrantyState.REVIEW.value:
            status = "review"
        return self._add_confirmation(
            actor, ticket, "warranty", status, details, note, [Role.WARRANTY]
        )

    def confirm_part(self, actor, ticket, *, part, model, firmware_from, firmware_to,
                     replacement=None, catalog_revision_id=None, note=None):
        """备件工程师确认替代关系及适用固件。

        catalog_revision_id 是确认时看到的有效目录修订；目录一旦更新，这条确认自动失效。
        """
        if not part or not model:
            raise ValidationError("备件确认必须提供备件号与型号", code="incomplete_part_confirmation")
        if not firmware_from and not firmware_to:
            raise NotConfirmed(
                "备件确认必须给出适用固件范围，不能默认适用全部版本",
                code="missing_firmware_scope",
            )
        # 记录确认所依据的目录修订；调用方不传时，取当前有效修订。
        # 目录一旦更新，这条确认就会在评估时被判定为失效，而不是继续沿用旧结论。
        if catalog_revision_id is None:
            current = self.catalog.effective(part.strip(), model.strip())
            if current is not None:
                catalog_revision_id = current.id
        details = {
            "part": part.strip(),
            "model": model.strip(),
            "replacement": replacement.strip() if replacement else None,
            "firmware_from": firmware_from or None,
            "firmware_to": firmware_to or None,
            "catalog_revision_id": catalog_revision_id,
        }
        return self._add_confirmation(actor, ticket, "part", "confirmed", details, note, [Role.PART])

    def _maybe_invalidate(self, ticket, kind, now):
        # 确认是追加的，旧记录保留；失效判断在评估时按"最新有效"处理。
        return None

    # ------------------------------------------------------------------ 最新确认
    def latest_confirmation(self, ticket, kind):
        row = self.db.one(
            "SELECT * FROM confirmations WHERE ticket = ? AND kind = ? "
            "ORDER BY decided_at DESC, id DESC LIMIT 1",
            (ticket, kind),
        )
        if row is None:
            return None
        out = dict(row)
        out["details"] = json.loads(row["details"] or "{}")
        return out

    def confirmation_state(self, ticket):
        row = self.get(ticket)
        asset = self.latest_confirmation(ticket, "asset")
        warranty = self.latest_confirmation(ticket, "warranty")
        part = self.latest_confirmation(ticket, "part")

        asset_ok = bool(asset and asset["status"] == "confirmed")
        config = json.loads(row["site_config"] or "{}")
        effective = None
        if asset_ok:
            effective = dict(asset["details"].get("config") or {})
            effective.update({
                "asset": asset["details"].get("serial"),
                "model": asset["details"].get("model"),
                "firmware": asset["details"].get("firmware"),
            })
            # 确认后又回填了新配置：确认针对的是旧配置版本，必须重新核验。
            confirmed_rev = row["confirmed_config_revision"]
            if confirmed_rev is not None and row["config_revision"] > confirmed_rev:
                asset_ok = False
                asset = dict(asset, status="superseded",
                             stale_reason="资产确认后现场配置已更新")

        return {
            "asset": asset,
            "warranty": warranty,
            "part": part,
            "asset_ok": asset_ok,
            "effective_config": effective,
            "reported_config": config,
            "firmware": (effective or {}).get("firmware"),
        }

    # ------------------------------------------------------------------ 变更后的复核
    def _refresh_after_change(self, ticket, *, reason):
        row = self.get(ticket)
        if row["dispatch_state"] in {DispatchState.DISPATCHED.value, DispatchState.SUPERVISOR_REVIEW.value}:
            self.db.update(
                "tickets", "ticket", ticket,
                {
                    "dispatch_state": DispatchState.SUPERVISOR_REVIEW.value,
                    "status_note": reason,
                    "updated_at": to_iso(self.clock.now()),
                },
            )
            self.db.event(
                ticket=ticket, at=self.clock.now(), kind="supervisor_review_required",
                message=f"已派出工单需要服务主管处理：{reason}",
                actor="system", role=Role.SUPERVISOR.value, data={"reason": reason},
            )
            return {"state": DispatchState.SUPERVISOR_REVIEW.value, "reason": reason}
        # 未派出：只有原本已通过检查的工单才需要显式标记复核；
        # 草稿/阻断工单的结论由调用方重新评估后自然更新。
        if row["dispatch_state"] != DispatchState.QUEUED.value:
            return {"state": row["dispatch_state"], "reason": reason}
        self.db.update(
            "tickets", "ticket", ticket,
            {"dispatch_state": DispatchState.NEEDS_RECHECK.value,
             "status_note": reason, "updated_at": to_iso(self.clock.now())},
        )
        self.db.event(
            ticket=ticket, at=self.clock.now(), kind="recheck_required",
            message=f"未派出工单需要重新核验：{reason}",
            actor="system", data={"reason": reason},
        )
        return {"state": DispatchState.NEEDS_RECHECK.value, "reason": reason}
