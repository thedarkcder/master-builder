from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from temporalio.client import WorkflowUpdateFailedError
from temporalio.exceptions import ApplicationError

from orchestrator.api.admin.route_helpers import reconcile_tenant_projects
from orchestrator.core.jira_project_reconciliation.scheduler import run_scheduled_jira_project_reconciliation_pass
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import Project, Tenant
from tests.test_support.admin_api_harness import AdminApiTestHarness


def _now() -> datetime:
    return datetime.now(timezone.utc)


class JiraProjectReconciliationTriggerTests(AdminApiTestHarness):
    def test_manual_project_reconciliation_route_is_not_registered(self) -> None:
        paths = {getattr(route, "path", "") for route in self.client.app.routes}

        self.assertNotIn(
            "/api/admin/tenants/{tenant_id}/projects/{project_id}/jira-work/reconcile",
            paths,
        )
        self.assertNotIn(
            "/api/admin/workflow-types/{workflow_type_key}/executions",
            paths,
        )

    def test_generic_workflow_start_route_starts_project_reconciliation(self) -> None:
        payload = self._tenant_payload()
        self._insert_jira_connection(connection_id="conn-1")
        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)

        with patch(
            "orchestrator.api.admin.workflows.use_cases.start_jira_project_reconciliation",
            return_value=SimpleNamespace(
                execution_id="wfexec-jira-reconcile-1",
                workflow_id="jira_project_reconciliation:tenant-a-default",
                workflow_type_key="jira_project_reconciliation",
                status="running",
                started_attempt_id="attempt-jira-reconcile-1",
            ),
        ) as start_mock:
            response = self.client.post(
                "/api/admin/workflows",
                json={
                    "workflow_type_key": "jira_project_reconciliation",
                    "tenant_id": "tenant-a",
                    "project_id": "tenant-a-default",
                    "input": {"max_items": 250},
                },
                auth=("admin", "secret"),
            )

        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["execution_id"], "wfexec-jira-reconcile-1")
        self.assertEqual(response.json()["workflow_id"], "jira_project_reconciliation:tenant-a-default")
        self.assertEqual(response.json()["workflow_type_key"], "jira_project_reconciliation")
        self.assertEqual(response.json()["started_attempt_id"], "attempt-jira-reconcile-1")
        start_mock.assert_called_once()
        start_kwargs = start_mock.call_args.kwargs
        self.assertEqual(start_kwargs["tenant"].tenant_id, "tenant-a")
        self.assertEqual(start_kwargs["project"].project_id, "tenant-a-default")
        self.assertEqual(start_kwargs["max_items"], 250)
        self.assertEqual(start_kwargs["trigger_event"], "admin_workflow_start")

    def test_generic_workflow_start_route_returns_reauth_required_when_jira_token_is_invalid(self) -> None:
        payload = self._tenant_payload()
        self._insert_jira_connection(connection_id="conn-1")
        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)

        temporal_error = WorkflowUpdateFailedError(
            ApplicationError(
                'terminal_workflow_advance_error: Atlassian request failed (403): '
                '{"error":"unauthorized_client","error_description":"refresh_token is invalid"}'
            )
        )
        with patch(
            "orchestrator.api.admin.workflows.use_cases.start_jira_project_reconciliation",
            side_effect=temporal_error,
        ):
            response = self.client.post(
                "/api/admin/workflows",
                json={
                    "workflow_type_key": "jira_project_reconciliation",
                    "tenant_id": "tenant-a",
                    "project_id": "tenant-a-default",
                    "input": {"max_items": 250},
                },
                auth=("admin", "secret"),
            )

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"], "Atlassian connection requires reauthentication.")

        notifications_response = self.client.get(
            "/api/admin/tenants/tenant-a/notifications",
            auth=("admin", "secret"),
        )
        self.assertEqual(notifications_response.status_code, 200, notifications_response.text)
        notifications = notifications_response.json()["notifications"]
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0]["kind"], "reauth_required")
        self.assertEqual(notifications[0]["scope_type"], "jira_connection")
        self.assertEqual(notifications[0]["scope_id"], "conn-1")

    def test_tenant_setup_reconciliation_uses_shared_workflow_start_path(self) -> None:
        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            now = _now()
            tenant = Tenant(
                tenant_id="example",
                name="Route 25",
                is_enabled=True,
                archived_at=None,
                purge_after_at=None,
                jira_config={},
                github_config={},
                repos_config={},
                policy_config={},
                discord_config={},
                experience_config={},
                setup_state={},
                created_at=now,
                updated_at=now,
            )
            project = Project(
                project_id="example-default",
                tenant_id="example",
                name="Route 25",
                github_repository="org/repo",
                jira_project_key="MAB",
                policy_overrides={},
                architecture_docs_config={},
                environment={},
                secret_refs={},
                discord_config={},
                is_archived=False,
                created_at=now,
                updated_at=now,
            )
            session.add_all([tenant, project])
            session.commit()
            persisted_tenant = session.get(Tenant, "example")
            self.assertIsNotNone(persisted_tenant)

            with (
                patch("orchestrator.api.admin.route_helpers.ensure_default_project_for_tenant") as ensure_default_project_mock,
                patch("orchestrator.api.admin.route_helpers.sync_tenant_jira_project_keys") as sync_jira_keys_mock,
                patch("orchestrator.api.admin.route_helpers._sync_tenant_project_discord_channels_impl") as sync_discord_mock,
                patch("orchestrator.api.admin.route_helpers.start_jira_project_reconciliation") as start_reconciliation_mock,
            ):
                reconcile_tenant_projects(session, tenant=persisted_tenant)

        ensure_default_project_mock.assert_called_once()
        sync_jira_keys_mock.assert_called_once()
        sync_discord_mock.assert_called_once()
        start_reconciliation_mock.assert_called_once()
        start_kwargs = start_reconciliation_mock.call_args.kwargs
        self.assertEqual(start_kwargs["tenant"].tenant_id, "example")
        self.assertEqual(start_kwargs["project"].project_id, "example-default")
        self.assertEqual(start_kwargs["trigger_event"], "tenant_setup_reconciliation")

    def test_scheduled_reconciliation_uses_shared_workflow_start_path(self) -> None:
        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            now = _now()
            tenant = Tenant(
                tenant_id="example",
                name="Route 25",
                is_enabled=True,
                archived_at=None,
                purge_after_at=None,
                jira_config={},
                github_config={},
                repos_config={},
                policy_config={},
                discord_config={},
                experience_config={},
                setup_state={},
                created_at=now,
                updated_at=now,
            )
            project = Project(
                project_id="example-default",
                tenant_id="example",
                name="Route 25",
                github_repository="org/repo",
                jira_project_key="MAB",
                policy_overrides={},
                architecture_docs_config={},
                environment={},
                secret_refs={},
                discord_config={},
                is_archived=False,
                created_at=now,
                updated_at=now,
            )
            session.add_all([tenant, project])
            session.commit()

            started: list[dict[str, object]] = []
            run_scheduled_jira_project_reconciliation_pass(
                session=session,
                settings=SimpleNamespace(),
                start_reconciliation_fn=lambda **kwargs: started.append(dict(kwargs)) or SimpleNamespace(
                    workflow_id="jira_project_reconciliation:example-default",
                    execution_id="wfexec-scheduled-jira-reconcile-1",
                ),
            )

        self.assertEqual(len(started), 1)
        self.assertEqual(started[0]["tenant"].tenant_id, "example")
        self.assertEqual(started[0]["project"].project_id, "example-default")
        self.assertEqual(started[0]["trigger_event"], "scheduled_reconciliation")
