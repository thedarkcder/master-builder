import unittest
from datetime import datetime, timezone

from cryptography.fernet import Fernet

from orchestrator.core.config import get_settings
from orchestrator.core.decision.gate import DecisionGateResult
from orchestrator.core.decision.state_machine import resolve_execution_gate_state
from orchestrator.core.decision.types import DecisionClassification, IngressDecision
from orchestrator.core.decision.engine import DecisionEngineResult
from orchestrator.core.gtd import GoodToDoValidationResult
from orchestrator.core.precheck.pre_run_check import PreRunCheckResult
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.models import Project, Run, WorkflowExecution
from tests.test_support.db_harness import SqliteTemplateApiTestCase


class DiscordCommandReplyHarness(SqliteTemplateApiTestCase):
    _template_tenant_id: str
    _template_default_project_id: str
    _secrets_encryption_key: str

    @classmethod
    def setUpClass(cls) -> None:
        cls._secrets_encryption_key = Fernet.generate_key().decode("utf-8")
        cls._project_checkout_patcher = unittest.mock.patch(
            "orchestrator.api.admin.route_helpers.ensure_project_repository_checkout",
            return_value=None,
        )
        cls._project_checkout_patcher.start()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            super().tearDownClass()
        finally:
            cls._project_checkout_patcher.stop()

    @classmethod
    def class_environment_overrides(cls) -> dict[str, str]:
        return {
            "ORCHESTRATOR_ADMIN_USERNAME": "admin",
            "ORCHESTRATOR_ADMIN_PASSWORD": "secret",
            "ORCHESTRATOR_SECRETS_ENCRYPTION_KEY": cls._secrets_encryption_key,
        }

    @classmethod
    def bootstrap_template_state(cls) -> None:
        create_response = cls._class_client.post(
            "/api/admin/tenants",
            json={
                "name": "Discord Tenant",
                "is_enabled": True,
                "jira": {
                    "connection_id": None,
                    "project_keys": ["TP"],
                    "ready_statuses": ["To Do"],
                    "ready_jql": 'project = TP AND status = "To Do"',
                    "ready_label": "agent:ready",
                    "in_progress_label": "agent:in-progress",
                    "blocked_label": "agent:blocked",
                    "done_label": None,
                    "webhook_secret_ref": None,
                },
                "github": {
                    "mode": "github_app",
                    "webhook_secret_ref": None,
                    "installation_id": "12345",
                },
                "repos": {"github_repository": "https://github.com/example/repo"},
                "policy": {
                    "allow_jira_transitions": False,
                    "allow_pr_creation": True,
                    "allow_label_mutations": True,
                    "max_runtime_minutes": 30,
                    "max_dev_test_review_loops": 2,
                    "max_concurrent_runs": 2,
                    "allowed_commands": [],
                    "require_agents_md": False,
                },
                "discord": {
                    "channel_id": "discord-channel-1",
                    "notify_events": ["run_started"],
                    "command_secret_ref": None,
                },
            },
            auth=("admin", "secret"),
        )
        assert create_response.status_code == 201, create_response.text
        cls._template_tenant_id = create_response.json()["tenant_id"]
        cls._template_default_project_id = f"{cls._template_tenant_id}-default"
        cls._set_project_allowed_users_for_database(
            database_url=cls._template_database_url,
            project_id=cls._template_default_project_id,
            user_ids=["u-admin"],
        )

    def setUp(self) -> None:
        self.database_url = self._start_test_database(name_prefix="discord-reply")
        self.session_factory = create_session_factory(database_url=self.database_url)
        self.tenant_id = self._template_tenant_id
        self.default_project_id = self._template_default_project_id

    def tearDown(self) -> None:
        self._cleanup_test_database()
        get_settings.cache_clear()
        reset_db_engine_cache()

    @staticmethod
    def _set_project_allowed_users_for_database(*, database_url: str, project_id: str, user_ids: list[str]) -> None:
        session_factory = create_session_factory(database_url=database_url)
        with session_factory() as session:
            project = session.get(Project, project_id)
            assert project is not None
            discord_config = dict(project.discord_config or {})
            discord_config["allowed_user_ids"] = [str(value).strip() for value in user_ids if str(value).strip()]
            project.discord_config = discord_config
            session.commit()

    def _queue_run(self, *, run_id: str, issue_key: str, status: str, project_id: str | None = None) -> None:
        with self.session_factory() as session:
            now = datetime.now(timezone.utc)
            workflow_id = f"workflow-{run_id}"
            project_id = project_id or self.default_project_id
            session.add(
                WorkflowExecution(
                    workflow_id=workflow_id,
                    workflow_type_key="issue_execution",
                    tenant_id=self.tenant_id,
                    project_id=project_id,
                    source_system="jira",
                    source_ref=issue_key,
                    display_name=f"Issue {issue_key}",
                    source_description="desc",
                    repo_url="https://github.com/example/repo",
                    branch=None,
                    pr_url=None,
                    orchestration_backend="legacy",
                    dedupe_scope="issue_execution",
                    status=status,
                    last_error=None,
                    active_run_id=run_id,
                    latest_checkpoint_id=None,
                    source_workflow_id=None,
                    source_run_id=None,
                    created_at=now,
                    started_at=now if status == "running" else None,
                    finished_at=None if status in {"queued", "running"} else now,
                    updated_at=now,
                )
            )
            session.add(
                Run(
                    run_id=run_id,
                    workflow_id=workflow_id,
                    tenant_id=self.tenant_id,
                    project_id=project_id,
                    issue_key=issue_key,
                    issue_summary=f"Issue {issue_key}",
                    issue_description="desc",
                    repo_url="https://github.com/example/repo",
                    branch=None,
                    pr_url=None,
                    attempt_number=1,
                    parent_run_id=None,
                    entry_mode="fresh",
                    entry_stage="orchestrated",
                    entry_checkpoint_id=None,
                    dedupe_scope="issue_execution",
                    status=status,
                    last_error=None,
                    plan=None,
                    created_at=now,
                    started_at=now if status == "running" else None,
                    last_heartbeat_at=None,
                    worker_service_instance_id=None,
                    finished_at=None if status in {"queued", "running"} else now,
                )
            )
            session.commit()

    def _ready_precheck_result(self) -> PreRunCheckResult:
        return PreRunCheckResult(
            outcome="ready_for_agent",
            ready_label="agent:ready",
            ready_label_present=True,
            required_worker_capability="linux",
            required_worker_label="worker:linux",
            required_worker_label_present=True,
            decision_gate=DecisionGateResult(
                triggered=False,
                reason="Decision Gate not required",
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

    def _ready_decision_result(self) -> DecisionEngineResult:
        pre_check = self._ready_precheck_result()
        decision = IngressDecision(
            source="discord_run",
            pre_check=pre_check,
            block_reason=None,
            guidance=None,
            policy_error=None,
            label_actions=(),
        )
        return DecisionEngineResult(
            decision=decision,
            issue_labels=["agent:ready"],
            classification=DecisionClassification.CLEAR,
            missing_slots=[],
            auto_resolved_slots=[],
            case_id="case-ready",
            case_state="clear",
            cycle_id=None,
            outbox_effect_ids=(),
            duplicate_event=False,
            execution_gate=resolve_execution_gate_state(
                decision=decision,
                classification=DecisionClassification.CLEAR,
            ),
        )

    def _missing_ready_precheck_result(self) -> PreRunCheckResult:
        return PreRunCheckResult(
            outcome="missing_ready_label",
            ready_label="agent:ready",
            ready_label_present=False,
            required_worker_capability="linux",
            required_worker_label="worker:linux",
            required_worker_label_present=True,
            decision_gate=DecisionGateResult(
                triggered=False,
                reason="Decision Gate not required",
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

    def _missing_ready_decision_result(self) -> DecisionEngineResult:
        pre_check = self._missing_ready_precheck_result()
        decision = IngressDecision(
            source="discord_run",
            pre_check=pre_check,
            block_reason="missing_ready_label",
            policy_error=None,
            guidance="Issue is missing the configured ready label. (agent:ready)",
            label_actions=(),
        )
        return DecisionEngineResult(
            decision=decision,
            issue_labels=[],
            classification=DecisionClassification.CLEAR,
            missing_slots=[],
            auto_resolved_slots=[],
            case_id="case-missing-ready",
            case_state="blocked",
            cycle_id=None,
            outbox_effect_ids=(),
            duplicate_event=False,
            execution_gate=resolve_execution_gate_state(
                decision=decision,
                classification=DecisionClassification.CLEAR,
            ),
        )
