from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

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


class _IssueDetailClient:
    def __init__(
        self,
        *,
        summary: str = "Summary",
        description: str = "Description",
        labels: list[str] | None = None,
    ) -> None:
        self.summary = summary
        self.description = description
        self.labels = list(labels or [])

    def get_issue_detail(self, *, access_token: str, cloud_id: str, issue_id_or_key: str):  # noqa: ARG002
        return SimpleNamespace(
            key=issue_id_or_key,
            summary=self.summary,
            description=self.description,
            labels=list(self.labels),
        )


def _oauth_context(*, client: _IssueDetailClient):
    return SimpleNamespace(
        client=client,
        connection=SimpleNamespace(cloud_id="cloud-1"),
        access_token="access-token",
    )


def _run() -> SimpleNamespace:
    return SimpleNamespace(
        tenant_id="tenant-1",
        issue_key="YANA-46",
        run_id="run-1",
        project_id=None,
        issue_summary="Summary",
        issue_description="Description",
        status="queued",
        last_error=None,
        finished_at=None,
        plan=None,
    )


def _readiness_check(
    *,
    outcome: str,
    ready_label: str = "agent:ready",
    ready_label_present: bool = True,
) -> PreRunCheckResult:
    return PreRunCheckResult(
        outcome=outcome,
        ready_label=ready_label,
        ready_label_present=ready_label_present,
        required_worker_capability="linux",
        required_worker_label="worker:linux",
        required_worker_label_present=True,
        decision_gate=DecisionGateResult(
            triggered=False,
            reason="Decision Gate permanently satisfied",
            missing_sections=(),
            questions=(),
            recommendation="Proceed",
            tags=(),
        ),
        gtd=GoodToDoValidationResult(
            valid=True,
            missing_criteria=(),
            clarification_questions=(),
        ),
    )


def test_apply_decision_gate_marks_failed_on_configuration_error() -> None:
    session = _Session()
    run = _run()
    tenant = SimpleNamespace(tenant_id="tenant-1")

    with patch("orchestrator.core.worker.decision_gate.resolve_project_for_run", return_value=None):
        terminal, meta = apply_decision_gate(
            session=session,
            run=run,
            tenant=tenant,
            settings=SimpleNamespace(admin_ui_base_url="https://admin.example.test"),
            tenant_jira_oauth_context_fn=lambda **_: None,
            send_discord_message_fn=lambda **_: None,
            send_jira_message_fn=lambda **_: None,
            ask_reply_components_fn=lambda: [],
            blocked_status="blocked",
            failed_status="failed",
        )

    assert terminal is run
    assert meta is None
    assert run.status == "failed"
    assert "Execution readiness check failed" in (run.last_error or "")
    assert isinstance(run.finished_at, datetime)


def test_apply_decision_gate_returns_none_when_issue_is_ready() -> None:
    session = _Session()
    run = _run()
    tenant = SimpleNamespace(tenant_id="tenant-1", jira_config={"ready_label": "agent:ready"})
    oauth = _oauth_context(client=_IssueDetailClient(labels=["agent:ready"]))

    with patch("orchestrator.core.worker.decision_gate.resolve_project_for_run", return_value=None):
        terminal, meta = apply_decision_gate(
            session=session,
            run=run,
            tenant=tenant,
            settings=SimpleNamespace(admin_ui_base_url="https://admin.example.test"),
            tenant_jira_oauth_context_fn=lambda **_: oauth,
            evaluate_execution_readiness_fn=lambda **_: _readiness_check(outcome="ready_for_agent"),
            send_discord_message_fn=lambda **_: None,
            send_jira_message_fn=lambda **_: None,
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
    tenant = SimpleNamespace(
        tenant_id="tenant-1",
        jira_config={"connection_id": "conn-1", "ready_label": "agent:ready"},
    )
    sent_discord_messages: list[str] = []
    oauth = _oauth_context(client=_IssueDetailClient(labels=[]))

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

    with (
        patch("orchestrator.core.worker.decision_gate.resolve_project_for_run", return_value=None),
        patch("orchestrator.core.worker.decision_gate.mark_run_terminal", return_value=run),
    ):
        terminal, meta = apply_decision_gate(
            session=session,
            run=run,
            tenant=tenant,
            settings=SimpleNamespace(admin_ui_base_url="https://admin.example.test"),
            tenant_jira_oauth_context_fn=lambda **_: oauth,
            evaluate_execution_readiness_fn=lambda **_: _readiness_check(
                outcome="missing_ready_label",
                ready_label_present=False,
            ),
            send_discord_message_fn=_send_discord_message_fn,
            send_jira_message_fn=lambda **_: None,
            ask_reply_components_fn=lambda: [],
            blocked_status="blocked",
            failed_status="failed",
        )
    assert terminal is not None
    assert meta is not None
    assert meta["stage_update"]["stage"] == "run_not_ready"
    assert sent_discord_messages
    assert "https://jira.example.test/browse/YANA-46" in sent_discord_messages[0]


def test_apply_decision_gate_passes_issue_context_into_readiness_check() -> None:
    session = _Session()
    run = _run()
    tenant = SimpleNamespace(tenant_id="tenant-1", jira_config={"ready_label": "agent:ready"})
    captured: dict[str, object] = {}
    oauth = _oauth_context(
        client=_IssueDetailClient(
            summary="Live issue summary",
            description="Live issue description",
            labels=["agent:ready", "worker:linux"],
        )
    )

    def _capture_readiness(**kwargs):  # noqa: ANN003
        captured.update(kwargs)
        return _readiness_check(outcome="ready_for_agent")

    with patch("orchestrator.core.worker.decision_gate.resolve_project_for_run", return_value=None):
        terminal, meta = apply_decision_gate(
            session=session,
            run=run,
            tenant=tenant,
            settings=SimpleNamespace(admin_ui_base_url="https://admin.example.test"),
            tenant_jira_oauth_context_fn=lambda **_: oauth,
            evaluate_execution_readiness_fn=_capture_readiness,
            send_discord_message_fn=lambda **_: None,
            send_jira_message_fn=lambda **_: None,
            ask_reply_components_fn=lambda: [],
            blocked_status="blocked",
            failed_status="failed",
        )

    assert terminal is None
    assert meta is None
    assert captured["tenant_id"] == "tenant-1"
    assert captured["project_id"] is None
    assert captured["issue_key"] == "YANA-46"
    assert captured["issue_summary"] == "Live issue summary"
    assert captured["issue_description"] == "Live issue description"
    assert captured["issue_labels"] == ["agent:ready", "worker:linux"]
    assert captured["ready_label"] == "agent:ready"


def test_apply_decision_gate_preserves_trigger_context_on_block() -> None:
    session = _Session()
    run = _run()
    run.plan = {"trigger_context": {"source": "github_pr_review_feedback", "pr_number": 6}}
    tenant = SimpleNamespace(tenant_id="tenant-1", jira_config={"ready_label": "agent:ready"})
    oauth = _oauth_context(client=_IssueDetailClient(labels=[]))

    with (
        patch("orchestrator.core.worker.decision_gate.resolve_project_for_run", return_value=None),
        patch("orchestrator.core.worker.decision_gate.mark_run_terminal", return_value=run),
    ):
        terminal, _meta = apply_decision_gate(
            session=session,
            run=run,
            tenant=tenant,
            settings=SimpleNamespace(admin_ui_base_url="https://admin.example.test"),
            tenant_jira_oauth_context_fn=lambda **_: oauth,
            evaluate_execution_readiness_fn=lambda **_: _readiness_check(
                outcome="missing_ready_label",
                ready_label_present=False,
            ),
            send_discord_message_fn=lambda **_: SimpleNamespace(sent=True, reason="sent"),
            send_jira_message_fn=lambda **_: None,
            ask_reply_components_fn=lambda: [],
            blocked_status="blocked",
            failed_status="failed",
        )

    assert terminal is run
    assert isinstance(run.plan, dict)
    assert run.plan.get("trigger_context") == {"source": "github_pr_review_feedback", "pr_number": 6}
    assert run.plan.get("stage_updates", [{}])[0].get("stage") == "run_not_ready"
