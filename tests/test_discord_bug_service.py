from orchestrator.api.discord.bug.service import (
    build_discord_bug_description,
    normalize_discord_attachments,
)


def test_normalize_discord_attachments_limits_and_filters() -> None:
    attachments = normalize_discord_attachments(
        [
            {
                "filename": "a.png",
                "url": "https://example.test/a.png",
                "content_type": "image/png",
            },
            {
                "filename": "b.png",
                "url": "https://cdn.discordapp.com/b.png",
                "proxy_url": "https://media.discordapp.net/b.png",
            },
            {"filename": "missing-url"},
        ]
    )
    assert len(attachments) == 2
    assert attachments[0]["filename"] == "a.png"
    assert "proxy_url" not in attachments[0]
    assert attachments[1]["filename"] == "b.png"
    assert attachments[1]["url"] == "https://cdn.discordapp.com/b.png"
    assert attachments[1]["proxy_url"] == "https://media.discordapp.net/b.png"


def test_build_discord_bug_description_renders_attachment_names_without_links() -> None:
    description = build_discord_bug_description(
        summary="Webhook failed",
        details="Got a 502 from Jira webhook registration.",
        reporter_user_id="u-admin",
        channel_id="123",
        related_issue_key="DEMO-46",
        attachments=[
            {
                "filename": "trace.png",
                "url": "https://example.test/trace.png",
                "content_type": "image/png",
            }
        ],
    )
    assert "Summary" in description
    assert "Details" in description
    assert "Related issue: DEMO-46" in description
    assert "trace.png (image/png)" in description
    assert "https://example.test/trace.png" not in description
