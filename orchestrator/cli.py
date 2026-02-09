from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import datetime, timezone

from sqlalchemy import func, select

from orchestrator.core.runs import enqueue_run
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import Run, Tenant
from orchestrator.worker import main as worker_main


def _coerce_positive_int(value: object, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, parsed)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="master-builder orchestrator")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("worker", help="Run background worker loop")
    subparsers.add_parser("migrate", help="Apply DB migrations")

    run_parser = subparsers.add_parser("run", help="Queue a manual run for a tenant issue")
    run_parser.add_argument("--tenant", required=True, help="Tenant identifier")
    run_parser.add_argument("--issue", required=True, help="Jira issue key (for example MAB-123)")

    poll_parser = subparsers.add_parser(
        "poll",
        help="Poll queue readiness information for one tenant or all tenants",
    )
    poll_parser.add_argument(
        "--tenant",
        default="all",
        help="Tenant identifier or 'all' (default)",
    )

    return parser


def _handle_run(*, tenant_id: str, issue_key: str) -> int:
    session_factory = create_session_factory()
    with session_factory() as session:
        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            print(json.dumps({"ok": False, "error": f"Unknown tenant '{tenant_id}'"}))
            return 1
        if not tenant.is_enabled:
            print(json.dumps({"ok": False, "error": f"Tenant '{tenant_id}' is disabled"}))
            return 1

        result = enqueue_run(
            session,
            tenant_id=tenant_id,
            project_id=None,
            issue_key=issue_key,
            repo_url=None,
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
    return int(
        session.execute(
            select(func.count(Run.run_id)).where(
                Run.tenant_id == tenant_id,
                Run.status == status,
            )
        ).scalar_one()
    )


def _tenant_poll_snapshot(session, tenant: Tenant) -> dict:  # noqa: ANN001
    max_concurrent_runs = _coerce_positive_int(
        tenant.policy_config.get("max_concurrent_runs"),
        default=1,
    )
    return {
        "tenant_id": tenant.tenant_id,
        "enabled": tenant.is_enabled,
        "queued_runs": _count_runs_for_tenant(session, tenant_id=tenant.tenant_id, status="queued"),
        "running_runs": _count_runs_for_tenant(
            session,
            tenant_id=tenant.tenant_id,
            status="running",
        ),
        "max_concurrent_runs": max_concurrent_runs,
    }


def _handle_poll(*, tenant_filter: str) -> int:
    session_factory = create_session_factory()
    with session_factory() as session:
        query = select(Tenant).order_by(Tenant.tenant_id.asc())
        if tenant_filter != "all":
            query = query.where(Tenant.tenant_id == tenant_filter)

        tenants = session.execute(query).scalars().all()
        if not tenants:
            print(json.dumps({"ok": False, "error": f"No tenant found for '{tenant_filter}'"}))
            return 1

        payload = {
            "ok": True,
            "polled_at": datetime.now(timezone.utc).isoformat(),
            "tenant_filter": tenant_filter,
            "tenants": [_tenant_poll_snapshot(session, tenant) for tenant in tenants],
        }
        print(json.dumps(payload))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "worker":
        worker_main()
        return 0

    if args.command == "migrate":
        run_migrations()
        return 0

    if args.command == "run":
        return _handle_run(tenant_id=args.tenant, issue_key=args.issue)

    if args.command == "poll":
        return _handle_poll(tenant_filter=args.tenant)

    parser.error(f"Unknown command: {args.command}")
    return 2
