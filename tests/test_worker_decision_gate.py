from datetime import datetime, timezone
from types import SimpleNamespace

from orchestrator.core.worker_decision_gate import apply_decision_gate


class _Session:
    def __init__(self) -> None:
        self._commits = 0
        self.refreshed = False

    def commit(self) -> None:
        self._commits += 1

    def refresh(self, _run) -> None:  # noqa: ANN001
        self.refreshed = True


def _run() -> SimpleNamespace:
    return SimpleNamespace(
        tenant_id="tenant-1",
        issue_key="example-46",
        run_id="run-1",
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
        settings=SimpleNamespace(),
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
        settings=SimpleNamespace(),
        evaluate_decision_gate_fn=lambda **_kwargs: result,
        send_discord_message_fn=lambda **_kwargs: None,
        send_jira_message_fn=lambda **_kwargs: None,
        ask_reply_components_fn=lambda: [],
        blocked_status="blocked",
        failed_status="failed",
    )
    assert terminal is None
    assert meta is None
