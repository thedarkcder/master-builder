from __future__ import annotations


class RetryableWebhookJobError(RuntimeError):
    """Signals a transient webhook processing failure that should be requeued."""

    def __init__(self, message: str, *, retry_after_seconds: int = 30) -> None:
        super().__init__(message)
        self.retry_after_seconds = max(int(retry_after_seconds), 0)
