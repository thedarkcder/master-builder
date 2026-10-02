from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from statistics import mean

from fastapi import HTTPException, status
from sqlalchemy import select

from orchestrator.api.schemas import (
    TokenAlertRead,
    TokenOverviewKpiRead,
    TokenOverviewRead,
    TokenOverviewSeriesByDayRead,
    TokenOverviewTopRunRead,
)
from orchestrator.storage.models import Run, RunTokenUsage

_RETRY_GROWTH_RATIO = 0.30
_DAY_SPAN_MAX = timedelta(days=180)
_STAGE_SPIKE_THRESHOLD = 2000
_UNCACHED_SPIKE_THRESHOLD = 1500
_CACHE_RATIO_COLLAPSE = 0.25


def _normalize_issue_keys(issue_key: str | None) -> list[str]:
    if not issue_key:
        return []
    return [value.strip() for value in issue_key.split(",") if value.strip()]


def _coerce_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _safe_ratio(numerator: float, denominator: float | int) -> float:
    if not denominator:
        return 0.0
    return max(0.0, numerator / float(denominator))


def _to_non_negative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return int(value) if value > 0 else 0
    return 0


def _coalesce_delta(stored: int | None, computed: int) -> int:
    if stored is None:
        return max(0, computed)
    return max(0, stored)


def _build_delta(
    prev: tuple[int, int, int] | None, current: tuple[int, int, int]
) -> tuple[int, int, int]:
    if prev is None:
        return current
    prev_input, prev_cached, prev_output = prev
    return (
        max(0, current[0] - prev_input),
        max(0, (current[0] - current[1]) - (prev_input - prev_cached)),
        max(0, current[2] - prev_output),
    )


def _spike_alerts(
    *,
    run_id: str,
    turn_id: str | None,
    stage: str,
    attempt: int | None,
    input_tokens: int,
    delta_input: int,
    delta_uncached: int,
    cache_ratio: float,
) -> list[TokenAlertRead]:
    alerts: list[TokenAlertRead] = []
    if delta_input >= _STAGE_SPIKE_THRESHOLD:
        alerts.append(
            TokenAlertRead(
                rule="stage_delta_spike_tokens",
                severity="warning",
                run_id=run_id,
                turn_id=str(turn_id or ""),
                stage=stage,
                attempt=attempt,
                value=delta_input,
                threshold=_STAGE_SPIKE_THRESHOLD,
                message=f"Turn delta input exceeded {_STAGE_SPIKE_THRESHOLD}.",
            )
        )
    if delta_uncached >= _UNCACHED_SPIKE_THRESHOLD:
        alerts.append(
            TokenAlertRead(
                rule="uncached_growth_spike_tokens",
                severity="warning",
                run_id=run_id,
                turn_id=str(turn_id or ""),
                stage=stage,
                attempt=attempt,
                value=delta_uncached,
                threshold=_UNCACHED_SPIKE_THRESHOLD,
                message=f"Uncached growth exceeded {_UNCACHED_SPIKE_THRESHOLD}.",
            )
        )
    if input_tokens > 0 and cache_ratio < _CACHE_RATIO_COLLAPSE:
        alerts.append(
            TokenAlertRead(
                rule="cache_ratio_collapse",
                severity="warning",
                run_id=run_id,
                turn_id=str(turn_id or ""),
                stage=stage,
                attempt=attempt,
                value=cache_ratio,
                threshold=_CACHE_RATIO_COLLAPSE,
                message="Cache ratio fell below 25% threshold.",
            )
        )
    return alerts


def _build_runs_query(
    *,
    session,
    tenant_id: str,
    project_id: str,
    issue_key: str | None,
    run_status: str | None,
    stage: str | None,
    attempt: int | None,
    model: str | None,
    start_date: datetime | None,
    end_date: datetime | None,
    only_retried: bool,
    only_with_test_stage: bool,
) -> list[RunTokenUsage]:
    query = (
        select(RunTokenUsage)
        .join(Run, Run.run_id == RunTokenUsage.run_id)
        .where(RunTokenUsage.recorded_at.is_not(None))
    )
    query = query.where(Run.tenant_id == tenant_id).where(Run.project_id == project_id)
    issue_keys = _normalize_issue_keys(issue_key)
    if issue_keys:
        if len(issue_keys) == 1:
            query = query.where(Run.issue_key == issue_keys[0])
        else:
            query = query.where(Run.issue_key.in_(issue_keys))
    if run_status:
        query = query.where(Run.status == run_status)
    if stage:
        query = query.where(RunTokenUsage.stage == str(stage).strip().lower())
    if attempt is not None:
        query = query.where(RunTokenUsage.attempt == attempt)
    if model:
        query = query.where(RunTokenUsage.model == str(model).strip())
    if start_date:
        query = query.where(RunTokenUsage.recorded_at >= start_date)
    if end_date:
        query = query.where(RunTokenUsage.recorded_at <= end_date)
    if only_retried:
        query = query.where(RunTokenUsage.attempt > 1)
    if only_with_test_stage:
        query = query.where(
            RunTokenUsage.run_id.in_(
                select(RunTokenUsage.run_id)
                .where(RunTokenUsage.stage == "test")
                .distinct()
            )
        )

    return (
        session.execute(
            query.order_by(
                RunTokenUsage.recorded_at.asc(),
                RunTokenUsage.run_id.asc(),
                RunTokenUsage.id.asc(),
            )
        )
        .scalars()
        .all()
    )


def _runtime_summary(values: Iterable[float]) -> tuple[float, float]:
    vals = [max(0.0, float(v)) for v in values if v is not None]
    if not vals:
        return 0.0, 0.0
    ordered = sorted(vals)
    avg_runtime = mean(ordered)
    p95_index = max(0, int(len(ordered) * 0.95) - 1)
    p95_runtime = ordered[min(p95_index, len(ordered) - 1)]
    return avg_runtime, float(p95_runtime)


def _distribution_summary(values: Iterable[float]) -> tuple[float, float]:
    vals = [max(0.0, float(v)) for v in values if v is not None]
    if not vals:
        return 0.0, 0.0
    ordered = sorted(vals)
    avg_value = mean(ordered)
    p95_index = max(0, int(len(ordered) * 0.95) - 1)
    return avg_value, float(ordered[min(p95_index, len(ordered) - 1)])


def get_token_overview(
    *,
    session,
    tenant_id: str,
    project_id: str,
    issue_key: str | None = None,
    run_status: str | None = None,
    stage: str | None = None,
    attempt: int | None = None,
    model: str | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    only_retried: bool = False,
    only_with_test_stage: bool = False,
    page: int = 1,
    page_size: int = 20,
) -> TokenOverviewRead:
    tenant_id = str(tenant_id).strip()
    project_id = str(project_id).strip()
    if not tenant_id or not project_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="tenant_id and project_id are required",
        )

    if page < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="page must be >= 1"
        )
    if page_size < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="page_size must be >= 1"
        )
    if start_date and end_date and start_date > end_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="start_date must be <= end_date",
        )

    start = _coerce_aware(start_date) if start_date is not None else None
    end = _coerce_aware(end_date) if end_date is not None else None
    if start and end and (end - start) > _DAY_SPAN_MAX:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="date range max is 180 days",
        )

    rows = _build_runs_query(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        issue_key=issue_key,
        run_status=run_status,
        stage=stage,
        attempt=attempt,
        model=model,
        start_date=start,
        end_date=end,
        only_retried=only_retried,
        only_with_test_stage=only_with_test_stage,
    )
    run_ids = {str(row.run_id) for row in rows if row.run_id}
    run_records: dict[str, Run] = {}
    if run_ids:
        run_rows = (
            session.execute(select(Run).where(Run.run_id.in_(run_ids))).scalars().all()
        )
        run_records = {row.run_id: row for row in run_rows}

    run_totals: dict[
        str,
        dict[str, int | float | str | None],
    ] = {}
    chain_prev: dict[tuple[str, str, int | None, str], tuple[int, int, int]] = {}
    prev_attempt_input: dict[tuple[str, str, str, int], int] = {}
    stage_attempt_delta: dict[tuple[str, str, int], int] = {}
    dev_attempt_seen: set[tuple[str, int]] = set()
    run_day_totals: dict[str, dict[str, int]] = {}
    alerts: list[TokenAlertRead] = []
    runtime_values: list[float] = []

    for row in rows:
        row_run_id = str(row.run_id)
        input_tokens = _to_non_negative_int(row.input_tokens)
        cached_input_tokens = _to_non_negative_int(row.cached_input_tokens)
        output_tokens = _to_non_negative_int(row.output_tokens)
        uncached_input_tokens = max(0, input_tokens - cached_input_tokens)
        day_bucket = _coerce_aware(row.recorded_at).date().isoformat()

        if row_run_id not in run_totals:
            run_obj = run_records.get(row_run_id)
            run_totals[row_run_id] = {
                "issue_key": str(run_obj.issue_key if run_obj else ""),
                "tenant_id": str(run_obj.tenant_id if run_obj else ""),
                "project_id": str(run_obj.project_id or ""),
                "status": str(run_obj.status if run_obj else ""),
                "input": 0,
                "cached_input": 0,
                "output": 0,
                "uncached_input": 0,
                "delta_input": 0,
                "delta_output": 0,
                "delta_uncached": 0,
            }

        accumulator = run_totals[row_run_id]
        accumulator["input"] = int(accumulator["input"]) + input_tokens
        accumulator["cached_input"] = (
            int(accumulator["cached_input"]) + cached_input_tokens
        )
        accumulator["output"] = int(accumulator["output"]) + output_tokens
        accumulator["uncached_input"] = (
            int(accumulator["uncached_input"]) + uncached_input_tokens
        )

        day_bucket_stats = run_day_totals.setdefault(
            day_bucket,
            {
                "total_input": 0,
                "total_uncached_input": 0,
                "total_output": 0,
                "total_io": 0,
                "delta_input": 0,
                "delta_output": 0,
                "delta_uncached": 0,
                "runtimes": [],
                "run_ids": set[str](),
            },
        )
        day_bucket_stats["total_input"] += input_tokens
        day_bucket_stats["total_uncached_input"] += uncached_input_tokens
        day_bucket_stats["total_output"] += output_tokens
        day_bucket_stats["total_io"] += input_tokens + output_tokens
        day_bucket_stats["run_ids"].add(row_run_id)
        if isinstance(row.runtime_ms, int | float) and row.runtime_ms is not None:
            runtime_values.append(float(row.runtime_ms))
            day_bucket_stats["runtimes"].append(float(row.runtime_ms))

        chain_key = (
            row_run_id,
            str(row.invocation_id or "default"),
            str(row.stage or "").strip().lower(),
            row.attempt,
        )
        prev = chain_prev.get(chain_key)
        prev_input, prev_cached, prev_output = (
            prev if prev is not None else (None, None, None)
        )
        delta_input_raw, delta_uncached_raw, delta_output_raw = _build_delta(
            prev=(prev_input, prev_cached, prev_output) if prev is not None else None,
            current=(input_tokens, cached_input_tokens, output_tokens),
        )
        delta_input = _coalesce_delta(row.delta_input, delta_input_raw)
        delta_uncached = _coalesce_delta(row.delta_uncached, delta_uncached_raw)
        delta_output = _coalesce_delta(row.delta_output, delta_output_raw)
        accumulator["delta_input"] = int(accumulator["delta_input"]) + delta_input
        accumulator["delta_output"] = int(accumulator["delta_output"]) + delta_output
        accumulator["delta_uncached"] = (
            int(accumulator["delta_uncached"]) + delta_uncached
        )
        day_bucket_stats["delta_input"] += delta_input
        day_bucket_stats["delta_output"] += delta_output
        day_bucket_stats["delta_uncached"] += delta_uncached

        chain_prev[chain_key] = (input_tokens, cached_input_tokens, output_tokens)

        cache_ratio = _safe_ratio(cached_input_tokens, input_tokens)
        alerts.extend(
            _spike_alerts(
                run_id=row_run_id,
                turn_id=str(row.turn_id or ""),
                stage=str(row.stage or ""),
                attempt=row.attempt,
                input_tokens=input_tokens,
                delta_input=delta_input,
                delta_uncached=delta_uncached,
                cache_ratio=cache_ratio,
            )
        )

        if row.stage == "test" and row.attempt and row.attempt > 1:
            previous_key = (
                row_run_id,
                str(row.invocation_id or "default"),
                "test",
                row.attempt - 1,
            )
            previous_input = prev_attempt_input.get(previous_key)
            if previous_input is not None and previous_input > 0:
                ratio = delta_input / float(previous_input)
                if ratio >= _RETRY_GROWTH_RATIO:
                    alerts.append(
                        TokenAlertRead(
                            rule="retry_test_growth_ratio",
                            severity="warning",
                            run_id=row_run_id,
                            turn_id=str(row.turn_id or ""),
                            stage="test",
                            attempt=row.attempt,
                            value=ratio,
                            threshold=_RETRY_GROWTH_RATIO,
                            message="Repeated test attempt grew inputs by 30%+ vs previous attempt.",
                        )
                    )
            prev_attempt_input[
                (row_run_id, str(row.invocation_id or "default"), "test", row.attempt)
            ] = input_tokens
        if row.attempt and row.attempt > 0:
            stage_name = str(row.stage or "").strip().lower()
            stage_attempt_delta[(row_run_id, stage_name, row.attempt)] = (
                stage_attempt_delta.get((row_run_id, stage_name, row.attempt), 0)
                + delta_input
            )
            if stage_name == "dev":
                dev_attempt_seen.add((row_run_id, row.attempt))

    kpi_input = sum(int(item["input"]) for item in run_totals.values())
    kpi_uncached = sum(int(item["uncached_input"]) for item in run_totals.values())
    kpi_output = sum(int(item["output"]) for item in run_totals.values())
    kpi_total_io = kpi_input + kpi_output
    runtime_avg, runtime_p95 = _runtime_summary(runtime_values)
    avg_io_per_run, p95_io_per_run = _distribution_summary(
        float(int(item["input"]) + int(item["output"])) for item in run_totals.values()
    )
    kpi_cache_ratio = _safe_ratio(
        sum(int(item["cached_input"]) for item in run_totals.values()),
        max(1, kpi_input),
    )
    repeated_test_delta = 0
    repeated_test_waste = 0
    for (
        run_id,
        stage_name,
        attempt_number,
    ), delta_value in stage_attempt_delta.items():
        if stage_name != "test" or attempt_number <= 1:
            continue
        repeated_test_delta += max(0, delta_value)
        if (run_id, attempt_number) not in dev_attempt_seen:
            repeated_test_waste += max(0, delta_value)
    retest_waste_score = _safe_ratio(repeated_test_waste, repeated_test_delta)

    series: list[TokenOverviewSeriesByDayRead] = []
    for day in sorted(run_day_totals.keys()):
        day_stats = run_day_totals[day]
        day_runtime = [float(v) for v in day_stats["runtimes"]]
        day_avg, day_p95 = _runtime_summary(day_runtime)
        series.append(
            TokenOverviewSeriesByDayRead(
                day=day,
                total_input=int(day_stats["total_input"]),
                total_uncached_input=int(day_stats["total_uncached_input"]),
                total_output=int(day_stats["total_output"]),
                total_io=int(day_stats["total_io"]),
                delta_input=int(day_stats["delta_input"]),
                delta_uncached=int(day_stats["delta_uncached"]),
                delta_output=int(day_stats["delta_output"]),
                delta_total_io=int(
                    day_stats["delta_input"] + day_stats["delta_output"]
                ),
                avg_runtime_ms=day_avg,
                p95_runtime_ms=day_p95,
                run_count=len(day_stats["run_ids"]),
            )
        )

    sorted_runs = sorted(
        run_totals.items(),
        key=lambda item: (
            int(item[1]["input"]) + int(item[1]["output"]),
            int(item[1]["delta_input"]) + int(item[1]["delta_output"]),
        ),
        reverse=True,
    )
    start_idx = (page - 1) * page_size
    end_idx = start_idx + page_size
    top_costly_runs = [
        TokenOverviewTopRunRead(
            run_id=run_id,
            issue_key=str(data["issue_key"]),
            tenant_id=str(data["tenant_id"]),
            project_id=str(data["project_id"]) if data["project_id"] else None,
            status=str(data["status"]),
            input=int(data["input"]),
            uncached_input=int(data["uncached_input"]),
            output=int(data["output"]),
            total_io=int(data["input"]) + int(data["output"]),
            delta_total_io=int(data["delta_input"]) + int(data["delta_output"]),
            cache_ratio=_safe_ratio(int(data["cached_input"]), int(data["input"])),
        )
        for run_id, data in sorted_runs[start_idx:end_idx]
    ]

    return TokenOverviewRead(
        kpis=TokenOverviewKpiRead(
            total_input=kpi_input,
            total_uncached_input=kpi_uncached,
            total_output=kpi_output,
            total_io=kpi_total_io,
            cache_ratio=kpi_cache_ratio,
            avg_runtime_ms=runtime_avg,
            p95_runtime_ms=runtime_p95,
            avg_io_per_run=avg_io_per_run,
            p95_io_per_run=p95_io_per_run,
            retest_waste_score=retest_waste_score,
        ),
        series_by_day=series,
        top_costly_runs=top_costly_runs,
        alerts=alerts,
    )
