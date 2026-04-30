import pytest

from orchestrator.core.jira_parent_child_sync_publishers import (
    DISCORD_MESSAGE_CONTENT_LIMIT,
    chunk_discord_message_content,
    post_discord_message_content,
)


class _FakeDiscordClient:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def post_message(self, *, channel_id: str, content: str) -> dict[str, str]:
        self.messages.append((channel_id, content))
        return {"id": f"message-{len(self.messages)}"}


def test_chunk_discord_message_content_keeps_messages_within_discord_limit() -> None:
    content = "\n".join(
        [
            "Decision needed for `MAB-238` before I can finish backlog planning.",
            "Question one: " + ("a" * 1700),
            "Question two: " + ("b" * 1700),
        ]
    )

    chunks = chunk_discord_message_content(content)

    assert len(chunks) == 2
    assert all(0 < len(chunk) <= DISCORD_MESSAGE_CONTENT_LIMIT for chunk in chunks)
    assert "\n".join(chunks) == content


def test_chunk_discord_message_content_splits_single_line_over_discord_limit() -> None:
    content = "x" * (DISCORD_MESSAGE_CONTENT_LIMIT + 10)

    chunks = chunk_discord_message_content(content)

    assert [len(chunk) for chunk in chunks] == [DISCORD_MESSAGE_CONTENT_LIMIT, 10]
    assert "".join(chunks) == content


def test_post_discord_message_content_posts_each_chunk() -> None:
    client = _FakeDiscordClient()
    content = "x" * (DISCORD_MESSAGE_CONTENT_LIMIT + 10)

    responses = post_discord_message_content(client=client, channel_id="thread-1", content=content)  # type: ignore[arg-type]

    assert responses == [{"id": "message-1"}, {"id": "message-2"}]
    assert [channel_id for channel_id, _ in client.messages] == ["thread-1", "thread-1"]
    assert all(len(message) <= DISCORD_MESSAGE_CONTENT_LIMIT for _, message in client.messages)


def test_chunk_discord_message_content_rejects_empty_content() -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        chunk_discord_message_content(" ")
