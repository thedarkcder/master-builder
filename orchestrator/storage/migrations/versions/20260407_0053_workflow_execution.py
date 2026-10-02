"""replace ad hoc hitl resume with workflow execution model

Revision ID: 20260407_0053
Revises: 20260330_0052
Create Date: 2026-04-07 18:00:00.000000
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


revision = "20260407_0053"
down_revision = "20260330_0052"
branch_labels = None
depends_on = None

_WORKFLOW_ACTIVE_STATUSES = {"queued", "running", "waiting_for_input"}
_LEGACY_TRIGGER_CONTEXT_KEYS = {
    "resume_stage",
    "resume_session_id",
    "resume_source_run_id",
    "resume_source_plan",
    "resume_source_state",
    "human_input_request_ids",
}
_WORKFLOW_EXECUTION_LOCK_KEY = 202604070053


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


def _has_index(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return any(
        index.get("name") == index_name for index in inspector.get_indexes(table_name)
    )


def _drop_index_if_exists(table_name: str, index_name: str) -> None:
    if _has_index(table_name, index_name):
        op.drop_index(index_name, table_name=table_name)


def _loads_json(value: object) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        text_value = value.strip()
        if not text_value:
            return {}
        loaded = json.loads(text_value)
        return dict(loaded) if isinstance(loaded, dict) else {}
    return {}


def _dumps_json(value: dict) -> str:
    return json.dumps(value, sort_keys=True)


def _scrub_payload_dict(payload: dict) -> dict:
    next_payload = dict(payload)
    trigger_context = next_payload.get("trigger_context")
    if isinstance(trigger_context, dict):
        next_trigger_context = {
            key: value
            for key, value in trigger_context.items()
            if key not in _LEGACY_TRIGGER_CONTEXT_KEYS
        }
        if next_trigger_context:
            next_payload["trigger_context"] = next_trigger_context
        else:
            next_payload.pop("trigger_context", None)
    return next_payload


def _scrub_request_context(payload: dict) -> dict:
    return {
        key: value
        for key, value in payload.items()
        if key
        not in {"resume_source_plan", "resume_source_state", "human_input_request_ids"}
    }


def _create_workflow_tables() -> None:
    if not _has_table("workflow_executions"):
        op.create_table(
            "workflow_executions",
            sa.Column("workflow_id", sa.String(length=64), nullable=False),
            sa.Column("tenant_id", sa.String(length=128), nullable=False),
            sa.Column("project_id", sa.String(length=128), nullable=True),
            sa.Column("issue_key", sa.String(length=64), nullable=False),
            sa.Column("issue_summary", sa.Text(), nullable=True),
            sa.Column("issue_description", sa.Text(), nullable=True),
            sa.Column("repo_url", sa.String(length=512), nullable=True),
            sa.Column("branch", sa.String(length=255), nullable=True),
            sa.Column("pr_url", sa.String(length=512), nullable=True),
            sa.Column("dedupe_scope", sa.String(length=32), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("active_run_id", sa.String(length=64), nullable=True),
            sa.Column("latest_checkpoint_id", sa.String(length=64), nullable=True),
            sa.Column("source_workflow_id", sa.String(length=64), nullable=True),
            sa.Column("source_run_id", sa.String(length=64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["project_id"], ["projects.project_id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(
                ["source_workflow_id"],
                ["workflow_executions.workflow_id"],
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("workflow_id"),
        )
    for index_name, columns in (
        ("ix_workflow_executions_tenant_id", ["tenant_id"]),
        ("ix_workflow_executions_project_id", ["project_id"]),
        ("ix_workflow_executions_issue_key", ["issue_key"]),
        ("ix_workflow_executions_status", ["status"]),
    ):
        if all(
            _has_column("workflow_executions", column) for column in columns
        ) and not _has_index(
            "workflow_executions",
            index_name,
        ):
            op.create_index(index_name, "workflow_executions", columns, unique=False)
    if _has_column("workflow_executions", "issue_key") and not _has_index(
        "workflow_executions",
        "uq_workflow_executions_active_scope",
    ):
        op.create_index(
            "uq_workflow_executions_active_scope",
            "workflow_executions",
            ["tenant_id", "issue_key", "dedupe_scope"],
            unique=True,
            postgresql_where=sa.text(
                "status IN ('queued', 'running', 'waiting_for_input')"
            ),
            sqlite_where=sa.text(
                "status IN ('queued', 'running', 'waiting_for_input')"
            ),
        )

    if not _has_table("workflow_checkpoints"):
        op.create_table(
            "workflow_checkpoints",
            sa.Column("checkpoint_id", sa.String(length=64), nullable=False),
            sa.Column("workflow_id", sa.String(length=64), nullable=False),
            sa.Column("run_id", sa.String(length=64), nullable=False),
            sa.Column("checkpoint_kind", sa.String(length=32), nullable=False),
            sa.Column("stage", sa.String(length=32), nullable=False),
            sa.Column(
                "payload_json",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            ),
            sa.Column("codex_session_id", sa.String(length=64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["workflow_id"], ["workflow_executions.workflow_id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("checkpoint_id"),
            sa.UniqueConstraint(
                "run_id", "checkpoint_kind", name="uq_workflow_checkpoints_run_kind"
            ),
        )
    for index_name, columns in (
        ("ix_workflow_checkpoints_workflow_id", ["workflow_id"]),
        ("ix_workflow_checkpoints_run_id", ["run_id"]),
        ("ix_workflow_checkpoints_kind", ["checkpoint_kind"]),
        ("ix_workflow_checkpoints_created_at", ["created_at"]),
    ):
        if not _has_index("workflow_checkpoints", index_name):
            op.create_index(index_name, "workflow_checkpoints", columns, unique=False)


def _add_attempt_columns() -> None:
    with op.batch_alter_table("runs") as batch_op:
        if not _has_column("runs", "workflow_id"):
            batch_op.add_column(
                sa.Column("workflow_id", sa.String(length=64), nullable=True)
            )
        if not _has_column("runs", "attempt_number"):
            batch_op.add_column(
                sa.Column("attempt_number", sa.Integer(), nullable=True)
            )
        if not _has_column("runs", "parent_run_id"):
            batch_op.add_column(
                sa.Column("parent_run_id", sa.String(length=64), nullable=True)
            )
        if not _has_column("runs", "entry_mode"):
            batch_op.add_column(
                sa.Column("entry_mode", sa.String(length=32), nullable=True)
            )
        if not _has_column("runs", "entry_stage"):
            batch_op.add_column(
                sa.Column("entry_stage", sa.String(length=32), nullable=True)
            )
        if not _has_column("runs", "entry_checkpoint_id"):
            batch_op.add_column(
                sa.Column("entry_checkpoint_id", sa.String(length=64), nullable=True)
            )

    if not _has_index("runs", "ix_runs_workflow_id"):
        op.create_index("ix_runs_workflow_id", "runs", ["workflow_id"], unique=False)
    if not _has_index("runs", "ix_runs_workflow_attempt"):
        op.create_index(
            "ix_runs_workflow_attempt",
            "runs",
            ["workflow_id", "attempt_number"],
            unique=True,
        )


def _add_input_request_columns() -> None:
    if not _has_table("run_human_input_requests"):
        return
    with op.batch_alter_table("run_human_input_requests") as batch_op:
        if not _has_column("run_human_input_requests", "workflow_id"):
            batch_op.add_column(
                sa.Column("workflow_id", sa.String(length=64), nullable=True)
            )
        if not _has_column("run_human_input_requests", "checkpoint_id"):
            batch_op.add_column(
                sa.Column("checkpoint_id", sa.String(length=64), nullable=True)
            )
        if not _has_column("run_human_input_requests", "consumed_by_run_id"):
            batch_op.add_column(
                sa.Column("consumed_by_run_id", sa.String(length=64), nullable=True)
            )


def _backfill_workflows_and_attempts() -> None:
    bind = op.get_bind()
    rows = (
        bind.execute(
            text(
                """
            SELECT run_id, workflow_id, entry_checkpoint_id, tenant_id, project_id, issue_key, issue_summary,
                   issue_description, repo_url, branch, pr_url, dedupe_scope, status, last_error, plan, created_at,
                   started_at, finished_at
            FROM runs
            ORDER BY created_at, run_id
            """
            )
        )
        .mappings()
        .all()
    )

    for row in rows:
        workflow_id = str(row["workflow_id"] or "").strip() or f"wf-{row['run_id']}"
        checkpoint_id = (
            str(row["entry_checkpoint_id"] or "").strip() or f"wfc-{row['run_id']}"
        )
        scrubbed_plan = _scrub_payload_dict(_loads_json(row["plan"]))
        created_at = row["created_at"] or _now()
        started_at = row["started_at"]
        finished_at = row["finished_at"]
        updated_at = finished_at or started_at or created_at
        active_run_id = (
            row["run_id"] if str(row["status"]) in _WORKFLOW_ACTIVE_STATUSES else None
        )
        workflow_status = (
            "failed"
            if str(row["status"] or "").strip().lower() == "blocked"
            else row["status"]
        )
        workflow_last_error = row["last_error"]

        workflow_exists = bind.execute(
            text("SELECT 1 FROM workflow_executions WHERE workflow_id = :workflow_id"),
            {"workflow_id": workflow_id},
        ).scalar_one_or_none()
        if workflow_exists is None:
            bind.execute(
                text(
                    """
                    INSERT INTO workflow_executions (
                        workflow_id, tenant_id, project_id, issue_key, issue_summary, issue_description, repo_url,
                        branch, pr_url, dedupe_scope, status, active_run_id, latest_checkpoint_id, source_workflow_id,
                        source_run_id, created_at, started_at, finished_at, updated_at, last_error
                    ) VALUES (
                        :workflow_id, :tenant_id, :project_id, :issue_key, :issue_summary, :issue_description, :repo_url,
                        :branch, :pr_url, :dedupe_scope, :status, :active_run_id, :latest_checkpoint_id, NULL,
                        NULL, :created_at, :started_at, :finished_at, :updated_at, :last_error
                    )
                    """
                ),
                {
                    "workflow_id": workflow_id,
                    "tenant_id": row["tenant_id"],
                    "project_id": row["project_id"],
                    "issue_key": row["issue_key"],
                    "issue_summary": row["issue_summary"],
                    "issue_description": row["issue_description"],
                    "repo_url": row["repo_url"],
                    "branch": row["branch"],
                    "pr_url": row["pr_url"],
                    "dedupe_scope": row["dedupe_scope"],
                    "status": workflow_status,
                    "active_run_id": active_run_id,
                    "latest_checkpoint_id": checkpoint_id,
                    "created_at": created_at,
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "updated_at": updated_at,
                    "last_error": workflow_last_error,
                },
            )
        else:
            bind.execute(
                text(
                    """
                    UPDATE workflow_executions
                    SET status = :status,
                        last_error = :last_error,
                        active_run_id = :active_run_id,
                        latest_checkpoint_id = :latest_checkpoint_id,
                        branch = :branch,
                        pr_url = :pr_url,
                        finished_at = :finished_at,
                        updated_at = :updated_at
                    WHERE workflow_id = :workflow_id
                    """
                ),
                {
                    "status": workflow_status,
                    "last_error": workflow_last_error,
                    "active_run_id": active_run_id,
                    "latest_checkpoint_id": checkpoint_id,
                    "branch": row["branch"],
                    "pr_url": row["pr_url"],
                    "finished_at": finished_at,
                    "updated_at": updated_at,
                    "workflow_id": workflow_id,
                },
            )

        checkpoint_exists = bind.execute(
            text(
                "SELECT 1 FROM workflow_checkpoints WHERE checkpoint_id = :checkpoint_id"
            ),
            {"checkpoint_id": checkpoint_id},
        ).scalar_one_or_none()
        if checkpoint_exists is None:
            bind.execute(
                text(
                    """
                    INSERT INTO workflow_checkpoints (
                        checkpoint_id, workflow_id, run_id, checkpoint_kind, stage, payload_json, codex_session_id,
                        created_at, updated_at
                    ) VALUES (
                        :checkpoint_id, :workflow_id, :run_id, 'orchestrated', 'orchestrated', :payload_json, NULL,
                        :created_at, :updated_at
                    )
                    """
                ),
                {
                    "checkpoint_id": checkpoint_id,
                    "workflow_id": workflow_id,
                    "run_id": row["run_id"],
                    "payload_json": _dumps_json(scrubbed_plan),
                    "created_at": created_at,
                    "updated_at": updated_at,
                },
            )
        else:
            bind.execute(
                text(
                    """
                    UPDATE workflow_checkpoints
                    SET payload_json = :payload_json,
                        updated_at = :updated_at
                    WHERE checkpoint_id = :checkpoint_id
                    """
                ),
                {
                    "payload_json": _dumps_json(scrubbed_plan),
                    "updated_at": updated_at,
                    "checkpoint_id": checkpoint_id,
                },
            )
        bind.execute(
            text(
                """
                UPDATE runs
                SET workflow_id = :workflow_id,
                    attempt_number = 1,
                    parent_run_id = NULL,
                    entry_mode = 'fresh',
                    entry_stage = 'orchestrated',
                    entry_checkpoint_id = :checkpoint_id,
                    plan = :plan
                WHERE run_id = :run_id
                """
            ),
            {
                "workflow_id": workflow_id,
                "checkpoint_id": checkpoint_id,
                "plan": _dumps_json(scrubbed_plan) if scrubbed_plan else None,
                "run_id": row["run_id"],
            },
        )

    with op.batch_alter_table("runs") as batch_op:
        batch_op.alter_column(
            "workflow_id", existing_type=sa.String(length=64), nullable=False
        )
        batch_op.alter_column(
            "attempt_number", existing_type=sa.Integer(), nullable=False
        )
        batch_op.alter_column(
            "entry_mode", existing_type=sa.String(length=32), nullable=False
        )
        batch_op.alter_column(
            "entry_stage", existing_type=sa.String(length=32), nullable=False
        )


def _backfill_input_requests() -> None:
    if not _has_table("run_human_input_requests"):
        return
    bind = op.get_bind()
    resumed_column = (
        "resumed_run_id"
        if _has_column("run_human_input_requests", "resumed_run_id")
        else "consumed_by_run_id"
    )
    rows = (
        bind.execute(
            text(
                """
            SELECT request_id, source_run_id, workflow_id, checkpoint_id, status, request_context_json, """
                + resumed_column
                + """
            FROM run_human_input_requests
            ORDER BY created_at, request_id
            """
            )
        )
        .mappings()
        .all()
    )
    for row in rows:
        source_run = (
            bind.execute(
                text(
                    """
                SELECT workflow_id, entry_checkpoint_id
                FROM runs
                WHERE run_id = :run_id
                """
                ),
                {"run_id": row["source_run_id"]},
            )
            .mappings()
            .one()
        )
        next_status = row["status"]
        consumed_by_run_id = row[resumed_column]
        if str(row["status"]) == "answered" and consumed_by_run_id:
            next_status = "consumed"
        scrubbed_context = _scrub_request_context(
            _loads_json(row["request_context_json"])
        )
        bind.execute(
            text(
                """
                UPDATE run_human_input_requests
                SET workflow_id = :workflow_id,
                    checkpoint_id = :checkpoint_id,
                    consumed_by_run_id = :consumed_by_run_id,
                    status = :status,
                    request_context_json = :request_context_json
                WHERE request_id = :request_id
                """
            ),
            {
                "workflow_id": row["workflow_id"] or source_run["workflow_id"],
                "checkpoint_id": row["checkpoint_id"]
                or source_run["entry_checkpoint_id"],
                "consumed_by_run_id": consumed_by_run_id,
                "status": next_status,
                "request_context_json": _dumps_json(scrubbed_context),
                "request_id": row["request_id"],
            },
        )

    with op.batch_alter_table("run_human_input_requests") as batch_op:
        batch_op.alter_column(
            "workflow_id", existing_type=sa.String(length=64), nullable=False
        )
        batch_op.alter_column(
            "checkpoint_id", existing_type=sa.String(length=64), nullable=False
        )

    for index_name, columns in (
        ("ix_run_human_input_requests_workflow_id", ["workflow_id"]),
        ("ix_run_human_input_requests_checkpoint_id", ["checkpoint_id"]),
        ("ix_run_human_input_requests_consumed_by_run_id", ["consumed_by_run_id"]),
    ):
        if not _has_index("run_human_input_requests", index_name):
            op.create_index(
                index_name, "run_human_input_requests", columns, unique=False
            )

    if not _has_index(
        "run_human_input_requests", "uq_run_human_input_requests_pending_workflow"
    ):
        op.create_index(
            "uq_run_human_input_requests_pending_workflow",
            "run_human_input_requests",
            ["workflow_id"],
            unique=True,
            postgresql_where=sa.text("status = 'pending'"),
            sqlite_where=sa.text("status = 'pending'"),
        )


def _drop_legacy_run_columns() -> None:
    _drop_index_if_exists("runs", "ix_runs_dev_session_id")
    _drop_index_if_exists("runs", "ix_runs_pm_session_id")
    _drop_index_if_exists("runs", "ix_runs_orchestrated_session_id")
    with op.batch_alter_table("runs") as batch_op:
        if _has_column("runs", "dev_session_id"):
            batch_op.drop_column("dev_session_id")
        if _has_column("runs", "pm_session_id"):
            batch_op.drop_column("pm_session_id")
        if _has_column("runs", "orchestrated_session_id"):
            batch_op.drop_column("orchestrated_session_id")


def _drop_legacy_input_request_columns() -> None:
    if not _has_table("run_human_input_requests"):
        return
    _drop_index_if_exists(
        "run_human_input_requests", "ix_run_human_input_requests_resumed_run_id"
    )
    with op.batch_alter_table("run_human_input_requests") as batch_op:
        if _has_column("run_human_input_requests", "resumed_run_id"):
            batch_op.drop_column("resumed_run_id")
        if _has_column("run_human_input_requests", "resume_stage"):
            batch_op.drop_column("resume_stage")
        if _has_column("run_human_input_requests", "resume_session_id"):
            batch_op.drop_column("resume_session_id")


def _drop_run_locks() -> None:
    if not _has_table("run_locks"):
        return
    _drop_index_if_exists("run_locks", "ix_run_locks_run_id")
    op.drop_table("run_locks")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": _WORKFLOW_EXECUTION_LOCK_KEY},
        )

    _create_workflow_tables()
    _add_attempt_columns()
    _add_input_request_columns()
    _backfill_workflows_and_attempts()
    _backfill_input_requests()
    _drop_legacy_run_columns()
    _drop_legacy_input_request_columns()
    _drop_run_locks()


def downgrade() -> None:
    raise RuntimeError("Downgrade is not supported for workflow execution migration")
