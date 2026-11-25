"""厂商备件目录：修订历史、有效版本与兼容性判断。

规则要点：
- 每条修订都保留，`valid_to` 为空的那条才是当前有效版本；
- 同一修订号不允许用不同内容覆盖，否则无法判断哪份才是厂商发布；
- 修订号按自然顺序比较（2 比 10 旧），乱序到达的旧目录不会倒退；
- 目录撤回某条替代关系时，旧修订仍可追溯，但不再作为派单依据。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .db import to_iso
from .errors import Conflict, ValidationError
from .ingest import parse_catalog
from .versions import compare_revisions


@dataclass
class Revision:
    id: int
    part: str
    model: str
    firmware_from: str | None
    firmware_to: str | None
    replacement: str | None
    revision: str
    status: str
    source: str
    valid_from: str
    valid_to: str | None
    retracted: bool = False
    retraction_reason: str | None = None

    @classmethod
    def from_row(cls, row):
        return cls(
            id=row["id"],
            part=row["part"],
            model=row["model"],
            firmware_from=row["firmware_from"],
            firmware_to=row["firmware_to"],
            replacement=row["replacement"],
            revision=row["revision"],
            status=row["status"],
            source=row["source"],
            valid_from=row["valid_from"],
            valid_to=row["valid_to"],
            retracted=bool(row["retracted"]),
            retraction_reason=row["retraction_reason"],
        )

    def to_dict(self):
        return {
            "part": self.part,
            "model": self.model,
            "firmware_from": self.firmware_from,
            "firmware_to": self.firmware_to,
            "replacement": self.replacement,
            "revision": self.revision,
            "status": self.status,
            "source": self.source,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "retracted": self.retracted,
            "retraction_reason": self.retraction_reason,
            "revision_id": self.id,
        }

    def content(self):
        return {
            "firmware_from": self.firmware_from,
            "firmware_to": self.firmware_to,
            "replacement": self.replacement,
            "status": self.status,
        }


class Catalog:
    def __init__(self, db, clock):
        self.db = db
        self.clock = clock

    # ------------------------------------------------------------------ 读取
    def effective(self, part, model):
        row = self.db.one(
            "SELECT * FROM catalog_revisions "
            "WHERE part = ? AND model = ? AND valid_to IS NULL AND retracted = 0 "
            "ORDER BY id DESC LIMIT 1",
            (part, model),
        )
        return Revision.from_row(row) if row else None

    def history(self, part, model):
        rows = self.db.query(
            "SELECT * FROM catalog_revisions WHERE part = ? AND model = ? ORDER BY id ASC",
            (part, model),
        )
        return [Revision.from_row(row) for row in rows]

    def effective_for_part(self, part):
        rows = self.db.query(
            "SELECT * FROM catalog_revisions "
            "WHERE part = ? AND valid_to IS NULL AND retracted = 0 ORDER BY model",
            (part,),
        )
        return [Revision.from_row(row) for row in rows]

    def active_revisions(self):
        rows = self.db.query(
            "SELECT * FROM catalog_revisions WHERE valid_to IS NULL AND retracted = 0"
        )
        return [Revision.from_row(row) for row in rows]

    def revision(self, revision_id):
        row = self.db.one("SELECT * FROM catalog_revisions WHERE id = ?", (revision_id,))
        return Revision.from_row(row) if row else None

    # ------------------------------------------------------------------ 写入
    def ingest(self, path, *, source="manufacturer_catalog", complete=True, run_id=None):
        """导入一份目录快照。

        整份文件在一个事务里处理：任何一行不合法都整体失败，避免半份目录生效。
        返回变更摘要，供主管和工单复核使用。
        """
        rows = parse_catalog(path)
        now = self.clock.now()
        run_id = run_id or f"catalog-{to_iso(now)}"
        raw = path.read_text(encoding="utf-8-sig") if hasattr(path, "read_text") else None
        digest = _digest(rows)
        changes = []

        with self.db.tx():
            last = self.db.one(
                "SELECT digest FROM catalog_ingest_runs WHERE source = ? ORDER BY id DESC LIMIT 1",
                (source,),
            )
            if last and last["digest"] == digest:
                self.db.insert(
                    "catalog_ingest_runs",
                    {
                        "source": source,
                        "received_at": to_iso(now),
                        "digest": digest,
                        "row_count": len(rows),
                        "changed": 0,
                        "detail": json.dumps({"unchanged": True}, ensure_ascii=False),
                    },
                )
                return {"changed": [], "unchanged": True, "run_id": run_id, "digest": digest}

            self._check_duplicate_revisions(rows)
            seen = set()
            for row in rows:
                key = (row.part, row.model)
                seen.add(key)
                change = self._apply_row(row, source=source, now=now, run_id=run_id, digest=digest)
                if change:
                    changes.append(change)

            if complete:
                for revision in self.active_revisions():
                    if (revision.part, revision.model) not in seen:
                        self._retract(
                            revision,
                            now=now,
                            reason="厂商目录不再发布该条目",
                            run_id=run_id,
                        )
                        changes.append(
                            {
                                "part": revision.part,
                                "model": revision.model,
                                "kind": "entry_removed",
                                "before": revision.to_dict(),
                            }
                        )

            self.db.insert(
                "catalog_ingest_runs",
                {
                    "source": source,
                    "received_at": to_iso(now),
                    "digest": digest,
                    "row_count": len(rows),
                    "changed": 1 if changes else 0,
                    "detail": json.dumps(
                        {"changes": changes, "run_id": run_id}, ensure_ascii=False
                    ),
                },
            )
        return {"changed": changes, "unchanged": False, "run_id": run_id, "digest": digest}

    def _check_duplicate_revisions(self, rows):
        seen = {}
        for row in rows:
            key = (row.part, row.model, row.revision)
            content = (row.firmware_from, row.firmware_to, row.replacement)
            if key in seen and seen[key] != content:
                raise ValidationError(
                    f"目录中 {row.part}/{row.model} 的修订 {row.revision} 出现两次且内容不同",
                    code="duplicate_revision",
                )
            seen[key] = content

    def _apply_row(self, row, *, source, now, run_id, digest):
        current = self.effective(row.part, row.model)
        new_content = {
            "firmware_from": row.firmware_from,
            "firmware_to": row.firmware_to,
            "replacement": row.replacement,
            "status": getattr(row, "status", "active") or "active",
        }
        if current is None:
            self._insert(row, new_content, source=source, now=now, run_id=run_id)
            return {
                "part": row.part,
                "model": row.model,
                "kind": "revision_added",
                "after": {**new_content, "revision": row.revision},
            }

        order = _compare(row.revision, current.revision)
        if order == 0:
            if current.content() != new_content:
                raise Conflict(
                    f"{row.part}/{row.model} 的修订 {row.revision} 与现有有效版本内容不同，"
                    "拒绝覆盖；请厂商重新发布新修订号",
                    code="revision_reused",
                    details={"existing": current.content(), "incoming": new_content},
                )
            return None
        if order < 0:
            # 乱序到达的旧修订：保留到历史，但不让有效版本倒退。
            exists = self.db.one(
                "SELECT id FROM catalog_revisions WHERE part = ? AND model = ? AND revision = ?",
                (row.part, row.model, row.revision),
            )
            if exists is None:
                self._insert(
                    row, new_content, source=source, now=now, run_id=run_id,
                    valid_from=None, valid_to=current.valid_from,
                )
            return {
                "part": row.part,
                "model": row.model,
                "kind": "older_revision_ignored",
                "revision": row.revision,
                "effective": current.revision,
            }

        # 新修订生效：关闭旧版本，保留完整历史。
        self.db.update("catalog_revisions", "id", current.id, {"valid_to": to_iso(now)})
        self._insert(row, new_content, source=source, now=now, run_id=run_id)
        kinds = _change_kinds(current, new_content)
        return {
            "part": row.part,
            "model": row.model,
            "kind": "revision_replaced",
            "revision_from": current.revision,
            "revision_to": row.revision,
            "changes": kinds,
            "before": current.content(),
            "after": new_content,
            "replacement_withdrawn": bool(current.replacement and not row.replacement),
        }

    def _insert(self, row, content, *, source, now, run_id, valid_from=None, valid_to=None):
        self.db.insert(
            "catalog_revisions",
            {
                "part": row.part,
                "model": row.model,
                "firmware_from": row.firmware_from,
                "firmware_to": row.firmware_to,
                "replacement": row.replacement,
                "revision": row.revision,
                "status": content.get("status", "active"),
                "source": source,
                "ingested_at": to_iso(now),
                "valid_from": to_iso(now) if valid_from is None else valid_from,
                "valid_to": valid_to,
                "run_id": run_id,
                "raw": json.dumps(row.raw, ensure_ascii=False),
            },
        )

    def _retract(self, revision, *, now, reason, run_id):
        self.db.update(
            "catalog_revisions",
            "id",
            revision.id,
            {
                "valid_to": to_iso(now),
                "retracted": 1,
                "retracted_at": to_iso(now),
                "retraction_reason": reason,
            },
        )

    def retract_replacement(self, part, model, *, reason, actor="part_engineer"):
        """人工撤回某条替代关系（例如厂商口头通知，尚未发布正式目录）。"""
        current = self.effective(part, model)
        if current is None:
            raise ValidationError(f"没有 {part}/{model} 的有效目录条目", code="not_found")
        if not current.replacement:
            raise Conflict(f"{part}/{model} 当前没有替代件可撤回", code="nothing_to_retract")
        now = self.clock.now()
        with self.db.tx():
            self.db.update("catalog_revisions", "id", current.id, {"valid_to": to_iso(now)})
            self.db.insert(
                "catalog_revisions",
                {
                    "part": current.part,
                    "model": current.model,
                    "firmware_from": current.firmware_from,
                    "firmware_to": current.firmware_to,
                    "replacement": None,
                    "revision": _next_revision(current.revision),
                    "status": current.status,
                    "source": f"manual:{actor}",
                    "ingested_at": to_iso(now),
                    "valid_from": to_iso(now),
                    "valid_to": None,
                    "run_id": f"retract-{to_iso(now)}",
                    "raw": json.dumps({"reason": reason, "retracted": True}, ensure_ascii=False),
                },
            )
        return {
            "part": part,
            "model": model,
            "kind": "replacement_withdrawn",
            "reason": reason,
            "before": current.to_dict(),
        }


def _compare(left, right):
    return compare_revisions(left, right)


def _next_revision(current):
    try:
        return str(int(current) + 1)
    except (TypeError, ValueError):
        return f"{current}-revoked"


def _change_kinds(before, after):
    kinds = []
    if before.replacement != after.get("replacement"):
        kinds.append("replacement")
    if (before.firmware_from, before.firmware_to) != (
        after.get("firmware_from"),
        after.get("firmware_to"),
    ):
        kinds.append("firmware_range")
    if before.status != after.get("status"):
        kinds.append("status")
    return kinds


def _digest(rows):
    import hashlib

    canonical = json.dumps(
        [
            [r.part, r.model, r.firmware_from, r.firmware_to, r.replacement, r.revision,
             getattr(r, "status", "active")]
            for r in sorted(rows, key=lambda r: (r.part, r.model, r.revision))
        ],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
