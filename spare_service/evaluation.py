"""派单前的四项检查：保修决定、备件兼容、库存新鲜度、预约时段。

四项同时评估，任何一项不通过都不能派单。缺少必要输入时返回"待核选项"，
而不是挑一件有库存的备件当成兼容。

每个结论都附带证据和理由，客服可以直接看到为什么选用或排除某个来源。
"""

from __future__ import annotations

import json
from datetime import timedelta

from .catalog import Catalog
from .versions import firmware_in_range
from .db import to_iso
from .errors import ValidationError
from .inventory import Inventory
from .models import DispatchState, ReservationState, actor_name
from .tickets import Tickets

PASS, PENDING, BLOCKED = "pass", "pending", "blocked"


def _gate(status, summary, **evidence):
    return {"status": status, "summary": summary, "evidence": evidence}


class DispatchEvaluator:
    def __init__(self, db, clock):
        self.db = db
        self.clock = clock
        self.tickets = Tickets(db, clock)
        self.catalog = Catalog(db, clock)
        self.inventory = Inventory(db, clock)

    # ------------------------------------------------------------------ 入口
    def evaluate(self, ticket, *, quantity=1, max_inventory_age=None, persist=True,
                 actor="system"):
        """四项核心检查决定"能否去预留"；再叠加门户预留回执才决定"能否派单"。"""
        row = self.tickets.get(ticket)
        confirmations = self.tickets.confirmation_state(ticket)

        warranty_gate, warranty_ok = self._warranty_gate(row, confirmations)
        compatibility_gate, part = self._compatibility_gate(row, confirmations)
        inventory_gate, stock = self._inventory_gate(part, quantity=quantity,
                                                     max_age=max_inventory_age)
        appointment_gate = self._appointment_gate(row)
        hold_gate, _ = self._hold_gate(ticket, part, stock)

        core = {
            "warranty": warranty_gate,
            "compatibility": compatibility_gate,
            "inventory": inventory_gate,
            "appointment": appointment_gate,
        }
        gates = {**core, "reservation": hold_gate}
        core_statuses = {name: gate["status"] for name, gate in core.items()}
        inventory_evidence = inventory_gate["evidence"]
        only_inventory_stale = (
            inventory_gate["status"] == BLOCKED
            and all(status == PASS for name, status in core_statuses.items()
                    if name != "inventory")
            and inventory_evidence.get("freshness_state") == "stale"
        )
        if only_inventory_stale:
            # 其余检查都通过、只是库存快照过期：可操作结论是"先重新查库存"，
            # 而不是笼统地当成兼容性阻断。
            verdict = "stale"
        elif BLOCKED in core_statuses.values():
            verdict = "blocked"
        elif PENDING in core_statuses.values():
            verdict = "pending"
        elif hold_gate["status"] != PASS:
            verdict = "reserve_needed"
        else:
            verdict = "ready"

        blockers = [f"{name}: {gate['summary']}" for name, gate in gates.items()
                    if gate["status"] != PASS]
        result = {
            "ticket": ticket,
            "verdict": verdict,
            "can_reserve": verdict in ("reserve_needed", "ready"),
            "needs_fresh_inventory": verdict == "stale",
            "can_dispatch": verdict == "ready",
            "checked_at": to_iso(self.clock.now()),
            "gates": gates,
            "pending_or_blocking": blockers,
            "selected_part": part["install_part"] if part else None,
            "stock": stock,
            "config_source": "asset_confirmation" if confirmations["asset_ok"] else None,
            "effective_config": confirmations["effective_config"],
            "reported_config": confirmations["reported_config"],
        }

        if persist:
            self._persist(row, result, actor=actor)
        return result

    # ------------------------------------------------------------------ 保修
    def _warranty_gate(self, row, confirmations):
        confirmation = confirmations["warranty"]
        if confirmation is None:
            return _gate(
                PENDING, "尚无保修专员的故障与保修范围确认",
                source_warranty=row["warranty"], fault=row["fault"],
                hint="需要保修专员确认故障是否属于保修范围",
            ), False
        details = confirmation["details"]
        if confirmation["status"] != "confirmed" or not details.get("covered"):
            return _gate(
                BLOCKED, "保修专员未确认本次故障由保修承担",
                confirmation=confirmation, source_warranty=row["warranty"],
            ), False
        if details.get("warranty_state") != "valid":
            return _gate(
                BLOCKED, f"保修状态为 {details.get('warranty_state')}，不能按保修派单",
                confirmation=confirmation,
            ), False
        if row["fault"] and details.get("fault") and details["fault"] != row["fault"]:
            return _gate(
                BLOCKED, "保修确认的故障与工单故障不一致",
                reported_fault=row["fault"], confirmed_fault=details["fault"],
            ), False
        return _gate(
            PASS, "保修专员已确认故障在保修范围内",
            confirmation=confirmation, source_warranty=row["warranty"],
        ), True

    # ------------------------------------------------------------------ 兼容
    def _compatibility_gate(self, row, confirmations):
        if not confirmations["asset_ok"]:
            asset = confirmations["asset"]
            reason = "尚无资产管理员对序列号与现场配置的确认"
            if asset and asset["status"] == "superseded":
                reason = asset.get("stale_reason") or "资产确认已被新的现场配置取代"
            return _gate(
                PENDING, reason,
                reported_config=confirmations["reported_config"],
                asset_confirmation=asset,
                options=self._tentative_options(row, confirmations),
            ), None

        config = confirmations["effective_config"]
        model = config.get("model")
        firmware = config.get("firmware")
        if not model or not firmware:
            return _gate(PENDING, "现场配置缺少型号或固件版本，无法判断兼容性",
                         config=config), None

        part_confirmation = confirmations["part"]
        options = self._tentative_options(row, confirmations)

        if part_confirmation is None:
            return _gate(
                PENDING, "尚无备件工程师对替代关系及适用固件的确认",
                requested_part=row["part"], config=config, options=options,
            ), None
        if part_confirmation["status"] != "confirmed":
            return _gate(
                BLOCKED, "备件工程师未确认该备件可用",
                part_confirmation=part_confirmation, options=options,
            ), None

        details = part_confirmation["details"]
        install_part = details.get("part")
        substitution_of = details.get("replacement")
        if not install_part:
            return _gate(PENDING, "备件确认缺少实际安装的备件号", options=options), None

        # 1) 型号、固件范围以确认时填写的为准，必须与已确认现场配置一致
        if details.get("model") != model:
            return _gate(
                BLOCKED, "备件确认针对的型号与已确认现场配置不一致",
                confirmed_model=details.get("model"), site_model=model,
            ), None
        low, high = details.get("firmware_from"), details.get("firmware_to")
        if not low and not high:
            return _gate(
                PENDING, "备件确认没有给出适用固件范围，不能默认适用全部版本",
                config=config,
            ), None
        try:
            if not firmware_in_range(firmware, low, high):
                return _gate(
                    BLOCKED,
                    f"现场固件 {firmware} 不在备件确认的适用范围 {low or '*'}–{high or '*'}",
                    firmware=firmware, firmware_from=low, firmware_to=high,
                ), None
        except ValidationError as exc:
            return _gate(PENDING, exc.message, firmware=firmware), None

        # 2) 替换关系必须同时有工程师确认和厂商目录支持
        if not row["part"]:
            # 工单还没指定原申请备件：以工程师确认的备件为准，但仍要求目录有对应条目
            entry = self.catalog.effective(install_part, model)
            if entry is None:
                return _gate(
                    PENDING, f"厂商目录没有 {install_part}/{model} 的有效条目，无法核验兼容性",
                    install_part=install_part,
                ), None
        elif install_part != row["part"]:
            if substitution_of != row["part"]:
                return _gate(
                    BLOCKED,
                    "备件工程师确认的备件与原申请备件不同，但没有声明替代关系",
                    requested_part=row["part"], install_part=install_part,
                ), None
            original_entry = self.catalog.effective(row["part"], model)
            if original_entry is None:
                return _gate(
                    PENDING, "厂商目录没有原申请备件在本型号上的有效条目，无法核验替代关系",
                    requested_part=row["part"], install_part=install_part,
                ), None
            if original_entry.replacement != install_part:
                return _gate(
                    BLOCKED,
                    f"厂商目录不支持 {row['part']} 由 {install_part} 替代",
                    catalog=original_entry.to_dict(), install_part=install_part,
                ), None

        # 3) 原申请备件若已停产且不是替代件，不能派单
        original = self.catalog.effective(row["part"], model)
        catalog_evidence = None
        if original:
            catalog_evidence = original.to_dict()
            if original.status == "discontinued" and install_part == row["part"]:
                return _gate(
                    BLOCKED, f"{row['part']} 已停产，需要确认替代件", catalog=catalog_evidence,
                    options=options,
                ), None
            if install_part == row["part"] and not firmware_in_range(
                firmware, original.firmware_from, original.firmware_to
            ):
                return _gate(
                    BLOCKED,
                    f"厂商目录中 {row['part']} 不覆盖现场固件 {firmware}",
                    catalog=catalog_evidence, firmware=firmware,
                ), None

        # 4) 确认依据的目录修订必须仍然有效：厂商更新目录后，旧确认不能继续当作依据。
        #    但若当前目录本身已明确不兼容，仍按阻断报告，而不是含糊地退回待核。
        noted_id = details.get("catalog_revision_id")
        if noted_id is not None:
            noted = self.catalog.revision(noted_id)
            if noted is None or noted.valid_to is not None or noted.retracted:
                if install_part == row["part"] and original is not None:
                    current_low, current_high = original.firmware_from, original.firmware_to
                    if not firmware_in_range(firmware, current_low, current_high):
                        return _gate(
                            BLOCKED,
                            f"厂商目录已更新，当前目录中 {install_part} 不再覆盖固件 {firmware}",
                            confirmed_revision=noted_id, catalog=catalog_evidence,
                        ), None
                return _gate(
                    PENDING, "厂商目录已更新，备件确认依据的修订不再有效，需要重新核验",
                    confirmed_revision=noted_id,
                    current_revision=catalog_evidence,
                    install_catalog=(self.catalog.effective(install_part, model).to_dict()
                                     if self.catalog.effective(install_part, model) else None),
                ), None

        install_entry = self.catalog.effective(install_part, model)
        if install_entry and install_entry.status == "discontinued":
            return _gate(
                BLOCKED, f"实际安装的 {install_part} 在厂商目录中已停产",
                catalog=install_entry.to_dict(),
            ), None

        catalog_note = self._catalog_summary(original, install_part)
        return _gate(
            PASS, "备件与设备型号、固件及厂商替代关系一致",
            install_part=install_part, requested_part=row["part"],
            confirmation=part_confirmation, catalog=catalog_evidence,
            install_catalog=install_entry.to_dict() if install_entry else None,
            catalog_summary=catalog_note,
        ), {
            "install_part": install_part,
            "requested_part": row["part"],
            "model": model,
            "firmware": firmware,
            "substitution": install_part != row["part"],
            "catalog_revision_id": catalog_evidence["revision_id"] if catalog_evidence else None,
            "install_catalog_revision_id": install_entry.id if install_entry else None,
        }

    def _catalog_summary(self, original, install_part):
        if original and original.replacement and original.replacement == install_part:
            return f"按厂商目录用 {install_part} 替代 {original.part}"
        return None

    def _tentative_options(self, row, confirmations):
        """待核选项：仅罗列目录与库存事实，不宣称兼容。"""
        model = (confirmations.get("effective_config") or {}).get("model") or row["model"]
        options = []
        if not model:
            return options
        for entry in self.catalog.effective_for_part(row["part"]):
            if entry.model != model:
                continue
            options.append({
                "part": entry.part,
                "model": entry.model,
                "status": entry.status,
                "replacement": entry.replacement,
                "firmware_from": entry.firmware_from,
                "firmware_to": entry.firmware_to,
                "revision": entry.revision,
                "note": "目录记录仅供备件工程师核验，不代表已确认兼容",
            })
            if entry.replacement:
                for alt in self.catalog.effective_for_part(entry.replacement):
                    if alt.model != model:
                        continue
                    options.append({
                        "part": alt.part,
                        "model": alt.model,
                        "status": alt.status,
                        "replacement": None,
                        "firmware_from": entry.firmware_from,
                        "firmware_to": entry.firmware_to,
                        "revision": alt.revision,
                        "note": "厂商目录列出的替代件，需备件工程师确认适用固件后才能使用",
                    })
        return options

    # ------------------------------------------------------------------ 库存
    def _inventory_gate(self, part, *, quantity, max_age):
        if part is None:
            return _gate(PENDING, "尚未确定兼容备件，库存无法作为派单依据"), None
        item = part["install_part"]
        best, candidates, context = self.inventory.select(item, quantity=quantity, max_age=max_age)
        if best is None:
            latest = context.get("latest")
            if not candidates:
                return _gate(
                    BLOCKED, f"{item} 没有任何经销商库存快照",
                    item=item, candidates=[], context=context,
                    freshness_state=(latest or {}).get("freshness", {}).get("state"),
                ), None
            summaries = [f"{c['partner']}：{'; '.join(c['reasons']) or '不满足条件'}" for c in candidates]
            reason = "没有满足条件的库存来源"
            if latest and latest["freshness"]["state"] == "stale":
                reason = "所有库存快照均已过期"
            elif latest and latest["freshness"]["state"] == "future":
                reason = "库存查询时间晚于当前时间，来源时钟不可信"
            return _gate(
                BLOCKED, f"{item} {reason}",
                item=item, required_quantity=quantity,
                candidates=candidates, options_summary=summaries,
                context=context,
                freshness_state=(latest or {}).get("freshness", {}).get("state"),
            ), None
        return _gate(
            PASS,
            f"{best['partner']} 快照显示可用 {best['available_after_holds']} 件，"
            f"查询于 {best['checked_at']}",
            item=item, selected=best, candidates=candidates,
            required_quantity=quantity,
        ), best

    # ------------------------------------------------------------------ 预留
    def _hold_gate(self, ticket, part, stock):
        """派单必须对应一笔真实持有的预留；结果不明不能当作已预留。

        库存快照过期不等于预留失效：已经拿到的门户回执可以继续作为派单依据，
        但库存门禁会单独提示需要刷新快照。
        """
        if part is None:
            return _gate(PENDING, "尚未确定兼容备件，不能派单"), None
        if stock is None:
            # 没有可视为"当前"的库存来源时，回退到该工单已持有的预留进行核对
            row = self.db.one(
                "SELECT * FROM reservations WHERE ticket = ? "
                "AND state IN (?, ?) ORDER BY id DESC LIMIT 1",
                (ticket, ReservationState.HELD.value, ReservationState.CONSUMED.value),
            )
            if row is None:
                return _gate(PENDING, "尚未选定可用库存来源，不能派单"), None
            return self._hold_verdict(row), row
        row = self.db.one(
            "SELECT * FROM reservations WHERE ticket = ? AND partner = ? AND item = ? "
            "ORDER BY id DESC LIMIT 1",
            (ticket, stock["partner"], stock["item"]),
        )
        if row is None:
            return _gate(
                PENDING,
                f"尚未在 {stock['partner']} 门户预留 {stock['item']}",
                partner=stock["partner"], item=stock["item"],
            ), None
        return self._hold_verdict(row), row if row["state"] == ReservationState.HELD.value or row["state"] == ReservationState.CONSUMED.value else None

    def _hold_verdict(self, row):
        if row["state"] == ReservationState.HELD.value:
            return _gate(
                PASS, f"门户预留回执 {row['portal_ref']} 已确认",
                reservation_id=row["id"], portal_ref=row["portal_ref"],
                partner=row["partner"], item=row["item"],
            )
        if row["state"] == ReservationState.CONSUMED.value:
            return _gate(
                PASS, "该备件已有发货回执，不再占用门户库存",
                reservation_id=row["id"], portal_ref=row["portal_ref"],
            )
        if row["state"] == ReservationState.PENDING.value:
            return _gate(
                PENDING, "预留请求结果不明，必须先查询门户现有预留再决定是否重试",
                reservation_id=row["id"], request_key=row["request_key"],
            )
        return _gate(
            PENDING, f"预留状态为 {row['state']}，不能作为派单依据",
            reservation_id=row["id"], state=row["state"],
        )

    # ------------------------------------------------------------------ 预约
    def _appointment_gate(self, row):
        from .db import parse_time

        start, end = row["appointment_start"], row["appointment_end"]
        if not start:
            return _gate(PENDING, "尚未预约工程师上门时段")
        start_dt = parse_time(start)
        end_dt = parse_time(end) if end else None
        now = self.clock.now()
        if end_dt and end_dt <= start_dt:
            return _gate(BLOCKED, "预约时段结束早于开始", start=start, end=end)
        if end_dt and end_dt < now:
            return _gate(BLOCKED, "预约时段已过去", start=start, end=end)
        if start_dt < now - timedelta(minutes=5) and (end_dt is None or end_dt < now):
            return _gate(BLOCKED, "预约时段已过去", start=start, end=end)
        return _gate(
            PASS, f"预约时段 {start} 至 {end or '未指定'}",
            start=start, end=end, source=row["appointment_source"],
            note="时段开始时间早于当前时间，但尚未结束" if start_dt < now else None,
        )

    # ------------------------------------------------------------------ 持久化
    def _persist(self, row, result, *, actor):
        ticket = row["ticket"]
        desired = {
            "ready": DispatchState.QUEUED,
            "reserve_needed": DispatchState.DRAFT,
            "pending": DispatchState.DRAFT,
            "stale": DispatchState.BLOCKED,
            "blocked": DispatchState.BLOCKED,
        }[result["verdict"]]
        now = self.clock.now()
        with self.db.tx():
            current = self.tickets.get(ticket)
            current_state = current["dispatch_state"]
            # 已派出/主管处理/已取消的工单不能被一次自动重算改回未派出状态
            if current_state in {
                DispatchState.DISPATCHED.value, DispatchState.SUPERVISOR_REVIEW.value,
                DispatchState.CANCELLED.value,
            }:
                state = current_state
            elif (current_state == DispatchState.NEEDS_RECHECK.value
                  and result["verdict"] != "ready"):
                # 待复核标记保留到检查重新通过为止
                state = DispatchState.NEEDS_RECHECK.value
            else:
                state = desired.value
            self.db.update(
                "tickets", "ticket", ticket,
                {"dispatch_state": state,
                 "status_note": "; ".join(result["pending_or_blocking"]) or None,
                 "updated_at": to_iso(now)},
            )
            self.db.insert(
                "dispatch_checks",
                {
                    "ticket": ticket,
                    "checked_at": to_iso(now),
                    "actor": actor_name(actor),
                    "verdict": result["verdict"],
                    "result": json.dumps(result, ensure_ascii=False, default=str),
                    "snapshot_ids": json.dumps(
                        [c["snapshot_id"] for c in (result.get("gates", {}).get("inventory", {})
                         .get("evidence", {}).get("candidates") or [])]
                    ),
                },
            )
        result["dispatch_state"] = state
        return result

    # ------------------------------------------------------------------ 查看
    def explain(self, ticket):
        """给客服看的完整依据：当前检查、候选排除原因、预留回执与后续状态。"""
        row = self.tickets.get(ticket)
        result = self.evaluate(ticket, persist=False)
        reservations = self.db.query(
            "SELECT * FROM reservations WHERE ticket = ? ORDER BY id DESC", (ticket,)
        )
        attempts = self.db.query(
            "SELECT * FROM reservation_attempts WHERE reservation_id IN "
            "(SELECT id FROM reservations WHERE ticket = ?) ORDER BY id ASC", (ticket,)
        )
        events = self.db.query(
            "SELECT * FROM events WHERE ticket = ? ORDER BY id ASC", (ticket,)
        )
        return {
            **result,
            "ticket_state": dict(row),
            "reservations": [_reservation_dict(r) for r in reservations],
            "reservation_attempts": [_attempt_dict(a) for a in attempts],
            "history": [_event_dict(e) for e in events],
            "explanation": _build_explanation(result),
        }


def _build_explanation(result):
    lines = []
    verdict_text = {
        "ready": "四项检查与门户预留回执全部通过，可以派单",
        "reserve_needed": "四项检查通过，但尚未取得门户预留回执，不能派单",
        "stale": "库存快照已过期，先重新查询经销商库存再派单",
        "pending": "存在待核选项，补齐确认前不能派单",
        "blocked": "存在阻断项，不能派单",
    }[result["verdict"]]
    lines.append(verdict_text)
    for name, label in (("warranty", "保修"), ("compatibility", "兼容"),
                        ("inventory", "库存"), ("appointment", "预约"),
                        ("reservation", "门户预留")):
        gate = result["gates"][name]
        lines.append(f"{label}：{gate['summary']}")
        if name == "inventory":
            selected = gate["evidence"].get("selected")
            if selected:
                lines.append(
                    f"  选用：{selected['partner']} 快照 #{selected['snapshot_id']}，"
                    f"查询时间 {selected['checked_at']}，来源 {selected['source']}"
                )
            candidates = gate["evidence"].get("candidates") or []
            for candidate in candidates:
                if selected and candidate["snapshot_id"] == selected["snapshot_id"]:
                    continue
                if candidate["eligible"]:
                    lines.append(
                        f"  备选：{candidate['partner']} 快照 #{candidate['snapshot_id']}"
                        f"（{candidate['checked_at']}）— 可用，但本次选用更优来源"
                    )
                else:
                    why = "；".join(candidate["reasons"]) or "不满足条件"
                    lines.append(
                        f"  排除：{candidate['partner']} 快照 #{candidate['snapshot_id']}"
                        f"（{candidate['checked_at']}）— {why}"
                    )
            for old in gate["evidence"].get("context", {}).get("superseded") or []:
                lines.append(
                    f"  忽略：{old['partner']} 更早快照 #{old['snapshot_id']}"
                    f"（{old['checked_at']}）— 同一经销商已有更新快照"
                )
    if result.get("selected_part"):
        lines.append(f"拟用备件：{result['selected_part']}")
    return lines


def _reservation_dict(row):
    out = dict(row)
    out["response"] = json.loads(row["response"] or "{}")
    return out


def _attempt_dict(row):
    out = dict(row)
    out["response"] = json.loads(row["response"] or "{}")
    return out


def _event_dict(row):
    out = dict(row)
    out["data"] = json.loads(row["data"] or "{}")
    return out
