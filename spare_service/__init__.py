"""设备备件目录采集与保修服务派单。

核心不变量：
1. 没有三类确认（资产/保修/备件）就没有兼容结论，只能给待核选项；
2. 库存结论必须带查询时间与来源，过期快照不能派单；
3. 派单前保修、兼容、库存新鲜度、预约、门户预留回执必须同时通过；
4. 门户预留结果不明时先查询再重试，绝不盲目重发；
5. 已发货的备件不能被释放，也不能被当成可用库存；
6. 目录修订全部保留，撤回替代件或现场配置更正要触发复核。
"""

from .catalog import Catalog
from .clock import BusinessClock, FixedClock
from .db import Database
from .evaluation import DispatchEvaluator
from .inventory import Inventory
from .models import DispatchState, ReservationState, Role
from .portal import DemoPortal, ScriptedPortal
from .service import Service
from .tickets import Tickets

__all__ = [
    "Catalog",
    "BusinessClock",
    "Database",
    "FixedClock",
    "DispatchEvaluator",
    "Inventory",
    "DispatchState",
    "ReservationState",
    "Role",
    "DemoPortal",
    "ScriptedPortal",
    "Service",
    "Tickets",
]
