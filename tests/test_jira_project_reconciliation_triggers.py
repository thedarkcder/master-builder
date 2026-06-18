from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import select
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
    def _first_project_id(self, tenant_id: str = "tenant-a") -> str:
        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            project = session.execute(
                select(Project).where(Project.tenant_id == tenant_id).order_by(Project.created_at.asc())
            ).scalars().first()
            self.assertIsNotNone(project)
            assert project is not None
            return project.project_id

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
        project_id = self._first_project_id()

        with patch(
            "orchestrator.api.admin.workflows.use_cases.start_jira_project_reconciliation",
            return_value=SimpleNamespace(
                execution_id="wfexec-jira-reconcile-1",
                workflow_id=f"jira_project_reconciliation:{project_id}",
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
                    "project_id": project_id,
                    "input": {"max_items": 250},
                },
                auth=("admin", "secret"),
            )

        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["execution_id"], "wfexec-jira-reconcile-1")
        self.assertEqual(response.json()["workflow_id"], f"jira_project_reconciliation:{project_id}")
        self.assertEqual(response.json()["workflow_type_key"], "jira_project_reconciliation")
        self.assertEqual(response.json()["started_attempt_id"], "attempt-jira-reconcile-1")
        start_mock.assert_called_once()
        start_kwargs = start_mock.call_args.kwargs
        self.assertEqual(start_kwargs["tenant"].tenant_id, "tenant-a")
        self.assertEqual(start_kwargs["project"].project_id, project_id)
        self.assertEqual(start_kwargs["max_items"], 250)
        self.assertEqual(start_kwargs["trigger_event"], "admin_workflow_start")

    def test_generic_workflow_start_route_starts_demo_proof(self) -> None:
        payload = self._tenant_payload()
        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)
        project_id = self._first_project_id()

        with patch(
            "orchestrator.api.admin.workflows.use_cases.start_demo_proof_workflow",
            return_value=SimpleNamespace(
                execution_id="wfexec-demo-proof-1",
                workflow_id="demo_proof:run-1-main-abcdef1",
                workflow_type_key="demo_proof",
                status="waiting_for_input",
                started_attempt_id="attempt-preview-lease-1",
            ),
        ) as start_mock:
            response = self.client.post(
                "/api/admin/workflows",
                json={
                    "workflow_type_key": "demo_proof",
                    "tenant_id": "tenant-a",
                    "project_id": project_id,
                    "input": {
                        "proof_scope_id": "run-1-main-abcdef1",
                        "commit_sha": "abcdef1",
                        "trigger_mode": "from_pr",
                        "run_id": "run-1",
                        "pr_url": "https://github.com/acme/repo/pull/8",
                        "required_capture_targets": ["browser", "ios", "android"],
                    },
                },
                auth=("admin", "secret"),
            )

        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["execution_id"], "wfexec-demo-proof-1")
        self.assertEqual(response.json()["workflow_id"], "demo_proof:run-1-main-abcdef1")
        self.assertEqual(response.json()["workflow_type_key"], "demo_proof")
        self.assertEqual(response.json()["status"], "waiting_for_input")
        self.assertEqual(response.json()["started_attempt_id"], "attempt-preview-lease-1")
        start_mock.assert_called_once()
        start_kwargs = start_mock.call_args.kwargs
        self.assertEqual(start_kwargs["tenant"].tenant_id, "tenant-a")
        self.assertEqual(start_kwargs["project"].project_id, project_id)
        self.assertEqual(start_kwargs["proof_scope_id"], "run-1-main-abcdef1")
        self.assertEqual(start_kwargs["commit_sha"], "abcdef1")
        self.assertEqual(start_kwargs["trigger_mode"], "from_pr")
        self.assertEqual(start_kwargs["run_id"], "run-1")
        self.assertEqual(start_kwargs["pr_url"], "https://github.com/acme/repo/pull/8")
        self.assertEqual(start_kwargs["required_capture_targets"], ["browser", "ios", "android"])
        self.assertEqual(start_kwargs["trigger_event"], "admin_workflow_start")

    def test_generic_workflow_start_route_starts_from_release_demo_proof_with_release_id(self) -> None:
        payload = self._tenant_payload()
        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)
        project_id = self._first_project_id()

        with patch(
            "orchestrator.api.admin.workflows.use_cases.start_demo_proof_workflow",
            return_value=SimpleNamespace(
                execution_id="wfexec-demo-proof-release-1",
                workflow_id="demo_proof:release-proof-1",
                workflow_type_key="demo_proof",
                status="waiting_for_input",
                started_attempt_id="attempt-preview-lease-1",
            ),
        ) as start_mock:
            response = self.client.post(
                "/api/admin/workflows",
                json={
                    "workflow_type_key": "demo_proof",
                    "tenant_id": "tenant-a",
                    "project_id": project_id,
                    "input": {
                        "proof_scope_id": "release-proof-1",
                        "commit_sha": "abcdef1",
                        "trigger_mode": "from_release",
                        "release_id": "release-preview-1",
                        "pr_url": "https://github.com/acme/repo/pull/8",
                        "required_capture_targets": ["browser", "ios", "android"],
                    },
                },
                auth=("admin", "secret"),
            )

        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["workflow_id"], "demo_proof:release-proof-1")
        start_mock.assert_called_once()
        start_kwargs = start_mock.call_args.kwargs
        self.assertEqual(start_kwargs["trigger_mode"], "from_release")
        self.assertEqual(start_kwargs["release_id"], "release-preview-1")
        self.assertEqual(start_kwargs["trigger_event"], "admin_workflow_start")

    def test_generic_workflow_start_route_rejects_demo_proof_without_pr_url(self) -> None:
        payload = self._tenant_payload()
        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)
        project_id = self._first_project_id()

        with patch("orchestrator.core.qa.demo_proof_start.build_workflow_runtime") as runtime_mock:
            response = self.client.post(
                "/api/admin/workflows",
                json={
                    "workflow_type_key": "demo_proof",
                    "tenant_id": "tenant-a",
                    "project_id": project_id,
                    "input": {
                        "proof_scope_id": "run-1-main-abcdef1",
                        "commit_sha": "abcdef1",
                        "trigger_mode": "from_pr",
                        "run_id": "run-1",
                        "required_capture_targets": ["browser", "ios", "android"],
                    },
                },
                auth=("admin", "secret"),
            )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("pr_url", response.json()["detail"])
        runtime_mock.assert_not_called()

    def test_generic_workflow_start_route_starts_cleanup_only_demo_proof_without_pr_url(self) -> None:
        payload = self._tenant_payload()
        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)
        project_id = self._first_project_id()

        with patch(
            "orchestrator.api.admin.workflows.use_cases.start_demo_proof_workflow",
            return_value=SimpleNamespace(
                execution_id="wfexec-demo-proof-cleanup-1",
                workflow_id="demo_proof:run-1-main-abcdef1",
                workflow_type_key="demo_proof",
                status="waiting_for_input",
                started_attempt_id="attempt-cleanup-1",
            ),
        ) as start_mock:
            response = self.client.post(
                "/api/admin/workflows",
                json={
                    "workflow_type_key": "demo_proof",
                    "tenant_id": "tenant-a",
                    "project_id": project_id,
                    "input": {
                        "proof_scope_id": "run-1-main-abcdef1",
                        "commit_sha": "abcdef1",
                        "trigger_mode": "cleanup_only",
                        "required_capture_targets": ["browser", "ios", "android"],
                    },
                },
                auth=("admin", "secret"),
            )

        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["workflow_id"], "demo_proof:run-1-main-abcdef1")
        self.assertEqual(response.json()["status"], "waiting_for_input")
        start_mock.assert_called_once()
        start_kwargs = start_mock.call_args.kwargs
        self.assertEqual(start_kwargs["trigger_mode"], "cleanup_only")
        self.assertIsNone(start_kwargs["pr_url"])
        self.assertEqual(start_kwargs["trigger_event"], "admin_workflow_start")

    def test_generic_workflow_start_route_rejects_demo_proof_without_trigger_mode(self) -> None:
        payload = self._tenant_payload()
        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)
        project_id = self._first_project_id()

        with patch("orchestrator.core.qa.demo_proof_start.build_workflow_runtime") as runtime_mock:
            response = self.client.post(
                "/api/admin/workflows",
                json={
                    "workflow_type_key": "demo_proof",
                    "tenant_id": "tenant-a",
                    "project_id": project_id,
                    "input": {
                        "proof_scope_id": "run-1-main-abcdef1",
                        "commit_sha": "abcdef1",
                        "run_id": "run-1",
                        "pr_url": "https://github.com/acme/repo/pull/8",
                        "required_capture_targets": ["browser", "ios", "android"],
                    },
                },
                auth=("admin", "secret"),
            )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("trigger_mode", response.json()["detail"])
        runtime_mock.assert_not_called()

    def test_generic_workflow_start_route_returns_reauth_required_when_jira_token_is_invalid(self) -> None:
        payload = self._tenant_payload()
        self._insert_jira_connection(connection_id="conn-1")
        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)
        project_id = self._first_project_id()

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
                    "project_id": project_id,
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
                tenant_id="route25",
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
                project_id="route25-default",
                tenant_id="route25",
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
            persisted_tenant = session.get(Tenant, "route25")
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
        self.assertEqual(start_kwargs["tenant"].tenant_id, "route25")
        self.assertEqual(start_kwargs["project"].project_id, "route25-default")
        self.assertEqual(start_kwargs["trigger_event"], "tenant_setup_reconciliation")

    def test_scheduled_reconciliation_uses_shared_workflow_start_path(self) -> None:
        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            now = _now()
            tenant = Tenant(
                tenant_id="route25",
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
                project_id="route25-default",
                tenant_id="route25",
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
                    workflow_id="jira_project_reconciliation:route25-default",
                    execution_id="wfexec-scheduled-jira-reconcile-1",
                ),
            )

        self.assertEqual(len(started), 1)
        self.assertEqual(started[0]["tenant"].tenant_id, "route25")
        self.assertEqual(started[0]["project"].project_id, "route25-default")
        self.assertEqual(started[0]["trigger_event"], "scheduled_reconciliation")
