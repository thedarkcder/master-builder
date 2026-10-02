"""add release scoped deployment host commands

Revision ID: 20260601_0118
Revises: 20260530_0117
Create Date: 2026-06-01 10:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260601_0118"
down_revision = "20260530_0117"
branch_labels = None
depends_on = None

_RELEASE_FK_NAME = "fk_dep_host_cmds_release_id_dep_releases"


def _column_exists(table_name: str, column_name: str) -> bool:
    return column_name in {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def _index_exists(table_name: str, index_name: str) -> bool:
    return index_name in {
        index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table_name)
    }


def _foreign_key_exists(table_name: str, constraint_name: str) -> bool:
    return constraint_name in {
        key["name"] for key in sa.inspect(op.get_bind()).get_foreign_keys(table_name)
    }


def upgrade() -> None:
    if not _column_exists("deployment_host_commands", "release_id"):
        op.add_column(
            "deployment_host_commands",
            sa.Column("release_id", sa.String(length=64), nullable=True),
        )
    if not _foreign_key_exists(
        "deployment_host_commands",
        _RELEASE_FK_NAME,
    ):
        with op.batch_alter_table("deployment_host_commands", schema=None) as batch_op:
            batch_op.create_foreign_key(
                _RELEASE_FK_NAME,
                "project_deployment_releases",
                ["release_id"],
                ["release_id"],
                ondelete="SET NULL",
            )
    if not _index_exists(
        "deployment_host_commands", "ix_deployment_host_commands_release_id"
    ):
        op.create_index(
            "ix_deployment_host_commands_release_id",
            "deployment_host_commands",
            ["release_id"],
            unique=False,
        )
    if not _index_exists(
        "deployment_host_commands", "ix_deployment_host_commands_release_id_kind_status"
    ):
        op.create_index(
            "ix_deployment_host_commands_release_id_kind_status",
            "deployment_host_commands",
            ["release_id", "kind", "status"],
            unique=False,
        )


def downgrade() -> None:
    if _index_exists(
        "deployment_host_commands", "ix_deployment_host_commands_release_id_kind_status"
    ):
        op.drop_index(
            "ix_deployment_host_commands_release_id_kind_status",
            table_name="deployment_host_commands",
        )
    if _index_exists(
        "deployment_host_commands", "ix_deployment_host_commands_release_id"
    ):
        op.drop_index(
            "ix_deployment_host_commands_release_id",
            table_name="deployment_host_commands",
        )
    if _foreign_key_exists(
        "deployment_host_commands",
        _RELEASE_FK_NAME,
    ):
        with op.batch_alter_table("deployment_host_commands", schema=None) as batch_op:
            batch_op.drop_constraint(
                _RELEASE_FK_NAME,
                type_="foreignkey",
            )
    if _column_exists("deployment_host_commands", "release_id"):
        op.drop_column("deployment_host_commands", "release_id")
