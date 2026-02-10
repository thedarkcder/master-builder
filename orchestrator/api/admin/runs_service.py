from __future__ import annotations

from fastapi import HTTPException, status


def list_runs(
    *,
    session,
    tenant_id: str | None,
    project_id: str | None,
    status_filter: str | None,
    from_time,
    to_time,
    build_runs_query_fn,
    run_to_schema_fn,
):  # noqa: ANN001
    query = build_runs_query_fn(
        tenant_id=tenant_id,
        project_id=project_id,
        status_filter=status_filter,
        from_time=from_time,
        to_time=to_time,
    )
    runs = session.execute(query).scalars().all()
    return [run_to_schema_fn(run) for run in runs]


def get_run(*, session, run_id: str, run_model, run_to_schema_fn):  # noqa: ANN001
    run = session.get(run_model, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    return run_to_schema_fn(run)
