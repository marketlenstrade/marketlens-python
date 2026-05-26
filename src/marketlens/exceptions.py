from __future__ import annotations


class MarketLensError(Exception):
    """Base exception for all SDK errors."""


class APIError(MarketLensError):
    """API returned an error response."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class AuthenticationError(APIError):
    """401 Unauthorized."""


class ForbiddenError(APIError):
    """403 Forbidden."""


class NotFoundError(APIError):
    """404 Not Found."""


class InvalidParameterError(APIError):
    """400 Invalid Parameter."""


class RateLimitError(APIError):
    """429 Rate Limit Exceeded."""

    def __init__(self, status_code: int, code: str, message: str, retry_after: int | None = None) -> None:
        super().__init__(status_code, code, message)
        self.retry_after = retry_after


class DailyBudgetExceededError(APIError):
    """429 Daily event budget exhausted. Resets at midnight UTC.

    Not auto-retried by the SDK (unlike :class:`RateLimitError`).
    """

    def __init__(self, status_code: int, code: str, message: str, retry_after: int | None = None) -> None:
        super().__init__(status_code, code, message)
        self.retry_after = retry_after


class ExportNotReadyError(APIError):
    """409 EXPORT_NOT_READY — pre-built parquet is not on the bucket yet."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        export_status: str | None = None,
        last_error: str | None = None,
    ) -> None:
        super().__init__(status_code, code, message)
        self.export_status = export_status
        self.last_error = last_error


class ConnectionError(MarketLensError):
    """Network connection failure."""


class TimeoutError(MarketLensError):
    """Request timed out."""


_STATUS_TO_EXCEPTION: dict[int, type[APIError]] = {
    401: AuthenticationError,
    403: ForbiddenError,
    404: NotFoundError,
    429: RateLimitError,
}

_CODE_TO_EXCEPTION: dict[str, type[APIError]] = {
    "UNAUTHORIZED": AuthenticationError,
    "FORBIDDEN": ForbiddenError,
    "TIER_LIMIT_REACHED": ForbiddenError,
    "MARKET_NOT_FOUND": NotFoundError,
    "EVENT_NOT_FOUND": NotFoundError,
    "SERIES_NOT_FOUND": NotFoundError,
    "DATA_NOT_AVAILABLE": NotFoundError,
    "KEY_NOT_FOUND": NotFoundError,
    "INVALID_PARAMETER": InvalidParameterError,
    "RANGE_TOO_LARGE": InvalidParameterError,
    "CURSOR_EXPIRED": InvalidParameterError,
    "RATE_LIMITED": RateLimitError,
    "DAILY_BUDGET_EXCEEDED": DailyBudgetExceededError,
    "EXPORT_NOT_READY": ExportNotReadyError,
}
