from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import HTTPException

from orchestrator.api.admin import jira_route_service, jira_webhook_route_service
from orchestrator.api.admin.runs import service as runs_service
from orchestrator.api.routes import runs as runs_route
from orchestrator.core.projects import routing as project_routing


class JiraRouteServiceTests(unittest.TestCase):
    def test_list_jira_projects_for_connection_404(self) -> None:
        session = MagicMock()
        session.get.return_value = None

        with self.assertRaises(HTTPException) as exc_ctx:
            jira_route_service.list_jira_projects_for_connection(
                session=session,
                connection_id="conn-1",
                atlassian_oauth_connection_model=object,
                settings=SimpleNamespace(),
                refresh_atlassian_connection_tokens_fn=MagicMock(),
                atlassian_oauth_client_fn=MagicMock(),
            )

        self.assertEqual(exc_ctx.exception.status_code, 404)

    def test_list_jira_projects_for_connection_success(self) -> None:
        connection = SimpleNamespace(connection_id="conn-1", cloud_id="cloud-1")
        session = MagicMock()
        session.get.return_value = connection

        refresh_fn = MagicMock(return_value="token-1")
        client = MagicMock()
        client.list_projects.return_value = [
            SimpleNamespace(key="MAB", name="Master Builder"),
            SimpleNamespace(key="example", name="example"),
        ]
        client_factory = MagicMock(return_value=client)

        projects = jira_route_service.list_jira_projects_for_connection(
            session=session,
            connection_id="conn-1",
            atlassian_oauth_connection_model=object,
            settings=SimpleNamespace(),
            refresh_atlassian_connection_tokens_fn=refresh_fn,
            atlassian_oauth_client_fn=client_factory,
        )

        self.assertEqual([p.key for p in projects], ["MAB", "example"])
        self.assertEqual([p.name for p in projects], ["Master Builder", "example"])
        refresh_fn.assert_called_once()
        client.list_projects.assert_called_once_with(access_token="token-1", cloud_id="cloud-1")

    def test_get_jira_webhook_diagnostics(self) -> None:
        session = MagicMock()
        session.get.return_value = SimpleNamespace(jira_config={"managed_webhook_ids": [1, 2]})
        build_fn = MagicMock(return_value={"ok": True})

        payload = jira_webhook_route_service.get_jira_webhook_diagnostics(
            session=session,
            tenant_id="example",
            within_minutes=30,
            tenant_model=object,
            settings=SimpleNamespace(),
            build_jira_webhook_diagnostics_fn=build_fn,
            jira_webhook_callback_url_fn=MagicMock(),
            parse_managed_webhook_ids_fn=MagicMock(),
        )

        self.assertEqual(payload, {"ok": True})
        build_fn.assert_called_once()

    def test_get_jira_webhook_diagnostics_404(self) -> None:
        session = MagicMock()
        session.get.return_value = None

        with self.assertRaises(HTTPException) as exc_ctx:
                jira_webhook_route_service.get_jira_webhook_diagnostics(
                session=session,
                tenant_id="missing",
                within_minutes=30,
                tenant_model=object,
                settings=SimpleNamespace(),
                build_jira_webhook_diagnostics_fn=MagicMock(),
                jira_webhook_callback_url_fn=MagicMock(),
                parse_managed_webhook_ids_fn=MagicMock(),
            )

        self.assertEqual(exc_ctx.exception.status_code, 404)

    def test_run_tenant_jira_webhook_action(self) -> None:
        session = MagicMock()
        tenant = SimpleNamespace(tenant_id="example")
        session.get.return_value = tenant

        result = SimpleNamespace(ok=True, model_dump=lambda: {"ok": True, "action": "provision"})
        response = jira_webhook_route_service.run_tenant_jira_webhook_action(
            session=session,
            tenant_id="example",
            tenant_model=object,
            settings=SimpleNamespace(),
            provision_jira_webhook_fn=MagicMock(return_value=result),
            replace_existing=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body), {"ok": True, "action": "provision"})

    def test_run_tenant_jira_webhook_action_fails_when_provider_does_not_confirm(self) -> None:
        session = MagicMock()
        session.get.return_value = SimpleNamespace(tenant_id="example")
        result = SimpleNamespace(
            ok=False,
            details="Jira webhook creation failed",
            model_dump=lambda: {"ok": False, "details": "Jira webhook creation failed"},
        )

        with self.assertRaises(HTTPException) as exc_ctx:
            jira_webhook_route_service.run_tenant_jira_webhook_action(
                session=session,
                tenant_id="example",
                tenant_model=object,
                settings=SimpleNamespace(),
                provision_jira_webhook_fn=MagicMock(return_value=result),
                replace_existing=True,
            )

        self.assertEqual(exc_ctx.exception.status_code, 502)
        self.assertEqual(exc_ctx.exception.detail, "Jira webhook creation failed")


class RunsServiceTests(unittest.TestCase):
    def test_list_runs(self) -> None:
        session = MagicMock()
        run_one = SimpleNamespace(run_id="r1")
        run_two = SimpleNamespace(run_id="r2")
        scalar_result = MagicMock()
        scalar_result.all.return_value = [run_one, run_two]
        execute_result = MagicMock()
        execute_result.scalars.return_value = scalar_result
        session.execute.return_value = execute_result

        build_query = MagicMock(return_value="query")

        def to_schema(run: SimpleNamespace) -> str:
            return f"schema-{run.run_id}"

        rows = runs_service.list_runs(
            session=session,
            tenant_id="example",
            project_id="example-default",
            status_filter="queued",
            issue_query="GP-113",
            pr_state="none",
            from_time=None,
            to_time=None,
            limit=50,
            offset=0,
            build_runs_query_fn=build_query,
            run_to_schema_fn=to_schema,
            tenant_model=object,
            tenant_jira_issue_url_fn=MagicMock(return_value=None),
        )

        self.assertEqual(rows, ["schema-r1", "schema-r2"])
        build_query.assert_called_once_with(
            tenant_id="example",
            project_id="example-default",
            status_filter="queued",
            issue_query="GP-113",
            pr_state="none",
            from_time=None,
            to_time=None,
            limit=50,
            offset=0,
        )

    def test_get_run_404(self) -> None:
        session = MagicMock()
        session.get.return_value = None

        with self.assertRaises(HTTPException) as exc_ctx:
            runs_service.get_run(
                session=session,
                run_id="missing",
                run_model=object,
                run_to_schema_fn=MagicMock(),
                tenant_model=object,
                tenant_jira_issue_url_fn=MagicMock(return_value=None),
            )

        self.assertEqual(exc_ctx.exception.status_code, 404)

    def test_get_run_success(self) -> None:
        session = MagicMock()
        run = SimpleNamespace(run_id="r1", tenant_id="example", issue_key="R1")
        tenant = SimpleNamespace(tenant_id="example")
        session.get.side_effect = [run, tenant]

        to_schema = MagicMock(return_value={"run_id": "r1", "issue_url": None})
        issue_url_fn = MagicMock(return_value="https://jira.example/browse/R1")
        payload = runs_service.get_run(
            session=session,
            run_id="r1",
            run_model=object,
            run_to_schema_fn=to_schema,
            tenant_model=object,
            tenant_jira_issue_url_fn=issue_url_fn,
        )

        self.assertEqual(payload, {"run_id": "r1", "issue_url": "https://jira.example/browse/R1"})
        to_schema.assert_called_once_with(run)


class ProjectRoutingTests(unittest.TestCase):
    def test_jira_project_key_from_issue_key(self) -> None:
        self.assertEqual(project_routing.jira_project_key_from_issue_key("mab-123"), "MAB")
        self.assertEqual(project_routing.jira_project_key_from_issue_key(" MAB-1 "), "MAB")
        self.assertIsNone(project_routing.jira_project_key_from_issue_key("invalid"))
        self.assertIsNone(project_routing.jira_project_key_from_issue_key(""))

    def test_find_active_project_for_issue_key(self) -> None:
        session = MagicMock()
        session.execute.return_value.scalar_one_or_none.return_value = "project"

        project = project_routing.find_active_project_for_issue_key(
            session,
            tenant_id="example",
            issue_key="MAB-22",
        )
        self.assertEqual(project, "project")
        session.execute.assert_called_once()

        session.reset_mock()
        none_project = project_routing.find_active_project_for_issue_key(
            session,
            tenant_id="example",
            issue_key="invalid",
        )
        self.assertIsNone(none_project)
        session.execute.assert_not_called()

    def test_find_active_project_for_repo_full_name(self) -> None:
        session = MagicMock()
        session.execute.return_value.scalars.return_value = [
            SimpleNamespace(github_repository="https://github.com/org/one"),
            SimpleNamespace(github_repository="https://github.com/org/two"),
        ]

        match = project_routing.find_active_project_for_repo_full_name(
            session,
            tenant_id="example",
            repo_full_name="org/two",
        )
        self.assertIsNotNone(match)
        self.assertEqual(match.github_repository, "https://github.com/org/two")

        no_match = project_routing.find_active_project_for_repo_full_name(
            session,
            tenant_id="example",
            repo_full_name="org/missing",
        )
        self.assertIsNone(no_match)


class RunsRouteTests(unittest.TestCase):
    def test_run_to_schema_maps_project_id(self) -> None:
        now = datetime.now(timezone.utc)
        run = SimpleNamespace(
            run_id="r1",
            workflow_id="workflow-r1",
            attempt_number=1,
            parent_run_id=None,
            entry_mode="fresh",
            entry_stage="orchestrated",
            entry_checkpoint_id=None,
            tenant_id="example",
            project_id="example-default",
            issue_key="MAB-1",
            repo_url="https://github.com/org/repo",
            branch="feature/x",
            pr_url="https://github.com/org/repo/pull/1",
            status="queued",
            last_error=None,
            plan={"steps": []},
            created_at=now,
            started_at=None,
            finished_at=None,
        )

        payload = runs_route._run_to_schema(run)
        self.assertEqual(payload.project_id, "example-default")
        self.assertEqual(payload.issue_key, "MAB-1")

    def test_run_to_schema_clears_last_error_when_succeeded(self) -> None:
        now = datetime.now(timezone.utc)
        run = SimpleNamespace(
            run_id="r2",
            workflow_id="workflow-r2",
            attempt_number=1,
            parent_run_id=None,
            entry_mode="fresh",
            entry_stage="orchestrated",
            entry_checkpoint_id=None,
            tenant_id="example",
            project_id="example-default",
            issue_key="MAB-2",
            repo_url=None,
            branch=None,
            pr_url="https://github.com/org/repo/pull/2",
            status="succeeded",
            last_error="Workflow succeeded but no PR URL was produced",
            plan={},
            created_at=now,
            started_at=now,
            finished_at=now,
        )

        payload = runs_route._run_to_schema(run)
        self.assertIsNone(payload.last_error)

    def test_get_run_404(self) -> None:
        session = MagicMock()
        session.get.return_value = None

        with self.assertRaises(HTTPException) as exc_ctx:
            runs_route.get_run(run_id="missing", session=session, _="admin")

        self.assertEqual(exc_ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
