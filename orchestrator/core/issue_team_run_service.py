from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from orchestrator.core.config import get_settings
from orchestrator.core.project_policy import resolve_effective_policy
from orchestrator.core.runs import (
    RUN_STATUS_BLOCKED,
    RUN_STATUS_FAILED,
    RUN_STATUS_RUNNING,
    RUN_STATUS_SUCCEEDED,
    RUN_STATUS_WAITING_FOR_INPUT,
    RunStateTransitionError,
)
from orchestrator.core.run_human_input_service import (
    INPUT_STATUS_ANSWERED,
    INPUT_STATUS_CONSUMED,
)
from orchestrator.core.followup_context_service import (
    CLOSED_FOLLOWUP_CONTEXT_STATUS,
    FOLLOWUP_CONTEXT_HUMAN_INPUT,
    close_followup_contexts,
)
from orchestrator.core.worker.run_lifecycle import bind_run_project, resolve_project_for_run
from orchestrator.core.worker.runtime_factory import build_codex_workflow_agents_for_session
from orchestrator.core.worker.workflow_request_service import build_workflow_request_for_run
from orchestrator.core.workflow.checkpoints import checkpoint_kind_for_stage, upsert_workflow_checkpoint
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.core.workflow.runner import (
    DevResult,
    PmPlan,
    ReviewResult,
    TestResult,
    WorkflowStageCheckpoint,
)
from orchestrator.core.worker_capability_normalization import parse_worker_capability
from orchestrator.storage.models import Run, RunHumanInputRequest, Tenant, WorkflowExecution


@dataclass(frozen=True)
class IssueTeamTaskExecution:
    task_key: str
    team_run: dict[str, Any]
    run_status: str
    workflow_status: str
    last_error: str | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _runtime_state(team_run: dict[str, Any]) -> dict[str, Any]:
    raw = team_run.get("runtime_state")
    return deepcopy(raw) if isinstance(raw, dict) else {}


def _save_runtime_state(team_run: dict[str, Any], runtime_state: dict[str, Any]) -> None:
    team_run["runtime_state"] = deepcopy(runtime_state)


def _nodes(team_run: dict[str, Any]) -> list[dict[str, Any]]:
    return [node for node in team_run.get("nodes", []) if isinstance(node, dict)]


def _node(team_run: dict[str, Any], task_key: str) -> dict[str, Any]:
    for node in _nodes(team_run):
        if str(node.get("task_key") or "").strip() == task_key:
            return node
    raise RunStateTransitionError(f"Team task not found: {task_key}")


def _set_node_status(team_run: dict[str, Any], task_key: str, status: str, *, attempt: int | None = None, summary: str | None = None) -> None:
    node = _node(team_run, task_key)
    node["status"] = status
    if attempt is not None:
        node["attempt"] = int(attempt)
    if summary is not None:
        node["summary"] = summary


def _append_artifact(team_run: dict[str, Any], *, task_key: str, artifact_type: str, payload: dict[str, Any], summary: str | None, attempt: int) -> None:
    artifacts = list(team_run.get("artifacts") or [])
    artifacts.append(
        {
            "task_key": task_key,
            "artifact_types": [artifact_type],
            "payload": deepcopy(payload),
            "summary": summary,
            "attempt": attempt,
            "recorded_at": _now_iso(),
        }
    )
    team_run["artifacts"] = artifacts


def _append_history(runtime_state: dict[str, Any], *, stage: str, attempt: int, event: str) -> None:
    history = [dict(item) for item in runtime_state.get("history", []) if isinstance(item, dict)]
    history.append({"stage": stage, "attempt": str(attempt), "event": event})
    runtime_state["history"] = history[-40:]


def _replace_stage_trace_entry(runtime_state: dict[str, Any], *, stage: str, attempt: int, status: str, summary: str) -> None:
    stage_trace = [dict(item) for item in runtime_state.get("stage_trace", []) if isinstance(item, dict)]
    stage_trace.append({"stage": stage, "status": status, "attempt": attempt, "summary": summary})
    runtime_state["stage_trace"] = stage_trace[-40:]


def _checkpoint_status_for_outcome(outcome: str) -> str:
    normalized = str(outcome or "").strip().lower()
    if normalized == "continue":
        return "completed"
    if normalized == "waiting_for_input":
        return "waiting_for_input"
    if normalized == "requeue":
        return "requeue"
    return normalized or "failed"


def _workflow_outcome_for_stage_outcome(outcome: str) -> str:
    normalized = str(outcome or "").strip().lower()
    if normalized == "continue":
        return "success"
    if normalized in {"requeue", "waiting_for_input", "blocked", "failed"}:
        return normalized
    return "failed"


def _sync_snapshot_runtime_state(snapshot: ExecutionSnapshot, runtime_state: dict[str, Any]) -> None:
    snapshot.events.stage_trace = [dict(item) for item in runtime_state.get("stage_trace", []) if isinstance(item, dict)]


def _record_checkpoint(
    *,
    session: Session,
    run: Run,
    snapshot: ExecutionSnapshot,
    checkpoint: WorkflowStageCheckpoint,
) -> None:
    snapshot.apply_stage_checkpoint(checkpoint)
    checkpoint_kind = checkpoint_kind_for_stage(checkpoint.stage)
    if checkpoint_kind is not None:
        upsert_workflow_checkpoint(
            session,
            workflow_id=run.workflow_id,
            run_id=run.run_id,
            checkpoint_kind=checkpoint_kind,
            stage=checkpoint.stage,
            payload=snapshot.dump(),
            now=_now(),
        )
    if checkpoint.stage == "dev" and checkpoint.dev_result is not None:
        run.pr_url = checkpoint.dev_result.pr_url
    elif checkpoint.stage == "review" and checkpoint.review_result is not None:
        run.pr_url = checkpoint.review_result.pr_url or run.pr_url


def _workflow_request(
    *,
    session: Session,
    run: Run,
) -> tuple[Tenant, object, object]:  # noqa: ANN401
    tenant = session.get(Tenant, run.tenant_id)
    if tenant is None:
        raise RunStateTransitionError(f"Tenant not found for run {run.run_id}")
    project = resolve_project_for_run(session, run=run)
    if project is None:
        raise RunStateTransitionError(f"No active project mapping found for issue {run.issue_key}")
    if bool(getattr(project, "is_archived", False)):
        raise RunStateTransitionError(f"Project {project.project_id} is archived")
    bind_run_project(session, run=run, project=project)
    effective_policy = resolve_effective_policy(
        tenant_policy=tenant.policy_config,
        project_overrides=project.policy_overrides,
    )
    request = build_workflow_request_for_run(
        session=session,
        tenant=tenant,
        run=run,
        project=project,
        effective_policy=effective_policy,
        settings=get_settings(),
    )
    return tenant, project, request


def _require_plan(snapshot: ExecutionSnapshot) -> PmPlan:
    plan = snapshot.plan()
    if plan is None:
        raise RunStateTransitionError("Issue workflow requires a persisted PM plan before execution can continue")
    return plan


def _require_dev_result(snapshot: ExecutionSnapshot) -> DevResult:
    result = snapshot.dev_result()
    if result is None:
        raise RunStateTransitionError("Issue workflow requires a persisted dev result before execution can continue")
    return result


def _require_test_result(snapshot: ExecutionSnapshot) -> TestResult:
    result = snapshot.test_result()
    if result is None:
        raise RunStateTransitionError("Issue workflow requires a persisted test result before execution can continue")
    return result


def _pm_summary(plan: PmPlan) -> str:
    if plan.plan_steps:
        return f"PM produced {len(plan.plan_steps)} execution steps and {len(plan.acceptance_criteria)} acceptance criteria."
    if plan.acceptance_criteria:
        return f"PM captured {len(plan.acceptance_criteria)} acceptance criteria."
    return "PM planning completed."


def _dev_summary(result: DevResult) -> str:
    summary = "; ".join(result.change_summary[:2]).strip()
    return summary or "Dev stage completed."


def _test_summary(result: TestResult) -> str:
    feedback = str(result.feedback or "").strip()
    if feedback:
        return feedback
    guidance = [str(item).strip() for item in result.guidance if str(item).strip()]
    return "; ".join(guidance[:3]) or "Test stage completed."


def _review_summary(result: ReviewResult) -> str:
    feedback = str(result.feedback or "").strip()
    if feedback:
        return feedback
    summary = [str(item).strip() for item in result.summary if str(item).strip()]
    return "; ".join(summary[:3]) or "Review stage completed."


def _capability_mismatch_message(*, request, plan: PmPlan) -> str | None:  # noqa: ANN001
    required = parse_worker_capability(plan.execution_worker_capability)
    current = parse_worker_capability(request.current_worker_capability)
    if required is None or current is None or required == current:
        return None
    return (
        f"Execution capability mismatch: PM selected {required.value} but current worker is {current.value}. "
        f"Run must execute on worker:{required.value}."
    )


def execute_issue_workflow_task(
    *,
    session: Session,
    run: Run,
    workflow: WorkflowExecution,
    snapshot: ExecutionSnapshot,
    team_run: dict[str, Any],
    task_key: str,
) -> IssueTeamTaskExecution:
    runtime_state = _runtime_state(team_run)
    runtime_state.setdefault("attempt", 1)
    runtime_state.setdefault("max_attempts", 1)
    runtime_state.setdefault("next_feedback", None)
    runtime_state.setdefault("history", [])
    runtime_state.setdefault("stage_trace", [])
    runtime_state.setdefault("test_guidance", [])
    runtime_state.setdefault("dev_rationale", [])
    runtime_state.setdefault("review_summary", [])
    runtime_state.setdefault("review_feedback", None)
    _sync_snapshot_runtime_state(snapshot, runtime_state)

    _, _, request = _workflow_request(session=session, run=run)
    agents = build_codex_workflow_agents_for_session(session=session)

    configured_max_attempts = max(1, int(getattr(request, "max_dev_test_review_loops", 1) or 1))
    runtime_state["max_attempts"] = max(1, int(runtime_state.get("max_attempts") or 1), configured_max_attempts)
    attempt = max(1, int(runtime_state.get("attempt") or 1))
    max_attempts = max(1, int(runtime_state.get("max_attempts") or 1))

    if task_key == "pm":
        plan = agents.pm(
            request,
            1,
            None,
            [dict(item) for item in runtime_state.get("history", []) if isinstance(item, dict)],
            snapshot.dev_result(),
            snapshot.test_result(),
            snapshot.review_result(),
        )
        summary = plan.blocker_message or _pm_summary(plan)
        _record_checkpoint(
            session=session,
            run=run,
            snapshot=snapshot,
            checkpoint=WorkflowStageCheckpoint(
                stage="pm",
                attempt=1,
                status=_checkpoint_status_for_outcome(plan.outcome),
                summary=summary,
                plan=plan,
            ),
        )
        _append_artifact(team_run, task_key="pm", artifact_type="pm_plan", payload=deepcopy(plan.__dict__), summary=summary, attempt=1)
        _replace_stage_trace_entry(runtime_state, stage="pm", attempt=1, status=_checkpoint_status_for_outcome(plan.outcome), summary=summary)
        runtime_state["attempt"] = 1
        runtime_state["next_feedback"] = None
        runtime_state["test_guidance"] = list(request.suggested_test_commands or runtime_state.get("test_guidance") or [])
        if plan.outcome == "continue":
            mismatch_message = _capability_mismatch_message(request=request, plan=plan)
            _set_node_status(team_run, "pm", "completed", attempt=1, summary=_pm_summary(plan))
            if mismatch_message is not None:
                _append_history(runtime_state, stage="pm", attempt=1, event=mismatch_message)
                _save_runtime_state(team_run, runtime_state)
                team_run["status"] = RUN_STATUS_BLOCKED
                snapshot.workflow.outcome = RUN_STATUS_BLOCKED
                snapshot.workflow.attempts = 1
                snapshot.workflow.blocker_message = mismatch_message
                return IssueTeamTaskExecution(
                    task_key="pm",
                    team_run=team_run,
                    run_status=RUN_STATUS_BLOCKED,
                    workflow_status=RUN_STATUS_BLOCKED,
                    last_error=mismatch_message,
                )
            _set_node_status(team_run, "dev", "ready", attempt=1)
            _save_runtime_state(team_run, runtime_state)
            snapshot.workflow.outcome = None
            snapshot.workflow.attempts = 1
            return IssueTeamTaskExecution(
                task_key="pm",
                team_run=team_run,
                run_status=RUN_STATUS_RUNNING,
                workflow_status=RUN_STATUS_RUNNING,
            )
        _set_node_status(team_run, "pm", _checkpoint_status_for_outcome(plan.outcome), attempt=1, summary=summary)
        _append_history(runtime_state, stage="pm", attempt=1, event=summary)
        _save_runtime_state(team_run, runtime_state)
        outcome = _workflow_outcome_for_stage_outcome(plan.outcome)
        snapshot.workflow.outcome = outcome
        snapshot.workflow.attempts = 1
        snapshot.workflow.blocker_message = summary if outcome in {RUN_STATUS_BLOCKED, RUN_STATUS_WAITING_FOR_INPUT} else None
        return IssueTeamTaskExecution(
            task_key="pm",
            team_run=team_run,
            run_status=RUN_STATUS_WAITING_FOR_INPUT if outcome == RUN_STATUS_WAITING_FOR_INPUT else (RUN_STATUS_BLOCKED if outcome == RUN_STATUS_BLOCKED else RUN_STATUS_FAILED),
            workflow_status=RUN_STATUS_WAITING_FOR_INPUT if outcome == RUN_STATUS_WAITING_FOR_INPUT else (RUN_STATUS_BLOCKED if outcome == RUN_STATUS_BLOCKED else RUN_STATUS_FAILED),
            last_error=summary,
        )

    plan = _require_plan(snapshot)

    if task_key == "dev":
        dev_result = agents.dev(request, plan, attempt, str(runtime_state.get("next_feedback") or "").strip() or None)
        summary = dev_result.blocker_message or _dev_summary(dev_result)
        _record_checkpoint(
            session=session,
            run=run,
            snapshot=snapshot,
            checkpoint=WorkflowStageCheckpoint(
                stage="dev",
                attempt=attempt,
                status=_checkpoint_status_for_outcome(dev_result.outcome),
                summary=summary,
                dev_result=dev_result,
            ),
        )
        _append_artifact(team_run, task_key="dev", artifact_type="dev_result", payload=deepcopy(dev_result.__dict__), summary=summary, attempt=attempt)
        _replace_stage_trace_entry(runtime_state, stage="dev", attempt=attempt, status=_checkpoint_status_for_outcome(dev_result.outcome), summary=summary)
        runtime_state["dev_rationale"] = list(dev_result.change_summary)
        if dev_result.outcome == "continue":
            runtime_state["next_feedback"] = None
            _set_node_status(team_run, "dev", "completed", attempt=attempt, summary=summary)
            _set_node_status(team_run, "test", "ready", attempt=attempt)
            _save_runtime_state(team_run, runtime_state)
            snapshot.workflow.outcome = None
            snapshot.workflow.attempts = attempt
            return IssueTeamTaskExecution(task_key="dev", team_run=team_run, run_status=RUN_STATUS_RUNNING, workflow_status=RUN_STATUS_RUNNING)
        _set_node_status(team_run, "dev", _checkpoint_status_for_outcome(dev_result.outcome), attempt=attempt, summary=summary)
        _append_history(runtime_state, stage="dev", attempt=attempt, event=summary)
        _save_runtime_state(team_run, runtime_state)
        outcome = _workflow_outcome_for_stage_outcome(dev_result.outcome)
        snapshot.workflow.outcome = outcome
        snapshot.workflow.attempts = attempt
        snapshot.workflow.blocker_message = summary
        return IssueTeamTaskExecution(
            task_key="dev",
            team_run=team_run,
            run_status=RUN_STATUS_WAITING_FOR_INPUT if outcome == RUN_STATUS_WAITING_FOR_INPUT else (RUN_STATUS_BLOCKED if outcome == RUN_STATUS_BLOCKED else RUN_STATUS_FAILED),
            workflow_status=RUN_STATUS_WAITING_FOR_INPUT if outcome == RUN_STATUS_WAITING_FOR_INPUT else (RUN_STATUS_BLOCKED if outcome == RUN_STATUS_BLOCKED else RUN_STATUS_FAILED),
            last_error=summary,
        )

    dev_result = _require_dev_result(snapshot)

    if task_key == "test":
        test_result = agents.test(request, plan, dev_result, attempt)
        summary = test_result.blocker_message or _test_summary(test_result)
        _record_checkpoint(
            session=session,
            run=run,
            snapshot=snapshot,
            checkpoint=WorkflowStageCheckpoint(
                stage="test",
                attempt=attempt,
                status=_checkpoint_status_for_outcome(test_result.outcome),
                summary=summary,
                test_result=test_result,
            ),
        )
        _append_artifact(team_run, task_key="test", artifact_type="test_result", payload=deepcopy(test_result.__dict__), summary=summary, attempt=attempt)
        _replace_stage_trace_entry(runtime_state, stage="test", attempt=attempt, status=_checkpoint_status_for_outcome(test_result.outcome), summary=summary)
        runtime_state["test_guidance"] = list(test_result.guidance or runtime_state.get("test_guidance") or [])
        if test_result.outcome == "continue":
            _set_node_status(team_run, "test", "completed", attempt=attempt, summary=summary)
            _set_node_status(team_run, "review", "ready", attempt=attempt)
            _save_runtime_state(team_run, runtime_state)
            snapshot.workflow.outcome = None
            snapshot.workflow.attempts = attempt
            return IssueTeamTaskExecution(task_key="test", team_run=team_run, run_status=RUN_STATUS_RUNNING, workflow_status=RUN_STATUS_RUNNING)
        if test_result.outcome == "failed" and attempt < max_attempts:
            runtime_state["next_feedback"] = summary
            runtime_state["attempt"] = attempt + 1
            _append_history(runtime_state, stage="test", attempt=attempt, event=summary)
            _set_node_status(team_run, "dev", "ready", attempt=attempt + 1)
            _set_node_status(team_run, "test", "pending", attempt=attempt + 1, summary=None)
            _set_node_status(team_run, "review", "pending", attempt=attempt + 1, summary=None)
            _save_runtime_state(team_run, runtime_state)
            snapshot.workflow.outcome = None
            snapshot.workflow.attempts = attempt
            return IssueTeamTaskExecution(task_key="test", team_run=team_run, run_status=RUN_STATUS_RUNNING, workflow_status=RUN_STATUS_RUNNING)
        _set_node_status(team_run, "test", _checkpoint_status_for_outcome(test_result.outcome), attempt=attempt, summary=summary)
        _append_history(runtime_state, stage="test", attempt=attempt, event=summary)
        _save_runtime_state(team_run, runtime_state)
        outcome = "failed" if test_result.outcome == "failed" else _workflow_outcome_for_stage_outcome(test_result.outcome)
        snapshot.workflow.outcome = outcome
        snapshot.workflow.attempts = attempt
        snapshot.workflow.blocker_message = summary
        return IssueTeamTaskExecution(
            task_key="test",
            team_run=team_run,
            run_status=RUN_STATUS_WAITING_FOR_INPUT if outcome == RUN_STATUS_WAITING_FOR_INPUT else (RUN_STATUS_BLOCKED if outcome == RUN_STATUS_BLOCKED else RUN_STATUS_FAILED),
            workflow_status=RUN_STATUS_WAITING_FOR_INPUT if outcome == RUN_STATUS_WAITING_FOR_INPUT else (RUN_STATUS_BLOCKED if outcome == RUN_STATUS_BLOCKED else RUN_STATUS_FAILED),
            last_error=summary,
        )

    test_result = _require_test_result(snapshot)
    review_result = agents.review(request, plan, dev_result, test_result, attempt)
    summary = review_result.blocker_message or _review_summary(review_result)
    _record_checkpoint(
        session=session,
        run=run,
        snapshot=snapshot,
        checkpoint=WorkflowStageCheckpoint(
            stage="review",
            attempt=attempt,
            status=_checkpoint_status_for_outcome(review_result.outcome),
            summary=summary,
            review_result=review_result,
        ),
    )
    _append_artifact(team_run, task_key="review", artifact_type="review_result", payload=deepcopy(review_result.__dict__), summary=summary, attempt=attempt)
    _replace_stage_trace_entry(runtime_state, stage="review", attempt=attempt, status=_checkpoint_status_for_outcome(review_result.outcome), summary=summary)
    runtime_state["review_summary"] = list(review_result.summary)
    runtime_state["review_feedback"] = review_result.feedback
    if review_result.outcome == "continue":
        snapshot.workflow.outcome = "success"
        snapshot.workflow.attempts = attempt
        snapshot.workflow.summary = list(review_result.summary or dev_result.change_summary or ["Workflow completed"])
        snapshot.workflow.blocker_message = None
        _set_node_status(team_run, "review", "completed", attempt=attempt, summary=summary)
        _save_runtime_state(team_run, runtime_state)
        return IssueTeamTaskExecution(
            task_key="review",
            team_run=team_run,
            run_status=RUN_STATUS_SUCCEEDED,
            workflow_status=RUN_STATUS_SUCCEEDED,
        )
    if review_result.outcome == "failed" and attempt < max_attempts:
        runtime_state["next_feedback"] = summary
        runtime_state["attempt"] = attempt + 1
        _append_history(runtime_state, stage="review", attempt=attempt, event=summary)
        _set_node_status(team_run, "dev", "ready", attempt=attempt + 1)
        _set_node_status(team_run, "test", "pending", attempt=attempt + 1, summary=None)
        _set_node_status(team_run, "review", "pending", attempt=attempt + 1, summary=None)
        _save_runtime_state(team_run, runtime_state)
        snapshot.workflow.outcome = None
        snapshot.workflow.attempts = attempt
        return IssueTeamTaskExecution(task_key="review", team_run=team_run, run_status=RUN_STATUS_RUNNING, workflow_status=RUN_STATUS_RUNNING)
    _set_node_status(team_run, "review", _checkpoint_status_for_outcome(review_result.outcome), attempt=attempt, summary=summary)
    _append_history(runtime_state, stage="review", attempt=attempt, event=summary)
    _save_runtime_state(team_run, runtime_state)
    outcome = "failed" if review_result.outcome == "failed" else _workflow_outcome_for_stage_outcome(review_result.outcome)
    snapshot.workflow.outcome = outcome
    snapshot.workflow.attempts = attempt
    snapshot.workflow.blocker_message = summary
    return IssueTeamTaskExecution(
        task_key="review",
        team_run=team_run,
        run_status=RUN_STATUS_WAITING_FOR_INPUT if outcome == RUN_STATUS_WAITING_FOR_INPUT else (RUN_STATUS_BLOCKED if outcome == RUN_STATUS_BLOCKED else RUN_STATUS_FAILED),
        workflow_status=RUN_STATUS_WAITING_FOR_INPUT if outcome == RUN_STATUS_WAITING_FOR_INPUT else (RUN_STATUS_BLOCKED if outcome == RUN_STATUS_BLOCKED else RUN_STATUS_FAILED),
        last_error=summary,
    )


def resume_issue_workflow_human_input(
    *,
    session: Session,
    run: Run,
    workflow: WorkflowExecution,
    snapshot: ExecutionSnapshot,
    team_run: dict[str, Any],
    request_id: str,
) -> IssueTeamTaskExecution:
    request = session.get(RunHumanInputRequest, request_id)
    if request is None:
        raise RunStateTransitionError(f"Human input request not found: {request_id}")
    if str(request.source_run_id or "").strip() != str(run.run_id):
        raise RunStateTransitionError("Human input request does not belong to this run")
    if str(request.status or "").strip().lower() == INPUT_STATUS_CONSUMED:
        return IssueTeamTaskExecution(task_key=str(request.source_stage or "").strip() or "pm", team_run=team_run, run_status=RUN_STATUS_RUNNING, workflow_status=RUN_STATUS_RUNNING)
    if str(request.status or "").strip().lower() != INPUT_STATUS_ANSWERED:
        raise RunStateTransitionError("Human input request is not answered")
    request.status = INPUT_STATUS_CONSUMED
    request.consumed_by_run_id = run.run_id
    request.updated_at = _now()
    close_followup_contexts(
        session=session,
        tenant_id=request.tenant_id,
        context_type=FOLLOWUP_CONTEXT_HUMAN_INPUT,
        request_id=request.request_id,
        status=CLOSED_FOLLOWUP_CONTEXT_STATUS,
    )
    normalized_stage = str(request.source_stage or "").strip().lower() or "pm"
    _set_node_status(team_run, normalized_stage, "ready")
    runtime_state = _runtime_state(team_run)
    _save_runtime_state(team_run, runtime_state)
    snapshot.workflow.outcome = None
    snapshot.workflow.blocker_message = None
    return IssueTeamTaskExecution(
        task_key=normalized_stage,
        team_run=team_run,
        run_status=RUN_STATUS_RUNNING,
        workflow_status=RUN_STATUS_RUNNING,
    )
