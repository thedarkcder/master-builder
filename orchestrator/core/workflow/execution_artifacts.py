from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.storage.models import (
    Run,
    WorkflowCheckpoint,
    WorkflowExecutionArtifact,
)

EXECUTION_BRANCH_ARTIFACT_KIND = "execution_branch"
EXECUTION_ARTIFACT_STATUS_PUSHED = "pushed"
_DURABLE_EXECUTION_STAGES = {"dev", "test", "review"}


@dataclass(frozen=True)
class ExecutionArtifactRef:
    artifact_id: str
    branch: str
    commit_sha: str
    repo_url: str


class MissingDurableExecutionArtifactError(RuntimeError):
    pass


def record_pushed_execution_artifact(
    session: Session,
    *,
    run: Run,
    repo_url: str,
    branch: str,
    commit_sha: str,
    diff_stat: dict[str, object] | None = None,
    pushed_at: datetime | None = None,
) -> WorkflowExecutionArtifact:
    normalized_repo_url = str(repo_url or "").strip()
    normalized_branch = str(branch or "").strip()
    normalized_commit_sha = str(commit_sha or "").strip()
    if not normalized_repo_url or not normalized_branch or not normalized_commit_sha:
        raise ValueError(
            "repo_url, branch, and commit_sha are required for a durable execution artifact"
        )
    now = pushed_at or datetime.now(timezone.utc)
    artifact = session.execute(
        select(WorkflowExecutionArtifact).where(
            WorkflowExecutionArtifact.run_id == run.run_id,
            WorkflowExecutionArtifact.artifact_kind == EXECUTION_BRANCH_ARTIFACT_KIND,
        )
    ).scalar_one_or_none()
    if artifact is None:
        artifact = WorkflowExecutionArtifact(
            artifact_id=uuid4().hex,
            tenant_id=run.tenant_id,
            project_id=run.project_id,
            workflow_id=run.workflow_id,
            run_id=run.run_id,
            artifact_kind=EXECUTION_BRANCH_ARTIFACT_KIND,
            status=EXECUTION_ARTIFACT_STATUS_PUSHED,
            repo_url=normalized_repo_url,
            branch=normalized_branch,
            commit_sha=normalized_commit_sha,
            diff_stat_json=dict(diff_stat or {}),
            pushed_at=now,
            created_at=now,
            updated_at=now,
        )
        session.add(artifact)
        session.flush()
        return artifact
    artifact.tenant_id = run.tenant_id
    artifact.project_id = run.project_id
    artifact.workflow_id = run.workflow_id
    artifact.status = EXECUTION_ARTIFACT_STATUS_PUSHED
    artifact.repo_url = normalized_repo_url
    artifact.branch = normalized_branch
    artifact.commit_sha = normalized_commit_sha
    artifact.diff_stat_json = dict(diff_stat or {})
    artifact.pushed_at = now
    artifact.updated_at = now
    session.flush()
    return artifact


def latest_pushed_execution_artifact_for_run(
    *, session: Session, run_id: str | None
) -> WorkflowExecutionArtifact | None:
    normalized_run_id = str(run_id or "").strip()
    if not normalized_run_id:
        return None
    return session.execute(
        select(WorkflowExecutionArtifact).where(
            WorkflowExecutionArtifact.run_id == normalized_run_id,
            WorkflowExecutionArtifact.artifact_kind == EXECUTION_BRANCH_ARTIFACT_KIND,
            WorkflowExecutionArtifact.status == EXECUTION_ARTIFACT_STATUS_PUSHED,
        )
    ).scalar_one_or_none()


def checkpoint_requires_durable_execution_artifact(
    *, checkpoint: WorkflowCheckpoint
) -> bool:
    if (
        str(getattr(checkpoint, "checkpoint_kind", "") or "").strip().lower()
        != "execution"
    ):
        return False
    return snapshot_requires_durable_execution_artifact(checkpoint.payload_json)


def snapshot_requires_durable_execution_artifact(payload: object | None) -> bool:
    try:
        snapshot = ExecutionSnapshot.require(payload, allow_empty=False)
    except ValueError:
        return False
    for stage, record in snapshot.stages.items():
        normalized_stage = str(stage or "").strip().lower()
        if normalized_stage not in _DURABLE_EXECUTION_STAGES:
            continue
        if str(record.status or "").strip().lower() != "completed":
            continue
        if isinstance(record.artifact, dict) and record.artifact:
            return True
    return False


def require_durable_execution_artifact_for_checkpoint(
    *,
    session: Session,
    checkpoint: WorkflowCheckpoint,
) -> ExecutionArtifactRef | None:
    if not checkpoint_requires_durable_execution_artifact(checkpoint=checkpoint):
        return None
    artifact = latest_pushed_execution_artifact_for_run(
        session=session, run_id=checkpoint.run_id
    )
    if artifact is None:
        raise MissingDurableExecutionArtifactError(
            "Selected execution checkpoint is not resumable because its code artifact was not pushed."
        )
    return ExecutionArtifactRef(
        artifact_id=artifact.artifact_id,
        branch=artifact.branch,
        commit_sha=artifact.commit_sha,
        repo_url=artifact.repo_url,
    )


def attach_artifact_to_resume_plan(
    *,
    plan: dict[str, object],
    artifact: ExecutionArtifactRef | None,
) -> dict[str, object]:
    if artifact is None:
        return plan
    snapshot = ExecutionSnapshot.require(plan, allow_empty=False)
    snapshot.context.execution_context["durable_artifact_id"] = artifact.artifact_id
    snapshot.context.execution_context["durable_execution_branch"] = artifact.branch
    snapshot.context.execution_context["durable_commit_sha"] = artifact.commit_sha
    snapshot.context.execution_context["durable_repo_url"] = artifact.repo_url
    return snapshot.dump()
