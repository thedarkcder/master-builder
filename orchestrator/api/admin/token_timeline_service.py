from __future__ import annotations

from datetime import datetime, timezone
from statistics import mean

from fastapi import HTTPException, status
from sqlalchemy import select

from orchestrator.api.admin.token_usage_backfill import materialize_token_usage_for_scope
from orchestrator.api.schemas import (
    TokenTimelineRead,
    TokenTimelineTotalsRead,
    TokenTimelineTurnRead,
)
from orchestrator.storage.models import Run, RunTokenUsage


_STAGE_SPIKE_THRESHOLD = 2000
_UNCACHED_SPIKE_THRESHOLD = 1500
_CACHE_RATIO_COLLAPSE = 0.25


def _safe_ratio(numerator: float, denominator: float | int) -> float:
    if not denominator:
        return 0.0
    return max(0.0, numerator / float(denominator))


def _normalize_chain_id(row: RunTokenUsage) -> tuple[str, str, int | None]:
    return (str(row.invocation_id or "").strip() or "default", str(row.stage or "").strip().lower(), row.attempt)


def _to_non_negative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return int(value) if value > 0 else 0
    return 0


def _build_spike_reasons(*, delta_input: int, delta_uncached: int, cache_ratio: float, input_tokens: int) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if delta_input >= _STAGE_SPIKE_THRESHOLD:
        reasons.append(f"stage_delta_spike_{_STAGE_SPIKE_THRESHOLD}")
    if delta_uncached >= _UNCACHED_SPIKE_THRESHOLD:
        reasons.append(f"uncached_growth_spike_{_UNCACHED_SPIKE_THRESHOLD}")
    if input_tokens > 0 and cache_ratio < _CACHE_RATIO_COLLAPSE:
        reasons.append(f"cache_ratio_collapse_{int(_CACHE_RATIO_COLLAPSE * 100)}")
    return bool(reasons), reasons


def _ensure_aware_timestamp(value: datetime | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc).isoformat()
    return value.isoformat()


def _coalesce_delta(stored: int | None, computed: int) -> int:
    if stored is None:
        return max(0, computed)
    return max(0, stored)


def _build_delta(prev: tuple[int, int, int] | None, current: tuple[int, int, int]) -> tuple[int, int, int]:
    if prev is None:
        return current
    prev_input, prev_cached, prev_output = prev
    input_delta = current[0] - prev_input
    uncached_delta = max(0, (current[0] - current[1]) - (prev_input - prev_cached))
    output_delta = current[2] - prev_output
    return (
        max(0, input_delta),
        max(0, uncached_delta),
        max(0, output_delta),
    )


def _ensure_float_list(values: list[float], *, default: float = 0.0) -> list[float]:
    if not values:
        return [default]
    return values


def _load_rows(
    *,
    session,
    run_id: str,
    stage: str | None = None,
    attempt: int | None = None,
    include_retries: bool = False,
    model: str | None = None,
):
    query = (
        select(RunTokenUsage)
        .where(RunTokenUsage.run_id == run_id)
        .where(RunTokenUsage.turn_id.is_not(None))
    )
    if stage:
        query = query.where(RunTokenUsage.stage == str(stage).strip().lower())
    if attempt is not None:
        query = query.where(RunTokenUsage.attempt == attempt)
    elif not include_retries:
        query = query.where((RunTokenUsage.attempt.is_(None)) | (RunTokenUsage.attempt <= 1))
    if model:
        query = query.where(RunTokenUsage.model == str(model).strip())
    return session.execute(query.order_by(RunTokenUsage.recorded_at.asc(), RunTokenUsage.id.asc())).scalars().all()


def get_run_token_timeline(
    *,
    session,
    run_id: str,
    tenant_id: str,
    stage: str | None = None,
    attempt: int | None = None,
    include_retries: bool = False,
    model: str | None = None,
) -> TokenTimelineRead:
    tenant_id = str(tenant_id).strip()
    if not tenant_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="tenant_id is required")

    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    if str(run.tenant_id or "").strip() != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    rows = _load_rows(
        session=session,
        run_id=run_id,
        stage=stage,
        attempt=attempt,
        include_retries=include_retries,
        model=model,
    )
    if not rows:
        materialize_token_usage_for_scope(
            session=session,
            tenant_id=tenant_id,
            project_id=str(run.project_id or "").strip(),
            run_ids=[run_id],
        )
        rows = _load_rows(
            session=session,
            run_id=run_id,
            stage=stage,
            attempt=attempt,
            include_retries=include_retries,
            model=model,
        )

    turn_records: list[TokenTimelineTurnRead] = []
    total_input_tokens = 0
    total_cached_input_tokens = 0
    total_output_tokens = 0
    total_uncached_input_tokens = 0
    runtimes: list[float] = []
    chain_prev: dict[tuple[str, str, int | None], tuple[int, int, int]] = {}
    for row in rows:
        input_tokens = _to_non_negative_int(row.input_tokens)
        cached_input_tokens = _to_non_negative_int(row.cached_input_tokens)
        output_tokens = _to_non_negative_int(row.output_tokens)
        uncached_input_tokens = max(0, input_tokens - cached_input_tokens)

        chain_id = _normalize_chain_id(row)
        prev_input, prev_cached, prev_output = chain_prev.get(chain_id, (None, None, None))
        prev = chain_prev.get(chain_id)
        delta_input_raw, delta_uncached_raw, delta_output_raw = _build_delta(
            prev=(prev_input, prev_cached, prev_output) if prev is not None else None,
            current=(input_tokens, cached_input_tokens, output_tokens),
        )

        delta_input = _coalesce_delta(row.delta_input, delta_input_raw)
        delta_uncached = _coalesce_delta(
            row.delta_uncached,
            delta_uncached_raw,
        )
        delta_output = _coalesce_delta(row.delta_output, delta_output_raw)

        cache_ratio = _safe_ratio(float(cached_input_tokens), float(input_tokens))
        is_growth_spike, spike_reason = _build_spike_reasons(
            delta_input=delta_input,
            delta_uncached=delta_uncached,
            cache_ratio=cache_ratio,
            input_tokens=input_tokens,
        )

        turn_records.append(
            TokenTimelineTurnRead(
                turn_id=str(row.turn_id or ""),
                invocation_id=str(row.invocation_id or ""),
                stage=str(row.stage or ""),
                attempt=row.attempt,
                recorded_at=_ensure_aware_timestamp(row.recorded_at),
                input_tokens=input_tokens,
                cached_input_tokens=cached_input_tokens,
                output_tokens=output_tokens,
                delta_input=delta_input,
                delta_uncached=delta_uncached,
                delta_output=delta_output,
                runtime_ms=_to_non_negative_int(row.runtime_ms) if row.runtime_ms is not None else None,
                is_growth_spike=is_growth_spike,
                spike_reason=spike_reason,
            )
        )

        total_input_tokens += input_tokens
        total_cached_input_tokens += cached_input_tokens
        total_output_tokens += output_tokens
        total_uncached_input_tokens += uncached_input_tokens
        if row.runtime_ms is not None and row.runtime_ms >= 0:
            runtimes.append(float(row.runtime_ms))
        chain_prev[chain_id] = (input_tokens, cached_input_tokens, output_tokens)
    sorted_runtimes = sorted(_ensure_float_list(runtimes, default=0.0))
    avg_runtime_ms = mean(sorted_runtimes)
    p95_index = max(0, int(len(sorted_runtimes) * 0.95) - 1)
    p95_runtime_ms = float(sorted_runtimes[min(p95_index, len(sorted_runtimes) - 1)])

    totals = TokenTimelineTotalsRead(
        input=total_input_tokens,
        uncached_input=total_uncached_input_tokens,
        output=total_output_tokens,
        cached_input=total_cached_input_tokens,
        cache_ratio=_safe_ratio(total_cached_input_tokens, total_input_tokens),
        total_io=total_input_tokens + total_output_tokens,
        avg_runtime_ms=avg_runtime_ms,
        p95_runtime_ms=p95_runtime_ms,
    )

    return TokenTimelineRead(
        run_id=run.run_id,
        issue_key=str(run.issue_key or ""),
        model=str(rows[-1].model) if rows and rows[-1].model else None,
        status=run.status,
        totals=totals,
        turns=turn_records,
    )
