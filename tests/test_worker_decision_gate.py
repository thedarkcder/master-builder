from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator.core.decision_engine import WorkerDecision
from orchestrator.core.decision_gate import DecisionGateResult
from orchestrator.core.gtd import GoodToDoValidationResult
from orchestrator.core.pre_run_check import PreRunCheckResult
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

    def execute(self, _statement):  # noqa: ANN001
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


def _worker_decision(
    *,
    outcome: str,
    policy_error: str | None = None,
    decision_gate_reason: str = "Decision Gate not required",
    decision_gate_questions: tuple[str, ...] = (),
) -> WorkerDecision:
    pre_check = PreRunCheckResult(
        outcome=outcome,
        ready_label="agent:ready",
        ready_label_present=True,
        required_worker_capability="linux",
        required_worker_label="worker:linux",
        required_worker_label_present=True,
        decision_gate=DecisionGateResult(
            triggered=outcome == "decision_gate_required",
            reason=decision_gate_reason,
            missing_sections=(),
            questions=decision_gate_questions,
            recommendation="Clarification required" if outcome == "decision_gate_required" else "Proceed",
            tags=(),
        ),
        gtd=GoodToDoValidationResult(
            valid=outcome != "gtd_required",
            missing_criteria=(),
            clarification_questions=(),
        ),
    )
    return WorkerDecision(
        allowed=outcome not in {"decision_gate_required", "gtd_required"} and not policy_error,
        decision_gate=pre_check.decision_gate if outcome == "decision_gate_required" else None,
        configuration_error=policy_error,
        block_reason=outcome if outcome in {"decision_gate_required", "gtd_required"} else None,
        classification="decision_gate" if outcome == "decision_gate_required" else "clear",
        pre_check=pre_check,
    )


def test_apply_decision_gate_marks_failed_on_configuration_error() -> None:
    session = _Session()
    run = _run()
    tenant = SimpleNamespace(tenant_id="tenant-1")

    def _send_discord_message(
        *,
        session,
        tenant,
        project,
        message: str,
        settings,
        event: str,
        open_thread: bool = False,
        thread_name: str | None = None,
        thread_intro: str | None = None,
        thread_intro_components: list[dict[str, object]] | None = None,
    ) -> None:
        _ = session, tenant, project, message, settings, event, open_thread, thread_name, thread_intro, thread_intro_components
        return None

    def _send_jira_message(*, session, tenant, issue_key: str, stage: str, message: str, settings) -> None:  # noqa: ANN001
        _ = session, tenant, issue_key, stage, message, settings
        return None

    with patch(
        "orchestrator.core.worker.decision_gate.evaluate_worker_decision",
        return_value=_worker_decision(outcome="clear", policy_error="rules missing"),
    ), patch(
        "orchestrator.core.worker.decision_gate.resolve_project_for_run",
        return_value=None,
    ):
        terminal, meta = apply_decision_gate(
            session=session,
            run=run,
            tenant=tenant,
            settings=SimpleNamespace(admin_ui_base_url="https://admin.example.test"),
            tenant_jira_oauth_context_fn=lambda **_: None,
            send_discord_message_fn=_send_discord_message,
            send_jira_message_fn=_send_jira_message,
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
    def _send_discord_message(
        *,
        session,
        tenant,
        project,
        message: str,
        settings,
        event: str,
        open_thread: bool = False,
        thread_name: str | None = None,
        thread_intro: str | None = None,
        thread_intro_components: list[dict[str, object]] | None = None,
    ) -> None:
        _ = session, tenant, project, message, settings, event, open_thread, thread_name, thread_intro, thread_intro_components
        return None

    def _send_jira_message(*, session, tenant, issue_key: str, stage: str, message: str, settings) -> None:  # noqa: ANN001
        _ = session, tenant, issue_key, stage, message, settings
        return None

    with patch(
        "orchestrator.core.worker.decision_gate.evaluate_worker_decision",
        return_value=_worker_decision(outcome="ready_for_agent"),
    ), patch(
        "orchestrator.core.worker.decision_gate.resolve_project_for_run",
        return_value=None,
    ):
        terminal, meta = apply_decision_gate(
            session=session,
            run=run,
            tenant=tenant,
            settings=SimpleNamespace(admin_ui_base_url="https://admin.example.test"),
            tenant_jira_oauth_context_fn=lambda **_: None,
            send_discord_message_fn=_send_discord_message,
            send_jira_message_fn=_send_jira_message,
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
    sent_discord_messages: list[str] = []

    def _send_discord_message_fn(
        *,
        session,
        tenant,
        project,
        message: str,
        settings,
        event: str,
        open_thread: bool = False,
        thread_name: str | None = None,
        thread_intro: str | None = None,
        thread_intro_components: list[dict[str, object]] | None = None,
    ):
        _ = session, tenant, project, settings, event, open_thread, thread_name, thread_intro, thread_intro_components
        sent_discord_messages.append(str(message or ""))
        return SimpleNamespace(sent=True, reason="sent")

    def _send_jira_message(*, session, tenant, issue_key: str, stage: str, message: str, settings) -> None:  # noqa: ANN001
        _ = session, tenant, issue_key, stage, message, settings
        return None

    with (
        patch(
            "orchestrator.core.worker.decision_gate.evaluate_worker_decision",
            return_value=_worker_decision(
                outcome="decision_gate_required",
                decision_gate_reason="Need PM clarity",
                decision_gate_questions=("What is in scope?",),
            ),
        ),
        patch("orchestrator.core.worker.decision_gate.resolve_project_for_run", return_value=None),
        patch("orchestrator.core.worker.decision_gate.mark_run_terminal", return_value=run),
    ):
        terminal, meta = apply_decision_gate(
            session=session,
            run=run,
            tenant=tenant,
            settings=SimpleNamespace(admin_ui_base_url="https://admin.example.test"),
            tenant_jira_oauth_context_fn=lambda **_: None,
            send_discord_message_fn=_send_discord_message_fn,
            send_jira_message_fn=_send_jira_message,
            ask_reply_components_fn=lambda: [],
            blocked_status="blocked",
            failed_status="failed",
        )
    assert terminal is not None
    assert meta is not None
    assert sent_discord_messages
    assert "https://jira.example.test/browse/example-46" in sent_discord_messages[0]
