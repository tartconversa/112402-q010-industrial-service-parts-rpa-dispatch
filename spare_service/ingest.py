"""CSV 采集解析。

采集是只读的：解析函数不写数据库，便于先做 dry-run、校验再入库。
空单元格一律视为"来源没有提供"，绝不填充猜测值。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from .db import parse_time
from .errors import ValidationError

INVENTORY_COLUMNS = {"stock", "partner", "item", "quantity", "checked"}
CATALOG_COLUMNS = {"part", "model", "firmware_from", "firmware_to", "replacement", "revision"}
TICKET_COLUMNS = {"ticket", "asset", "model", "part", "firmware", "fault", "warranty", "reservation", "dispatch"}


def _blank(value):
    return value is None or str(value).strip() == ""


def _read_rows(path, required):
    path = Path(path)
    if not path.is_file():
        raise ValidationError(f"来源文件不存在：{path}", code="source_missing")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, strict=True)
        if reader.fieldnames is None:
            raise ValidationError(f"{path.name} 缺少表头", code="bad_source")
        missing = required - set(reader.fieldnames)
        if missing:
            raise ValidationError(
                f"{path.name} 缺少列：{', '.join(sorted(missing))}",
                code="bad_source",
                details={"missing": sorted(missing)},
            )
        rows = []
        for line_no, row in enumerate(reader, start=2):
            if None in row:
                raise ValidationError(f"{path.name} 第 {line_no} 行列数多于表头", code="bad_source")
            rows.append((line_no, {k: (v if v is not None else "") for k, v in row.items()}))
    return rows


@dataclass
class InventoryRow:
    partner: str
    item: str
    quantity: int
    checked_at: object
    raw: dict = field(default_factory=dict)


def parse_inventory(path):
    """解析经销商库存快照。

    同一 (partner, item) 出现多条时，它们只是不同时点的快照，全部保留；
    是否过期由后续选择逻辑按查询时间判断，而不是在这里丢弃。
    """
    out = []
    for line_no, row in _read_rows(path, INVENTORY_COLUMNS):
        partner, item = row["partner"].strip(), row["item"].strip()
        if not partner or not item:
            raise ValidationError(f"库存第 {line_no} 行缺少经销商或备件号", code="bad_source")
        if _blank(row["quantity"]):
            raise ValidationError(f"库存第 {line_no} 行缺少数量，不能猜测", code="bad_source")
        try:
            quantity = int(row["quantity"].strip())
        except ValueError:
            raise ValidationError(f"库存第 {line_no} 行数量不是整数：{row['quantity']!r}", code="bad_source") from None
        if quantity < 0:
            raise ValidationError(f"库存第 {line_no} 行数量为负", code="bad_source")
        if _blank(row["checked"]):
            raise ValidationError(
                f"库存第 {line_no} 行缺少查询时间，无法判断新鲜度", code="bad_source"
            )
        try:
            checked_at = parse_time(row["checked"])
        except ValueError as exc:
            raise ValidationError(f"库存第 {line_no} 行查询时间格式错误：{exc}", code="bad_source") from None
        out.append(InventoryRow(partner, item, quantity, checked_at, dict(row)))
    return out


@dataclass
class CatalogRow:
    part: str
    model: str
    firmware_from: str | None
    firmware_to: str | None
    replacement: str | None
    revision: str
    status: str = "active"
    raw: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.status not in {"active", "discontinued"}:
            raise ValidationError(
                f"未知的备件状态：{self.status!r}", code="bad_source"
            )
        # 固件边界必须是可比较的版本号；"latest"/"下一版" 之类的文本一律拒绝，
        # 否则兼容性判断会静默失效。
        from .versions import firmware_key

        for label, value in (("firmware_from", self.firmware_from),
                             ("firmware_to", self.firmware_to)):
            if value:
                try:
                    firmware_key(value)
                except ValidationError:
                    raise ValidationError(
                        f"目录中 {self.part}/{self.model} 的 {label} 不是可比较的版本号：{value!r}",
                        code="bad_source",
                    ) from None


def parse_catalog(path):
    """解析厂商备件目录。

    固件范围留空表示厂商没有给出该边界。缺少 revision 的行不能判断是否新版本，
    直接拒绝；宁可让采集失败，也不要用旧目录冒充新目录。
    """
    out = []
    for line_no, row in _read_rows(path, CATALOG_COLUMNS):
        part, model = row["part"].strip(), row["model"].strip()
        if not part or not model:
            raise ValidationError(f"目录第 {line_no} 行缺少备件号或型号", code="bad_source")
        if _blank(row["revision"]):
            raise ValidationError(f"目录第 {line_no} 行缺少修订号", code="bad_source")
        fw_from = row["firmware_from"].strip() or None
        fw_to = row["firmware_to"].strip() or None
        if (fw_from is None) != (fw_to is None):
            raise ValidationError(
                f"目录第 {line_no} 行固件范围只给了一端，无法判断适用性", code="bad_source"
            )
        out.append(
            CatalogRow(
                part=part,
                model=model,
                firmware_from=fw_from,
                firmware_to=fw_to,
                replacement=row["replacement"].strip() or None,
                revision=row["revision"].strip(),
                status=(row.get("status") or "active").strip() or "active",
                raw=dict(row),
            )
        )
    return out


@dataclass
class TicketRow:
    ticket: str
    asset: str | None
    model: str | None
    part: str | None
    firmware: str | None
    fault: str | None
    warranty: str | None
    reservation: str | None
    dispatch: str | None
    raw: dict = field(default_factory=dict)


def parse_tickets(path):
    out = []
    for line_no, row in _read_rows(path, TICKET_COLUMNS):
        ticket = row["ticket"].strip()
        if not ticket:
            raise ValidationError(f"工单第 {line_no} 行缺少工单号", code="bad_source")
        out.append(
            TicketRow(
                ticket=ticket,
                asset=row["asset"].strip() or None,
                model=row["model"].strip() or None,
                part=row["part"].strip() or None,
                firmware=row["firmware"].strip() or None,
                fault=row["fault"].strip() or None,
                warranty=row["warranty"].strip() or None,
                reservation=row["reservation"].strip() or None,
                dispatch=row["dispatch"].strip() or None,
                raw=dict(row),
            )
        )
    return out
