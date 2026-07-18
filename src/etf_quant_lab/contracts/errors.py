"""Domain error contract used by services, jobs, CLI and UI."""

from collections.abc import Mapping
from enum import StrEnum
from typing import Final


class ErrorCode(StrEnum):
    """Common stable error codes; feature modules may add their own codes."""

    SYS_INTERNAL_ERROR = "SYS_INTERNAL_ERROR"
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    CONFIG_INVALID = "CONFIG_INVALID"
    CONFIG_SECRET_MISSING = "CONFIG_SECRET_MISSING"
    CONFIG_PATH_NOT_WRITABLE = "CONFIG_PATH_NOT_WRITABLE"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    RATE_LIMITED = "RATE_LIMITED"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    DATA_QUALITY_BLOCKED = "DATA_QUALITY_BLOCKED"
    DATA_SOURCE_DIVERGENCE = "DATA_SOURCE_DIVERGENCE"


_EMPTY_DETAILS: Final[Mapping[str, object]] = {}


class DomainError(Exception):
    """Expected business failure with a stable, presentation-safe contract."""

    def __init__(
        self,
        code: str | ErrorCode,
        message: str,
        *,
        details: Mapping[str, object] = _EMPTY_DETAILS,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = message
        self.details = dict(details)
        self.retryable = retryable

    def as_dict(self) -> dict[str, object]:
        """Return the safe fields intended for UI or job-status serialization."""

        return {
            "code": self.code,
            "message": self.message,
            "details": dict(self.details),
            "retryable": self.retryable,
        }
