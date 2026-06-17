from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from orchestrator.core.qa.demo_proof_handlers import DEMO_PROOF_HANDLER_KEY, DemoProofWorkflowAdvanceHandler
from orchestrator.core.workflow.execution_projection import (
    WorkflowExecutionReference,
    WorkflowSourceReference,
    workflow_execution_id,
)
from orchestrator.core.workflow.handler_registry import build_workflow_handler_registry
from orchestrator.core.workflow.runtime import WorkflowAdvanceRequest, WorkflowTrigger, build_workflow_runtime
from orchestrator.storage.models import Project, Tenant, WorkflowExecution, WorkflowOperation, WorkflowOperationAttempt

DEMO_PROOF_WORKFLOW_TYPE_KEY = "demo_proof"


@dataclass(frozen=True)
class DemoProofWorkflowStartResult:
    execution_id: str
    workflow_id: str
    workflow_type_key: str
    status: str
    started_attempt_id: str | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_proof_scope_id(value: object) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError("Demo proof start requires proof_scope_id")
    if not re.fullmatch(r"[A-Za-z0-9._:-]+", normalized):
        raise ValueError("Demo proof proof_scope_id contains unsupported characters")
    return normalized


def _normalize_commit_sha(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{7,64}", normalized):
        raise ValueError("Demo proof start requires a valid commit_sha")
    return normalized


def _normalize_required_capture_targets(value: object) -> tuple[str, ...]:
    if value is None:
        return ("browser", "ios", "android")
    if not isinstance(value, list):
        raise ValueError("Demo proof required_capture_targets must be a list")
    targets = tuple(str(item or "").strip() for item in value if str(item or "").strip())
    if not targets:
        raise ValueError("Demo proof required_capture_targets must not be empty")
    unsupported = [target for target in targets if target not in {"browser", "ios", "android"}]
    if unsupported:
        raise ValueError("Demo proof required_capture_targets contains unsupported target(s): " + ", ".join(unsupported))
    return targets


def _request_id(*, tenant: Tenant, project: Project, proof_scope_id: str, trigger_event: str) -> str:
    return (
        f"demo-proof:{tenant.tenant_id}:{project.project_id}:{proof_scope_id}:"
        f"{trigger_event}:{_now().isoformat(timespec='seconds')}"
    )


def _latest_attempt_id(*, session: Session, workflow_id: str) -> str | None:
    attempt = session.execute(
        select(WorkflowOperationAttempt)
        .join(WorkflowOperation, WorkflowOperation.operation_id == WorkflowOperationAttempt.operation_id)
        .where(WorkflowOperation.workflow_id == workflow_id)
        .order_by(desc(WorkflowOperationAttempt.created_at))
        .limit(1)
    ).scalar_one_or_none()
    if attempt is None:
        return None
    return attempt.attempt_id


def start_demo_proof_workflow(
    *,
    session: Session,
    settings,  # noqa: ANN001
    tenant: Tenant,
    project: Project,
    proof_scope_id: str,
    commit_sha: str,
    trigger_event: str,
    run_id: str | None = None,
    pr_url: str | None = None,
    required_capture_targets: list[object] | None = None,
) -> DemoProofWorkflowStartResult:
    if tenant is None:
        raise ValueError("Demo proof start requires tenant")
    if project is None:
        raise ValueError("Demo proof start requires project")
    if project.tenant_id != tenant.tenant_id:
        raise ValueError("Demo proof project must belong to the supplied tenant")
    if bool(getattr(project, "is_archived", False)):
        raise ValueError("Demo proof cannot run for an archived project")

    normalized_scope = _normalize_proof_scope_id(proof_scope_id)
    normalized_commit_sha = _normalize_commit_sha(commit_sha)
    normalized_targets = _normalize_required_capture_targets(required_capture_targets)
    normalized_run_id = str(run_id or "").strip() or None
    normalized_pr_url = str(pr_url or "").strip() or None
    normalized_trigger_event = str(trigger_event or "").strip() or "demo_proof_start"

    handler_registry = build_workflow_handler_registry(
        advance_handlers={
            DEMO_PROOF_HANDLER_KEY: DemoProofWorkflowAdvanceHandler(),
        },
        operation_retry_handlers={},
    )
    runtime = build_workflow_runtime(
        session=session,
        settings=settings,
        process_claimed_run_fn=None,
        build_runner_fn=None,
        runtime_kwargs_fn=None,
        resolve_advance_handler_fn=handler_registry.resolve_advance_handler,
        workflow_handler_registry=handler_registry,
    )
    runtime.advance(
        request=WorkflowAdvanceRequest(
            workflow_handler_key=DEMO_PROOF_HANDLER_KEY,
            tenant_id=tenant.tenant_id,
            tenant=tenant,
            project_id=project.project_id,
            execution=WorkflowExecutionReference(
                key=normalized_scope,
                source=WorkflowSourceReference(
                    source_system="demo_proof",
                    source_ref=normalized_scope,
                    external_id=normalized_scope,
                    display_name=f"Demo proof {normalized_scope}",
                    description=f"Prove delivered feature for scope {normalized_scope}.",
                    attributes={
                        "proof_scope_id": normalized_scope,
                        "commit_sha": normalized_commit_sha,
                    },
                ),
            ),
            payload={
                "request_id": _request_id(
                    tenant=tenant,
                    project=project,
                    proof_scope_id=normalized_scope,
                    trigger_event=normalized_trigger_event,
                ),
                "proof_scope_id": normalized_scope,
                "commit_sha": normalized_commit_sha,
                "run_id": normalized_run_id,
                "pr_url": normalized_pr_url,
                "required_capture_targets": list(normalized_targets),
            },
            trigger=WorkflowTrigger(event=normalized_trigger_event),
        )
    )
    workflow_id = workflow_execution_id(
        workflow_type_key=DEMO_PROOF_WORKFLOW_TYPE_KEY,
        execution_key=normalized_scope,
    )
    workflow = session.get(WorkflowExecution, workflow_id)
    if workflow is None:
        raise RuntimeError("Demo proof workflow did not create a durable workflow execution")
    return DemoProofWorkflowStartResult(
        execution_id=workflow.execution_id,
        workflow_id=workflow.workflow_id,
        workflow_type_key=workflow.workflow_type_key,
        status=workflow.status,
        started_attempt_id=_latest_attempt_id(session=session, workflow_id=workflow.workflow_id),
    )
