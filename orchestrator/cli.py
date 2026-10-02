from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from orchestrator.core.source_layout import require_source_checkout


def _coerce_positive_int(value: object, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, parsed)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="master-builder orchestrator")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("worker", help="Run background run worker loop")
    subparsers.add_parser(
        "worker-runs", help="Run background issue-execution worker loop"
    )
    subparsers.add_parser("worker-webhooks", help="Run background webhook worker loop")
    subparsers.add_parser("worker-child-runs", help="Run one child issue-execution job")
    subparsers.add_parser("worker-child-webhooks", help="Run one child webhook job")
    subparsers.add_parser(
        "deployment-host-agent", help="Run the managed deployment host agent"
    )
    subparsers.add_parser(
        "deployment-reconciler", help="Run the deployment release reconciliation loop"
    )
    subparsers.add_parser(
        "temporal-orchestrator", help="Run Temporal workflow orchestrator loop"
    )
    subparsers.add_parser("discord-gateway", help="Run Discord gateway leader loop")
    subparsers.add_parser(
        "discord-live-voice", help="Run Discord live voice leader loop"
    )
    subparsers.add_parser(
        "knowledge-jira-sync", help="Run Jira knowledge sync leader loop"
    )
    subparsers.add_parser(
        "jira-project-reconciliation",
        help="Run Jira project reconciliation leader loop",
    )
    subparsers.add_parser(
        "project-automation", help="Run project automation scheduler leader loop"
    )
    subparsers.add_parser(
        "knowledge-prewarm", help="Prewarm knowledge embedding dependencies"
    )
    subparsers.add_parser("migrate", help="Apply DB migrations")
    subparsers.add_parser("voice-prewarm", help="Prewarm voice model dependencies")

    run_parser = subparsers.add_parser(
        "run", help="Queue a manual run for a tenant issue"
    )
    run_parser.add_argument("--tenant", required=True, help="Tenant identifier")
    run_parser.add_argument(
        "--issue", required=True, help="Jira issue key (for example EXAMPLE-123)"
    )

    poll_parser = subparsers.add_parser(
        "poll",
        help="Poll queue readiness information for one tenant or all tenants",
    )
    poll_parser.add_argument(
        "--tenant",
        default="all",
        help="Tenant identifier or 'all' (default)",
    )

    tool_parser = subparsers.add_parser(
        "agent-tool", help="Execute an agent tool action"
    )
    tool_parser.add_argument("--tenant", required=True, help="Tenant identifier")
    tool_parser.add_argument("--project", default=None, help="Project identifier")
    tool_parser.add_argument("--run", default=None, help="Run identifier")
    tool_parser.add_argument(
        "--worker-platform",
        default=None,
        choices=["linux", "macos"],
        help="Execution worker platform for platform-scoped tools",
    )
    tool_parser.add_argument("--issue", required=True, help="Issue key")
    tool_parser.add_argument(
        "--stage", required=True, help="Workflow stage (pm|dev|test|review)"
    )
    tool_parser.add_argument("--tool", required=True, help="Tool name")
    tool_parser.add_argument(
        "--args",
        default="{}",
        help="JSON object containing tool args",
    )

    return parser


def _handle_run(*, tenant_id: str, issue_key: str) -> int:
    from orchestrator.core.config import get_settings
    from orchestrator.core.runs.service import (
        enqueue_run,
        resolve_enqueue_precheck_outcome,
        resolve_precheck_outcome_for_enqueue,
    )
    from orchestrator.storage.database_support import ensure_postgres_database_url
    from orchestrator.storage.db import create_session_factory
    from orchestrator.storage.models import Tenant

    settings = get_settings()
    ensure_postgres_database_url(
        database_url=settings.database_url,
        context="CLI runtime",
        allow_sqlite_for_tests=bool(getattr(settings, "allow_sqlite_for_tests", False)),
    )
    session_factory = create_session_factory()
    with session_factory() as session:
        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            print(json.dumps({"ok": False, "error": f"Unknown tenant '{tenant_id}'"}))
            return 1
        if not tenant.is_enabled:
            print(
                json.dumps({"ok": False, "error": f"Tenant '{tenant_id}' is disabled"})
            )
            return 1

        result = enqueue_run(
            session,
            tenant_id=tenant_id,
            project_id=None,
            issue_key=issue_key,
            repo_url=None,
            precheck_outcome=resolve_enqueue_precheck_outcome(
                source="cli_run",
                precheck_outcome=resolve_precheck_outcome_for_enqueue(
                    precheck_outcome="ready_for_agent"
                ),
            ),
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "tenant_id": tenant_id,
                    "issue_key": issue_key,
                    "enqueued": result.enqueued,
                    "reason": result.reason,
                    "run_id": result.run.run_id,
                }
            )
        )
    return 0


def _count_runs_for_tenant(session, *, tenant_id: str, status: str) -> int:  # noqa: ANN001
    from sqlalchemy import func, select

    from orchestrator.storage.models import Run

    return int(
        session.execute(
            select(func.count(Run.run_id)).where(
                Run.tenant_id == tenant_id,
                Run.status == status,
            )
        ).scalar_one()
    )


def _tenant_poll_snapshot(session, tenant: Any) -> dict:  # noqa: ANN001
    max_concurrent_runs = _coerce_positive_int(
        tenant.policy_config.get("max_concurrent_runs"),
        default=1,
    )
    return {
        "tenant_id": tenant.tenant_id,
        "enabled": tenant.is_enabled,
        "queued_runs": _count_runs_for_tenant(
            session, tenant_id=tenant.tenant_id, status="queued"
        ),
        "running_runs": _count_runs_for_tenant(
            session,
            tenant_id=tenant.tenant_id,
            status="running",
        ),
        "max_concurrent_runs": max_concurrent_runs,
    }


def _handle_poll(*, tenant_filter: str) -> int:
    from sqlalchemy import select

    from orchestrator.core.config import get_settings
    from orchestrator.storage.database_support import ensure_postgres_database_url
    from orchestrator.storage.db import create_session_factory
    from orchestrator.storage.models import Tenant

    settings = get_settings()
    ensure_postgres_database_url(
        database_url=settings.database_url,
        context="CLI runtime",
        allow_sqlite_for_tests=bool(getattr(settings, "allow_sqlite_for_tests", False)),
    )
    session_factory = create_session_factory()
    with session_factory() as session:
        query = select(Tenant).order_by(Tenant.tenant_id.asc())
        if tenant_filter != "all":
            query = query.where(Tenant.tenant_id == tenant_filter)

        tenants = session.execute(query).scalars().all()
        if not tenants:
            print(
                json.dumps(
                    {"ok": False, "error": f"No tenant found for '{tenant_filter}'"}
                )
            )
            return 1

        payload = {
            "ok": True,
            "polled_at": datetime.now(timezone.utc).isoformat(),
            "tenant_filter": tenant_filter,
            "tenants": [_tenant_poll_snapshot(session, tenant) for tenant in tenants],
        }
        print(json.dumps(payload))
    return 0


def _handle_agent_tool(
    *,
    tenant_id: str,
    project_id: str | None,
    run_id: str | None,
    issue_key: str,
    stage: str,
    tool_name: str,
    args_json: str,
    worker_platform: str | None = None,
) -> int:
    from orchestrator.core.config import get_settings
    from orchestrator.core.runtime.tools import execute_agent_tool, print_tool_event
    from orchestrator.storage.database_support import ensure_postgres_database_url
    from orchestrator.storage.db import create_session_factory

    try:
        parsed_args = json.loads(args_json)
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "error": f"Invalid --args JSON: {exc}"}))
        return 2
    if not isinstance(parsed_args, dict):
        print(json.dumps({"ok": False, "error": "--args must decode to a JSON object"}))
        return 2

    settings = get_settings()
    ensure_postgres_database_url(
        database_url=settings.database_url,
        context="CLI runtime",
        allow_sqlite_for_tests=bool(getattr(settings, "allow_sqlite_for_tests", False)),
    )
    session_factory = create_session_factory()
    print_tool_event(
        stage=stage, tool_name=tool_name, args=parsed_args, outcome="started"
    )
    with session_factory() as session:
        try:
            result: dict[str, Any] = execute_agent_tool(
                session=session,
                settings=settings,
                tenant_id=tenant_id,
                project_id=project_id,
                run_id=run_id,
                issue_key=issue_key,
                stage=stage,
                tool_name=tool_name,
                tool_args=parsed_args,
                worker_platform=worker_platform,
            )
            print_tool_event(
                stage=stage, tool_name=tool_name, args=parsed_args, outcome="succeeded"
            )
            print(json.dumps({"ok": True, "tool": tool_name, "result": result}))
            return 0
        except Exception as exc:  # noqa: BLE001
            print_tool_event(
                stage=stage, tool_name=tool_name, args=parsed_args, outcome="failed"
            )
            print(json.dumps({"ok": False, "tool": tool_name, "error": str(exc)}))
            return 1


def _handle_voice_prewarm() -> int:
    from orchestrator.core.config import get_settings
    from orchestrator.core.voice.prewarm import prewarm_voice_dependencies

    settings = get_settings()
    result = prewarm_voice_dependencies(settings=settings)
    print(
        json.dumps(
            {
                "ok": True,
                "voice_stt_provider": result.voice_stt_provider,
                "voice_tts_provider": result.voice_tts_provider,
                "transcription_ready": result.transcription_ready,
                "prewarmed_voice_ids": list(result.prewarmed_voice_ids),
            }
        )
    )
    return 0


def _handle_knowledge_prewarm() -> int:
    from orchestrator.core.config import get_settings
    from orchestrator.core.knowledge.prewarm import prewarm_knowledge_dependencies

    settings = get_settings()
    result = prewarm_knowledge_dependencies(settings=settings)
    print(
        json.dumps(
            {
                "ok": True,
                "embedding_model": result.embedding_model,
            }
        )
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        require_source_checkout()
    except RuntimeError as exc:
        parser.error(str(exc))

    if args.command == "worker":
        from orchestrator.worker import main as worker_main

        worker_main(mode="runs")
        return 0

    if args.command == "worker-runs":
        from orchestrator.worker import main as worker_main

        worker_main(mode="runs")
        return 0

    if args.command == "worker-webhooks":
        from orchestrator.worker import main as worker_main

        worker_main(mode="webhooks")
        return 0

    if args.command == "worker-child-runs":
        from orchestrator.worker import run_worker_child_once

        return int(run_worker_child_once(mode="runs"))

    if args.command == "worker-child-webhooks":
        from orchestrator.worker import run_worker_child_once

        return int(run_worker_child_once(mode="webhooks"))

    if args.command == "deployment-host-agent":
        from orchestrator.core.deployment_host_agent_runtime import (
            run_deployment_host_agent,
        )

        run_deployment_host_agent()
        return 0

    if args.command == "deployment-reconciler":
        from orchestrator.core.deployment_runtime import run_deployment_reconciler

        run_deployment_reconciler()
        return 0

    if args.command == "temporal-orchestrator":
        from orchestrator.temporal.worker import run_temporal_worker

        asyncio.run(run_temporal_worker())
        return 0

    if args.command == "discord-gateway":
        from orchestrator.core.discord.gateway_runtime import run_discord_gateway

        run_discord_gateway()
        return 0

    if args.command == "discord-live-voice":
        from orchestrator.core.discord.live_voice_gateway_runtime import (
            run_discord_live_voice,
        )

        run_discord_live_voice()
        return 0

    if args.command == "knowledge-jira-sync":
        from orchestrator.core.knowledge.jira_sync_runtime import (
            run_knowledge_jira_sync,
        )

        run_knowledge_jira_sync()
        return 0

    if args.command == "jira-project-reconciliation":
        from orchestrator.core.jira_project_reconciliation.scheduler import (
            run_jira_project_reconciliation_runtime,
        )

        run_jira_project_reconciliation_runtime()
        return 0

    if args.command == "project-automation":
        from orchestrator.core.projects.automation_runtime import (
            run_project_automation_runtime,
        )

        run_project_automation_runtime()
        return 0

    if args.command == "knowledge-prewarm":
        return _handle_knowledge_prewarm()

    if args.command == "migrate":
        from orchestrator.storage.migrations import run_migrations

        run_migrations()
        return 0

    if args.command == "voice-prewarm":
        return _handle_voice_prewarm()

    if args.command == "run":
        return _handle_run(tenant_id=args.tenant, issue_key=args.issue)

    if args.command == "poll":
        return _handle_poll(tenant_filter=args.tenant)

    if args.command == "agent-tool":
        return _handle_agent_tool(
            tenant_id=args.tenant,
            project_id=args.project,
            run_id=args.run,
            issue_key=args.issue,
            stage=args.stage,
            tool_name=args.tool,
            args_json=args.args,
            worker_platform=args.worker_platform,
        )

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
