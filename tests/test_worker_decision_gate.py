from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator.core.worker.decision_gate import apply_decision_gate
from orchestrator.storage.models import JiraOAuthConnection


class _Session:
    def __init__(self) -> None:
        self._commits = 0
        self.refreshed = False

    def commit(self) -> None:
        self._commits += 1

    def refresh(self, _run) -> None:  # noqa: ANN001
        self.refreshed = True

    def get(self, _model, _key):  # noqa: ANN001
        return None


def _run() -> SimpleNamespace:
    return SimpleNamespace(
        tenant_id="tenant-1",
        issue_key="example-46",
        run_id="run-1",
        project_id=None,
        issue_summary="Summary",
        issue_description="Description",
        status="queued",
        last_error=None,
        finished_at=None,
        plan=None,
    )


def test_apply_decision_gate_marks_failed_on_configuration_error() -> None:
    session = _Session()
    run = _run()
    tenant = SimpleNamespace(tenant_id="tenant-1")

    terminal, meta = apply_decision_gate(
        session=session,
        run=run,
        tenant=tenant,
        settings=SimpleNamespace(admin_ui_base_url="https://admin.example.test"),
        evaluate_decision_gate_fn=lambda **_kwargs: (_ for _ in ()).throw(ValueError("rules missing")),
        send_discord_message_fn=lambda **_kwargs: None,
        send_jira_message_fn=lambda **_kwargs: None,
        ask_reply_components_fn=lambda: [],
        blocked_status="blocked",
        failed_status="failed",
    )

    assert terminal is run
    assert meta is None
    assert run.status == "failed"
    assert "Decision Gate configuration error" in (run.last_error or "")
    assert isinstance(run.finished_at, datetime)


def test_apply_decision_gate_returns_none_when_not_triggered() -> None:
    session = _Session()
    run = _run()
    tenant = SimpleNamespace(tenant_id="tenant-1")
    result = SimpleNamespace(triggered=False)

    terminal, meta = apply_decision_gate(
        session=session,
        run=run,
        tenant=tenant,
        settings=SimpleNamespace(admin_ui_base_url="https://admin.example.test"),
        evaluate_decision_gate_fn=lambda **_kwargs: result,
        send_discord_message_fn=lambda **_kwargs: None,
        send_jira_message_fn=lambda **_kwargs: None,
        ask_reply_components_fn=lambda: [],
        blocked_status="blocked",
        failed_status="failed",
    )
    assert terminal is None
    assert meta is None


def test_apply_decision_gate_uses_tenant_jira_connection_url_for_stage_update() -> None:
    class _TriggeredSession(_Session):
        def get(self, model, key):  # noqa: ANN001
            if model is JiraOAuthConnection and key == "conn-1":
                return SimpleNamespace(site_url="https://jira.example.test")
            return None

    session = _TriggeredSession()
    run = _run()
    tenant = SimpleNamespace(tenant_id="tenant-1", jira_config={"connection_id": "conn-1"})
    decision_gate = SimpleNamespace(
        triggered=True,
        reason="Need PM clarity",
        questions=["What is in scope?"],
        to_payload=lambda: {"triggered": True},
    )
    sent_discord_messages: list[str] = []

    def _send_discord_message_fn(**kwargs):  # noqa: ANN003
        sent_discord_messages.append(str(kwargs.get("message") or ""))
        return SimpleNamespace(sent=True, reason="sent")

    with (
        patch("orchestrator.core.worker.decision_gate.resolve_project_for_run", return_value=None),
        patch("orchestrator.core.worker.decision_gate.mark_run_terminal", return_value=run),
    ):
        terminal, meta = apply_decision_gate(
            session=session,
            run=run,
            tenant=tenant,
            settings=SimpleNamespace(admin_ui_base_url="https://admin.example.test"),
            evaluate_decision_gate_fn=lambda **_kwargs: decision_gate,
            send_discord_message_fn=_send_discord_message_fn,
            send_jira_message_fn=lambda **_kwargs: None,
            ask_reply_components_fn=lambda: [],
            blocked_status="blocked",
            failed_status="failed",
        )
    assert terminal is not None
    assert meta is not None
    assert sent_discord_messages
    assert "https://jira.example.test/browse/example-46" in sent_discord_messages[0]
