"""统一的业务异常。

所有面向客服的错误都应携带稳定的代码和中文可读说明，方便在工单界面直接展示，
同时避免把未经确认的推测当成结论。
"""


class ServiceError(Exception):
    """业务异常的基类，携带机器可读的 code。"""

    code = "service_error"

    def __init__(self, message, *, code=None, details=None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.details = dict(details or {})

    def to_dict(self):
        return {"code": self.code, "message": self.message, "details": self.details}


class ValidationError(ServiceError):
    code = "validation_error"


class NotFound(ServiceError):
    code = "not_found"


class Conflict(ServiceError):
    code = "conflict"


class PermissionDenied(ServiceError):
    code = "permission_denied"


class StaleData(ServiceError):
    """库存快照超过允许的新鲜度。"""

    code = "stale_inventory"


class NotConfirmed(ServiceError):
    """缺少必要的确认，只能提出待核选项。"""

    code = "pending_verification"


class ReservationConflict(ServiceError):
    """经销商门户库存已被其他订单占用。"""

    code = "reservation_conflict"
