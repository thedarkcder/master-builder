from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchestrator.storage.models import PrReviewPublication

PR_REVIEW_PUBLICATION_KIND_INLINE = "inline_review"
PR_REVIEW_PUBLICATION_KIND_STICKY = "sticky_review"

PR_REVIEW_PUBLICATION_STATUS_FAILED = "failed"
PR_REVIEW_PUBLICATION_STATUS_PENDING = "pending"
PR_REVIEW_PUBLICATION_STATUS_PUBLISHED = "published"

_LEASE_DURATION = timedelta(minutes=5)


@dataclass(frozen=True)
class ReviewPublicationAcquireResult:
    acquired: bool
    reason: str | None
    publication: PrReviewPublication | None


def acquire_review_publication(
    session: Session,
    *,
    tenant_id: str,
    project_id: str,
    repo_full_name: str,
    pr_number: int,
    head_sha: str,
    review_kind: str,
    signature: str,
    request_id: str,
    now: datetime | None = None,
) -> ReviewPublicationAcquireResult:
    timestamp = now or datetime.now(timezone.utc)
    publication = _find_publication(
        session,
        tenant_id=tenant_id,
        project_id=project_id,
        repo_full_name=repo_full_name,
        pr_number=pr_number,
        head_sha=head_sha,
        review_kind=review_kind,
        signature=signature,
    )
    if publication is not None:
        return _reacquire_existing_publication(
            session,
            publication=publication,
            request_id=request_id,
            now=timestamp,
        )

    publication = PrReviewPublication(
        publication_id=str(uuid4()),
        tenant_id=tenant_id,
        project_id=project_id,
        repo_full_name=repo_full_name,
        pr_number=pr_number,
        head_sha=head_sha,
        review_kind=review_kind,
        signature=signature,
        status=PR_REVIEW_PUBLICATION_STATUS_PENDING,
        owner_request_id=request_id,
        lease_expires_at=timestamp + _LEASE_DURATION,
        review_id=None,
        published_at=None,
        last_error=None,
        created_at=timestamp,
        updated_at=timestamp,
    )
    try:
        with session.begin_nested():
            session.add(publication)
            session.flush()
    except IntegrityError:
        publication = _find_publication(
            session,
            tenant_id=tenant_id,
            project_id=project_id,
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            head_sha=head_sha,
            review_kind=review_kind,
            signature=signature,
        )
        if publication is None:
            raise
        return _reacquire_existing_publication(
            session,
            publication=publication,
            request_id=request_id,
            now=timestamp,
        )
    return ReviewPublicationAcquireResult(
        acquired=True, reason=None, publication=publication
    )


def mark_review_publication_published(
    session: Session,
    *,
    publication: PrReviewPublication,
    review_id: int | None,
    published_at: datetime | None = None,
) -> None:
    timestamp = published_at or datetime.now(timezone.utc)
    publication.status = PR_REVIEW_PUBLICATION_STATUS_PUBLISHED
    publication.owner_request_id = None
    publication.lease_expires_at = None
    publication.review_id = review_id
    publication.published_at = timestamp
    publication.last_error = None
    publication.updated_at = timestamp
    session.flush()


def mark_review_publication_failed(
    session: Session,
    *,
    publication: PrReviewPublication,
    error: str | None,
    failed_at: datetime | None = None,
) -> None:
    timestamp = failed_at or datetime.now(timezone.utc)
    publication.status = PR_REVIEW_PUBLICATION_STATUS_FAILED
    publication.owner_request_id = None
    publication.lease_expires_at = None
    publication.last_error = str(error or "").strip() or None
    publication.updated_at = timestamp
    session.flush()


def _find_publication(
    session: Session,
    *,
    tenant_id: str,
    project_id: str,
    repo_full_name: str,
    pr_number: int,
    head_sha: str,
    review_kind: str,
    signature: str,
) -> PrReviewPublication | None:
    return session.execute(
        select(PrReviewPublication).where(
            PrReviewPublication.tenant_id == tenant_id,
            PrReviewPublication.project_id == project_id,
            PrReviewPublication.repo_full_name == repo_full_name,
            PrReviewPublication.pr_number == pr_number,
            PrReviewPublication.head_sha == head_sha,
            PrReviewPublication.review_kind == review_kind,
            PrReviewPublication.signature == signature,
        )
    ).scalar_one_or_none()


def _reacquire_existing_publication(
    session: Session,
    *,
    publication: PrReviewPublication,
    request_id: str,
    now: datetime,
) -> ReviewPublicationAcquireResult:
    if publication.status == PR_REVIEW_PUBLICATION_STATUS_PUBLISHED:
        return ReviewPublicationAcquireResult(
            acquired=False,
            reason="duplicate_signature",
            publication=publication,
        )
    lease_expires_at = publication.lease_expires_at
    if (
        publication.status == PR_REVIEW_PUBLICATION_STATUS_PENDING
        and lease_expires_at is not None
        and lease_expires_at > now
        and publication.owner_request_id != request_id
    ):
        return ReviewPublicationAcquireResult(
            acquired=False,
            reason="lease_held",
            publication=publication,
        )

    publication.status = PR_REVIEW_PUBLICATION_STATUS_PENDING
    publication.owner_request_id = request_id
    publication.lease_expires_at = now + _LEASE_DURATION
    publication.last_error = None
    publication.updated_at = now
    session.flush()
    return ReviewPublicationAcquireResult(
        acquired=True, reason=None, publication=publication
    )
