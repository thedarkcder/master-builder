from __future__ import annotations

import os
from datetime import datetime, timezone
from tempfile import TemporaryDirectory

from orchestrator.core.platform_team_catalog_service import platform_team_catalog_service
from orchestrator.core.team_run_service import complete_team_task, initialize_team_run
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import Tenant
from tests.workflow_test_support import add_run_with_workflow, make_run


def _seed_tenant(session, *, now: datetime) -> None:  # noqa: ANN001
    session.add(
        Tenant(
            tenant_id="tenant-guardrails",
            name="Tenant Guardrails",
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


def _custom_team_run_snapshot() -> dict[str, object]:
    return {
        "team_key": "marketing_launch",
        "team_label": "Marketing Launch Team",
        "definition_version": 3,
        "nodes": [
            {
                "task_key": "brief",
                "label": "Brief",
                "owner_role_key": "strategist",
                "owner_persona_key": "launch_strategist",
                "owner_agent_key": "launch_strategy_agent",
                "status": "ready",
                "dependency_keys": [],
                "artifact_contract": {"produces": ["brief"]},
                "approval_rule": {},
                "executor_kind": None,
            },
            {
                "task_key": "audience_research",
                "label": "Audience Research",
                "owner_role_key": "research",
                "owner_persona_key": "audience_researcher",
                "owner_agent_key": "audience_research_agent",
                "status": "pending",
                "dependency_keys": ["brief"],
                "artifact_contract": {"produces": ["research_notes"]},
                "approval_rule": {},
                "executor_kind": None,
            },
            {
                "task_key": "message_map",
                "label": "Message Map",
                "owner_role_key": "writer",
                "owner_persona_key": "campaign_writer",
                "owner_agent_key": "campaign_writer_agent",
                "status": "pending",
                "dependency_keys": ["audience_research"],
                "artifact_contract": {"produces": ["message_map"]},
                "approval_rule": {},
                "executor_kind": None,
            },
            {
                "task_key": "copy_draft",
                "label": "Copy Draft",
                "owner_role_key": "writer",
                "owner_persona_key": "campaign_writer",
                "owner_agent_key": "campaign_writer_agent",
                "status": "pending",
                "dependency_keys": ["message_map"],
                "artifact_contract": {"produces": ["copy_draft"]},
                "approval_rule": {},
                "executor_kind": None,
            },
            {
                "task_key": "launch_approval",
                "label": "Launch Approval",
                "owner_role_key": "approver",
                "owner_persona_key": "launch_approver",
                "owner_agent_key": "launch_approver_agent",
                "status": "pending",
                "dependency_keys": ["copy_draft"],
                "artifact_contract": {"produces": ["launch_packet"]},
                "approval_rule": {},
                "executor_kind": None,
            },
        ],
        "edges": [
            {"from_task_key": "brief", "to_task_key": "audience_research"},
            {"from_task_key": "audience_research", "to_task_key": "message_map"},
            {"from_task_key": "message_map", "to_task_key": "copy_draft"},
            {"from_task_key": "copy_draft", "to_task_key": "launch_approval"},
        ],
        "artifacts": [],
        "approvals": [],
        "status": "queued",
        "runtime_state": {},
    }


def test_runtime_bindings_are_catalog_derived() -> None:
    temp_dir = TemporaryDirectory()
    database_url = f"sqlite:///{temp_dir.name}/runtime_bindings_guardrails.db"
    os.environ["ORCHESTRATOR_DATABASE_URL"] = database_url
    reset_db_engine_cache()
    run_migrations(database_url=database_url)
    session_factory = create_session_factory(database_url=database_url)
    now = datetime.now(timezone.utc)
    try:
        with session_factory() as session:
            _seed_tenant(session, now=now)
            persona = platform_team_catalog_service.create_persona(
                session=session,
                payload={
                    "persona_key": "brand_strategist",
                    "label": "Brand Strategist",
                    "description": "Owns strategy",
                    "allowed_surfaces": ["team_run_execution"],
                    "is_active": True,
                },
            )
            assert persona.persona_key == "brand_strategist"
            agent = platform_team_catalog_service.create_agent(
                session=session,
                payload={
                    "agent_key": "brand_strategy_agent",
                    "label": "Brand Strategy Agent",
                    "description": "Routes strategy tasks",
                    "persona_key": "brand_strategist",
                    "runtime_role_key": "brand_strategy",
                    "named_agent_key": "brand_strategy_primary",
                    "selector_key": "team.brand.strategy",
                    "default_profile_name": "general_planning_default",
                    "is_active": True,
                },
            )
            assert agent.agent_key == "brand_strategy_agent"

            runtime_bindings = platform_team_catalog_service.list_runtime_bindings(session=session)
            assert "brand_strategy" in runtime_bindings["available_roles"]
            assert "brand_strategy_primary" in runtime_bindings["available_named_agents"]
            assert "team.brand.strategy" in runtime_bindings["available_selectors"]
    finally:
        temp_dir.cleanup()
        os.environ.pop("ORCHESTRATOR_DATABASE_URL", None)
        reset_db_engine_cache()


def test_team_run_path_executes_custom_non_stage_task_keys() -> None:
    temp_dir = TemporaryDirectory()
    database_url = f"sqlite:///{temp_dir.name}/team_run_guardrails.db"
    os.environ["ORCHESTRATOR_DATABASE_URL"] = database_url
    reset_db_engine_cache()
    run_migrations(database_url=database_url)
    session_factory = create_session_factory(database_url=database_url)
    now = datetime.now(timezone.utc)
    try:
        with session_factory() as session:
            _seed_tenant(session, now=now)
            snapshot = ExecutionSnapshot.empty()
            snapshot.context.execution_context["team_run"] = _custom_team_run_snapshot()
            run = make_run(
                run_id="run-team-guardrail-1",
                tenant_id="tenant-guardrails",
                issue_key="MK-101",
                issue_summary="Launch spring campaign",
                created_at=now,
                project_id="project-guardrails",
                status="running",
                entry_stage="team",
                plan=snapshot.dump(),
            )
            add_run_with_workflow(session, run, workflow_status="running")
            session.commit()

            run = initialize_team_run(session=session, run_id=run.run_id)
            run = complete_team_task(session=session, run_id=run.run_id, task_key="brief", summary="Brief ready")
            run = complete_team_task(session=session, run_id=run.run_id, task_key="audience_research", summary="Research complete")
            run = complete_team_task(session=session, run_id=run.run_id, task_key="message_map", summary="Message map complete")
            run = complete_team_task(session=session, run_id=run.run_id, task_key="copy_draft", summary="Copy complete")
            run = complete_team_task(session=session, run_id=run.run_id, task_key="launch_approval", summary="Approved")

            persisted = ExecutionSnapshot.require(run.plan)
            team_run = persisted.context.execution_context["team_run"]
            task_keys = [str(node.get("task_key")) for node in team_run["nodes"]]
            assert run.status == "succeeded"
            assert task_keys == ["brief", "audience_research", "message_map", "copy_draft", "launch_approval"]
            assert "pm" not in task_keys
            assert "dev" not in task_keys
            assert "test" not in task_keys
            assert "review" not in task_keys
            assert all(str(node.get("status")) == "completed" for node in team_run["nodes"])
    finally:
        temp_dir.cleanup()
        os.environ.pop("ORCHESTRATOR_DATABASE_URL", None)
        reset_db_engine_cache()
