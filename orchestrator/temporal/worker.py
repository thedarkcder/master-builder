from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from orchestrator.core.config import get_settings
from orchestrator.core.observability.logging import configure_logging
from orchestrator.core.observability.otel_telemetry import initialize_telemetry
from orchestrator.temporal.client import connect_temporal_client, temporal_task_queue


async def run_temporal_worker() -> None:
    try:  # pragma: no cover - exercised when temporal backend is enabled
        from temporalio.worker import Worker
    except (
        ImportError
    ) as exc:  # pragma: no cover - exercised when temporal backend is enabled
        raise RuntimeError(
            "Temporal backend requires temporalio to be installed"
        ) from exc
    from orchestrator.temporal.telemetry import TemporalWorkerTelemetryInterceptor

    from orchestrator.temporal.activities.run_execution import (
        execute_claimed_run_activity,
        resume_human_input_activity,
    )
    from orchestrator.temporal.activities.handler_workflow import (
        process_handler_workflow_advance_activity,
        retry_handler_workflow_operation_activity,
    )
    from orchestrator.temporal.activities.project_deployment_setup import (
        run_project_deployment_setup_activity,
    )
    from orchestrator.temporal.workflows.development_team_run import (
        DevelopmentTeamRunWorkflow,
    )
    from orchestrator.temporal.workflows.handler_backed_workflow import (
        HandlerBackedWorkflow,
    )
    from orchestrator.temporal.workflows.project_deployment_setup import (
        ProjectDeploymentSetupWorkflow,
    )

    settings = get_settings()
    configure_logging(
        settings.log_level,
        environment=settings.sentry_environment,
        platform_version=settings.sentry_release or "dev-local",
        default_agent_id="temporal-orchestrator",
    )
    initialize_telemetry(settings=settings, service_name="temporal-orchestrator")
    client = await connect_temporal_client(settings)
    worker = Worker(
        client,
        task_queue=temporal_task_queue(settings),
        workflows=[
            DevelopmentTeamRunWorkflow,
            HandlerBackedWorkflow,
            ProjectDeploymentSetupWorkflow,
        ],
        activities=[
            execute_claimed_run_activity,
            resume_human_input_activity,
            process_handler_workflow_advance_activity,
            retry_handler_workflow_operation_activity,
            run_project_deployment_setup_activity,
        ],
        activity_executor=ThreadPoolExecutor(max_workers=4),
        interceptors=[TemporalWorkerTelemetryInterceptor()],
    )
    await worker.run()
