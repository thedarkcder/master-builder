from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchestrator.storage.models import WebhookJob, WebhookSubjectClaim

WEBHOOK_JOB_STATUS_PENDING = "pending"
WEBHOOK_JOB_STATUS_PROCESSING = "processing"
WEBHOOK_JOB_STATUS_DONE = "done"
WEBHOOK_JOB_STATUS_FAILED = "failed"

WEBHOOK_TRANSPORT_JIRA = "jira_webhook"
WEBHOOK_TRANSPORT_GITHUB = "github_webhook"
WEBHOOK_TRANSPORT_GITHUB_DEPLOYMENT = "github_deployment_webhook"
WEBHOOK_TRANSPORT_DISCORD_COMMAND = "discord_webhook"
WEBHOOK_TRANSPORT_DISCORD_INTERACTION = "discord_interaction"
WEBHOOK_TRANSPORT_PROJECT_AUTOMATION = "project_automation"
WEBHOOK_TRANSPORT_COOLIFY_DEPLOYMENT = "coolify_deployment_webhook"
WEBHOOK_TRANSPORT_PROJECT_APP_ANALYSIS = "project_app_analysis"
WEBHOOK_SUBJECT_BATCH_TRANSPORTS = frozenset(
    {WEBHOOK_TRANSPORT_JIRA, WEBHOOK_TRANSPORT_GITHUB}
)

_LEASE_DURATION = timedelta(minutes=5)


@dataclass(frozen=True)
class WebhookJobEnqueueRequest:
    transport: str
    request_id: str
    tenant_id: str
    project_id: str | None
    subject_key: str
    payload_json: dict[str, object]
    context_json: dict[str, object]
    dedupe_key: str | None = None
    event_type: str | None = None


@dataclass(frozen=True)
class WebhookJobEnqueueResult:
    created: bool
    reason: str | None
    job: WebhookJob


@dataclass(frozen=True)
class WebhookJobBatch:
    owner_id: str
    transport: str
    subject_key: str
    jobs: tuple[WebhookJob, ...]

    @property
    def primary_job(self) -> WebhookJob:
        if not self.jobs:
            raise RuntimeError("Webhook job batch has no jobs")
        return self.jobs[0]

    @property
    def related_jobs(self) -> tuple[WebhookJob, ...]:
        return self.jobs[1:]

    @property
    def job_ids(self) -> tuple[str, ...]:
        return tuple(str(job.job_id) for job in self.jobs)


@dataclass(frozen=True)
class WebhookJobBatchClaimResult:
    acquired: bool
    reason: str | None
    batch: WebhookJobBatch | None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_utc(timestamp: datetime | None) -> datetime | None:
    if timestamp is None:
        return None
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _is_postgres(session: Session) -> bool:
    bind = session.get_bind()
    return bind is not None and bind.dialect.name == "postgresql"


def enqueue_webhook_job(
    session: Session,
    *,
    request: WebhookJobEnqueueRequest,
    now: datetime | None = None,
) -> WebhookJobEnqueueResult:
    timestamp = now or _now()
    normalized_tenant_id = str(request.tenant_id or "").strip()
    if not normalized_tenant_id:
        raise ValueError("webhook jobs require tenant_id")
    normalized_dedupe_key = str(request.dedupe_key or "").strip() or None
    if normalized_dedupe_key is not None:
        existing = session.execute(
            select(WebhookJob).where(
                WebhookJob.transport == request.transport,
                WebhookJob.tenant_id == normalized_tenant_id,
                WebhookJob.dedupe_key == normalized_dedupe_key,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return WebhookJobEnqueueResult(
                created=False, reason="duplicate_delivery", job=existing
            )

    job = WebhookJob(
        job_id=str(uuid4()),
        transport=request.transport,
        tenant_id=normalized_tenant_id,
        project_id=request.project_id,
        subject_key=request.subject_key,
        dedupe_key=normalized_dedupe_key,
        request_id=request.request_id,
        event_type=str(request.event_type or "").strip() or None,
        status=WEBHOOK_JOB_STATUS_PENDING,
        owner_id=None,
        lease_expires_at=None,
        available_at=timestamp,
        attempt_count=0,
        last_error=None,
        payload_json=dict(request.payload_json or {}),
        context_json=dict(request.context_json or {}),
        created_at=timestamp,
        updated_at=timestamp,
        started_at=None,
        completed_at=None,
    )
    try:
        with session.begin_nested():
            session.add(job)
            session.flush()
    except IntegrityError:
        if normalized_dedupe_key is None:
            raise
        existing = session.execute(
            select(WebhookJob).where(
                WebhookJob.transport == request.transport,
                WebhookJob.tenant_id == normalized_tenant_id,
                WebhookJob.dedupe_key == normalized_dedupe_key,
            )
        ).scalar_one_or_none()
        if existing is None:
            raise
        return WebhookJobEnqueueResult(
            created=False, reason="duplicate_delivery", job=existing
        )
    session.flush()
    session.refresh(job)
    return WebhookJobEnqueueResult(created=True, reason=None, job=job)


def _find_or_create_subject_claim(
    session: Session,
    *,
    subject_key: str,
    now: datetime,
) -> WebhookSubjectClaim:
    claim = session.get(WebhookSubjectClaim, {"subject_key": subject_key})
    if claim is not None:
        return claim
    claim = WebhookSubjectClaim(
        subject_key=subject_key,
        owner_id=None,
        lease_expires_at=None,
        updated_at=now,
    )
    try:
        with session.begin_nested():
            session.add(claim)
            session.flush()
    except IntegrityError:
        claim = session.get(WebhookSubjectClaim, {"subject_key": subject_key})
        if claim is None:
            raise
    return claim


def _acquire_subject_claim(
    session: Session,
    *,
    subject_key: str,
    owner_id: str,
    now: datetime,
) -> bool:
    _find_or_create_subject_claim(session, subject_key=subject_key, now=now)
    result = session.execute(
        update(WebhookSubjectClaim)
        .where(WebhookSubjectClaim.subject_key == subject_key)
        .where(
            or_(
                WebhookSubjectClaim.owner_id == owner_id,
                WebhookSubjectClaim.owner_id.is_(None),
                WebhookSubjectClaim.lease_expires_at.is_(None),
                WebhookSubjectClaim.lease_expires_at <= now,
            )
        )
        .values(
            owner_id=owner_id,
            lease_expires_at=now + _LEASE_DURATION,
            updated_at=now,
        )
    )
    session.flush()
    return int(result.rowcount or 0) == 1


def _release_subject_claim(
    session: Session,
    *,
    subject_key: str,
    owner_id: str,
    now: datetime,
) -> None:
    claim = session.get(WebhookSubjectClaim, {"subject_key": subject_key})
    if claim is None or claim.owner_id != owner_id:
        return
    claim.owner_id = None
    claim.lease_expires_at = None
    claim.updated_at = now
    session.flush()


def _claimable_job_filter(timestamp: datetime):  # noqa: ANN202
    return or_(
        WebhookJob.status == WEBHOOK_JOB_STATUS_PENDING,
        (
            (WebhookJob.status == WEBHOOK_JOB_STATUS_PROCESSING)
            & (WebhookJob.lease_expires_at.is_not(None))
            & (WebhookJob.lease_expires_at <= timestamp)
        ),
    )


def _claim_job_batch_for_candidate(
    session: Session,
    *,
    candidate_job_id: str,
    owner_id: str,
    now: datetime,
) -> WebhookJobBatch | None:
    candidate_query = select(WebhookJob).where(
        WebhookJob.job_id == candidate_job_id,
        WebhookJob.available_at <= now,
        _claimable_job_filter(now),
    )
    if _is_postgres(session):
        candidate_query = candidate_query.with_for_update(skip_locked=True)
    candidate = session.execute(candidate_query).scalar_one_or_none()
    if candidate is None:
        return None
    if not _acquire_subject_claim(
        session,
        subject_key=candidate.subject_key,
        owner_id=owner_id,
        now=now,
    ):
        return None

    batch_query = (
        select(WebhookJob)
        .where(
            WebhookJob.transport == candidate.transport,
            WebhookJob.subject_key == candidate.subject_key,
            WebhookJob.available_at <= now,
            _claimable_job_filter(now),
        )
        .order_by(WebhookJob.created_at.asc())
    )
    if candidate.transport not in WEBHOOK_SUBJECT_BATCH_TRANSPORTS:
        batch_query = batch_query.where(WebhookJob.job_id == candidate.job_id)
    if _is_postgres(session):
        batch_query = batch_query.with_for_update(skip_locked=True)
    jobs = tuple(session.execute(batch_query).scalars().all())
    if not jobs:
        raise RuntimeError(
            f"Webhook subject claim acquired but no claimable jobs found for '{candidate.subject_key}'."
        )

    for job in jobs:
        job.status = WEBHOOK_JOB_STATUS_PROCESSING
        job.owner_id = owner_id
        job.lease_expires_at = now + _LEASE_DURATION
        job.attempt_count = int(job.attempt_count or 0) + 1
        job.last_error = None
        job.updated_at = now
        if job.started_at is None:
            job.started_at = now
    session.flush()
    session.commit()
    for job in jobs:
        session.refresh(job)
    return WebhookJobBatch(
        owner_id=owner_id,
        transport=str(jobs[0].transport),
        subject_key=str(jobs[0].subject_key),
        jobs=jobs,
    )


def claim_next_webhook_subject_batch(
    session: Session,
    *,
    owner_id: str,
    now: datetime | None = None,
) -> WebhookJobBatchClaimResult:
    timestamp = now or _now()
    candidate_query = (
        select(WebhookJob.job_id)
        .where(
            WebhookJob.available_at <= timestamp,
            _claimable_job_filter(timestamp),
        )
        .order_by(WebhookJob.created_at.asc())
    )
    candidate_ids = [row[0] for row in session.execute(candidate_query).all()]
    for job_id in candidate_ids:
        batch = _claim_job_batch_for_candidate(
            session,
            candidate_job_id=job_id,
            owner_id=owner_id,
            now=timestamp,
        )
        if batch is None:
            session.rollback()
            continue
        return WebhookJobBatchClaimResult(acquired=True, reason=None, batch=batch)
    return WebhookJobBatchClaimResult(
        acquired=False, reason="no_pending_job", batch=None
    )


def _finalize_jobs(
    session: Session,
    *,
    jobs: tuple[WebhookJob, ...],
    owner_id: str,
    status: str,
    error: str | None = None,
    now: datetime | None = None,
) -> tuple[WebhookJob, ...]:
    job_ids = tuple(str(job.job_id) for job in jobs)
    return _finalize_job_ids(
        session,
        job_ids=job_ids,
        owner_id=owner_id,
        status=status,
        error=error,
        now=now,
    )


def _finalize_job_ids(
    session: Session,
    *,
    job_ids: tuple[str, ...],
    owner_id: str,
    status: str,
    error: str | None = None,
    now: datetime | None = None,
) -> tuple[WebhookJob, ...]:
    if not job_ids:
        return ()
    timestamp = now or _now()
    refreshed: list[WebhookJob] = []
    for job_id in job_ids:
        persisted = session.get(WebhookJob, job_id)
        if persisted is None:
            continue
        if persisted.owner_id != owner_id:
            raise RuntimeError(
                f"Webhook job '{persisted.job_id}' is owned by '{persisted.owner_id}', expected '{owner_id}'."
            )
        persisted.status = status
        persisted.owner_id = None
        persisted.lease_expires_at = None
        persisted.last_error = error
        persisted.updated_at = timestamp
        persisted.completed_at = timestamp
        refreshed.append(persisted)
    session.flush()
    subject_keys = {
        job.subject_key for job in refreshed if str(job.subject_key or "").strip()
    }
    for subject_key in subject_keys:
        _release_subject_claim(
            session,
            subject_key=subject_key,
            owner_id=owner_id,
            now=timestamp,
        )
    session.commit()
    for job in refreshed:
        session.refresh(job)
    return tuple(refreshed)


def requeue_webhook_job_ids(
    session: Session,
    *,
    job_ids: tuple[str, ...],
    owner_id: str,
    error: str,
    retry_after_seconds: int = 30,
    now: datetime | None = None,
) -> tuple[WebhookJob, ...]:
    if not job_ids:
        return ()
    timestamp = now or _now()
    available_at = timestamp + timedelta(seconds=max(int(retry_after_seconds), 0))
    refreshed: list[WebhookJob] = []
    for job_id in job_ids:
        persisted = session.get(WebhookJob, job_id)
        if persisted is None:
            continue
        if persisted.owner_id != owner_id:
            raise RuntimeError(
                f"Webhook job '{persisted.job_id}' is owned by '{persisted.owner_id}', expected '{owner_id}'."
            )
        persisted.status = WEBHOOK_JOB_STATUS_PENDING
        persisted.owner_id = None
        persisted.lease_expires_at = None
        persisted.last_error = error
        persisted.available_at = available_at
        persisted.updated_at = timestamp
        persisted.completed_at = None
        refreshed.append(persisted)
    session.flush()
    subject_keys = {
        job.subject_key for job in refreshed if str(job.subject_key or "").strip()
    }
    for subject_key in subject_keys:
        _release_subject_claim(
            session,
            subject_key=subject_key,
            owner_id=owner_id,
            now=timestamp,
        )
    session.commit()
    for job in refreshed:
        session.refresh(job)
    return tuple(refreshed)


def mark_webhook_job_ids_done(
    session: Session,
    *,
    job_ids: tuple[str, ...],
    owner_id: str,
    now: datetime | None = None,
) -> tuple[WebhookJob, ...]:
    return _finalize_job_ids(
        session,
        job_ids=job_ids,
        owner_id=owner_id,
        status=WEBHOOK_JOB_STATUS_DONE,
        now=now,
    )


def mark_webhook_job_ids_failed(
    session: Session,
    *,
    job_ids: tuple[str, ...],
    owner_id: str,
    error: str,
    now: datetime | None = None,
) -> tuple[WebhookJob, ...]:
    return _finalize_job_ids(
        session,
        job_ids=job_ids,
        owner_id=owner_id,
        status=WEBHOOK_JOB_STATUS_FAILED,
        error=error,
        now=now,
    )


def mark_webhook_jobs_done(
    session: Session,
    *,
    jobs: tuple[WebhookJob, ...],
    owner_id: str,
    now: datetime | None = None,
) -> tuple[WebhookJob, ...]:
    return _finalize_jobs(
        session,
        jobs=jobs,
        owner_id=owner_id,
        status=WEBHOOK_JOB_STATUS_DONE,
        now=now,
    )


def mark_webhook_jobs_failed(
    session: Session,
    *,
    jobs: tuple[WebhookJob, ...],
    owner_id: str,
    error: str,
    now: datetime | None = None,
) -> tuple[WebhookJob, ...]:
    return _finalize_jobs(
        session,
        jobs=jobs,
        owner_id=owner_id,
        status=WEBHOOK_JOB_STATUS_FAILED,
        error=error,
        now=now,
    )
