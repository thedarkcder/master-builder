from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator.core.decision_gate import DecisionGateResult
from orchestrator.core.worker.decision_gate import apply_decision_gate
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.storage.models import JiraOAuthConnection


class _Session:
    def __init__(self) -> None:
        self._commits = 0
        self.refreshed = False
        self.refresh_attribute_names: list[object] = []

    def commit(self) -> None:
        self._commits += 1

    def refresh(self, _run, attribute_names=None) -> None:  # noqa: ANN001
        self.refreshed = True
        self.refresh_attribute_names.append(attribute_names)

    def get(self, _model, _key):  # noqa: ANN001
        return None

    def execute(self, _statement):  # noqa: ANN001
        return None


def _oauth_context():
    return SimpleNamespace(
        client=None,
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


def _worker_decision(
    *,
    allowed: bool,
    block_reason: str | None = None,
    ready_label: str = "agent:ready",
    pre_check_outcome: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        allowed=allowed,
        block_reason=block_reason,
        ready_label=ready_label,
        pre_check=SimpleNamespace(
            outcome=pre_check_outcome,
            ready_label=ready_label,
        ),
        decision_gate=DecisionGateResult(
            triggered=not allowed,
            reason="Need a decision",
            missing_sections=(),
            questions=(),
            recommendation="Proceed",
            tags=(),
        ),
    )


def test_apply_decision_gate_marks_failed_on_configuration_error() -> None:
    session = _Session()
    run = _run()
    tenant = SimpleNamespace(tenant_id="tenant-1")
    captured: dict[str, object] = {}

    def _mark_terminal(*, session, run_id: str, terminal_status: str, last_error: str | None = None):  # noqa: ANN001
        _ = session
        captured["run_id"] = run_id
        captured["terminal_status"] = terminal_status
        captured["last_error"] = last_error
        run.status = terminal_status
        run.last_error = last_error
        run.finished_at = datetime.now()
        return run

    with patch("orchestrator.core.worker.decision_gate.resolve_project_for_run", return_value=None):
        terminal, meta = apply_decision_gate(
            session=session,
            run=run,
            tenant=tenant,
            settings=SimpleNamespace(admin_ui_base_url="https://admin.example.test"),
            tenant_jira_oauth_context_fn=lambda **_: None,
            evaluate_worker_decision_fn=lambda **_: (_ for _ in ()).throw(RuntimeError("Tenant Jira OAuth context is incomplete")),
            send_discord_message_fn=lambda **_: None,
            send_jira_message_fn=lambda **_: None,
            ask_reply_components_fn=lambda: [],
            blocked_status="blocked",
            failed_status="failed",
            mark_run_terminal_fn=_mark_terminal,
        )

    assert terminal is run
    assert meta is None
    assert captured["run_id"] == "run-1"
    assert captured["terminal_status"] == "failed"
    assert run.status == "failed"
    assert "Execution readiness check failed" in (run.last_error or "")
    assert isinstance(run.finished_at, datetime)


def test_apply_decision_gate_marks_failed_when_blocked_decision_has_no_reason() -> None:
    session = _Session()
    run = _run()
    tenant = SimpleNamespace(tenant_id="tenant-1")
    captured: dict[str, object] = {}

    def _mark_terminal(*, session, run_id: str, terminal_status: str, last_error: str | None = None):  # noqa: ANN001
        _ = session
        captured["run_id"] = run_id
        captured["terminal_status"] = terminal_status
        captured["last_error"] = last_error
        run.status = terminal_status
        run.last_error = last_error
        run.finished_at = datetime.now()
        return run

    worker_decision = _worker_decision(allowed=False, block_reason="decision_gate_required")
    worker_decision.decision_gate = DecisionGateResult(
        triggered=True,
        reason="",
        missing_sections=(),
        questions=(),
        recommendation="Proceed",
        tags=(),
    )

    with patch("orchestrator.core.worker.decision_gate.resolve_project_for_run", return_value=None):
        terminal, meta = apply_decision_gate(
            session=session,
            run=run,
            tenant=tenant,
            settings=SimpleNamespace(admin_ui_base_url="https://admin.example.test"),
            tenant_jira_oauth_context_fn=lambda **_: None,
            evaluate_worker_decision_fn=lambda **_: worker_decision,
            send_discord_message_fn=lambda **_: None,
            send_jira_message_fn=lambda **_: None,
            ask_reply_components_fn=lambda: [],
            blocked_status="blocked",
            failed_status="failed",
            mark_run_terminal_fn=_mark_terminal,
        )

    assert terminal is run
    assert meta is None
    assert captured["terminal_status"] == "failed"
    assert "missing decision gate reason" in str(captured["last_error"] or "")


def test_apply_decision_gate_returns_none_when_issue_is_ready() -> None:
    session = _Session()
    run = _run()
    tenant = SimpleNamespace(tenant_id="tenant-1", jira_config={"ready_label": "agent:ready"})
    oauth = _oauth_context()

    with patch("orchestrator.core.worker.decision_gate.resolve_project_for_run", return_value=None):
        terminal, meta = apply_decision_gate(
            session=session,
            run=run,
            tenant=tenant,
            settings=SimpleNamespace(admin_ui_base_url="https://admin.example.test"),
            tenant_jira_oauth_context_fn=lambda **_: oauth,
            evaluate_worker_decision_fn=lambda **_: _worker_decision(allowed=True),
            send_discord_message_fn=lambda **_: None,
            send_jira_message_fn=lambda **_: None,
            ask_reply_components_fn=lambda: [],
            blocked_status="blocked",
            failed_status="failed",
        )

    assert terminal is None
    assert meta is None


def test_apply_decision_gate_marks_failed_when_worker_decision_has_no_gate_payload() -> None:
    session = _Session()
    run = _run()
    tenant = SimpleNamespace(tenant_id="tenant-1")
    captured: dict[str, object] = {}

    def _mark_terminal(*, session, run_id: str, terminal_status: str, last_error: str | None = None):  # noqa: ANN001
        _ = session
        captured["run_id"] = run_id
        captured["terminal_status"] = terminal_status
        captured["last_error"] = last_error
        run.status = terminal_status
        run.last_error = last_error
        run.finished_at = datetime.now()
        return run

    worker_decision = SimpleNamespace(
        allowed=False,
        decision_gate=None,
        configuration_error="Execution readiness check failed: run was queued with non-ready pre_check_outcome 'gtd_required'",
        block_reason="policy_eval_failed",
        classification="clear",
    )

    with patch("orchestrator.core.worker.decision_gate.resolve_project_for_run", return_value=None):
        terminal, meta = apply_decision_gate(
            session=session,
            run=run,
            tenant=tenant,
            settings=SimpleNamespace(admin_ui_base_url="https://admin.example.test"),
            tenant_jira_oauth_context_fn=lambda **_: None,
            evaluate_worker_decision_fn=lambda **_: worker_decision,
            send_discord_message_fn=lambda **_: None,
            send_jira_message_fn=lambda **_: None,
            ask_reply_components_fn=lambda: [],
            blocked_status="blocked",
            failed_status="failed",
            mark_run_terminal_fn=_mark_terminal,
        )

    assert terminal is run
    assert meta is None
    assert captured["terminal_status"] == "failed"
    assert "non-ready pre_check_outcome" in str(captured["last_error"] or "")


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
    oauth = _oauth_context()

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
            evaluate_worker_decision_fn=lambda **_: _worker_decision(
                allowed=False,
                block_reason="missing_ready_label",
                pre_check_outcome="missing_ready_label",
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


def test_apply_decision_gate_passes_issue_context_into_worker_decision() -> None:
    session = _Session()
    run = _run()
    tenant = SimpleNamespace(tenant_id="tenant-1", jira_config={"ready_label": "agent:ready"})
    captured: dict[str, object] = {}
    oauth = SimpleNamespace(
        client=SimpleNamespace(
            get_issue_detail=lambda **_: SimpleNamespace(
                summary="Live summary",
                description="Live description",
                labels=["agent:ready", "worker:linux"],
            )
        ),
        connection=SimpleNamespace(cloud_id="cloud-1"),
        access_token="access-token",
    )

    def _capture_worker_decision(**kwargs):  # noqa: ANN003
        captured.update(kwargs)
        return _worker_decision(allowed=True)

    with patch("orchestrator.core.worker.decision_gate.resolve_project_for_run", return_value=None):
        terminal, meta = apply_decision_gate(
            session=session,
            run=run,
            tenant=tenant,
            settings=SimpleNamespace(admin_ui_base_url="https://admin.example.test"),
            tenant_jira_oauth_context_fn=lambda **_: oauth,
            evaluate_worker_decision_fn=_capture_worker_decision,
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
    assert captured["issue_summary"] == "Live summary"
    assert captured["issue_description"] == "Live description"
    assert captured["issue_labels"] == ["agent:ready", "worker:linux"]


def test_apply_decision_gate_preserves_trigger_context_on_block() -> None:
    session = _Session()
    run = _run()
    run.plan = ExecutionSnapshot.empty(
        trigger_context={"source": "github_pr_review_feedback", "pr_number": 6}
    ).dump()
    tenant = SimpleNamespace(tenant_id="tenant-1", jira_config={"ready_label": "agent:ready"})
    oauth = _oauth_context()

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
            evaluate_worker_decision_fn=lambda **_: _worker_decision(
                allowed=False,
                block_reason="missing_ready_label",
                pre_check_outcome="missing_ready_label",
            ),
            send_discord_message_fn=lambda **_: SimpleNamespace(sent=True, reason="sent"),
            send_jira_message_fn=lambda **_: None,
            ask_reply_components_fn=lambda: [],
            blocked_status="blocked",
            failed_status="failed",
        )

    assert terminal is run
    assert isinstance(run.plan, dict)
    assert run.plan["context"]["trigger_context"] == {"source": "github_pr_review_feedback", "pr_number": 6}
    assert run.plan["events"]["stage_updates"][0]["stage"] == "run_not_ready"
    assert run.plan["context"]["execution_context"]["run_not_ready"]["ready_label"] == "agent:ready"
    assert run.plan["context"]["execution_context"]["run_not_ready"]["next_steps"] == [
        "Apply ready label `agent:ready` to the Jira issue, then retry the run."
    ]
    assert session.refresh_attribute_names[0] == ["plan"]


def test_apply_decision_gate_refreshes_latest_plan_before_replacing_it() -> None:
    session = _Session()
    run = _run()
    run.plan = {}
    tenant = SimpleNamespace(tenant_id="tenant-1", jira_config={"ready_label": "agent:ready"})
    oauth = _oauth_context()

    def _refresh(_run, attribute_names=None):  # noqa: ANN001
        session.refreshed = True
        session.refresh_attribute_names.append(attribute_names)
        if attribute_names == ["plan"]:
            run.plan = ExecutionSnapshot.empty(
                trigger_context={"source": "github_pr_review_feedback", "pr_number": 6}
            ).dump()

    session.refresh = _refresh

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
            evaluate_worker_decision_fn=lambda **_: _worker_decision(
                allowed=False,
                block_reason="missing_ready_label",
                pre_check_outcome="missing_ready_label",
            ),
            send_discord_message_fn=lambda **_: SimpleNamespace(sent=True, reason="sent"),
            send_jira_message_fn=lambda **_: None,
            ask_reply_components_fn=lambda: [],
            blocked_status="blocked",
            failed_status="failed",
        )

    assert terminal is run
    assert isinstance(run.plan, dict)
    assert run.plan["context"]["trigger_context"] == {"source": "github_pr_review_feedback", "pr_number": 6}
