"""经销商门户的可替换适配层与本地演示实现。

真实部署时把 DemoPortal 换成浏览器自动化（Playwright/Selenium）或 HTTP 客户端即可，
上层工作流只依赖 PortalAdapter 的四个方法。
"""

from __future__ import annotations

import itertools
import threading

from .reservations import PortalAdapter


class DemoPortal(PortalAdapter):
    """内存中的假门户，用于演示与测试。

    - conflict_on_hold: 预留时模拟"库存被其他订单占用"；
    - ambiguous_reserve: 第一次预留回执丢失（本地记 pending），对账可查回；
    - ambiguous_release: 释放回执丢失，对账可确认已释放；
    - shipped: 指定预留已发货。
    """

    def __init__(self, clock, stock=None):
        self.clock = clock
        self.stock = dict(stock or {})
        self._held = {}
        self._released = set()
        self._shipped = set()
        self._counter = itertools.count(1)
        self._lock = threading.Lock()
        self.calls = []
        self.conflict_on_hold = False
        self.ambiguous_reserve = False
        self.ambiguous_release = False
        self.fail_reserve_once = False

    # -- 测试钩子 --------------------------------------------------------
    def set_stock(self, partner, item, quantity):
        self.stock[(partner, item)] = quantity

    def mark_shipped(self, portal_ref):
        self._shipped.add(portal_ref)

    # -- 适配器实现 ------------------------------------------------------
    def reserve(self, *, request_key, partner, item, quantity, ticket):
        with self._lock:
            self.calls.append(("reserve", request_key, partner, item))
            # 按幂等键去重：重复请求返回同一回执，不重复占库
            existing = self._held.get(request_key)
            if existing:
                return {"state": "held", "portal_ref": existing, "deduplicated": True}
            if self.fail_reserve_once:
                self.fail_reserve_once = False
                raise TimeoutError("经销商门户响应超时")
            if self.conflict_on_hold:
                return {"state": "conflict", "reason": "库存已被其他订单占用",
                        "request_key": request_key}
            if self.stock.get((partner, item), 0) < quantity:
                return {"state": "out_of_stock", "reason": "经销商可用库存不足",
                        "request_key": request_key}
            self.stock[(partner, item)] -= quantity
            ref = f"H-{next(self._counter):04d}"
            self._held[request_key] = ref
            if self.ambiguous_reserve:
                # 门户其实成功了，但回执在返回途中丢失
                self.ambiguous_reserve = False
                return {"state": "unknown", "request_key": request_key}
            return {"state": "held", "portal_ref": ref, "request_key": request_key}

    def query(self, *, request_key=None, portal_ref=None, partner=None, item=None):
        with self._lock:
            self.calls.append(("query", request_key, portal_ref))
            ref = self._held.get(request_key) if request_key else None
            if ref:
                if ref in self._shipped:
                    return {"state": "consumed", "portal_ref": ref}
                if ref in self._released:
                    return {"state": "released", "portal_ref": ref}
                return {"state": "held", "portal_ref": ref}
            if portal_ref and portal_ref in self._shipped:
                return {"state": "consumed", "portal_ref": portal_ref}
            if portal_ref and portal_ref in self._released:
                return {"state": "released", "portal_ref": portal_ref}
            return {"state": "none", "portal_ref": None}

    def release(self, *, portal_ref, request_key, partner, item, quantity):
        with self._lock:
            self.calls.append(("release", portal_ref, request_key))
            if portal_ref in self._shipped:
                return {"state": "consumed", "reason": "已发货，不能释放"}
            self._released.add(portal_ref)
            self._held.pop(request_key, None)
            self.stock[(partner, item)] = self.stock.get((partner, item), 0) + quantity
            if self.ambiguous_release:
                self.ambiguous_release = False
                return {"state": "unknown"}
            return {"state": "released"}

    def shipment(self, *, portal_ref):
        with self._lock:
            if portal_ref in self._shipped:
                return "shipped"
            if portal_ref in self._released:
                return "pending"
            return "pending" if portal_ref in self._held.values() else "unknown"

    # -- 展示 ------------------------------------------------------------
    def snapshot(self):
        return {
            "stock": {f"{p}/{i}": q for (p, i), q in sorted(self.stock.items())},
            "held": dict(sorted(self._held.items())),
            "released": sorted(self._released),
            "shipped": sorted(self._shipped),
            "calls": list(self.calls),
        }


class ScriptedPortal(PortalAdapter):
    """按脚本逐次返回响应，便于测试各种门户异常顺序。"""

    def __init__(self, reserve_responses=(), query_responses=(), release_responses=(),
                 shipment_responses=()):
        self.reserve_responses = list(reserve_responses)
        self.query_responses = list(query_responses)
        self.release_responses = list(release_responses)
        self.shipment_responses = list(shipment_responses)
        self.calls = []

    def _next(self, queue, name, **payload):
        self.calls.append((name, payload))
        if not queue:
            raise AssertionError(f"{name} 被额外调用：{payload}")
        value = queue.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def reserve(self, **payload):
        return self._next(self.reserve_responses, "reserve", **payload)

    def query(self, **payload):
        return self._next(self.query_responses, "query", **payload)

    def release(self, **payload):
        return self._next(self.release_responses, "release", **payload)

    def shipment(self, **payload):
        return self._next(self.shipment_responses, "shipment", **payload)
