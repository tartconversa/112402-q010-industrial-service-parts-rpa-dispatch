"""经销商门户预留库存的 RPA 工作流。

关键约束：
- 每次请求都带幂等键 request_key；同一工单同一来源同一备件只发一次。
- 结果不明（超时、门户无响应、响应无法解析）时，先查询现有预留，再决定是否重试；
  绝不盲目重发，避免重复占库。
- 已发货（consumed）的备件释放请求会被拒绝，也不能被目录更新当成可用库存。
- 订单取消时主动释放仍在 held 的库存；释放结果不明同样先查询再重试。
"""

from __future__ import annotations

import hashlib
import json
import threading

from .db import to_iso
from .errors import Conflict, NotFound, ValidationError
from .models import DispatchState, ReservationState, Role, actor_name, require_role


class PortalAdapter:
    """门户交互接口。真实环境可换成浏览器自动化或 HTTP 客户端。"""

    def reserve(self, *, request_key, partner, item, quantity, ticket):
        raise NotImplementedError

    def query(self, *, request_key=None, portal_ref=None, partner=None, item=None):
        """查询已有预留；返回 ReservationState 值或 None。"""
        raise NotImplementedError

    def release(self, *, portal_ref, request_key, partner, item, quantity):
        raise NotImplementedError

    def shipment(self, *, portal_ref):
        """返回 'shipped' | 'pending' | 'unknown'。"""
        raise NotImplementedError


class Reservations:
    def __init__(self, db, clock, portal, inventory=None):
        self.db = db
        self.clock = clock
        self.portal = portal
        self.inventory = inventory
        # 每个 (ticket, partner, item) 一把发送锁：同一进程内并发调用时，
        # 后来的调用等前一次门户请求落地后再判断，避免把 in-flight 误判成"无预留"。
        self._send_locks = {}
        self._locks_guard = threading.Lock()

    def _send_lock(self, ticket, partner, item):
        key = (ticket, partner, item)
        with self._locks_guard:
            return self._send_locks.setdefault(key, threading.RLock())

    # ------------------------------------------------------------------ 工具
    @staticmethod
    def request_key(ticket, partner, item):
        digest = hashlib.sha256(f"{ticket}|{partner}|{item}".encode()).hexdigest()[:20]
        return f"res-{digest}"

    def _reservation(self, ticket, partner, item):
        return self.db.one(
            "SELECT * FROM reservations WHERE ticket = ? AND partner = ? AND item = ?",
            (ticket, partner, item),
        )

    def _must_get(self, reservation_id):
        row = self.db.one("SELECT * FROM reservations WHERE id = ?", (reservation_id,))
        if row is None:
            raise NotFound("预留记录不存在", code="reservation_not_found")
        return row

    def _update(self, reservation_id, values, *, ticket, at, event, actor, role=None, data=None):
        values = dict(values)
        values.setdefault("updated_at", to_iso(at))
        self.db.update("reservations", "id", reservation_id, values)
        self.db.event(ticket=ticket, at=at, kind=event["kind"], message=event["message"],
                      actor=actor, role=role or event.get("role"), data=data or {})

    def _attempt(self, reservation_id, *, request_key, before, outcome, portal_ref, response, at):
        number = self.db.one(
            "SELECT COUNT(*) AS n FROM reservation_attempts WHERE reservation_id = ?",
            (reservation_id,),
        )["n"] + 1
        self.db.insert(
            "reservation_attempts",
            {
                "reservation_id": reservation_id,
                "attempt_no": number,
                "request_key": request_key,
                "state_before": before,
                "sent_at": to_iso(at),
                "outcome": outcome,
                "portal_ref": portal_ref,
                "response": json.dumps(response or {}, ensure_ascii=False, default=str),
            },
        )

    # ------------------------------------------------------------------ 预留
    def reserve(self, actor, ticket, *, partner, item, quantity=1, note=None):
        """向门户预留库存。返回的 state 一定是确定的，或明确标记 pending。"""
        require_role(actor, [Role.CS])
        if quantity < 1:
            raise ValidationError("预留数量必须为正", code="bad_quantity")

        key = self.request_key(ticket, partner, item)
        with self._send_lock(ticket, partner, item):
            return self._reserve_locked(actor, ticket, partner=partner, item=item,
                                        quantity=quantity, note=note, key=key)

    def _reserve_locked(self, actor, ticket, *, partner, item, quantity, note, key):
        reconcile_id = None
        with self.db.tx():
            existing = self._reservation(ticket, partner, item)
            now = self.clock.now()
            if existing:
                state = existing["state"]
                if state in (ReservationState.HELD.value, ReservationState.CONSUMED.value):
                    return self._public(existing, note="已有有效预留，未重复请求")
                if state == ReservationState.PENDING.value:
                    # 上一次结果不明：先查询，不重试。查询会走门户，必须等事务提交后再做。
                    reconcile_id = existing["id"]
                elif state == ReservationState.RELEASING.value:
                    raise Conflict("该预留正在释放中，先确认释放结果再重新预留",
                                   code="reservation_releasing")
                elif state == ReservationState.FAILED.value:
                    # 明确失败后才允许用同一个幂等键重新请求
                    reservation_id = existing["id"]
                    self.db.update("reservations", "id", reservation_id,
                                   {"state": ReservationState.PENDING.value,
                                    "updated_at": to_iso(now)})
                else:
                    raise Conflict(f"预留状态为 {state}，不能重新请求", code="reservation_state")
            else:
                reservation_id = self.db.insert(
                    "reservations",
                    {
                        "ticket": ticket,
                        "partner": partner,
                        "item": item,
                        "request_key": key,
                        "state": ReservationState.PENDING.value,
                        "requested_at": to_iso(now),
                        "updated_at": to_iso(now),
                        "quantity": quantity,
                        "response": "{}",
                    },
                )

        if reconcile_id is not None:
            return self.reconcile(actor, reconcile_id, reason="预留结果不明，先查询已有预留")
        return self._send(actor, reservation_id, key=key, partner=partner, item=item,
                          quantity=quantity, ticket=ticket, reason="发起预留", note=note)

    def _send(self, actor, reservation_id, *, key, partner, item, quantity, ticket, reason, note=None):
        """发出预留请求并按结果落库。调用方已确保不会重复发送。"""
        now = self.clock.now()
        try:
            response = self.portal.reserve(
                request_key=key, partner=partner, item=item, quantity=quantity, ticket=ticket
            )
        except Exception as exc:  # 门户超时/网络错误：结果不明
            with self.db.tx():
                self._attempt(reservation_id, request_key=key,
                              before=ReservationState.PENDING.value, outcome="pending",
                              portal_ref=None, response={"error": repr(exc)}, at=now)
                self._update(
                    reservation_id,
                    {"state": ReservationState.PENDING.value},
                    ticket=ticket, at=now,
                    event={"kind": "reservation_result_unknown",
                           "message": f"预留请求未得到明确结果，需先查询再重试：{reason}"},
                    actor=actor_name(actor), role=Role.CS.value,
                    data={"request_key": key, "error": repr(exc)},
                )
            return self._public(self._must_get(reservation_id),
                                note="结果不明，必须先用查询确认，不要重试")

        outcome = _classify(response)
        current = self._must_get(reservation_id)
        if outcome == ReservationState.HELD.value and not response.get("portal_ref"):
            # 门户说"已预留"却没给回执号：无法在取消/发货时对账，按结果不明处理。
            response = {**response, "state": "unknown",
                        "reason": "门户未返回预留回执号"}
            outcome = ReservationState.PENDING.value
        with self.db.tx():
            self._attempt(reservation_id, request_key=key,
                          before=current["state"], outcome=outcome,
                          portal_ref=response.get("portal_ref"), response=response, at=now)
            if outcome == ReservationState.HELD.value:
                self._update(
                    reservation_id,
                    {"state": ReservationState.HELD.value,
                     "portal_ref": response.get("portal_ref"),
                     "response": json.dumps(response, ensure_ascii=False, default=str)},
                    ticket=ticket, at=now,
                    event={"kind": "reservation_held",
                           "message": f"门户已确认预留 {item} @ {partner}，回执 {response.get('portal_ref')}"},
                    actor=actor_name(actor), role=Role.CS.value,
                    data={"portal_ref": response.get("portal_ref"), "request_key": key},
                )
                self._remember(ticket, item)
            elif outcome == ReservationState.PENDING.value:
                self._update(
                    reservation_id,
                    {"state": ReservationState.PENDING.value,
                     "response": json.dumps(response, ensure_ascii=False, default=str)},
                    ticket=ticket, at=now,
                    event={"kind": "reservation_result_unknown",
                           "message": "门户响应不明确，需先查询现有预留"},
                    actor=actor_name(actor), role=Role.CS.value,
                    data={"request_key": key, "response": response},
                )
            else:
                self._update(
                    reservation_id,
                    {"state": ReservationState.FAILED.value,
                     "response": json.dumps(response, ensure_ascii=False, default=str)},
                    ticket=ticket, at=now,
                    event={"kind": "reservation_failed",
                           "message": f"门户拒绝预留：{response.get('reason') or outcome}"},
                    actor=actor_name(actor), role=Role.CS.value,
                    data={"request_key": key, "response": response},
                )
        return self._public(self._must_get(reservation_id), note=note)

    def _remember(self, ticket, item):
        self.db.update("tickets", "ticket", ticket, {"reserved_part": item})

    # ------------------------------------------------------------------ 对账
    def reconcile(self, actor, reservation_id, *, reason="查询现有预留"):
        """查询门户现有预留并对齐本地状态。结果不明时的唯一正确入口。"""
        row = self._must_get(reservation_id)
        now = self.clock.now()
        try:
            found = self.portal.query(
                request_key=row["request_key"],
                portal_ref=row["portal_ref"],
                partner=row["partner"],
                item=row["item"],
            )
        except Exception as exc:
            with self.db.tx():
                self._attempt(reservation_id, request_key=row["request_key"],
                              before=row["state"], outcome="pending",
                              portal_ref=row["portal_ref"], response={"error": repr(exc)}, at=now)
            return self._public(self._must_get(reservation_id),
                                note="对账失败，仍为结果不明；不要重试，稍后再查询")

        state = _classify_state(found)
        with self.db.tx():
            self._attempt(reservation_id, request_key=row["request_key"],
                          before=row["state"], outcome="reconciled",
                          portal_ref=(found or {}).get("portal_ref") if isinstance(found, dict) else row["portal_ref"],
                          response=found if isinstance(found, dict) else {"state": found}, at=now)
            if state == ReservationState.NONE.value or state is None:
                # 门户确认没有这笔预留，上一次请求确实没成功，可以安全重试
                self._update(
                    reservation_id,
                    {"state": ReservationState.FAILED.value},
                    ticket=row["ticket"], at=now,
                    event={"kind": "reconciled_absent",
                           "message": f"{reason}：门户没有该预留，可安全重试"},
                    actor=actor_name(actor),
                    data={"request_key": row["request_key"]},
                )
            elif state == ReservationState.HELD.value:
                self._update(
                    reservation_id,
                    {"state": ReservationState.HELD.value,
                     "portal_ref": (found or {}).get("portal_ref", row["portal_ref"]),
                     "response": json.dumps(found, ensure_ascii=False, default=str)},
                    ticket=row["ticket"], at=now,
                    event={"kind": "reconciled_held",
                           "message": "对账确认预留已存在，不再重试"},
                    actor=actor_name(actor), role=Role.CS.value,
                    data={"request_key": row["request_key"]},
                )
                self._remember(row["ticket"], row["item"])
            elif state == ReservationState.CONSUMED.value:
                self._update(
                    reservation_id,
                    {"state": ReservationState.CONSUMED.value,
                     "portal_ref": (found or {}).get("portal_ref", row["portal_ref"])},
                    ticket=row["ticket"], at=now,
                    event={"kind": "reconciled_consumed",
                           "message": "对账确认该备件已发货，不能释放或再次使用"},
                    actor=actor_name(actor), role=Role.CS.value,
                )
            elif state in (ReservationState.RELEASED.value, ReservationState.FAILED.value):
                self._update(
                    reservation_id, {"state": state},
                    ticket=row["ticket"], at=now,
                    event={"kind": "reconciled_released",
                           "message": f"对账确认门户预留状态为 {state}"},
                    actor=actor_name(actor),
                )
            else:
                self._update(
                    reservation_id, {"state": ReservationState.PENDING.value},
                    ticket=row["ticket"], at=now,
                    event={"kind": "reconcile_inconclusive",
                           "message": "对账仍未得到明确结果，保持待确认"},
                    actor=actor_name(actor),
                )
        return self._public(self._must_get(reservation_id), note="已执行门户对账")

    def retry(self, actor, ticket, *, partner, item, quantity=1):
        """结果不明时的重试入口：先对账，确认门户没有预留后才重新发送。"""
        require_role(actor, [Role.CS])
        with self._send_lock(ticket, partner, item):
            return self._retry_locked(actor, ticket, partner=partner, item=item,
                                      quantity=quantity)

    def _retry_locked(self, actor, ticket, *, partner, item, quantity):
        row = self._reservation(ticket, partner, item)
        if row is None:
            return self.reserve(actor, ticket, partner=partner, item=item, quantity=quantity)
        if row["state"] == ReservationState.PENDING.value:
            row_after = self.reconcile(actor, row["id"], reason="重试前先查询")
            if row_after["state"] != ReservationState.FAILED.value:
                return row_after
        current = self._must_get(row["id"])
        if current["state"] == ReservationState.HELD.value:
            return self._public(current, note="已有预留，未重试")
        if current["state"] == ReservationState.CONSUMED.value:
            raise Conflict("该备件已发货，不能重新预留", code="already_shipped")
        if current["state"] not in (ReservationState.FAILED.value,):
            raise Conflict(f"预留状态为 {current['state']}，不能重试", code="reservation_state")
        with self.db.tx():
            self.db.update("reservations", "id", current["id"],
                           {"state": ReservationState.PENDING.value,
                            "updated_at": to_iso(self.clock.now())})
        return self._send(actor, current["id"], key=current["request_key"], partner=partner,
                          item=item, quantity=current["quantity"], ticket=ticket,
                          reason="对账确认无预留后重试")

    # ------------------------------------------------------------------ 释放
    def release_on_cancel(self, actor, ticket, *, reason="订单取消"):
        """订单取消：释放仍在持有的预留。

        已发货的预留不会因为取消而被放回库存；取消本身照常完成，但结果里
        会明确列出"已发货、未释放"的那几行，不能静默忽略。
        """
        require_role(actor, [Role.CS, Role.SUPERVISOR])
        rows = self.db.query(
            "SELECT * FROM reservations WHERE ticket = ? AND state IN (?, ?, ?) "
            "ORDER BY id",
            (ticket, ReservationState.HELD.value, ReservationState.PENDING.value,
             ReservationState.CONSUMED.value),
        )
        results = []
        failed = None
        for row in rows:
            try:
                with self._send_lock(row["ticket"], row["partner"], row["item"]):
                    results.append(self._release_one(actor, row, reason=reason))
            except Conflict as exc:
                results.append({
                    "reservation_id": row["id"], "item": row["item"],
                    "partner": row["partner"], "state": "consumed",
                    "released": False, "code": exc.code,
                    "note": exc.message,
                })
                failed = failed or exc
        now = self.clock.now()
        with self.db.tx():
            self.db.update("tickets", "ticket", ticket,
                           {"dispatch_state": DispatchState.CANCELLED.value,
                            "updated_at": to_iso(now)})
            self.db.event(ticket=ticket, at=now, kind="ticket_cancelled",
                          message=f"工单取消：{reason}", actor=actor_name(actor),
                          role=Role.CS.value, data={"releases": results})
        if failed is not None and not any(r.get("released") for r in results):
            raise Conflict(
                "工单已取消，但备件已发货，不能当未使用库存释放",
                code="already_shipped", details={"releases": results},
            )
        return results

    def _release_one(self, actor, row, *, reason):
        now = self.clock.now()
        if row["state"] == ReservationState.PENDING.value:
            # 不知道有没有真的预留成功，先对账，不能直接当成未使用
            self.reconcile(actor, row["id"], reason=f"取消前对账：{reason}")
            row = self._must_get(row["id"])
            if row["state"] == ReservationState.HELD.value:
                return self._release_one(actor, row, reason=reason)
            if row["state"] == ReservationState.CONSUMED.value:
                raise Conflict("备件已发货，订单取消不能把它当未使用库存释放",
                               code="already_shipped")
            return self._public(row, note="对账确认无有效预留，无需释放")

        if row["state"] == ReservationState.CONSUMED.value:
            raise Conflict("备件已发货，不能释放", code="already_shipped")

        # 发货检查必须在释放之前：已发货的备件不能被目录更新或取消订单当回未使用库存。
        shipped = _shipment_state(self.portal, row, now=now, db=self.db)
        if shipped == "shipped":
            self._mark_consumed(actor, row, reason="发货记录显示已发出")
            raise Conflict("备件已发货，订单取消不能把它当未使用库存释放",
                           code="already_shipped")

        try:
            response = self.portal.release(
                portal_ref=row["portal_ref"], request_key=row["request_key"],
                partner=row["partner"], item=row["item"], quantity=row["quantity"],
            )
        except Exception as exc:
            with self.db.tx():
                self._attempt(row["id"], request_key=row["request_key"],
                              before=row["state"], outcome="pending", portal_ref=row["portal_ref"],
                              response={"error": repr(exc)}, at=now)
                self._update(
                    row["id"], {"state": ReservationState.RELEASING.value},
                    ticket=row["ticket"], at=now,
                    event={"kind": "release_result_unknown",
                           "message": "释放请求结果不明，需先查询再重试"},
                    actor=actor_name(actor),
                )
            return self._public(self._must_get(row["id"]), note="释放结果不明，先查询")

        outcome = _classify_state(response)
        if outcome in (ReservationState.RELEASED.value, ReservationState.NONE.value):
            self._finish_release(actor, row["id"], response)
            return self._public(self._must_get(row["id"]), note="库存已释放")
        if outcome == ReservationState.CONSUMED.value:
            self._mark_consumed(actor, row, reason=reason)
            raise Conflict("门户显示备件已发货，不能释放", code="already_shipped")
        with self.db.tx():
            self._attempt(row["id"], request_key=row["request_key"], before=row["state"],
                          outcome="pending", portal_ref=row["portal_ref"],
                          response=response, at=now)
            self._update(
                row["id"], {"state": ReservationState.RELEASING.value},
                ticket=row["ticket"], at=now,
                event={"kind": "release_result_unknown",
                       "message": "门户未确认释放结果，需先查询"},
                actor=actor_name(actor), data={"response": response},
            )
        return self._public(self._must_get(row["id"]), note="释放结果不明，先查询")

    def retry_release(self, actor, reservation_id):
        """释放结果不明时：先查询门户，确认仍持有再重试释放。"""
        row = self._must_get(reservation_id)
        reconciled = self.reconcile(actor, reservation_id, reason="释放前先查询")
        current = self._must_get(reservation_id)
        if current["state"] in (ReservationState.RELEASED.value,):
            return self._public(current, note="对账确认已释放")
        if current["state"] == ReservationState.CONSUMED.value:
            raise Conflict("备件已发货，不能释放", code="already_shipped")
        if current["state"] not in (ReservationState.HELD.value, ReservationState.RELEASING.value):
            return reconciled
        return self._release_one(actor, current, reason="释放重试")

    def _finish_release(self, actor, reservation_id, response):
        row = self._must_get(reservation_id)
        now = self.clock.now()
        with self.db.tx():
            self._attempt(reservation_id, request_key=row["request_key"], before=row["state"],
                          outcome="released", portal_ref=row["portal_ref"],
                          response=response, at=now)
            self._update(
                reservation_id, {"state": ReservationState.RELEASED.value},
                ticket=row["ticket"], at=now,
                event={"kind": "reservation_released",
                       "message": "订单取消，已释放门户预留库存"},
                actor=actor_name(actor), role=Role.CS.value,
            )

    def mark_shipped(self, actor, reservation_id, *, portal_ref=None):
        """发货回执：转为已使用，之后不能被释放或当成可用库存。"""
        row = self._must_get(reservation_id)
        with self.db.tx():
            self._mark_consumed(actor, row, reason="收到发货回执")
        return self._public(self._must_get(reservation_id))

    def _mark_consumed(self, actor, row, *, reason):
        now = self.clock.now()
        self._update(
            row["id"], {"state": ReservationState.CONSUMED.value},
            ticket=row["ticket"], at=now,
            event={"kind": "reservation_consumed",
                   "message": f"{row['item']} 已发货，不再属于可用库存：{reason}"},
            actor=actor_name(actor), role=Role.CS.value,
        )

    # ------------------------------------------------------------------ 输出
    def _public(self, row, *, note=None):
        attempts = self.db.query(
            "SELECT * FROM reservation_attempts WHERE reservation_id = ? ORDER BY id ASC",
            (row["id"],),
        )
        return {
            "reservation_id": row["id"],
            "ticket": row["ticket"],
            "partner": row["partner"],
            "item": row["item"],
            "quantity": row["quantity"],
            "state": row["state"],
            "portal_ref": row["portal_ref"],
            "request_key": row["request_key"],
            "requested_at": row["requested_at"],
            "updated_at": row["updated_at"],
            "response": json.loads(row["response"] or "{}"),
            "attempts": [
                {**dict(a), "response": json.loads(a["response"] or "{}")} for a in attempts
            ],
            "note": note,
        }


def _classify(response):
    """门户预留响应 -> 状态。不可识别的响应一律视为待确认，不乐观放行。"""
    if not isinstance(response, dict):
        return ReservationState.PENDING.value
    state = (response.get("state") or "").strip().lower()
    if state in {"held", "reserved", "confirmed", "ok", "success"}:
        return ReservationState.HELD.value
    if state in {"conflict", "unavailable", "out_of_stock", "rejected", "failed"}:
        return ReservationState.FAILED.value
    if state in {"pending", "unknown", "timeout", ""}:
        return ReservationState.PENDING.value
    return ReservationState.PENDING.value


def _classify_state(found):
    if isinstance(found, dict):
        return (found.get("state") or "").strip().lower() or None
    if isinstance(found, str):
        return found.strip().lower() or None
    return None


def _shipment_state(portal, row, *, now, db):
    """在释放前检查发货状态；无法确认时按"未确认"继续，但不会乐观释放。"""
    try:
        return (portal.shipment(portal_ref=row["portal_ref"]) or "unknown").lower()
    except Exception:
        return "unknown"
