from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from orchestrator.api.main import create_app
from orchestrator.core.agent_observability import reset_agent_observability_for_tests
from orchestrator.core.config import get_settings
from orchestrator.core.secrets import encrypt_value
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.models import JiraOAuthConnection, Run
from tests.test_support.db_harness import SqliteTemplateApiTestCase
from tests.workflow_test_support import add_human_input_request, add_run_with_workflow, add_workflow_attempt, make_run


class AdminApiTestHarness(SqliteTemplateApiTestCase):
    _secrets_encryption_key: str
    _provision_jira_webhook_patcher: object
    client: TestClient
    database_url: str

    @classmethod
    def setUpClass(cls) -> None:
        cls._secrets_encryption_key = Fernet.generate_key().decode("utf-8")
        super().setUpClass()

    @classmethod
    def class_environment_overrides(cls) -> dict[str, str]:
        return {
            "ORCHESTRATOR_ADMIN_USERNAME": "admin",
            "ORCHESTRATOR_ADMIN_PASSWORD": "secret",
            "ORCHESTRATOR_ADMIN_TOKEN_SECRET": "admin-token-secret-for-tests-0123456789",
            "ORCHESTRATOR_GITHUB_INSTALL_STATE_SECRET": "unit-test-secret",
            "ORCHESTRATOR_ADMIN_UI_BASE_URL": "http://localhost:4100",
            "ORCHESTRATOR_PUBLIC_API_BASE_URL": "http://localhost:4000",
            "ORCHESTRATOR_JIRA_OAUTH_STATE_SECRET": "jira-oauth-state-secret",
            "ORCHESTRATOR_GITHUB_APP_SLUG": "master-builder-app",
            "ORCHESTRATOR_SECRETS_ENCRYPTION_KEY": cls._secrets_encryption_key,
            "ORCHESTRATOR_CODEX_MODEL": "gpt-5.4",
            "ORCHESTRATOR_CODEX_SUPPORTED_MODELS": "gpt-5.4,gpt-5.3-codex,gpt-5.3-codex-spark",
        }

    @classmethod
    def bootstrap_template_state(cls) -> None:
        seed_slug_secret_response = cls._class_client.put(
            "/api/admin/secrets/platform%2FGITHUB_APP_SLUG",
            json={"value": "master-builder-app"},
            auth=("admin", "secret"),
        )
        if seed_slug_secret_response.status_code != 200:
            raise RuntimeError(
                f"Failed to seed GITHUB_APP_SLUG secret for tests: {seed_slug_secret_response.text}"
            )

        cls._class_client.put(
            "/api/admin/secrets/platform%2FGITHUB_APP_ID",
            json={"value": "12345"},
            auth=("admin", "secret"),
        )
        cls._class_client.put(
            "/api/admin/secrets/platform%2FGITHUB_APP_PRIVATE_KEY",
            json={"value": "not-a-real-key-for-tests"},
            auth=("admin", "secret"),
        )
        cls._class_client.put(
            "/api/admin/secrets/platform%2FJIRA_OAUTH_CLIENT_ID",
            json={"value": "jira-client-id"},
            auth=("admin", "secret"),
        )
        cls._class_client.put(
            "/api/admin/secrets/platform%2FJIRA_OAUTH_CLIENT_SECRET",
            json={"value": "jira-client-secret"},
            auth=("admin", "secret"),
        )

    def setUp(self) -> None:
        self.database_url = self._start_test_database(name_prefix="admin-api")

        get_settings.cache_clear()
        reset_db_engine_cache()
        reset_agent_observability_for_tests()

        def _stub_provision_jira_webhook(**kwargs: object) -> SimpleNamespace:
            _ = kwargs
            return SimpleNamespace(
                ok=True,
                action="provision",
                details="Provisioned 0 Jira webhook(s).",
                webhook_ids=[],
            )

        self._provision_jira_webhook_patcher = patch(
            "orchestrator.api.routes.admin_tenants.provision_jira_webhook",
            side_effect=_stub_provision_jira_webhook,
        )
        self._provision_jira_webhook_patcher.start()
        self.client = TestClient(create_app(), raise_server_exceptions=False)

    def tearDown(self) -> None:
        self.client.close()
        self._provision_jira_webhook_patcher.stop()
        self._cleanup_test_database()
        os.environ.pop("ORCHESTRATOR_WORKER_CAPABILITIES", None)

        get_settings.cache_clear()
        reset_db_engine_cache()
        reset_agent_observability_for_tests()

    def _tenant_payload(self) -> dict:
        return {
            "name": "Tenant A",
            "is_enabled": True,
            "jira": {
                "connection_id": "conn-1",
                "project_keys": ["TP"],
                "ready_statuses": ["Ready for Agent"],
                "ready_jql": 'project = TP AND status = "Ready for Agent"',
                "ready_label": "agent:ready",
                "in_progress_label": "agent:in-progress",
                "blocked_label": "agent:blocked",
                "done_label": "agent:done",
                "webhook_secret_ref": "secret/webhook",
            },
            "github": {
                "mode": "github_app",
                "webhook_secret_ref": "secret/github-webhook",
                "installation_id": "12345",
            },
            "repos": {
                "allowlist": ["https://github.com/example/repo"],
                "mapping_rules_by_project_key": {"TP": "https://github.com/example/repo"},
                "mapping_rules_by_component": {},
                "fallback_repo": None,
            },
            "policy": {
                "allow_jira_transitions": False,
                "allow_pr_creation": True,
                "allow_code_reviews": True,
                "allow_pr_remediation": True,
                "allow_manual_pr_fix_requests": True,
                "allow_label_mutations": True,
                "max_runtime_minutes": 30,
                "max_dev_test_review_loops": 2,
                "max_concurrent_runs": 2,
                "allowed_commands": ["python -m unittest"],
                "require_agents_md": False,
                "codex_model": "gpt-5.4",
                "codex_reasoning_effort": "medium",
            },
            "discord": {
                "channel_id": "discord-channel-1",
                "notify_events": ["run_started"],
            },
        }

    def _insert_jira_connection(self, connection_id: str = "conn-1") -> None:
        session_factory = create_session_factory(self.database_url)
        settings = get_settings()
        now = datetime.now(timezone.utc)
        with session_factory() as session:
            session.add(
                JiraOAuthConnection(
                    connection_id=connection_id,
                    account_id="account-1",
                    account_email="test@example.com",
                    cloud_id="cloud-1",
                    site_url="https://example.atlassian.net",
                    scopes=["read:jira-work", "write:jira-work"],
                    access_token_encrypted=encrypt_value(
                        plaintext="access-token",
                        encryption_key=settings.secrets_encryption_key,
                    ),
                    refresh_token_encrypted=encrypt_value(
                        plaintext="refresh-token",
                        encryption_key=settings.secrets_encryption_key,
                    ),
                    access_token_expires_at=now + timedelta(hours=1),
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

    def _seed_workflow_attempt(
        self,
        *,
        workflow_id: str,
        run_id: str,
        tenant_id: str = "tenant-a",
        project_id: str = "tenant-a-default",
        issue_key: str = "TP-1",
        issue_summary: str = "workflow attempt",
        issue_description: str | None = "desc",
        workflow_status: str = "queued",
        run_status: str = "queued",
        checkpoint_id: str | None = None,
        checkpoint_kind: str | None = None,
        checkpoint_stage: str | None = None,
        pending_request_id: str | None = None,
    ) -> None:
        session_factory = create_session_factory(self.database_url)
        now = datetime.now(timezone.utc)
        with session_factory() as session:
            _workflow, run_row, checkpoint_row = add_workflow_attempt(
                session,
                workflow_id=workflow_id,
                run_id=run_id,
                tenant_id=tenant_id,
                project_id=project_id,
                issue_key=issue_key,
                issue_summary=issue_summary,
                issue_description=issue_description,
                repo_url="https://github.com/example/repo",
                branch="feature/test",
                workflow_status=workflow_status,
                run_status=run_status,
                entry_checkpoint_id=checkpoint_id,
                checkpoint_kind=checkpoint_kind,
                checkpoint_stage=checkpoint_stage or ("pm" if checkpoint_kind == "pm" else "test"),
                checkpoint_payload={"checkpoint": checkpoint_kind, "run_id": run_id},
                checkpoint_session_id="checkpoint-session" if checkpoint_kind == "pm" else None,
                blocked_reason="human_input_expired" if workflow_status == "blocked" else None,
                last_error=None if workflow_status != "failed" and run_status not in {"failed", "blocked"} else "run failed",
                plan=(
                    ExecutionSnapshot.empty(trigger_context={"source": "test"}).dump()
                    if checkpoint_id
                    else None
                ),
                now=now,
            )
            if checkpoint_id:
                run_snapshot = ExecutionSnapshot.require(run_row.plan, allow_empty=True)
                run_snapshot.context.execution_context["pre_check_outcome"] = "ready_for_agent"
                run_row.plan = run_snapshot.dump()
                assert checkpoint_row is not None
                checkpoint_snapshot = ExecutionSnapshot.empty(trigger_context={"source": "test"})
                checkpoint_snapshot.context.execution_context["pre_check_outcome"] = "ready_for_agent"
                checkpoint_row.payload_json = checkpoint_snapshot.dump()
            if pending_request_id:
                request_status = "pending" if workflow_status == "waiting_for_input" else "answered"
                add_human_input_request(
                    session,
                    request_id=pending_request_id,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    workflow_id=workflow_id,
                    checkpoint_id=checkpoint_id or "checkpoint-missing",
                    source_run_id=run_id,
                    issue_key=issue_key,
                    source_stage="pm",
                    request_type="human_reply",
                    status=request_status,
                    now=now,
                )
            session.commit()

    def _persist_run(self, session, *, workflow_status: str | None = None, **run_kwargs) -> Run:
        run = make_run(**run_kwargs)
        add_run_with_workflow(session, run, workflow_status=workflow_status)
        return run
