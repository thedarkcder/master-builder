from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import select

from orchestrator.api.admin.token_usage_backfill import materialize_token_usage_for_scope
from orchestrator.api.schemas import (
    TokenCompareRead,
    TokenCompareRequest,
    TokenCompareRunRead,
    TokenCompareRunTotalsRead,
    TokenCompareStageTotalsRead,
    TokenCompareWaterfallStepRead,
)
from orchestrator.storage.models import Run, RunTokenUsage

_MAX_RUNS = 20
_STAGE_SPIKE_THRESHOLD = 2000
_UNCACHED_SPIKE_THRESHOLD = 1500


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


def _build_spike_reasons(*, delta_input: int, delta_uncached: int) -> list[str]:
    reasons: list[str] = []
    if delta_input >= _STAGE_SPIKE_THRESHOLD:
        reasons.append(f"stage_delta_spike_{_STAGE_SPIKE_THRESHOLD}")
    if delta_uncached >= _UNCACHED_SPIKE_THRESHOLD:
        reasons.append(f"uncached_growth_spike_{_UNCACHED_SPIKE_THRESHOLD}")
    return reasons


def _load_run_rows(
    *,
    session,
    run_ids: list[str],
) -> list[RunTokenUsage]:
    return (
        session.execute(
            select(RunTokenUsage)
            .where(RunTokenUsage.run_id.in_(run_ids))
            .order_by(RunTokenUsage.recorded_at.asc(), RunTokenUsage.run_id.asc(), RunTokenUsage.id.asc())
        )
        .scalars()
        .all()
    )


def _load_runs(session, run_ids: list[str]) -> dict[str, Run]:
    run_rows = session.execute(select(Run).where(Run.run_id.in_(run_ids))).scalars().all()
    return {row.run_id: row for row in run_rows}


def compare_run_tokens(
    *,
    session,
    payload: TokenCompareRequest,
    tenant_id: str,
    project_id: str,
) -> TokenCompareRead:
    run_ids = [str(run_id).strip() for run_id in payload.run_ids if str(run_id).strip()]
    if len(run_ids) < 2:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="run_ids requires at least 2 runs")
    if len(run_ids) > _MAX_RUNS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Too many runs; max is 20")

    align_by = (payload.align_by or "turn_sequence").strip().lower()
    if align_by not in {"turn_sequence", "recorded_at"}:
        align_by = "turn_sequence"
    tenant_id = str(tenant_id).strip()
    project_id = str(project_id).strip()
    if not tenant_id or not project_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="tenant_id and project_id are required")

    run_rows = _load_runs(session, run_ids)
    for requested_id in run_ids:
        if requested_id not in run_rows:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run not found: {requested_id}")
    mismatched_tenant_runs = [run_id for run_id, row in run_rows.items() if str(row.tenant_id or "").strip() != tenant_id]
    if mismatched_tenant_runs:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="All runs must belong to the selected tenant.",
        )
    mismatched_project_runs = [
        run_id for run_id, row in run_rows.items() if str(row.project_id or "").strip() != project_id
    ]
    if mismatched_project_runs:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="All runs must belong to the selected project.",
        )

    rows = _load_run_rows(session=session, run_ids=run_ids)
    if not rows:
        materialize_token_usage_for_scope(
            session=session,
            tenant_id=tenant_id,
            project_id=project_id,
            run_ids=run_ids,
        )
        rows = _load_run_rows(session=session, run_ids=run_ids)

    totals_by_run: dict[str, dict[str, int]] = {
        run_id: {
            "input": 0,
            "cached_input": 0,
            "output": 0,
            "uncached_input": 0,
        }
        for run_id in run_ids
    }
    stage_totals_by_run: dict[str, dict[str, dict[str, int]]] = {run_id: {} for run_id in run_ids}
    turns_by_run: dict[str, list[TokenCompareWaterfallStepRead]] = {run_id: [] for run_id in run_ids}
    chain_prev: dict[tuple[str, str, str, int | None], tuple[int, int, int]] = {}
    align_axis: list[int] = []
    axis_lookup: dict[str, int] = {}
    run_turn_index: dict[str, int] = {run_id: 0 for run_id in run_ids}

    if align_by == "recorded_at":
        axis_source_rows = sorted(rows, key=lambda row: _coerce_aware(row.recorded_at) or datetime.min)
        for row in axis_source_rows:
            timestamp = str(_coerce_aware(row.recorded_at).isoformat())
            if timestamp not in axis_lookup:
                axis_lookup[timestamp] = len(axis_lookup) + 1
                align_axis.append(axis_lookup[timestamp])
        align_axis = sorted(set(align_axis))

    for row in rows:
        run_id = str(row.run_id)
        if align_by == "turn_sequence":
            run_turn_index[run_id] += 1
            turn_order = run_turn_index[run_id]
        else:
            timestamp = str(_coerce_aware(row.recorded_at).isoformat())
            turn_order = axis_lookup.get(timestamp)
            if turn_order is None:
                turn_order = len(axis_lookup) + 1
                axis_lookup[timestamp] = turn_order
                align_axis = sorted(set(align_axis + [turn_order]))

        input_tokens = _to_non_negative_int(row.input_tokens)
        cached_input_tokens = _to_non_negative_int(row.cached_input_tokens)
        output_tokens = _to_non_negative_int(row.output_tokens)
        uncached_input_tokens = max(0, input_tokens - cached_input_tokens)

        chain_key = (
            run_id,
            str(row.invocation_id or "default"),
            str(row.stage or "").strip().lower(),
            row.attempt,
        )
        prev = chain_prev.get(chain_key)
        delta_input_raw, delta_uncached_raw, _ = _build_delta(
            prev=prev,
            current=(input_tokens, cached_input_tokens, output_tokens),
        )
        chain_prev[chain_key] = (input_tokens, cached_input_tokens, output_tokens)

        delta_input = _coalesce_delta(row.delta_input, delta_input_raw)
        delta_uncached = _coalesce_delta(row.delta_uncached, delta_uncached_raw)

        run_total = totals_by_run[run_id]
        run_total["input"] += input_tokens
        run_total["cached_input"] += cached_input_tokens
        run_total["output"] += output_tokens
        run_total["uncached_input"] += uncached_input_tokens

        stage_key = str(row.stage or "").strip().lower()
        stage_totals = stage_totals_by_run[run_id].setdefault(
            stage_key,
            {"input": 0, "uncached_input": 0, "output": 0},
        )
        stage_totals["input"] += input_tokens
        stage_totals["uncached_input"] += uncached_input_tokens
        stage_totals["output"] += output_tokens

        turns_by_run[run_id].append(
            TokenCompareWaterfallStepRead(
                run_id=run_id,
                turn_order=turn_order,
                turn_id=str(row.turn_id or ""),
                recorded_at=_coerce_aware(row.recorded_at),
                stage=stage_key,
                attempt=row.attempt,
                delta_input=delta_input,
                uncached_delta=delta_uncached,
                output_tokens=output_tokens,
                delta_reason=_build_spike_reasons(delta_input=delta_input, delta_uncached=delta_uncached),
            )
        )

    if align_by == "turn_sequence":
        max_turns = max(run_turn_index.values(), default=0)
        align_axis = list(range(1, max_turns + 1))
    elif not align_axis:
        align_axis = [value for _, value in sorted(axis_lookup.items(), key=lambda item: item[1])]

    runs: list[TokenCompareRunRead] = []
    for run_id in run_ids:
        run_obj = run_rows[run_id]
        run_total = totals_by_run[run_id]
        input_total = int(run_total["input"])
        cached_total = int(run_total["cached_input"])
        output_total = int(run_total["output"])
        total_io = input_total + output_total
        runs.append(
            TokenCompareRunRead(
                run_id=run_id,
                issue_key=str(run_obj.issue_key or run_id),
                status=str(run_obj.status),
                totals=TokenCompareRunTotalsRead(
                    input=input_total,
                    uncached_input=int(run_total["uncached_input"]),
                    output=output_total,
                    cached_input=cached_total,
                    total_io=total_io,
                    cache_ratio=_safe_ratio(cached_total, input_total),
                ),
                stage_totals=[
                    TokenCompareStageTotalsRead(
                        stage=stage_name,
                        input=int(stage_totals["input"]),
                        uncached_input=int(stage_totals["uncached_input"]),
                        output=int(stage_totals["output"]),
                        total_io=int(stage_totals["input"]) + int(stage_totals["output"]),
                    )
                    for stage_name, stage_totals in stage_totals_by_run[run_id].items()
                ],
            )
        )

    waterfall: list[TokenCompareWaterfallStepRead] = []
    for run_id in run_ids:
        waterfall.extend(turns_by_run[run_id])
    waterfall.sort(key=lambda step: (step.turn_order, step.run_id, step.recorded_at))
    return TokenCompareRead(runs=runs, align_axis=align_axis, waterfall=waterfall)
