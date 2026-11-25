"""库存采集与"为什么选这个来源"的依据。

每条库存记录都带查询时间与来源。同一备件可能来自多个经销商、多个时间点；
选择时优先新鲜、可用、数量足够的来源，但不会把过期快照当成现货。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .db import to_iso
from .errors import ValidationError
from .ingest import parse_inventory


@dataclass
class StockNote:
    snapshot_id: int
    partner: str
    item: str
    quantity: int
    checked_at: str
    source: str
    ingested_at: str
    freshness: dict

    def to_dict(self):
        return {
            "snapshot_id": self.snapshot_id,
            "partner": self.partner,
            "item": self.item,
            "quantity": self.quantity,
            "checked_at": self.checked_at,
            "source": self.source,
            "ingested_at": self.ingested_at,
            "freshness": self.freshness,
        }


class Inventory:
    def __init__(self, db, clock):
        self.db = db
        self.clock = clock

    def ingest(self, path, *, source="dealer_portal", run_id=None):
        """写入一批库存快照。完全重复（同来源同查询时间同数量）不再重复入库。"""
        rows = parse_inventory(path)
        now = self.clock.now()
        run_id = run_id or f"inventory-{to_iso(now)}"
        inserted, skipped = [], []
        with self.db.tx():
            for row in rows:
                duplicate = self.db.one(
                    "SELECT id FROM inventory_snapshots "
                    "WHERE partner = ? AND item = ? AND checked_at = ? AND quantity = ? "
                    "AND source = ?",
                    (row.partner, row.item, to_iso(row.checked_at), row.quantity, source),
                )
                if duplicate:
                    skipped.append({"partner": row.partner, "item": row.item,
                                    "checked_at": to_iso(row.checked_at), "snapshot_id": duplicate["id"]})
                    continue
                snapshot_id = self.db.insert(
                    "inventory_snapshots",
                    {
                        "partner": row.partner,
                        "item": row.item,
                        "quantity": row.quantity,
                        "checked_at": to_iso(row.checked_at),
                        "source": source,
                        "ingested_at": to_iso(now),
                        "run_id": run_id,
                        "raw": json.dumps(row.raw, ensure_ascii=False),
                    },
                )
                inserted.append(snapshot_id)
        return {"inserted": inserted, "skipped": skipped, "run_id": run_id}

    def latest_for_item(self, item):
        """最新一条快照，不论新鲜度；用于解释为什么不能派单。"""
        row = self.db.one(
            "SELECT * FROM inventory_snapshots WHERE item = ? ORDER BY checked_at DESC, id DESC LIMIT 1",
            (item,),
        )
        return self._note(row) if row else None

    def snapshots_for_item(self, item, *, max_age=None):
        rows = self.db.query(
            "SELECT * FROM inventory_snapshots WHERE item = ? ORDER BY checked_at DESC, id DESC",
            (item,),
        )
        notes = [self._note(row) for row in rows]
        if max_age is not None:
            notes = [n for n in notes if self.clock.freshness(n.checked_at, max_age=max_age).usable]
        return notes

    def _note(self, row):
        return StockNote(
            snapshot_id=row["id"],
            partner=row["partner"],
            item=row["item"],
            quantity=row["quantity"],
            checked_at=row["checked_at"],
            source=row["source"],
            ingested_at=row["ingested_at"],
            freshness=self.clock.freshness(row["checked_at"]).to_dict(),
        )

    def select(self, item, *, quantity=1, max_age=None):
        """返回 (最佳来源, 所有候选)。

        候选按"新鲜优先、数量足够优先、查询时间新优先"排序；被排除的也返回，
        方便客服看到为什么没有用它。
        最佳来源必须是：快照新鲜、数量足够、且该经销商没有未完成的预留占用。
        """
        if quantity < 1:
            raise ValidationError("预留数量必须为正", code="bad_quantity")
        notes = self.snapshots_for_item(item, max_age=max_age)
        if not notes:
            latest = self.latest_for_item(item)
            return None, [], {"reason": "没有任何库存快照", "latest": latest.to_dict() if latest else None}

        # 同一经销商同一备件只认最新一份快照：更新的快照说 0 件，
        # 不能拿更早那份"还有 3 件"当依据。快照本身已按查询时间倒序。
        latest_by_partner = {}
        superseded = []
        for note in notes:
            if note.partner not in latest_by_partner:
                latest_by_partner[note.partner] = note
            else:
                superseded.append(note)

        taken = self._open_holds(item)
        candidates = []
        for note in latest_by_partner.values():
            fresh = note.freshness["state"] == "fresh"
            # 只扣除在快照查询时点之后才产生的占用：更早的占用理论上已反映在门户快照里，
            # 再扣一次会重复计算。
            held = taken.get(note.partner, ())
            held_qty = sum(h["quantity"] for h in held if h["requested_at"] > note.checked_at)
            available = max(note.quantity - held_qty, 0)
            reasons = []
            if not fresh:
                reasons.append(note.freshness["reason"])
            if available < quantity:
                reasons.append(
                    f"可用 {available} 件（快照 {note.quantity} 件，快照之后被其他预留占用 "
                    f"{held_qty} 件），少于所需 {quantity} 件"
                )
            candidates.append(
                {
                    **note.to_dict(),
                    "available_after_holds": available,
                    "later_holds": held_qty,
                    "eligible": not reasons,
                    "reasons": reasons,
                }
            )

        eligible = [c for c in candidates if c["eligible"]]
        eligible.sort(key=lambda c: (c["checked_at"], -c["snapshot_id"]), reverse=True)
        eligible.sort(key=lambda c: -c["available_after_holds"])
        best = eligible[0] if eligible else None
        return best, candidates, {
            "open_holds": {
                partner: sum(h["quantity"] for h in holds) for partner, holds in taken.items()
            },
            "required_quantity": quantity,
            "latest": candidates[0] if candidates else None,
            "superseded": [n.to_dict() for n in superseded],
        }

    def _open_holds(self, item):
        """同一经销商同一备件尚未完成/释放的预留，视为已占用库存。"""
        rows = self.db.query(
            "SELECT partner, quantity, requested_at FROM reservations "
            "WHERE item = ? AND state IN ('pending', 'held', 'consumed')",
            (item,),
        )
        out = {}
        for row in rows:
            out.setdefault(row["partner"], []).append(
                {"quantity": int(row["quantity"]), "requested_at": row["requested_at"]}
            )
        return out
