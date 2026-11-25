"""领域枚举与工单状态机。

状态流转集中在这里，任何写操作都必须经过这里，避免不同入口把工单带到
不一致的状态。
"""

from enum import Enum


class Role(str, Enum):
    """有资格做出各类确认的角色。"""

    ASSET = "asset_admin"          # 资产管理员：设备序列号与现场配置
    WARRANTY = "warranty_specialist"  # 保修专员：故障与保修范围
    PART = "part_engineer"         # 备件工程师：替代关系及适用固件
    CS = "customer_service"        # 客服：派单、预留、查看
    SUPERVISOR = "service_supervisor"  # 服务主管：处理已派出工单的目录/配置变更


class WarrantyState(str, Enum):
    VALID = "valid"
    REVIEW = "review"
    EXPIRED = "expired"


class FaultState(str, Enum):
    """故障是否被保修专员确认属于本次保修范围。"""

    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    REVIEW = "review"


class ReservationState(str, Enum):
    NONE = "none"
    PENDING = "pending"     # 已发出预留请求，结果不明
    HELD = "held"           # 门户回执确认已预留
    CONSUMED = "consumed"   # 已发货/已使用，不再属于可用库存
    RELEASING = "releasing"  # 已请求释放，门户回执不明
    RELEASED = "released"   # 取消后已释放
    FAILED = "failed"       # 明确失败（无货、冲突等）


class DispatchState(str, Enum):
    DRAFT = "draft"                    # 待核选项，不能派单
    BLOCKED = "blocked"                # 有明确阻断原因
    QUEUED = "queued"                  # 四项检查通过，可派单
    DISPATCHED = "dispatched"          # 已派出
    NEEDS_RECHECK = "needs_recheck"    # 变更后需重新核验（未派出）
    SUPERVISOR_REVIEW = "supervisor_review"  # 已派出但变更影响，交服务主管
    CANCELLED = "cancelled"            # 订单取消


# 未派出的状态：可以自由重新核验
UNSENT_STATES = {DispatchState.DRAFT, DispatchState.BLOCKED, DispatchState.QUEUED, DispatchState.NEEDS_RECHECK}
SENT_STATES = {DispatchState.DISPATCHED, DispatchState.SUPERVISOR_REVIEW}


def require_role(actor, allowed):
    """校验操作者角色；缺少角色信息直接拒绝，不默认放行。"""
    from .errors import PermissionDenied

    roles = actor_roles(actor)
    allowed_values = {a.value if isinstance(a, Role) else a for a in allowed}
    if not roles & allowed_values:
        raise PermissionDenied(
            "当前操作者没有执行该操作的权限",
            details={"required_roles": sorted(allowed_values),
                     "actor_roles": sorted(r for r in roles if isinstance(r, str))},
        )


def actor_roles(actor):
    """接受 {"role": ...} / {"roles": [...]} / Role / 字符串 / 可迭代角色。"""
    if isinstance(actor, dict):
        roles = set(actor.get("roles") or ())
        if actor.get("role"):
            roles.add(actor["role"])
    elif not actor:
        return set()
    elif isinstance(actor, Role):
        return {actor.value}
    elif isinstance(actor, str):
        return {actor}
    else:
        roles = set(actor)
    return {r.value if isinstance(r, Role) else str(r) for r in roles if r is not None}


def actor_name(actor):
    if isinstance(actor, dict):
        return actor.get("name") or actor.get("id") or "unknown"
    return str(actor)
