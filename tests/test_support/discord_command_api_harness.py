from __future__ import annotations

from datetime import datetime, timezone
from cryptography.fernet import Fernet
from unittest.mock import patch

from orchestrator.core.config import get_settings
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.models import Project, Run, WorkflowExecution
from tests.test_support.db_harness import SqliteTemplateApiTestCase


class DiscordCommandApiTestHarness(SqliteTemplateApiTestCase):
    _template_tenant_id: str
    _template_default_project_id: str
    _secrets_encryption_key: str

    @classmethod
    def setUpClass(cls) -> None:
        cls._secrets_encryption_key = Fernet.generate_key().decode("utf-8")
        cls._project_checkout_patcher = patch(
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
        self.database_url = self._start_test_database(name_prefix="discord-commands")
        self.session_factory = create_session_factory(database_url=self.database_url)
        self.tenant_id = self._template_tenant_id
        self.default_project_id = self._template_default_project_id

    def tearDown(self) -> None:
        self._cleanup_test_database()
        get_settings.cache_clear()
        reset_db_engine_cache()

    def _set_project_allowed_users(self, *, project_id: str, user_ids: list[str]) -> None:
        self._set_project_allowed_users_for_database(
            database_url=self.database_url,
            project_id=project_id,
            user_ids=user_ids,
        )

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

    def _create_project(self, *, project_id: str, jira_project_key: str, channel_id: str) -> None:
        with self.session_factory() as session:
            now = datetime.now(timezone.utc)
            session.add(
                Project(
                    project_id=project_id,
                    tenant_id=self.tenant_id,
                    name=project_id,
                    github_repository=f"https://github.com/example/{project_id}",
                    jira_project_key=jira_project_key,
                    policy_overrides={},
                    environment={},
                    secret_refs={},
                    discord_config={"channel_id": channel_id, "notify_events": []},
                    is_archived=False,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

    def _queue_run(self, *, run_id: str, issue_key: str, status: str, project_id: str | None = None) -> None:
        with self.session_factory() as session:
            now = datetime.now(timezone.utc)
            workflow_id = f"workflow-{run_id}"
            resolved_project_id = project_id or self.default_project_id
            session.add(
                WorkflowExecution(
                    workflow_id=workflow_id,
                    tenant_id=self.tenant_id,
                    project_id=resolved_project_id,
                    issue_key=issue_key,
                    issue_summary=f"Issue {issue_key}",
                    issue_description="desc",
                    repo_url="https://github.com/example/repo",
                    branch=None,
                    pr_url=None,
                    dedupe_scope="issue_execution",
                    status=status,
                    last_error=None,
                    active_run_id=run_id,
                    latest_checkpoint_id=None,
                    source_workflow_id=None,
                    source_run_id=None,
                    blocked_reason=None,
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
                    project_id=resolved_project_id,
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
