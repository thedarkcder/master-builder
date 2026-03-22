"""add durable PR review publication idempotency

Revision ID: 20260321_0032
Revises: 20260320_0031
Create Date: 2026-03-21 15:35:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260321_0032"
down_revision = "20260320_0031"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in set(inspector.get_table_names())


def _has_index(table_name: str, index_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(index.get("name") == index_name for index in inspector.get_indexes(table_name))


def upgrade() -> None:
    if not _has_table("pr_review_publications"):
        op.create_table(
            "pr_review_publications",
            sa.Column("publication_id", sa.String(length=64), nullable=False),
            sa.Column("tenant_id", sa.String(length=128), nullable=False),
            sa.Column("project_id", sa.String(length=128), nullable=False),
            sa.Column("repo_full_name", sa.String(length=512), nullable=False),
            sa.Column("pr_number", sa.Integer(), nullable=False),
            sa.Column("head_sha", sa.String(length=255), nullable=False),
            sa.Column("review_kind", sa.String(length=32), nullable=False),
            sa.Column("signature", sa.String(length=128), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("owner_request_id", sa.String(length=64), nullable=True),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("review_id", sa.Integer(), nullable=True),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["project_id"], ["projects.project_id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("publication_id"),
            sa.UniqueConstraint(
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
    if not _has_index("pr_review_publications", "ix_pr_review_publications_tenant_id"):
        op.create_index(
            "ix_pr_review_publications_tenant_id",
            "pr_review_publications",
            ["tenant_id"],
            unique=False,
        )
    if not _has_index("pr_review_publications", "ix_pr_review_publications_project_id"):
        op.create_index(
            "ix_pr_review_publications_project_id",
            "pr_review_publications",
            ["project_id"],
            unique=False,
        )
    if not _has_index("pr_review_publications", "ix_pr_review_publications_repo_full_name"):
        op.create_index(
            "ix_pr_review_publications_repo_full_name",
            "pr_review_publications",
            ["repo_full_name"],
            unique=False,
        )
    if not _has_index("pr_review_publications", "ix_pr_review_publications_pr_number"):
        op.create_index(
            "ix_pr_review_publications_pr_number",
            "pr_review_publications",
            ["pr_number"],
            unique=False,
        )
    if not _has_index("pr_review_publications", "ix_pr_review_publications_head_sha"):
        op.create_index(
            "ix_pr_review_publications_head_sha",
            "pr_review_publications",
            ["head_sha"],
            unique=False,
        )
    if not _has_index("pr_review_publications", "ix_pr_review_publications_review_kind"):
        op.create_index(
            "ix_pr_review_publications_review_kind",
            "pr_review_publications",
            ["review_kind"],
            unique=False,
        )
    if not _has_index("pr_review_publications", "ix_pr_review_publications_status"):
        op.create_index(
            "ix_pr_review_publications_status",
            "pr_review_publications",
            ["status"],
            unique=False,
        )
    if not _has_index("pr_review_publications", "ix_pr_review_publications_owner_request_id"):
        op.create_index(
            "ix_pr_review_publications_owner_request_id",
            "pr_review_publications",
            ["owner_request_id"],
            unique=False,
        )
    if not _has_index("pr_review_publications", "ix_pr_review_publications_lease_expires_at"):
        op.create_index(
            "ix_pr_review_publications_lease_expires_at",
            "pr_review_publications",
            ["lease_expires_at"],
            unique=False,
        )
    if not _has_index("pr_review_publications", "ix_pr_review_publications_published_at"):
        op.create_index(
            "ix_pr_review_publications_published_at",
            "pr_review_publications",
            ["published_at"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_index("ix_pr_review_publications_published_at", table_name="pr_review_publications")
    op.drop_index("ix_pr_review_publications_lease_expires_at", table_name="pr_review_publications")
    op.drop_index("ix_pr_review_publications_owner_request_id", table_name="pr_review_publications")
    op.drop_index("ix_pr_review_publications_status", table_name="pr_review_publications")
    op.drop_index("ix_pr_review_publications_review_kind", table_name="pr_review_publications")
    op.drop_index("ix_pr_review_publications_head_sha", table_name="pr_review_publications")
    op.drop_index("ix_pr_review_publications_pr_number", table_name="pr_review_publications")
    op.drop_index("ix_pr_review_publications_repo_full_name", table_name="pr_review_publications")
    op.drop_index("ix_pr_review_publications_project_id", table_name="pr_review_publications")
    op.drop_index("ix_pr_review_publications_tenant_id", table_name="pr_review_publications")
    op.drop_table("pr_review_publications")
