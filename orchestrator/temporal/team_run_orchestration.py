from __future__ import annotations

import asyncio
import threading
from uuid import uuid4

from orchestrator.temporal.client import connect_temporal_client, temporal_task_queue
from orchestrator.temporal.team_run_payloads import (
    TeamRunApprovalInput,
    TeamRunHumanInputInput,
    TeamRunTaskCompletionInput,
    TeamRunUpdateResult,
    TeamRunWorkflowInput,
)


def team_run_workflow_id(workflow_id: str) -> str:
    return f"team-run:{workflow_id}"


def _run_sync(awaitable):  # noqa: ANN001, ANN201
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)

    result: dict[str, object] = {}
    error: dict[str, BaseException] = {}

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(awaitable)
        except BaseException as exc:  # noqa: BLE001
            error["value"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()
    if "value" in error:
        raise error["value"]
    return result.get("value")


def start_team_run_workflow_for_run(*, settings, run) -> None:  # noqa: ANN001
    _run_sync(_start_team_run_workflow_for_run(settings=settings, run=run))


async def _start_team_run_workflow_for_run(*, settings, run) -> None:  # noqa: ANN001
    from temporalio.exceptions import WorkflowAlreadyStartedError

    from orchestrator.temporal.workflows.team_run import TeamRunWorkflow

    client = await connect_temporal_client(settings)
    workflow_input = TeamRunWorkflowInput(
        run_id=str(run.run_id),
        workflow_id=str(run.workflow_id),
        tenant_id=str(run.tenant_id),
        project_id=str(run.project_id or "").strip() or None,
        issue_key=str(run.issue_key),
    )
    try:
        await client.start_workflow(
            TeamRunWorkflow.run,
            workflow_input,
            id=team_run_workflow_id(str(run.workflow_id)),
            task_queue=temporal_task_queue(settings),
        )
    except WorkflowAlreadyStartedError:
        return


def complete_team_task_via_temporal(
    *,
    settings,
    workflow_id: str,
    run_id: str,
    task_key: str,
    artifact_payload: dict | None = None,
    summary: str | None = None,
) -> TeamRunUpdateResult:
    result = _run_sync(
        _complete_team_task_via_temporal(
            settings=settings,
            workflow_id=workflow_id,
            run_id=run_id,
            task_key=task_key,
            artifact_payload=artifact_payload,
            summary=summary,
        )
    )
    if not isinstance(result, TeamRunUpdateResult):
        raise RuntimeError("Temporal team task update returned an invalid result")
    return result


async def _complete_team_task_via_temporal(
    *,
    settings,
    workflow_id: str,
    run_id: str,
    task_key: str,
    artifact_payload: dict | None = None,
    summary: str | None = None,
) -> TeamRunUpdateResult:
    from orchestrator.temporal.workflows.team_run import TeamRunWorkflow

    client = await connect_temporal_client(settings)
    handle = client.get_workflow_handle_for(
        TeamRunWorkflow,
        team_run_workflow_id(str(workflow_id)),
    )
    return await handle.execute_update(
        TeamRunWorkflow.complete_task,
        TeamRunTaskCompletionInput(
            operation_id=uuid4().hex,
            run_id=str(run_id),
            task_key=str(task_key),
            artifact_payload=dict(artifact_payload or {}),
            summary=str(summary or "").strip() or None,
        ),
    )


def submit_team_approval_via_temporal(
    *,
    settings,
    workflow_id: str,
    run_id: str,
    task_key: str,
    decision: str,
    comment: str | None = None,
) -> TeamRunUpdateResult:
    result = _run_sync(
        _submit_team_approval_via_temporal(
            settings=settings,
            workflow_id=workflow_id,
            run_id=run_id,
            task_key=task_key,
            decision=decision,
            comment=comment,
        )
    )
    if not isinstance(result, TeamRunUpdateResult):
        raise RuntimeError("Temporal team approval update returned an invalid result")
    return result


def submit_team_human_input_via_temporal(
    *,
    settings,
    workflow_id: str,
    request_id: str,
) -> TeamRunUpdateResult:
    result = _run_sync(
        _submit_team_human_input_via_temporal(
            settings=settings,
            workflow_id=workflow_id,
            request_id=request_id,
        )
    )
    if not isinstance(result, TeamRunUpdateResult):
        raise RuntimeError("Temporal team human-input update returned an invalid result")
    return result


async def _submit_team_approval_via_temporal(
    *,
    settings,
    workflow_id: str,
    run_id: str,
    task_key: str,
    decision: str,
    comment: str | None = None,
) -> TeamRunUpdateResult:
    from orchestrator.temporal.workflows.team_run import TeamRunWorkflow

    client = await connect_temporal_client(settings)
    handle = client.get_workflow_handle_for(
        TeamRunWorkflow,
        team_run_workflow_id(str(workflow_id)),
    )
    return await handle.execute_update(
        TeamRunWorkflow.submit_approval,
        TeamRunApprovalInput(
            operation_id=uuid4().hex,
            run_id=str(run_id),
            task_key=str(task_key),
            decision=str(decision),
            comment=str(comment or "").strip() or None,
        ),
    )


async def _submit_team_human_input_via_temporal(
    *,
    settings,
    workflow_id: str,
    request_id: str,
) -> TeamRunUpdateResult:
    from orchestrator.temporal.workflows.team_run import TeamRunWorkflow

    client = await connect_temporal_client(settings)
    handle = client.get_workflow_handle_for(
        TeamRunWorkflow,
        team_run_workflow_id(str(workflow_id)),
    )
    return await handle.execute_update(
        TeamRunWorkflow.submit_human_input,
        TeamRunHumanInputInput(
            operation_id=uuid4().hex,
            request_id=str(request_id),
        ),
    )
