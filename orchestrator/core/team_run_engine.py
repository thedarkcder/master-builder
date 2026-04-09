from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from orchestrator.core.issue_team_run_service import (
    execute_issue_workflow_task,
    is_issue_workflow_executor_kind,
    resume_issue_workflow_human_input,
)
from orchestrator.core.platform_team_catalog_service import platform_team_catalog_service
from orchestrator.core.runs import (
    RUN_STATUS_BLOCKED,
    RUN_STATUS_RUNNING,
    RUN_STATUS_SUCCEEDED,
    RunStateTransitionError,
)
from orchestrator.core.team_run_state import (
    derive_team_run_status,
    first_ready_auto_team_task,
    promote_ready_team_run_nodes,
    team_run_node,
)
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.storage.models import Run, WorkflowExecution

TERMINAL_TEAM_RUN_STATUSES = {RUN_STATUS_SUCCEEDED, RUN_STATUS_BLOCKED, "failed", "cancelled"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TeamRunEngine:
    def _normalize_team_run(self, snapshot: ExecutionSnapshot) -> dict[str, Any]:
        team_run = platform_team_catalog_service.extract_team_run_from_plan(plan=snapshot.dump())
        if not isinstance(team_run, dict):
            raise RunStateTransitionError("Run does not contain a team_run snapshot")
        return deepcopy(team_run)

    def _load_run_bundle(self, *, session: Session, run_id: str) -> tuple[Run, WorkflowExecution, ExecutionSnapshot, dict[str, Any]]:
        run = session.get(Run, run_id)
        if run is None:
            raise RunStateTransitionError(f"Run not found: {run_id}")
        workflow = session.get(WorkflowExecution, run.workflow_id)
        if workflow is None:
            raise RunStateTransitionError(f"Workflow not found for run {run_id}")
        snapshot = ExecutionSnapshot.require(run.plan)
        team_run = self._normalize_team_run(snapshot)
        return run, workflow, snapshot, team_run

    def _persist_team_run(
        self,
        *,
        session: Session,
        run: Run,
        workflow: WorkflowExecution,
        snapshot: ExecutionSnapshot,
        team_run: dict[str, Any],
        run_status: str,
        workflow_status: str,
        last_error: str | None = None,
    ) -> Run:
        now = datetime.now(timezone.utc)
        snapshot.context.execution_context["team_run"] = deepcopy(team_run)
        run.plan = snapshot.dump()
        run.status = run_status
        run.last_error = last_error
        run.started_at = run.started_at or now
        run.finished_at = now if run_status in TERMINAL_TEAM_RUN_STATUSES else None
        workflow.status = workflow_status
        workflow.last_error = last_error
        workflow.active_run_id = run.run_id
        workflow.updated_at = now
        workflow.finished_at = run.finished_at
        workflow.blocked_reason = last_error if workflow_status == RUN_STATUS_BLOCKED else None
        session.add(run)
        session.add(workflow)
        session.commit()
        session.refresh(run)
        return run

    @staticmethod
    def _find_approval(team_run: dict[str, Any], task_key: str) -> dict[str, Any] | None:
        for approval in team_run.get("approvals", []):
            if not isinstance(approval, dict):
                continue
            if str(approval.get("task_key") or "").strip() == task_key:
                return approval
        return None

    def initialize(self, *, session: Session, run_id: str) -> Run:
        run, workflow, snapshot, team_run = self._load_run_bundle(session=session, run_id=run_id)
        team_run["status"] = RUN_STATUS_RUNNING
        promote_ready_team_run_nodes(team_run)
        return self._persist_team_run(
            session=session,
            run=run,
            workflow=workflow,
            snapshot=snapshot,
            team_run=team_run,
            run_status=RUN_STATUS_RUNNING,
            workflow_status=RUN_STATUS_RUNNING,
        )

    def advance_auto(self, *, session: Session, run_id: str) -> tuple[Run, str | None]:
        run, workflow, snapshot, team_run = self._load_run_bundle(session=session, run_id=run_id)
        node = first_ready_auto_team_task(team_run)
        if node is None:
            return run, None
        task_key = str(node.get("task_key") or "").strip()
        executor_kind = str(node.get("executor_kind") or "").strip().lower()
        if not executor_kind:
            return run, None
        if is_issue_workflow_executor_kind(executor_kind):
            execution = execute_issue_workflow_task(
                session=session,
                run=run,
                workflow=workflow,
                snapshot=snapshot,
                team_run=team_run,
                task_key=task_key,
            )
            return (
                self._persist_team_run(
                    session=session,
                    run=run,
                    workflow=workflow,
                    snapshot=snapshot,
                    team_run=execution.team_run,
                    run_status=execution.run_status,
                    workflow_status=execution.workflow_status,
                    last_error=execution.last_error,
                ),
                task_key,
            )
        node["status"] = "blocked"
        unsupported_executor_error = f"Unsupported team task executor for task {task_key}: {executor_kind}"
        node["summary"] = unsupported_executor_error
        team_run["status"] = RUN_STATUS_BLOCKED
        return (
            self._persist_team_run(
                session=session,
                run=run,
                workflow=workflow,
                snapshot=snapshot,
                team_run=team_run,
                run_status=RUN_STATUS_BLOCKED,
                workflow_status=RUN_STATUS_BLOCKED,
                last_error=unsupported_executor_error,
            ),
            task_key,
        )

    def complete_task(
        self,
        *,
        session: Session,
        run_id: str,
        task_key: str,
        artifact_payload: dict[str, Any] | None = None,
        summary: str | None = None,
    ) -> Run:
        run, workflow, snapshot, team_run = self._load_run_bundle(session=session, run_id=run_id)
        node = team_run_node(team_run, task_key)
        current_status = str(node.get("status") or "").strip().lower()
        if current_status not in {"ready", "running"}:
            raise RunStateTransitionError(f"Task {task_key} is not ready to complete")
        produced_artifact_types = [
            str(item or "").strip()
            for item in list((node.get("artifact_contract") or {}).get("produces") or [])
            if str(item or "").strip()
        ]
        if produced_artifact_types or artifact_payload or summary:
            artifacts = list(team_run.get("artifacts") or [])
            artifacts.append(
                {
                    "task_key": task_key,
                    "artifact_types": produced_artifact_types,
                    "payload": dict(artifact_payload or {}),
                    "summary": str(summary or "").strip() or None,
                    "recorded_at": _now_iso(),
                }
            )
            team_run["artifacts"] = artifacts
        approval_type = str((node.get("approval_rule") or {}).get("type") or "").strip().lower()
        if approval_type == "manual":
            node["status"] = "awaiting_approval"
            approvals = list(team_run.get("approvals") or [])
            approvals = [
                approval
                for approval in approvals
                if not (isinstance(approval, dict) and str(approval.get("task_key") or "").strip() == task_key)
            ]
            approvals.append(
                {
                    "task_key": task_key,
                    "status": "pending",
                    "decision": None,
                    "comment": None,
                    "requested_at": _now_iso(),
                }
            )
            team_run["approvals"] = approvals
            team_run["status"] = RUN_STATUS_RUNNING
            return self._persist_team_run(
                session=session,
                run=run,
                workflow=workflow,
                snapshot=snapshot,
                team_run=team_run,
                run_status=RUN_STATUS_RUNNING,
                workflow_status=RUN_STATUS_RUNNING,
            )
        node["status"] = "completed"
        promote_ready_team_run_nodes(team_run)
        run_status, workflow_status, last_error = derive_team_run_status(team_run)
        team_run["status"] = run_status
        return self._persist_team_run(
            session=session,
            run=run,
            workflow=workflow,
            snapshot=snapshot,
            team_run=team_run,
            run_status=run_status,
            workflow_status=workflow_status,
            last_error=last_error,
        )

    def submit_approval(
        self,
        *,
        session: Session,
        run_id: str,
        task_key: str,
        decision: str,
        comment: str | None = None,
    ) -> Run:
        run, workflow, snapshot, team_run = self._load_run_bundle(session=session, run_id=run_id)
        node = team_run_node(team_run, task_key)
        if str(node.get("status") or "").strip().lower() != "awaiting_approval":
            raise RunStateTransitionError(f"Task {task_key} is not awaiting approval")
        approval = self._find_approval(team_run, task_key)
        if approval is None or str(approval.get("status") or "").strip().lower() != "pending":
            raise RunStateTransitionError(f"Task {task_key} does not have a pending approval")
        normalized_decision = str(decision or "").strip().lower()
        if normalized_decision not in {"approved", "rejected"}:
            raise RunStateTransitionError("Approval decision must be approved or rejected")
        approval["decision"] = normalized_decision
        approval["comment"] = str(comment or "").strip() or None
        approval["recorded_at"] = _now_iso()
        if normalized_decision == "approved":
            approval["status"] = "approved"
            node["status"] = "completed"
            promote_ready_team_run_nodes(team_run)
            run_status, workflow_status, last_error = derive_team_run_status(team_run)
            team_run["status"] = run_status
            return self._persist_team_run(
                session=session,
                run=run,
                workflow=workflow,
                snapshot=snapshot,
                team_run=team_run,
                run_status=run_status,
                workflow_status=workflow_status,
                last_error=last_error,
            )
        approval["status"] = "rejected"
        node["status"] = "blocked"
        team_run["status"] = RUN_STATUS_BLOCKED
        rejection_reason = approval["comment"] or f"Approval rejected for task {task_key}"
        return self._persist_team_run(
            session=session,
            run=run,
            workflow=workflow,
            snapshot=snapshot,
            team_run=team_run,
            run_status=RUN_STATUS_BLOCKED,
            workflow_status=RUN_STATUS_BLOCKED,
            last_error=str(rejection_reason),
        )

    def resume_human_input(
        self,
        *,
        session: Session,
        request_id: str,
    ) -> Run:
        from orchestrator.storage.models import RunHumanInputRequest

        request = session.get(RunHumanInputRequest, request_id)
        if request is None:
            raise RunStateTransitionError(f"Human input request not found: {request_id}")
        run, workflow, snapshot, team_run = self._load_run_bundle(session=session, run_id=str(request.source_run_id))
        execution = resume_issue_workflow_human_input(
            session=session,
            run=run,
            workflow=workflow,
            snapshot=snapshot,
            team_run=team_run,
            request_id=request_id,
        )
        return self._persist_team_run(
            session=session,
            run=run,
            workflow=workflow,
            snapshot=snapshot,
            team_run=execution.team_run,
            run_status=execution.run_status,
            workflow_status=execution.workflow_status,
            last_error=execution.last_error,
        )


team_run_engine = TeamRunEngine()
