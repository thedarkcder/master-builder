from datetime import datetime, timezone
from types import SimpleNamespace

from orchestrator.api.discord_ask_history_service import DiscordAskHistoryService


def _tenant(discord_config: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        discord_config=discord_config or {},
        jira_config={"connection_id": "conn-1"},
        updated_at=None,
    )


class _Session:
    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1


def test_store_and_consume_pending_ask_action_roundtrip() -> None:
    service = DiscordAskHistoryService()
    session = _Session()
    tenant = _tenant()

    stored = service.store_pending_ask_action(
        session=session,
        tenant=tenant,
        request_id="req-1",
        user_id="u1",
        channel_id="c1",
        question="q",
        summary="s",
        proposed_command="!run YANA-1",
    )
    assert stored["request_id"] == "req-1"
    assert session.commits == 1

    consumed = service.consume_pending_ask_action(session=session, tenant=tenant, request_id="req-1")
    assert consumed is not None
    assert consumed["proposed_command"] == "!run YANA-1"
    assert session.commits == 2


def test_collect_ask_context_with_history_context_uses_recent_issue_scope() -> None:
    service = DiscordAskHistoryService()
    session = _Session()
    tenant = _tenant(
        {
            "ask_history": [
                {
                    "user_id": "u1",
                    "channel_id": "c1",
                    "question": "old",
                    "answer": "ans",
                    "issue_key": "YANA-46",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            ]
        }
    )

    def _collect(**kwargs):  # type: ignore[no-untyped-def]
        assert kwargs["scoped_issue_key"] == "YANA-46"
        return "YANA-46", None, [{"key": "YANA-46", "summary": "x", "status": "Testing"}], {"Testing": 1}

    result = service.collect_ask_context_with_history_context(
        session=session,
        tenant=tenant,
        user_id="u1",
        channel_id="c1",
        question="what changed",
        scoped_issue_key=None,
        collect_ask_context_fn=_collect,
        existing_issue_keys_fn=lambda **_kwargs: {"YANA-46"},
    )
    assert result[0] == "YANA-46"
    assert result[4][0]["issue_key"] == "YANA-46"
