from __future__ import annotations


class DiscordInteractionWebhookExpiredError(RuntimeError):
    """Raised when Discord interaction follow-up webhook is no longer valid."""
