class DomainError(Exception):
    status = 400
    code = "bad_request"

    def __init__(self, message="", *, code=None):
        super().__init__(message or self.code)
        if code:
            self.code = code


class BadRequest(DomainError):
    status = 400
    code = "bad_request"


class Forbidden(DomainError):
    status = 403
    code = "forbidden"


class NotFound(DomainError):
    status = 404
    code = "not_found"


class Conflict(DomainError):
    status = 409
    code = "conflict"


class Gone(DomainError):
    status = 410
    code = "gone"


class QuotaExceeded(DomainError):
    """修改次数/报价约束被触发，需要追加报价(amendment)。"""
    status = 402
    code = "quota_exceeded"
