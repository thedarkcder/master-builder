"""add tenant identity, memberships, teams, invites, and tenant experience state

Revision ID: 20260327_0039
Revises: 20260323_0038
Create Date: 2026-03-27
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260327_0039"
down_revision = "20260323_0038"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def _column_exists(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(column.get("name") == column_name for column in inspector.get_columns(table_name))


def _has_index(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(index.get("name") == index_name for index in inspector.get_indexes(table_name))


def upgrade() -> None:
    if not _column_exists("tenants", "experience_config"):
        with op.batch_alter_table("tenants", recreate="auto") as batch_op:
            batch_op.add_column(
                sa.Column("experience_config", sa.JSON(), nullable=False, server_default=sa.text("'{}'"))
            )
    if not _column_exists("tenants", "setup_state"):
        with op.batch_alter_table("tenants", recreate="auto") as batch_op:
            batch_op.add_column(sa.Column("setup_state", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))

    tenants = sa.table(
        "tenants",
        sa.column("tenant_id", sa.String(length=128)),
        sa.column("experience_config", sa.JSON()),
        sa.column("setup_state", sa.JSON()),
    )
    bind = op.get_bind()
    tenant_rows = bind.execute(sa.select(tenants.c.tenant_id, tenants.c.experience_config, tenants.c.setup_state)).all()
    for row in tenant_rows:
        experience_config = dict(row.experience_config or {})
        if "default_mode" not in experience_config:
            experience_config["default_mode"] = "technical"
        setup_state = dict(row.setup_state or {})
        bind.execute(
            tenants.update()
            .where(tenants.c.tenant_id == str(row.tenant_id))
            .values(experience_config=experience_config, setup_state=setup_state)
        )

    if not _table_exists("tenant_users"):
        op.create_table(
            "tenant_users",
            sa.Column("user_id", sa.String(length=64), nullable=False),
            sa.Column("email", sa.String(length=320), nullable=False),
            sa.Column("full_name", sa.String(length=255), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("user_id"),
            sa.UniqueConstraint("email"),
        )
    for index_name, columns in (
        ("ix_tenant_users_email", ["email"]),
    ):
        if not _has_index("tenant_users", index_name):
            op.create_index(index_name, "tenant_users", columns, unique=False)

    if not _table_exists("tenant_user_credentials"):
        op.create_table(
            "tenant_user_credentials",
            sa.Column("user_id", sa.String(length=64), nullable=False),
            sa.Column("password_hash", sa.Text(), nullable=False),
            sa.Column("password_updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["tenant_users.user_id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("user_id"),
        )

    if not _table_exists("tenant_memberships"):
        op.create_table(
            "tenant_memberships",
            sa.Column("membership_id", sa.String(length=64), nullable=False),
            sa.Column("tenant_id", sa.String(length=128), nullable=False),
            sa.Column("user_id", sa.String(length=64), nullable=False),
            sa.Column("role", sa.String(length=32), nullable=False),
            sa.Column("mode_override", sa.String(length=32), nullable=True),
            sa.Column("onboarding_kind", sa.String(length=32), nullable=False, server_default="member_join"),
            sa.Column("first_signed_in_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("onboarding_completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("onboarding_version", sa.String(length=64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["tenant_users.user_id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("membership_id"),
            sa.UniqueConstraint("tenant_id", "user_id", name="uq_tenant_memberships_tenant_user"),
        )
    for index_name, columns in (
        ("ix_tenant_memberships_tenant_id", ["tenant_id"]),
        ("ix_tenant_memberships_user_id", ["user_id"]),
        ("ix_tenant_memberships_role", ["role"]),
    ):
        if not _has_index("tenant_memberships", index_name):
            op.create_index(index_name, "tenant_memberships", columns, unique=False)

    if not _table_exists("tenant_teams"):
        op.create_table(
            "tenant_teams",
            sa.Column("team_id", sa.String(length=64), nullable=False),
            sa.Column("tenant_id", sa.String(length=128), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("permission_keys", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("team_id"),
            sa.UniqueConstraint("tenant_id", "name", name="uq_tenant_teams_tenant_name"),
        )
    for index_name, columns in (
        ("ix_tenant_teams_tenant_id", ["tenant_id"]),
    ):
        if not _has_index("tenant_teams", index_name):
            op.create_index(index_name, "tenant_teams", columns, unique=False)

    if not _table_exists("tenant_team_memberships"):
        op.create_table(
            "tenant_team_memberships",
            sa.Column("team_membership_id", sa.String(length=64), nullable=False),
            sa.Column("team_id", sa.String(length=64), nullable=False),
            sa.Column("membership_id", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["team_id"], ["tenant_teams.team_id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["membership_id"], ["tenant_memberships.membership_id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("team_membership_id"),
            sa.UniqueConstraint("team_id", "membership_id", name="uq_tenant_team_memberships"),
        )
    for index_name, columns in (
        ("ix_tenant_team_memberships_team_id", ["team_id"]),
        ("ix_tenant_team_memberships_membership_id", ["membership_id"]),
    ):
        if not _has_index("tenant_team_memberships", index_name):
            op.create_index(index_name, "tenant_team_memberships", columns, unique=False)

    if not _table_exists("tenant_invites"):
        op.create_table(
            "tenant_invites",
            sa.Column("invite_id", sa.String(length=64), nullable=False),
            sa.Column("tenant_id", sa.String(length=128), nullable=False),
            sa.Column("email", sa.String(length=320), nullable=False),
            sa.Column("full_name", sa.String(length=255), nullable=True),
            sa.Column("role", sa.String(length=32), nullable=False),
            sa.Column("team_ids", sa.JSON(), nullable=False),
            sa.Column("mode_override", sa.String(length=32), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("invite_token_hash", sa.String(length=255), nullable=False),
            sa.Column("invited_by_user_id", sa.String(length=64), nullable=True),
            sa.Column("accepted_by_user_id", sa.String(length=64), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["invited_by_user_id"], ["tenant_users.user_id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["accepted_by_user_id"], ["tenant_users.user_id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("invite_id"),
            sa.UniqueConstraint("invite_token_hash"),
        )
    for index_name, columns in (
        ("ix_tenant_invites_tenant_id", ["tenant_id"]),
        ("ix_tenant_invites_email", ["email"]),
        ("ix_tenant_invites_status", ["status"]),
        ("ix_tenant_invites_invited_by_user_id", ["invited_by_user_id"]),
        ("ix_tenant_invites_accepted_by_user_id", ["accepted_by_user_id"]),
        ("ix_tenant_invites_expires_at", ["expires_at"]),
        ("ix_tenant_invites_tenant_status_created", ["tenant_id", "status", "created_at"]),
    ):
        if not _has_index("tenant_invites", index_name):
            op.create_index(index_name, "tenant_invites", columns, unique=False)


def downgrade() -> None:
    for table_name, index_names in (
        (
            "tenant_invites",
            [
                "ix_tenant_invites_tenant_status_created",
                "ix_tenant_invites_expires_at",
                "ix_tenant_invites_accepted_by_user_id",
                "ix_tenant_invites_invited_by_user_id",
                "ix_tenant_invites_status",
                "ix_tenant_invites_email",
                "ix_tenant_invites_tenant_id",
            ],
        ),
        (
            "tenant_team_memberships",
            [
                "ix_tenant_team_memberships_membership_id",
                "ix_tenant_team_memberships_team_id",
            ],
        ),
        (
            "tenant_teams",
            [
                "ix_tenant_teams_tenant_id",
            ],
        ),
        (
            "tenant_memberships",
            [
                "ix_tenant_memberships_role",
                "ix_tenant_memberships_user_id",
                "ix_tenant_memberships_tenant_id",
            ],
        ),
        ("tenant_user_credentials", []),
        ("tenant_users", ["ix_tenant_users_email"]),
    ):
        if _table_exists(table_name):
            for index_name in index_names:
                if _has_index(table_name, index_name):
                    op.drop_index(index_name, table_name=table_name)
            op.drop_table(table_name)

    if _column_exists("tenants", "setup_state"):
        with op.batch_alter_table("tenants", recreate="auto") as batch_op:
            batch_op.drop_column("setup_state")
    if _column_exists("tenants", "experience_config"):
        with op.batch_alter_table("tenants", recreate="auto") as batch_op:
            batch_op.drop_column("experience_config")
