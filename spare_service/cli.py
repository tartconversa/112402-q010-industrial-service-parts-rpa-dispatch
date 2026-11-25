"""命令行入口。

用法：
  python -m spare_service.cli init --db data.db
  python -m spare_service.cli ingest --db data.db --inventory ... --catalog ... --tickets ...
  python -m spare_service.cli watch --db data.db --config sources.json --interval 300
  python -m spare_service.cli board --db data.db [--json]
  python -m spare_service.cli explain --db data.db T1
  python -m spare_service.cli confirm-asset --db data.db T1 --serial A-11 --model P-4 --firmware 3.4
  python -m spare_service.cli confirm-warranty --db data.db T1 --covered yes --state valid
  python -m spare_service.cli confirm-part --db data.db T1 --part BEAR-1 --model P-4 \
      --firmware-from 3.0 --firmware-to 3.9
  python -m spare_service.cli reserve --db data.db T1
  python -m spare_service.cli dispatch --db data.db T1
  python -m spare_service.cli release --db data.db T1 --reason 客户取消
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .clock import BusinessClock
from .errors import ServiceError
from .models import Role
from .portal import DemoPortal
from .scheduler import Scheduler, Source
from .service import Service


def _service(args, *, portal=None):
    service = Service(
        db_path=args.db,
        clock=BusinessClock(),
        portal=portal,
    )
    return service


def _actor(role, name):
    return {"name": name, "role": role}


def _print(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def build_parser():
    parser = argparse.ArgumentParser(prog="spare_service", description="设备备件采集与保修派单")
    parser.add_argument("--db", default="spare_service.db", help="SQLite 文件路径")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="初始化数据库")

    ingest = sub.add_parser("ingest", help="采集一次库存/目录/工单回执")
    ingest.add_argument("--inventory", action="append", default=[])
    ingest.add_argument("--catalog", action="append", default=[])
    ingest.add_argument("--tickets", action="append", default=[])
    ingest.add_argument("--catalog-complete", action="store_true",
                        help="把目录视为完整快照：未出现的条目视为撤回")

    watch = sub.add_parser("watch", help="定时采集")
    watch.add_argument("--config", required=True, help="来源清单 JSON")
    watch.add_argument("--interval", type=int, default=300)
    watch.add_argument("--iterations", type=int, default=None)

    board = sub.add_parser("board", help="客服工作台")
    board.add_argument("--json", action="store_true")
    board.add_argument("--export", help="导出 CSV 路径")

    explain = sub.add_parser("explain", help="查看选用/排除依据与预留回执")
    explain.add_argument("ticket")

    asset = sub.add_parser("confirm-asset", help="资产管理员确认")
    asset.add_argument("ticket")
    asset.add_argument("--serial", required=True)
    asset.add_argument("--model", required=True)
    asset.add_argument("--firmware", required=True)
    asset.add_argument("--config", default="{}", help="额外现场配置 JSON")
    asset.add_argument("--actor", default="asset-admin")

    warranty = sub.add_parser("confirm-warranty", help="保修专员确认")
    warranty.add_argument("ticket")
    warranty.add_argument("--covered", choices=["yes", "no"], required=True)
    warranty.add_argument("--state", choices=["valid", "review", "expired"], required=True)
    warranty.add_argument("--fault")
    warranty.add_argument("--actor", default="warranty-specialist")

    part = sub.add_parser("confirm-part", help="备件工程师确认替代关系与适用固件")
    part.add_argument("ticket")
    part.add_argument("--part", required=True)
    part.add_argument("--model", required=True)
    part.add_argument("--firmware-from", default=None)
    part.add_argument("--firmware-to", default=None)
    part.add_argument("--replacement", default=None, help="原申请备件（发生替代时）")
    part.add_argument("--catalog-revision-id", type=int, default=None)
    part.add_argument("--actor", default="part-engineer")

    site = sub.add_parser("site-config", help="服务人员回填现场配置")
    site.add_argument("ticket")
    site.add_argument("--asset")
    site.add_argument("--model")
    site.add_argument("--firmware")
    site.add_argument("--note")
    site.add_argument("--actor", default="field-service")

    appointment = sub.add_parser("appointment", help="登记预约时段")
    appointment.add_argument("ticket")
    appointment.add_argument("--start", required=True)
    appointment.add_argument("--end")
    appointment.add_argument("--actor", default="cs-1")

    reserve = sub.add_parser("reserve", help="门户预留库存")
    reserve.add_argument("ticket")
    reserve.add_argument("--partner")
    reserve.add_argument("--item")
    reserve.add_argument("--quantity", type=int, default=1)
    reserve.add_argument("--actor", default="cs-1")

    reconcile = sub.add_parser("reconcile", help="查询门户现有预留")
    reconcile.add_argument("reservation_id", type=int)

    dispatch = sub.add_parser("dispatch", help="派单")
    dispatch.add_argument("ticket")
    dispatch.add_argument("--actor", default="cs-1")

    release = sub.add_parser("release", help="订单取消后释放库存")
    release.add_argument("ticket")
    release.add_argument("--reason", default="订单取消")
    release.add_argument("--actor", default="cs-1")

    supervisor = sub.add_parser("supervisor-queue", help="服务主管待办")
    supervisor.add_argument("--json", action="store_true")

    demo = sub.add_parser("demo", help="用样例数据跑一遍端到端流程")
    demo.add_argument("--samples", default="samples")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    samples = Path(getattr(args, "samples", "samples"))

    if args.command == "demo":
        return run_demo(samples)

    service = _service(args)
    try:
        return _dispatch_command(args, service)
    except ServiceError as exc:
        _print({"error": exc.to_dict()})
        return 2
    finally:
        service.close()


def _dispatch_command(args, service):
    if args.command == "init":
        _print({"db": str(service.db.path), "status": "initialized"})
        return 0
    if args.command == "ingest":
        out = {}
        for path in args.inventory:
            out.setdefault("inventory", []).append(service.ingest_inventory(path))
        for path in args.catalog:
            out.setdefault("catalog", []).append(
                service.ingest_catalog(path, complete=args.catalog_complete)
            )
        for path in args.tickets:
            receipt = service.ingest_tickets(path)
            out.setdefault("tickets", []).append(
                {"created": receipt.created, "updated": receipt.updated,
                 "conflicts": receipt.conflicts}
            )
        _print(out)
        return 0
    if args.command == "watch":
        config_path = Path(args.config)
        config = json.loads(config_path.read_text(encoding="utf-8"))
        base = config_path.parent
        sources = []
        for item in config["sources"]:
            item = dict(item)
            # 相对路径按配置文件所在目录解析，避免受启动目录影响
            item["path"] = str((base / item["path"]).resolve()) if not Path(item["path"]).is_absolute() else item["path"]
            sources.append(Source(**item))
        scheduler = Scheduler(service, sources)
        scheduler.run_forever(args.interval, iterations=args.iterations)
        return 0
    if args.command == "board":
        if args.export:
            service.export_board(args.export)
            _print({"exported": args.export})
        rows = service.board()
        if args.json:
            _print(rows)
        else:
            for row in rows:
                flags = "可派单" if row["can_dispatch"] else (
                    "可预留" if row["can_reserve"] else "待核/阻断"
                )
                print(f"{row['ticket']:<6} {row['state']:<16} {flags:<8} "
                      f"{'; '.join(row['blockers'])[:80]}")
        return 0
    if args.command == "supervisor-queue":
        rows = service.supervisor_queue()
        if args.json:
            _print(rows)
        else:
            for row in rows:
                print(f"{row['ticket']:<6} {row['reason']} (预留 "
                      f"{', '.join(r['portal_ref'] or r['state'] for r in row['reservations']) or '无'})")
        return 0
    if args.command == "explain":
        _print(service.explain(args.ticket))
        return 0
    if args.command == "confirm-asset":
        service.confirm_asset(
            _actor(Role.ASSET.value, args.actor), args.ticket,
            serial=args.serial, model=args.model, firmware=args.firmware,
            config=json.loads(args.config),
        )
        _print(service.explain(args.ticket)["gates"])
        return 0
    if args.command == "confirm-warranty":
        service.confirm_warranty(
            _actor(Role.WARRANTY.value, args.actor), args.ticket,
            covered=args.covered == "yes", warranty_state=args.state, fault=args.fault,
        )
        _print(service.explain(args.ticket)["gates"])
        return 0
    if args.command == "confirm-part":
        service.confirm_part(
            _actor(Role.PART.value, args.actor), args.ticket,
            part=args.part, model=args.model, firmware_from=args.firmware_from,
            firmware_to=args.firmware_to, replacement=args.replacement,
            catalog_revision_id=args.catalog_revision_id,
        )
        _print(service.explain(args.ticket)["gates"])
        return 0
    if args.command == "site-config":
        service.set_site_config(
            _actor(Role.ASSET.value, args.actor), args.ticket,
            asset=args.asset, model=args.model, firmware=args.firmware, note=args.note,
        )
        _print(service.board())
        return 0
    if args.command == "appointment":
        service.set_appointment(
            _actor(Role.CS.value, args.actor), args.ticket, start=args.start, end=args.end
        )
        _print(service.explain(args.ticket)["gates"])
        return 0
    if args.command == "reserve":
        if service.portal is None:
            service.set_portal(DemoPortal(service.clock))
            print("警告：未配置门户适配器，使用内存演示门户（不持久）", file=sys.stderr)
        result = service.reserve(
            _actor(Role.CS.value, args.actor), args.ticket, partner=args.partner,
            item=args.item, quantity=args.quantity,
        )
        _print(result)
        return 0
    if args.command == "reconcile":
        _print(service.reconcile_reservation(
            _actor(Role.CS.value, "cs-1"), args.reservation_id
        ))
        return 0
    if args.command == "dispatch":
        _print(service.dispatch(_actor(Role.CS.value, args.actor), args.ticket))
        return 0
    if args.command == "release":
        _print(service.release_on_cancel(
            _actor(Role.CS.value, args.actor), args.ticket, reason=args.reason
        ))
        return 0
    parser.error("未知命令")
    return 1


def run_demo(samples_dir):
    """用样例 CSV 串起完整流程，展示系统如何拒绝不完整/过期的依据。"""
    from datetime import timedelta

    from .clock import FixedClock
    from .portal import DemoPortal

    samples_dir = Path(samples_dir)
    # 演示时钟锚定在样例库存查询时间之后 1 分钟，便于展示"新鲜"与"过期"两种结论。
    from .ingest import parse_inventory

    checked = [row.checked_at for row in parse_inventory(samples_dir / "inventory.csv")]
    clock = FixedClock(max(checked) + timedelta(minutes=1))
    portal = DemoPortal(clock, stock={("D-1", "BEAR-1"): 3, ("D-2", "BEAR-2"): 1,
                                      ("D-1", "CTRL-4"): 0})
    service = Service(clock=clock, portal=portal)
    print("== 采集 ==")
    print("库存:", len(service.ingest_inventory(samples_dir / "inventory.csv")["inserted"]), "条")
    print("目录:", len(service.ingest_catalog(samples_dir / "compatibility.csv")["changed"]), "项变更")
    receipt = service.ingest_tickets(samples_dir / "tickets.csv")
    print("工单:", receipt.created)

    cs = {"name": "cs-1", "role": Role.CS.value}
    asset = {"name": "asset-1", "role": Role.ASSET.value}
    warranty = {"name": "warranty-1", "role": Role.WARRANTY.value}
    engineer = {"name": "part-1", "role": Role.PART.value}

    print("\n== T1：完整确认后派单 ==")
    service.set_appointment(cs, "T1", start=(clock.now() + timedelta(hours=2)).isoformat(),
                            end=(clock.now() + timedelta(hours=4)).isoformat())
    service.confirm_asset(asset, "T1", serial="A-11", model="P-4", firmware="3.4")
    service.confirm_warranty(warranty, "T1", covered=True, warranty_state="valid")
    service.confirm_part(engineer, "T1", part="BEAR-1", model="P-4",
                         firmware_from="3.0", firmware_to="3.9")
    for line in service.explain("T1")["explanation"]:
        print(" ", line)
    print("预留:", service.reserve(cs, "T1")["state"])
    print("派单:", service.dispatch(cs, "T1")["state"])

    print("\n== T2：缺少确认时只给待核选项 ==")
    for line in service.explain("T2")["explanation"]:
        print(" ", line)

    print("\n== T3：保修已过期 ==")
    service.confirm_asset(asset, "T3", serial="A-13", model="C-8", firmware="2.0")
    service.confirm_warranty(warranty, "T3", covered=False, warranty_state="expired")
    for line in service.explain("T3")["explanation"]:
        print(" ", line)

    print("\n== 客服工作台 ==")
    for row in service.board():
        print(f" {row['ticket']}: {row['state']} / {row['verdict']} / "
              f"派单={row['can_dispatch']}")
    service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
