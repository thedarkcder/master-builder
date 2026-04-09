from __future__ import annotations

import os
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator.core.platform_team_catalog_service import platform_team_catalog_service
from orchestrator.core.team_run_service import execute_next_ready_team_task
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.core.workflow.runner import DevResult, PmPlan, ReviewResult, TestResult
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import Tenant
from tests.workflow_test_support import add_run_with_workflow, make_run


class _StubAgents:
    def __init__(self, *, pm: PmPlan, dev: list[DevResult], test: list[TestResult], review: list[ReviewResult]) -> None:
        self._pm = pm
        self._dev = list(dev)
        self._test = list(test)
        self._review = list(review)

    def pm(self, request, attempt, feedback, history, last_dev_result, last_test_result, last_review_result):  # noqa: ANN001
        return self._pm

    def dev(self, request, plan, attempt, feedback):  # noqa: ANN001
        return self._dev.pop(0)

    def test(self, request, plan, dev_result, attempt):  # noqa: ANN001
        return self._test.pop(0)

    def review(self, request, plan, dev_result, test_result, attempt):  # noqa: ANN001
        return self._review.pop(0)


def _seed_tenant(session, *, now: datetime) -> None:  # noqa: ANN001
    session.add(
        Tenant(
            tenant_id="tenant-a",
            name="Tenant A",
            is_enabled=True,
            jira_config={},
            github_config={},
            repos_config={},
            policy_config={},
            discord_config=None,
            created_at=now,
            updated_at=now,
        )
    )
    session.commit()


def test_issue_team_run_executes_to_success() -> None:
    temp_dir = TemporaryDirectory()
    database_url = f"sqlite:///{temp_dir.name}/issue_team_run.db"
    os.environ["ORCHESTRATOR_DATABASE_URL"] = database_url
    reset_db_engine_cache()
    run_migrations(database_url=database_url)
    session_factory = create_session_factory(database_url=database_url)
    now = datetime.now(timezone.utc)
    try:
        with session_factory() as session:
            _seed_tenant(session, now=now)
            snapshot = ExecutionSnapshot.empty()
            snapshot.context.execution_context["team_run"] = platform_team_catalog_service.build_issue_workflow_team_run_snapshot(
                session=session,
                snapshot=snapshot,
                entry_mode="fresh",
                entry_stage=None,
            )
            run = make_run(
                run_id="run-issue-team-1",
                tenant_id="tenant-a",
                issue_key="TP-1",
                issue_summary="Issue team run",
                created_at=now,
                project_id="project-a",
                status="running",
                entry_stage="orchestrated",
                plan=snapshot.dump(),
            )
            add_run_with_workflow(session, run, workflow_status="running")
            session.commit()

            request = SimpleNamespace(
                tenant_id="tenant-a",
                project_id="project-a",
                run_id="run-issue-team-1",
                issue_key="TP-1",
                issue_summary="Issue team run",
                issue_description="desc",
                max_dev_test_review_loops=2,
                allow_pr_creation=False,
                suggested_test_commands=["pytest -q"],
                current_worker_capability="linux",
                available_worker_capabilities=("linux",),
            )
            agents = _StubAgents(
                pm=PmPlan(
                    plan_steps=["Plan it"],
                    acceptance_criteria=["Works"],
                    risks=[],
                    outcome="continue",
                    next_stage="dev",
                    execution_worker_capability="linux",
                ),
                dev=[DevResult(change_summary=["Implemented feature"], pr_url=None, outcome="continue")],
                test=[TestResult(guidance=["pytest -q"], outcome="continue")],
                review=[ReviewResult(summary=["Approved"], outcome="continue", pr_url=None)],
            )

            with (
                patch("orchestrator.core.issue_team_run_service._workflow_request", return_value=(None, None, request)),
                patch("orchestrator.core.issue_team_run_service.build_codex_workflow_agents_for_session", return_value=agents),
            ):
                run, task_key = execute_next_ready_team_task(session=session, run_id="run-issue-team-1")
                assert task_key == "pm"
                run, task_key = execute_next_ready_team_task(session=session, run_id="run-issue-team-1")
                assert task_key == "dev"
                run, task_key = execute_next_ready_team_task(session=session, run_id="run-issue-team-1")
                assert task_key == "test"
                run, task_key = execute_next_ready_team_task(session=session, run_id="run-issue-team-1")
                assert task_key == "review"

            session.refresh(run)
            persisted = ExecutionSnapshot.require(run.plan)
            team_run = persisted.context.execution_context["team_run"]
            assert run.status == "succeeded"
            assert persisted.workflow.outcome == "success"
            assert [node["status"] for node in team_run["nodes"]] == ["completed", "completed", "completed", "completed"]
    finally:
        temp_dir.cleanup()
        os.environ.pop("ORCHESTRATOR_DATABASE_URL", None)
        reset_db_engine_cache()


def test_issue_team_run_loops_back_to_dev_after_test_failure() -> None:
    temp_dir = TemporaryDirectory()
    database_url = f"sqlite:///{temp_dir.name}/issue_team_run_retry.db"
    os.environ["ORCHESTRATOR_DATABASE_URL"] = database_url
    reset_db_engine_cache()
    run_migrations(database_url=database_url)
    session_factory = create_session_factory(database_url=database_url)
    now = datetime.now(timezone.utc)
    try:
        with session_factory() as session:
            _seed_tenant(session, now=now)
            snapshot = ExecutionSnapshot.empty()
            snapshot.context.execution_context["team_run"] = platform_team_catalog_service.build_issue_workflow_team_run_snapshot(
                session=session,
                snapshot=snapshot,
                entry_mode="fresh",
                entry_stage=None,
            )
            run = make_run(
                run_id="run-issue-team-2",
                tenant_id="tenant-a",
                issue_key="TP-2",
                issue_summary="Issue team retry run",
                created_at=now,
                project_id="project-a",
                status="running",
                entry_stage="orchestrated",
                plan=snapshot.dump(),
            )
            add_run_with_workflow(session, run, workflow_status="running")
            session.commit()

            request = SimpleNamespace(
                tenant_id="tenant-a",
                project_id="project-a",
                run_id="run-issue-team-2",
                issue_key="TP-2",
                issue_summary="Issue team retry run",
                issue_description="desc",
                max_dev_test_review_loops=2,
                allow_pr_creation=False,
                suggested_test_commands=["pytest -q"],
                current_worker_capability="linux",
                available_worker_capabilities=("linux",),
            )
            agents = _StubAgents(
                pm=PmPlan(
                    plan_steps=["Plan it"],
                    acceptance_criteria=["Works"],
                    risks=[],
                    outcome="continue",
                    next_stage="dev",
                    execution_worker_capability="linux",
                ),
                dev=[
                    DevResult(change_summary=["Implemented feature"], pr_url=None, outcome="continue"),
                ],
                test=[
                    TestResult(guidance=["Fix edge case"], outcome="failed", feedback="Tests failed"),
                ],
                review=[],
            )

            with (
                patch("orchestrator.core.issue_team_run_service._workflow_request", return_value=(None, None, request)),
                patch("orchestrator.core.issue_team_run_service.build_codex_workflow_agents_for_session", return_value=agents),
            ):
                run, _ = execute_next_ready_team_task(session=session, run_id="run-issue-team-2")
                run, _ = execute_next_ready_team_task(session=session, run_id="run-issue-team-2")
                run, _ = execute_next_ready_team_task(session=session, run_id="run-issue-team-2")

            session.refresh(run)
            persisted = ExecutionSnapshot.require(run.plan)
            team_run = persisted.context.execution_context["team_run"]
            runtime_state = team_run["runtime_state"]
            assert run.status == "running"
            assert [node["status"] for node in team_run["nodes"]] == ["completed", "ready", "pending", "pending"]
            assert runtime_state["attempt"] == 2
            assert runtime_state["next_feedback"] == "Tests failed"
    finally:
        temp_dir.cleanup()
        os.environ.pop("ORCHESTRATOR_DATABASE_URL", None)
        reset_db_engine_cache()
