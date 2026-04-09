from __future__ import annotations

import os
from datetime import datetime, timezone
from tempfile import TemporaryDirectory

from orchestrator.core.team_run_engine import team_run_engine
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import Tenant
from tests.workflow_test_support import add_run_with_workflow, make_run


def _seed_tenant(session, *, now: datetime) -> None:  # noqa: ANN001
    session.add(
        Tenant(
            tenant_id="tenant-team-engine",
            name="Tenant Team Engine",
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


def test_team_run_engine_advances_manual_custom_team_to_success() -> None:
    temp_dir = TemporaryDirectory()
    database_url = f"sqlite:///{temp_dir.name}/team_run_engine_custom.db"
    os.environ["ORCHESTRATOR_DATABASE_URL"] = database_url
    reset_db_engine_cache()
    run_migrations(database_url=database_url)
    session_factory = create_session_factory(database_url=database_url)
    now = datetime.now(timezone.utc)
    try:
        with session_factory() as session:
            _seed_tenant(session, now=now)
            snapshot = ExecutionSnapshot.empty()
            snapshot.context.execution_context["team_run"] = {
                "team_key": "custom_team",
                "team_label": "Custom Team",
                "definition_version": 1,
                "nodes": [
                    {
                        "task_key": "brief",
                        "label": "Brief",
                        "owner_role_key": "lead",
                        "owner_persona_key": "lead_persona",
                        "owner_agent_key": "lead_agent",
                        "status": "ready",
                        "dependency_keys": [],
                        "artifact_contract": {},
                        "approval_rule": {},
                        "executor_kind": None,
                    },
                    {
                        "task_key": "launch",
                        "label": "Launch",
                        "owner_role_key": "ops",
                        "owner_persona_key": "ops_persona",
                        "owner_agent_key": "ops_agent",
                        "status": "pending",
                        "dependency_keys": ["brief"],
                        "artifact_contract": {},
                        "approval_rule": {},
                        "executor_kind": None,
                    },
                ],
                "edges": [{"from_task_key": "brief", "to_task_key": "launch"}],
                "artifacts": [],
                "approvals": [],
                "status": "queued",
                "runtime_state": {},
            }
            run = make_run(
                run_id="run-team-engine-1",
                tenant_id="tenant-team-engine",
                issue_key="MK-301",
                issue_summary="Team engine success path",
                created_at=now,
                project_id="project-engine",
                status="running",
                entry_stage="team",
                plan=snapshot.dump(),
            )
            add_run_with_workflow(session, run, workflow_status="running")
            session.commit()

            run = team_run_engine.initialize(session=session, run_id=run.run_id)
            run = team_run_engine.complete_task(session=session, run_id=run.run_id, task_key="brief", summary="brief complete")
            run = team_run_engine.complete_task(session=session, run_id=run.run_id, task_key="launch", summary="launch complete")

            persisted = ExecutionSnapshot.require(run.plan)
            team_run = persisted.context.execution_context["team_run"]
            assert run.status == "succeeded"
            assert all(str(node["status"]) == "completed" for node in team_run["nodes"])
    finally:
        temp_dir.cleanup()
        os.environ.pop("ORCHESTRATOR_DATABASE_URL", None)
        reset_db_engine_cache()


def test_team_run_engine_blocks_unknown_executor() -> None:
    temp_dir = TemporaryDirectory()
    database_url = f"sqlite:///{temp_dir.name}/team_run_engine_unknown_executor.db"
    os.environ["ORCHESTRATOR_DATABASE_URL"] = database_url
    reset_db_engine_cache()
    run_migrations(database_url=database_url)
    session_factory = create_session_factory(database_url=database_url)
    now = datetime.now(timezone.utc)
    try:
        with session_factory() as session:
            _seed_tenant(session, now=now)
            snapshot = ExecutionSnapshot.empty()
            snapshot.context.execution_context["team_run"] = {
                "team_key": "custom_team",
                "team_label": "Custom Team",
                "definition_version": 1,
                "nodes": [
                    {
                        "task_key": "auto_task",
                        "label": "Auto Task",
                        "owner_role_key": "ops",
                        "owner_persona_key": "ops_persona",
                        "owner_agent_key": "ops_agent",
                        "status": "ready",
                        "dependency_keys": [],
                        "artifact_contract": {},
                        "approval_rule": {},
                        "executor_kind": "workflow.unknown",
                    }
                ],
                "edges": [],
                "artifacts": [],
                "approvals": [],
                "status": "running",
                "runtime_state": {},
            }
            run = make_run(
                run_id="run-team-engine-2",
                tenant_id="tenant-team-engine",
                issue_key="MK-302",
                issue_summary="Team engine unknown executor",
                created_at=now,
                project_id="project-engine",
                status="running",
                entry_stage="team",
                plan=snapshot.dump(),
            )
            add_run_with_workflow(session, run, workflow_status="running")
            session.commit()

            run = team_run_engine.initialize(session=session, run_id=run.run_id)
            run, task_key = team_run_engine.advance_auto(session=session, run_id=run.run_id)

            assert task_key == "auto_task"
            assert run.status == "blocked"
            assert "Unsupported team task executor" in str(run.last_error or "")
    finally:
        temp_dir.cleanup()
        os.environ.pop("ORCHESTRATOR_DATABASE_URL", None)
        reset_db_engine_cache()
