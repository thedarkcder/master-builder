from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import mean

from fastapi import HTTPException, status
from sqlalchemy import select

from orchestrator.api.schemas import (
    TokenHeavyCommandRead,
    TokenIssueStageUsageRead,
    TokenScatterPointRead,
    TokenStageDiagnosticRead,
    TokenStageDiagnosticsRead,
    TokenStageHeatmapCellRead,
)
from orchestrator.storage.models import Run, RunTokenUsage

_KNOWN_STAGES = ("pm", "dev", "test", "review", "orchestrated_run")
_MAX_DATE_SPAN = timedelta(days=180)
_STAGE_SPIKE_THRESHOLD = 2000
_UNCACHED_SPIKE_THRESHOLD = 1500


def _normalize_issue_keys(issue_key: str | None) -> list[str]:
    if not issue_key:
        return []
    return [value.strip() for value in issue_key.split(",") if value.strip()]


def _coerce_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
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


def _build_delta(prev: tuple[int, int, int] | None, current: tuple[int, int, int]) -> tuple[int, int, int]:
    if prev is None:
        return current
    prev_input, prev_cached, prev_output = prev
    return (
        max(0, current[0] - prev_input),
        max(0, (current[0] - current[1]) - (prev_input - prev_cached)),
        max(0, current[2] - prev_output),
    )


def _is_spike(delta_input: int, delta_uncached: int) -> bool:
    return delta_input >= _STAGE_SPIKE_THRESHOLD or delta_uncached >= _UNCACHED_SPIKE_THRESHOLD


def _load_rows(
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
                select(RunTokenUsage.run_id).where(RunTokenUsage.stage == "test").distinct()
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


def get_token_stage_diagnostics(
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
) -> TokenStageDiagnosticsRead:
    tenant_id = str(tenant_id).strip()
    project_id = str(project_id).strip()
    if not tenant_id or not project_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="tenant_id and project_id are required")

    if page < 1:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="page must be >= 1")
    if page_size < 1:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="page_size must be >= 1")
    if start_date and end_date and start_date > end_date:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="start_date must be <= end_date")
    if start_date and end_date and (end_date - start_date) > _MAX_DATE_SPAN:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="date range max is 180 days")

    rows = _load_rows(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        issue_key=issue_key,
        run_status=run_status,
        stage=stage,
        attempt=attempt,
        model=model,
        start_date=start_date,
        end_date=end_date,
        only_retried=only_retried,
        only_with_test_stage=only_with_test_stage,
    )
    run_ids = {str(row.run_id) for row in rows if row.run_id}
    run_records = {run.run_id: run for run in session.execute(select(Run).where(Run.run_id.in_(run_ids))).scalars().all()}

    stage_sums: dict[str, dict[str, list[int]]] = {
        key: {"delta": [], "uncached": [], "base": [], "retry": []}
        for key in _KNOWN_STAGES
    }
    run_sets_by_stage: dict[str, set[str]] = {key: set() for key in _KNOWN_STAGES}
    heavy_command_stats: dict[tuple[str, str], dict[str, int | float]] = {}
    scatter: list[TokenScatterPointRead] = []
    chain_prev: dict[tuple[str, str, str, int | None], tuple[int, int, int]] = {}
    heatmap_buckets: dict[tuple[str, int], dict[str, int]] = {}
    stage_attempt_delta: dict[tuple[str, str, int], int] = {}
    dev_attempt_seen: set[tuple[str, int]] = set()
    issue_stage_totals: dict[tuple[str, str], dict[str, int | set[str]]] = {}

    for row in rows:
        stage_name = str(row.stage or "").strip().lower()
        if stage_name not in stage_sums:
            continue

        run_id = str(row.run_id)
        run_sets_by_stage[stage_name].add(run_id)
        run_obj = run_records.get(run_id)
        issue_key_value = str(run_obj.issue_key) if run_obj else run_id

        input_tokens = _to_non_negative_int(row.input_tokens)
        cached_input_tokens = _to_non_negative_int(row.cached_input_tokens)
        output_tokens = _to_non_negative_int(row.output_tokens)

        chain_key = (run_id, str(row.invocation_id or "default"), stage_name, row.attempt)
        prev = chain_prev.get(chain_key)
        prev_input, prev_cached, prev_output = prev if prev is not None else (None, None, None)
        delta_input_raw, delta_uncached_raw, delta_output_raw = _build_delta(
            prev=(prev_input, prev_cached, prev_output) if prev is not None else None,
            current=(input_tokens, cached_input_tokens, output_tokens),
        )
        chain_prev[chain_key] = (input_tokens, cached_input_tokens, output_tokens)

        delta_input = _coalesce_delta(row.delta_input, delta_input_raw)
        delta_uncached = _coalesce_delta(row.delta_uncached, delta_uncached_raw)
        delta_output = _coalesce_delta(row.delta_output, delta_output_raw)
        stage_sums[stage_name]["delta"].append(delta_input)
        stage_sums[stage_name]["uncached"].append(delta_uncached)
        issue_bucket = issue_stage_totals.setdefault(
            (issue_key_value, stage_name),
            {
                "input": 0,
                "uncached_input": 0,
                "output": 0,
                "total_io": 0,
                "delta_total_io": 0,
                "run_ids": set(),
            },
        )
        uncached_input_tokens = max(0, input_tokens - cached_input_tokens)
        issue_bucket["input"] = int(issue_bucket["input"]) + input_tokens
        issue_bucket["uncached_input"] = int(issue_bucket["uncached_input"]) + uncached_input_tokens
        issue_bucket["output"] = int(issue_bucket["output"]) + output_tokens
        issue_bucket["total_io"] = int(issue_bucket["total_io"]) + input_tokens + output_tokens
        issue_bucket["delta_total_io"] = int(issue_bucket["delta_total_io"]) + delta_input + delta_output
        run_id_set = issue_bucket["run_ids"]
        if isinstance(run_id_set, set):
            run_id_set.add(run_id)
        attempt_bucket = row.attempt if row.attempt is not None and row.attempt > 0 else 1
        bucket = heatmap_buckets.setdefault(
            (stage_name, attempt_bucket),
            {"delta_sum": 0, "uncached_sum": 0, "count": 0},
        )
        bucket["delta_sum"] += delta_input
        bucket["uncached_sum"] += delta_uncached
        bucket["count"] += 1
        stage_attempt_delta[(run_id, stage_name, attempt_bucket)] = (
            stage_attempt_delta.get((run_id, stage_name, attempt_bucket), 0) + delta_input
        )
        if stage_name == "dev":
            dev_attempt_seen.add((run_id, attempt_bucket))

        if row.attempt is None or row.attempt <= 1:
            stage_sums[stage_name]["base"].append(delta_input)
        else:
            stage_sums[stage_name]["retry"].append(delta_input)

        if _is_spike(delta_input=delta_input, delta_uncached=delta_uncached):
            cmd_key = (str(row.command or "").strip() or "unknown", stage_name)
            entry = heavy_command_stats.setdefault(
                cmd_key,
                {"count": 0, "delta": 0.0, "uncached_delta": 0.0},
            )
            entry["count"] = int(entry["count"]) + 1
            entry["delta"] = float(entry["delta"]) + float(delta_input)
            entry["uncached_delta"] = float(entry["uncached_delta"]) + float(delta_uncached)

        if row.runtime_ms is not None and row.runtime_ms >= 0:
            scatter.append(
                TokenScatterPointRead(
                    run_id=run_id,
                    issue_key=issue_key_value,
                    stage=stage_name,
                    attempt=row.attempt,
                    runtime_ms=_to_non_negative_int(row.runtime_ms),
                    token_delta=delta_input,
                    recorded_at=_coerce_aware(row.recorded_at),
                )
            )

    stages: list[TokenStageDiagnosticRead] = []
    for stage_name in _KNOWN_STAGES:
        stats = stage_sums[stage_name]
        base = [float(x) for x in stats["base"] if x is not None]
        retry = [float(x) for x in stats["retry"] if x is not None]
        delta = [float(x) for x in stats["delta"] if x is not None]
        uncached = [float(x) for x in stats["uncached"] if x is not None]
        retry_index = _safe_ratio(mean(retry), mean(base)) if base and retry else 0.0
        stages.append(
            TokenStageDiagnosticRead(
                stage=stage_name,
                avg_delta=mean(delta) if delta else 0.0,
                avg_uncached_delta=mean(uncached) if uncached else 0.0,
                retry_impact_index=retry_index,
                run_count=len(run_sets_by_stage[stage_name]),
            )
        )

    heavy_commands = [
        TokenHeavyCommandRead(
            command_signature=command_signature,
            stage=stage_name,
            spike_count=int(values["count"]),
            avg_delta=values["delta"] / values["count"] if values["count"] else 0.0,
            avg_uncached_delta=values["uncached_delta"] / values["count"] if values["count"] else 0.0,
        )
        for (command_signature, stage_name), values in heavy_command_stats.items()
    ]
    heavy_commands.sort(key=lambda item: item.spike_count, reverse=True)
    heatmap_cells = [
        TokenStageHeatmapCellRead(
            stage=stage_name,
            attempt=attempt_bucket,
            avg_delta=float(values["delta_sum"]) / float(values["count"]) if values["count"] else 0.0,
            avg_uncached_delta=float(values["uncached_sum"]) / float(values["count"]) if values["count"] else 0.0,
            sample_count=int(values["count"]),
        )
        for (stage_name, attempt_bucket), values in heatmap_buckets.items()
    ]
    stage_order = {name: index for index, name in enumerate(_KNOWN_STAGES)}
    heatmap_cells.sort(key=lambda cell: (stage_order.get(cell.stage, 999), cell.attempt))
    issue_stage_usage = [
        TokenIssueStageUsageRead(
            issue_key=issue_key_value,
            stage=stage_name,
            input=int(values["input"]),
            uncached_input=int(values["uncached_input"]),
            output=int(values["output"]),
            total_io=int(values["total_io"]),
            delta_total_io=int(values["delta_total_io"]),
            run_count=len(values["run_ids"]) if isinstance(values["run_ids"], set) else 0,
        )
        for (issue_key_value, stage_name), values in issue_stage_totals.items()
    ]
    issue_stage_usage.sort(
        key=lambda item: (
            item.issue_key,
            stage_order.get(item.stage, 999),
            -item.total_io,
        )
    )

    repeated_test_delta = 0
    repeated_test_waste = 0
    for (run_id, stage_name, attempt_number), delta_value in stage_attempt_delta.items():
        if stage_name != "test" or attempt_number <= 1:
            continue
        repeated_test_delta += max(0, delta_value)
        if (run_id, attempt_number) not in dev_attempt_seen:
            repeated_test_waste += max(0, delta_value)
    retest_waste_score = _safe_ratio(repeated_test_waste, repeated_test_delta)

    scatter.sort(key=lambda item: item.recorded_at or datetime.min)
    start_idx = (page - 1) * page_size
    end_idx = start_idx + page_size
    return TokenStageDiagnosticsRead(
        stages=stages,
        heavy_commands=heavy_commands[start_idx:end_idx],
        scatter_points=scatter[start_idx:end_idx],
        heatmap=heatmap_cells,
        retest_waste_score=retest_waste_score,
        issue_stage_totals=issue_stage_usage,
    )
