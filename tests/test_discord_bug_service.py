from orchestrator.api.discord.discord_bug_service import build_discord_bug_description, normalize_discord_attachments


def test_normalize_discord_attachments_limits_and_filters() -> None:
    attachments = normalize_discord_attachments(
        [
            {"filename": "a.png", "url": "https://example.test/a.png", "content_type": "image/png"},
            {"filename": "missing-url"},
        ]
    )
    assert len(attachments) == 1
    assert attachments[0]["filename"] == "a.png"


def test_build_discord_bug_description_renders_attachment_links() -> None:
    description = build_discord_bug_description(
        summary="Webhook failed",
        details="Got a 502 from Jira webhook registration.",
        reporter_user_id="u-admin",
        channel_id="123",
        related_issue_key="YANA-46",
        attachments=[{"filename": "trace.png", "url": "https://example.test/trace.png", "content_type": "image/png"}],
    )
    assert "Reported via Discord" in description
    assert "Related issue: YANA-46" in description
    assert "[trace.png](https://example.test/trace.png)" in description
