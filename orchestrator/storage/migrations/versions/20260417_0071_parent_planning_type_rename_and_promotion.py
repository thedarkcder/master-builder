"""rename parent planning workflow type and add child promotion operation

Revision ID: 20260417_0071
Revises: 20260417_0070
Create Date: 2026-04-17 15:25:00.000000
"""

from __future__ import annotations

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "20260417_0071"
down_revision = "20260417_0070"
branch_labels = None
depends_on = None

OLD_WORKFLOW_TYPE_KEY = "legacy-parent-planning"
NEW_WORKFLOW_TYPE_KEY = "parent_planning"
OLD_WORKFLOW_ID_PREFIX = f"{OLD_WORKFLOW_TYPE_KEY}:"
NEW_WORKFLOW_ID_PREFIX = f"{NEW_WORKFLOW_TYPE_KEY}:"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _has_table(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return any(
        column.get("name") == column_name
        for column in inspector.get_columns(table_name)
    )


WORKFLOW_ID_FK_SPECS = (
    {
        "table_name": "project_install_requests",
        "constraint_name": "project_install_requests_workflow_id_fkey",
        "local_columns": ["workflow_id"],
        "remote_table": "workflow_executions",
        "remote_columns": ["workflow_id"],
        "ondelete": "SET NULL",
    },
    {
        "table_name": "workflow_executions",
        "constraint_name": "workflow_executions_source_workflow_id_fkey",
        "local_columns": ["source_workflow_id"],
        "remote_table": "workflow_executions",
        "remote_columns": ["workflow_id"],
        "ondelete": "SET NULL",
    },
    {
        "table_name": "workflow_checkpoints",
        "constraint_name": "workflow_checkpoints_workflow_id_fkey",
        "local_columns": ["workflow_id"],
        "remote_table": "workflow_executions",
        "remote_columns": ["workflow_id"],
        "ondelete": "CASCADE",
    },
    {
        "table_name": "runs",
        "constraint_name": "runs_workflow_id_fkey",
        "local_columns": ["workflow_id"],
        "remote_table": "workflow_executions",
        "remote_columns": ["workflow_id"],
        "ondelete": "CASCADE",
    },
    {
        "table_name": "run_human_input_requests",
        "constraint_name": "run_human_input_requests_workflow_id_fkey",
        "local_columns": ["workflow_id"],
        "remote_table": "workflow_executions",
        "remote_columns": ["workflow_id"],
        "ondelete": "CASCADE",
    },
    {
        "table_name": "workflow_operations",
        "constraint_name": "workflow_operations_workflow_id_fkey",
        "local_columns": ["workflow_id"],
        "remote_table": "workflow_executions",
        "remote_columns": ["workflow_id"],
        "ondelete": "CASCADE",
    },
)


def _drop_workflow_id_foreign_keys(bind: sa.engine.Connection) -> None:
    if bind.dialect.name == "sqlite":
        return
    inspector = sa.inspect(bind)
    for table_name in inspector.get_table_names():
        for foreign_key in inspector.get_foreign_keys(table_name):
            if foreign_key.get("referred_table") != "workflow_executions":
                continue
            if list(foreign_key.get("referred_columns") or []) != ["workflow_id"]:
                continue
            constraint_name = foreign_key.get("name")
            if not constraint_name:
                continue
            op.drop_constraint(constraint_name, table_name, type_="foreignkey")


def _create_workflow_id_foreign_keys(bind: sa.engine.Connection) -> None:
    if bind.dialect.name == "sqlite":
        return
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())
    for spec in WORKFLOW_ID_FK_SPECS:
        table_name = spec["table_name"]
        if table_name not in table_names:
            continue
        if not all(
            _has_column(table_name, column_name)
            for column_name in spec["local_columns"]
        ):
            continue
        existing_foreign_keys = inspector.get_foreign_keys(table_name)
        if any(
            foreign_key.get("referred_table") == spec["remote_table"]
            and list(foreign_key.get("constrained_columns") or [])
            == spec["local_columns"]
            and list(foreign_key.get("referred_columns") or [])
            == spec["remote_columns"]
            for foreign_key in existing_foreign_keys
        ):
            continue
        op.create_foreign_key(
            spec["constraint_name"],
            table_name,
            spec["remote_table"],
            spec["local_columns"],
            spec["remote_columns"],
            ondelete=spec["ondelete"],
        )


def upgrade() -> None:
    bind = op.get_bind()
    _drop_workflow_id_foreign_keys(bind)

    if _has_column("workflow_operations", "blocker_id"):
        if bind.dialect.name != "sqlite":
            op.drop_column("workflow_operations", "blocker_id")
        else:
            with op.batch_alter_table("workflow_operations") as batch_op:
                batch_op.drop_column("blocker_id")
    if _has_table("workflow_blockers"):
        op.execute("DROP TABLE IF EXISTS workflow_blockers")

    bind.execute(
        sa.text(
            """
            INSERT INTO workflow_types (workflow_type_key, label, description, created_at, updated_at)
            SELECT :new_key, 'Parent Planning', description, created_at, updated_at
            FROM workflow_types
            WHERE workflow_type_key = :old_key
            """
        ),
        {"new_key": NEW_WORKFLOW_TYPE_KEY, "old_key": OLD_WORKFLOW_TYPE_KEY},
    )

    bind.execute(
        sa.text(
            """
            UPDATE workflow_type_operations
            SET operation_definition_id = REPLACE(operation_definition_id, :old_prefix, :new_prefix)
            WHERE workflow_type_key = :old_key
            """
        ),
        {
            "old_prefix": f"{OLD_WORKFLOW_TYPE_KEY}:",
            "new_prefix": f"{NEW_WORKFLOW_TYPE_KEY}:",
            "old_key": OLD_WORKFLOW_TYPE_KEY,
        },
    )
    bind.execute(
        sa.text(
            """
            UPDATE workflow_type_operations
            SET workflow_type_key = :new_key
            WHERE workflow_type_key = :old_key
            """
        ),
        {"new_key": NEW_WORKFLOW_TYPE_KEY, "old_key": OLD_WORKFLOW_TYPE_KEY},
    )
    bind.execute(
        sa.text(
            """
            UPDATE workflow_executions
            SET workflow_type_key = :new_key
            WHERE workflow_type_key = :old_key
            """
        ),
        {"new_key": NEW_WORKFLOW_TYPE_KEY, "old_key": OLD_WORKFLOW_TYPE_KEY},
    )
    bind.execute(
        sa.text(
            """
            DELETE FROM workflow_types
            WHERE workflow_type_key = :old_key
            """
        ),
        {"old_key": OLD_WORKFLOW_TYPE_KEY},
    )

    for table_name in (
        "workflow_operations",
        "project_install_requests",
        "run_human_input_requests",
        "workflow_checkpoints",
        "runs",
    ):
        bind.execute(
            sa.text(
                f"""
                UPDATE {table_name}
                SET workflow_id = REPLACE(workflow_id, :old_prefix, :new_prefix)
                WHERE workflow_id LIKE :old_pattern
                """
            ),
            {
                "old_prefix": OLD_WORKFLOW_ID_PREFIX,
                "new_prefix": NEW_WORKFLOW_ID_PREFIX,
                "old_pattern": f"{OLD_WORKFLOW_ID_PREFIX}%",
            },
        )
    bind.execute(
        sa.text(
            """
            UPDATE workflow_executions
            SET workflow_id = CASE
                    WHEN workflow_id LIKE :old_pattern
                    THEN REPLACE(workflow_id, :old_prefix, :new_prefix)
                    ELSE workflow_id
                END,
                source_workflow_id = CASE
                    WHEN source_workflow_id LIKE :old_pattern
                    THEN REPLACE(source_workflow_id, :old_prefix, :new_prefix)
                    ELSE source_workflow_id
                END
            WHERE workflow_id LIKE :old_pattern
               OR source_workflow_id LIKE :old_pattern
            """
        ),
        {
            "old_prefix": OLD_WORKFLOW_ID_PREFIX,
            "new_prefix": NEW_WORKFLOW_ID_PREFIX,
            "old_pattern": f"{OLD_WORKFLOW_ID_PREFIX}%",
        },
    )
    _create_workflow_id_foreign_keys(bind)

    if not _has_column("workflow_type_operations", "retry_policy"):
        return

    now = _now()
    operations_table = sa.table(
        "workflow_type_operations",
        sa.column("operation_definition_id", sa.String()),
        sa.column("workflow_type_key", sa.String()),
        sa.column("operation_type", sa.String()),
        sa.column("label", sa.String()),
        sa.column("retry_policy", sa.Text()),
        sa.column("description", sa.Text()),
        sa.column("required", sa.Boolean()),
        sa.column("sort_order", sa.Integer()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    bind.execute(
        sa.insert(operations_table).values(
            {
                "operation_definition_id": f"{NEW_WORKFLOW_TYPE_KEY}:jira_child_promotion",
                "workflow_type_key": NEW_WORKFLOW_TYPE_KEY,
                "operation_type": "jira_child_promotion",
                "label": "Promote engineering child tickets",
                "retry_policy": "Retry transient Jira transition failures. Fail when a child cannot be promoted to the requested board status.",
                "description": "Promote backlog engineering child tickets onto the working board when the parent moves forward.",
                "required": False,
                "sort_order": 45,
                "created_at": now,
                "updated_at": now,
            }
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            DELETE FROM workflow_type_operations
            WHERE operation_definition_id = :operation_definition_id
            """
        ),
        {"operation_definition_id": f"{NEW_WORKFLOW_TYPE_KEY}:jira_child_promotion"},
    )

    bind.execute(
        sa.text(
            """
            INSERT INTO workflow_types (workflow_type_key, label, description, created_at, updated_at)
            SELECT :old_key, 'Legacy Parent Planning', description, created_at, updated_at
            FROM workflow_types
            WHERE workflow_type_key = :new_key
            """
        ),
        {"old_key": OLD_WORKFLOW_TYPE_KEY, "new_key": NEW_WORKFLOW_TYPE_KEY},
    )
    _drop_workflow_id_foreign_keys(bind)

    for table_name in (
        "runs",
        "workflow_checkpoints",
        "run_human_input_requests",
        "project_install_requests",
        "workflow_operations",
    ):
        bind.execute(
            sa.text(
                f"""
                UPDATE {table_name}
                SET workflow_id = REPLACE(workflow_id, :new_prefix, :old_prefix)
                WHERE workflow_id LIKE :new_pattern
                """
            ),
            {
                "new_prefix": NEW_WORKFLOW_ID_PREFIX,
                "old_prefix": OLD_WORKFLOW_ID_PREFIX,
                "new_pattern": f"{NEW_WORKFLOW_ID_PREFIX}%",
            },
        )
    bind.execute(
        sa.text(
            """
            UPDATE workflow_executions
            SET workflow_id = CASE
                    WHEN workflow_id LIKE :new_pattern
                    THEN REPLACE(workflow_id, :new_prefix, :old_prefix)
                    ELSE workflow_id
                END,
                source_workflow_id = CASE
                    WHEN source_workflow_id LIKE :new_pattern
                    THEN REPLACE(source_workflow_id, :new_prefix, :old_prefix)
                    ELSE source_workflow_id
                END
            WHERE workflow_id LIKE :new_pattern
               OR source_workflow_id LIKE :new_pattern
            """
        ),
        {
            "new_prefix": NEW_WORKFLOW_ID_PREFIX,
            "old_prefix": OLD_WORKFLOW_ID_PREFIX,
            "new_pattern": f"{NEW_WORKFLOW_ID_PREFIX}%",
        },
    )
    _create_workflow_id_foreign_keys(bind)

    bind.execute(
        sa.text(
            """
            UPDATE workflow_executions
            SET workflow_type_key = :old_key
            WHERE workflow_type_key = :new_key
            """
        ),
        {"old_key": OLD_WORKFLOW_TYPE_KEY, "new_key": NEW_WORKFLOW_TYPE_KEY},
    )
    bind.execute(
        sa.text(
            """
            UPDATE workflow_type_operations
            SET workflow_type_key = :old_key
            WHERE workflow_type_key = :new_key
            """
        ),
        {"old_key": OLD_WORKFLOW_TYPE_KEY, "new_key": NEW_WORKFLOW_TYPE_KEY},
    )
    bind.execute(
        sa.text(
            """
            UPDATE workflow_type_operations
            SET operation_definition_id = REPLACE(operation_definition_id, :new_prefix, :old_prefix)
            WHERE workflow_type_key = :old_key
            """
        ),
        {
            "new_prefix": f"{NEW_WORKFLOW_TYPE_KEY}:",
            "old_prefix": f"{OLD_WORKFLOW_TYPE_KEY}:",
            "old_key": OLD_WORKFLOW_TYPE_KEY,
        },
    )
    bind.execute(
        sa.text(
            """
            DELETE FROM workflow_types
            WHERE workflow_type_key = :new_key
            """
        ),
        {"new_key": NEW_WORKFLOW_TYPE_KEY},
    )
