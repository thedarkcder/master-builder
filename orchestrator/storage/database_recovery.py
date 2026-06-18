from __future__ import annotations

from sqlalchemy.exc import OperationalError as SQLAlchemyOperationalError

RETRYABLE_DATABASE_RECOVERY_MESSAGES = (
    "database system is in recovery mode",
    "connection failed",
    "could not connect",
    "connection refused",
    "consuming input failed",
    "database system is not yet accepting connections",
    "server closed the connection unexpectedly",
    "the database system is starting up",
    "timeout expired",
)


def retryable_database_recovery_error(exc: BaseException) -> bool:
    if not isinstance(exc, SQLAlchemyOperationalError):
        return False
    return retryable_database_recovery_message(exc)


def retryable_database_recovery_message(exc: BaseException) -> bool:
    message = " ".join(
        part
        for part in (
            str(getattr(exc, "orig", "") or "").strip(),
            str(exc).strip(),
        )
        if part
    ).lower()
    if not message:
        return False
    return any(needle in message for needle in RETRYABLE_DATABASE_RECOVERY_MESSAGES)


def database_recovery_retry_delay_seconds(
    *,
    attempt: int,
    base_delay_seconds: float = 1.0,
    max_delay_seconds: float = 8.0,
) -> float:
    bounded_attempt = max(1, int(attempt))
    return min(
        max(0.0, float(max_delay_seconds)),
        max(0.0, float(base_delay_seconds)) * (2 ** (bounded_attempt - 1)),
    )
