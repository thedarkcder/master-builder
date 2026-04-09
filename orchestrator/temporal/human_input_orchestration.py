from __future__ import annotations

import asyncio
import threading

from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import Run
from orchestrator.temporal.client import connect_temporal_client
from orchestrator.temporal.human_input_payloads import HumanInputResumeResult
from orchestrator.temporal.team_run_payloads import TeamRunHumanInputInput
from orchestrator.temporal.team_run_orchestration import team_run_workflow_id


def temporal_orchestration_enabled(settings) -> bool:  # noqa: ANN001
    return str(getattr(settings, "orchestration_backend", "legacy") or "").strip().lower() == "temporal"


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


def resume_human_input_request_via_temporal(*, settings, request) -> HumanInputResumeResult:  # noqa: ANN001
    if not temporal_orchestration_enabled(settings):
        raise RuntimeError("Temporal orchestration backend is not enabled")
    result = _run_sync(_resume_human_input_request_via_temporal(settings=settings, request=request))
    if not isinstance(result, HumanInputResumeResult):
        raise RuntimeError("Temporal human input update returned an invalid result")
    return result


async def _resume_human_input_request_via_temporal(*, settings, request) -> HumanInputResumeResult:  # noqa: ANN001
    session_factory = create_session_factory()
    with session_factory() as session:
        source_run = session.get(Run, str(request.source_run_id or "").strip())
        source_plan = getattr(source_run, "plan", None) if source_run is not None else None
        has_team_run = (
            isinstance(source_plan, dict)
            and isinstance(source_plan.get("context"), dict)
            and isinstance(source_plan["context"].get("execution_context"), dict)
            and isinstance(source_plan["context"]["execution_context"].get("team_run"), dict)
        )

    client = await connect_temporal_client(settings)
    if not has_team_run:
        raise RuntimeError("Legacy workflow human-input resume is no longer supported")

    from orchestrator.temporal.workflows.team_run import TeamRunWorkflow

    handle = client.get_workflow_handle_for(
        TeamRunWorkflow,
        team_run_workflow_id(str(request.workflow_id)),
    )
    result = await handle.execute_update(
        TeamRunWorkflow.submit_human_input,
        TeamRunHumanInputInput(
            operation_id=str(request.request_id),
            request_id=str(request.request_id),
        ),
    )
    return HumanInputResumeResult(resumed_run_id=str(getattr(result, "run_id", "") or request.source_run_id))
