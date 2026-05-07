from __future__ import annotations

# Model modules share SQLAlchemy symbols from the storage model base.
# ruff: noqa: F401
from .base import (
    Base,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    LargeBinary,
    Mapped,
    String,
    Text,
    UniqueConstraint,
    VectorJSONCompat,
    datetime,
    mapped_column,
    text,
    uuid4,
)


class PrReviewPublication(Base):
    __tablename__ = "pr_review_publications"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "repo_full_name",
            "pr_number",
            "head_sha",
            "review_kind",
            "signature",
            name="uq_pr_review_publications_scope",
        ),
    )

    publication_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    repo_full_name: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    head_sha: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    review_kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    signature: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    owner_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    review_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RepoBootstrapState(Base):
    __tablename__ = "repo_bootstrap_states"

    tenant_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        primary_key=True,
    )
    repo_url: Mapped[str] = mapped_column(String(512), primary_key=True)
    last_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_created_files: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    bootstrap_count: Mapped[int] = mapped_column(nullable=False, default=1)
    bootstrapped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
