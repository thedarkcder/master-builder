from __future__ import annotations

from orchestrator.core.workflow_advance import execute_workflow_advance
from orchestrator.core.workflow_type_catalog import get_workflow_type_by_handler_key


def skip_product_event_notification(
    *,
    event_class: str,
    event_sequence: int,
    tenant_id: str,
    workflow_id: str | None,
    run_id: str | None,
    operation_id: str | None,
    attempt_id: str | None,
) -> None:
    del event_class, event_sequence, tenant_id, workflow_id, run_id, operation_id, attempt_id


def build_local_workflow_runtime(
    *,
    session,
    settings,
    process_claimed_run_fn,
    build_runner_fn,
    runtime_kwargs_fn,
    resolve_advance_handler_fn,
    workflow_handler_registry,
):  # noqa: ANN001, ANN202
    del process_claimed_run_fn, build_runner_fn, runtime_kwargs_fn, workflow_handler_registry

    class _LocalWorkflowRuntime:
        def advance(self, *, request):  # noqa: ANN001, ANN202
            workflow_type = get_workflow_type_by_handler_key(
                session,
                handler_key=request.workflow_handler_key,
            )
            return execute_workflow_advance(
                session=session,
                settings=settings,
                workflow_type=workflow_type,
                request=request,
                resolve_advance_handler_fn=resolve_advance_handler_fn,
            )

    return _LocalWorkflowRuntime()
