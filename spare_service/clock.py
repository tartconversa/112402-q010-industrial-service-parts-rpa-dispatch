"""业务时钟与数据新鲜度。

新鲜度按"查询时间"计算，而不是按我们入库的时间计算：经销商门户上的快照可能
在我们拉取之前就已经过期。库存在浏览器缓存里停留很久也不会因此变新。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta

from .db import Clock, parse_time


@dataclass(frozen=True)
class Freshness:
    state: str  # fresh | stale | future | unknown
    checked_at: object
    age: timedelta | None
    limit: timedelta
    reason: str

    @property
    def usable(self):
        return self.state == "fresh"

    def to_dict(self):
        return {
            "state": self.state,
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
            "age_seconds": int(self.age.total_seconds()) if self.age is not None else None,
            "limit_seconds": int(self.limit.total_seconds()),
            "reason": self.reason,
        }


def assess(checked_at, *, now, max_age, skew=timedelta(minutes=2)):
    """判断一条库存快照在 now 时刻是否还能作为派单依据。"""
    if checked_at is None:
        return Freshness("unknown", None, None, max_age, "来源没有提供查询时间")
    checked_at = parse_time(checked_at)
    age = now - checked_at
    if age < -skew:
        return Freshness(
            "future", checked_at, age, max_age,
            f"查询时间晚于当前时间 {-age.total_seconds():.0f} 秒，时钟不可信",
        )
    if age > max_age:
        return Freshness(
            "stale", checked_at, age, max_age,
            f"库存快照距今 {age.total_seconds()/60:.1f} 分钟，超过允许的 {max_age.total_seconds()/60:.0f} 分钟",
        )
    return Freshness("fresh", checked_at, age, max_age, "库存快照在允许的新鲜度内")


class BusinessClock(Clock):
    """可注入的时钟，测试中用它冻结时间，生产用系统时间。"""

    def __init__(self, max_inventory_age=timedelta(minutes=30)):
        self.max_inventory_age = max_inventory_age

    def freshness(self, checked_at, *, max_age=None):
        return assess(
            checked_at,
            now=self.now(),
            max_age=max_age or self.max_inventory_age,
        )


class FixedClock(BusinessClock):
    """冻结时间，可按秒推进，用于演示和可重复测试。"""

    def __init__(self, moment, max_inventory_age=timedelta(minutes=30)):
        super().__init__(max_inventory_age=max_inventory_age)
        self.moment = parse_time(moment) if not isinstance(moment, datetime) else moment
        self._offset = 0

    def now(self):
        return self.moment + timedelta(seconds=self._offset)

    def advance(self, seconds):
        self._offset += seconds
        return self.now()
