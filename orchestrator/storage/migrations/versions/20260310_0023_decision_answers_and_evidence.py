"""decision answer and evidence tables

Revision ID: 20260310_0023
Revises: 20260309_0022
Create Date: 2026-03-10
"""

from alembic import op
import sqlalchemy as sa


revision = "20260310_0023"
down_revision = "20260309_0022"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def _index_exists(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(
        index.get("name") == index_name for index in inspector.get_indexes(table_name)
    )


def _create_index_if_missing(
    index_name: str, table_name: str, columns: list[str], *, unique: bool = False
) -> None:
    if _table_exists(table_name) and not _index_exists(table_name, index_name):
        op.create_index(index_name, table_name, columns, unique=unique)


def _drop_index_if_exists(index_name: str, table_name: str) -> None:
    if _table_exists(table_name) and _index_exists(table_name, index_name):
        op.drop_index(index_name, table_name=table_name)


def upgrade() -> None:
    if not _table_exists("decision_answers"):
        op.create_table(
            "decision_answers",
            sa.Column("answer_id", sa.String(length=64), nullable=False),
            sa.Column("case_id", sa.String(length=64), nullable=False),
            sa.Column("cycle_id", sa.String(length=64), nullable=False),
            sa.Column("tenant_id", sa.String(length=128), nullable=False),
            sa.Column("project_id", sa.String(length=128), nullable=True),
            sa.Column("issue_key", sa.String(length=64), nullable=False),
            sa.Column("question_id", sa.String(length=128), nullable=False),
            sa.Column("question_kind", sa.String(length=32), nullable=False),
            sa.Column("question_text", sa.Text(), nullable=False),
            sa.Column(
                "status",
                sa.String(length=16),
                nullable=False,
                server_default=sa.text("'open'"),
            ),
            sa.Column("normalized_answer", sa.Text(), nullable=True),
            sa.Column("source_transport", sa.String(length=32), nullable=True),
            sa.Column("source_ref", sa.String(length=255), nullable=True),
            sa.Column("evidence_ids_json", sa.JSON(), nullable=False),
            sa.Column("metadata_json", sa.JSON(), nullable=False),
            sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["case_id"], ["decision_cases.case_id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["cycle_id"], ["decision_cycles.cycle_id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["project_id"], ["projects.project_id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(
                ["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("answer_id"),
            sa.UniqueConstraint(
                "cycle_id", "question_id", name="uq_decision_answers_cycle_question"
            ),
        )
    _create_index_if_missing(
        "ix_decision_answers_case_id", "decision_answers", ["case_id"]
    )
    _create_index_if_missing(
        "ix_decision_answers_cycle_id", "decision_answers", ["cycle_id"]
    )
    _create_index_if_missing(
        "ix_decision_answers_tenant_id", "decision_answers", ["tenant_id"]
    )
    _create_index_if_missing(
        "ix_decision_answers_project_id", "decision_answers", ["project_id"]
    )
    _create_index_if_missing(
        "ix_decision_answers_issue_key", "decision_answers", ["issue_key"]
    )
    _create_index_if_missing(
        "ix_decision_answers_question_id", "decision_answers", ["question_id"]
    )
    _create_index_if_missing(
        "ix_decision_answers_question_kind", "decision_answers", ["question_kind"]
    )
    _create_index_if_missing(
        "ix_decision_answers_status", "decision_answers", ["status"]
    )
    _create_index_if_missing(
        "ix_decision_answers_source_transport", "decision_answers", ["source_transport"]
    )
    _create_index_if_missing(
        "ix_decision_answers_source_ref", "decision_answers", ["source_ref"]
    )
    _create_index_if_missing(
        "ix_decision_answers_answered_at", "decision_answers", ["answered_at"]
    )
    _create_index_if_missing(
        "ix_decision_answers_accepted_at", "decision_answers", ["accepted_at"]
    )
    _create_index_if_missing(
        "ix_decision_answers_updated_at", "decision_answers", ["updated_at"]
    )

    if not _table_exists("decision_evidence"):
        op.create_table(
            "decision_evidence",
            sa.Column("evidence_id", sa.String(length=64), nullable=False),
            sa.Column("dedupe_key", sa.String(length=255), nullable=False),
            sa.Column("case_id", sa.String(length=64), nullable=False),
            sa.Column("cycle_id", sa.String(length=64), nullable=False),
            sa.Column("tenant_id", sa.String(length=128), nullable=False),
            sa.Column("project_id", sa.String(length=128), nullable=True),
            sa.Column("issue_key", sa.String(length=64), nullable=False),
            sa.Column("source_transport", sa.String(length=32), nullable=False),
            sa.Column("source_ref", sa.String(length=255), nullable=True),
            sa.Column("actor_ref", sa.String(length=255), nullable=True),
            sa.Column("raw_text", sa.Text(), nullable=False),
            sa.Column("question_ids_json", sa.JSON(), nullable=False),
            sa.Column("normalized_answers_json", sa.JSON(), nullable=False),
            sa.Column("metadata_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["case_id"], ["decision_cases.case_id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["cycle_id"], ["decision_cycles.cycle_id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["project_id"], ["projects.project_id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(
                ["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("evidence_id"),
            sa.UniqueConstraint(
                "cycle_id", "dedupe_key", name="uq_decision_evidence_cycle_dedupe"
            ),
        )
    _create_index_if_missing(
        "ix_decision_evidence_dedupe_key", "decision_evidence", ["dedupe_key"]
    )
    _create_index_if_missing(
        "ix_decision_evidence_case_id", "decision_evidence", ["case_id"]
    )
    _create_index_if_missing(
        "ix_decision_evidence_cycle_id", "decision_evidence", ["cycle_id"]
    )
    _create_index_if_missing(
        "ix_decision_evidence_tenant_id", "decision_evidence", ["tenant_id"]
    )
    _create_index_if_missing(
        "ix_decision_evidence_project_id", "decision_evidence", ["project_id"]
    )
    _create_index_if_missing(
        "ix_decision_evidence_issue_key", "decision_evidence", ["issue_key"]
    )
    _create_index_if_missing(
        "ix_decision_evidence_source_transport",
        "decision_evidence",
        ["source_transport"],
    )
    _create_index_if_missing(
        "ix_decision_evidence_source_ref", "decision_evidence", ["source_ref"]
    )
    _create_index_if_missing(
        "ix_decision_evidence_actor_ref", "decision_evidence", ["actor_ref"]
    )
    _create_index_if_missing(
        "ix_decision_evidence_created_at", "decision_evidence", ["created_at"]
    )


def downgrade() -> None:
    _drop_index_if_exists("ix_decision_evidence_created_at", "decision_evidence")
    _drop_index_if_exists("ix_decision_evidence_actor_ref", "decision_evidence")
    _drop_index_if_exists("ix_decision_evidence_source_ref", "decision_evidence")
    _drop_index_if_exists("ix_decision_evidence_source_transport", "decision_evidence")
    _drop_index_if_exists("ix_decision_evidence_issue_key", "decision_evidence")
    _drop_index_if_exists("ix_decision_evidence_project_id", "decision_evidence")
    _drop_index_if_exists("ix_decision_evidence_tenant_id", "decision_evidence")
    _drop_index_if_exists("ix_decision_evidence_cycle_id", "decision_evidence")
    _drop_index_if_exists("ix_decision_evidence_case_id", "decision_evidence")
    _drop_index_if_exists("ix_decision_evidence_dedupe_key", "decision_evidence")
    if _table_exists("decision_evidence"):
        op.drop_table("decision_evidence")

    _drop_index_if_exists("ix_decision_answers_updated_at", "decision_answers")
    _drop_index_if_exists("ix_decision_answers_accepted_at", "decision_answers")
    _drop_index_if_exists("ix_decision_answers_answered_at", "decision_answers")
    _drop_index_if_exists("ix_decision_answers_source_ref", "decision_answers")
    _drop_index_if_exists("ix_decision_answers_source_transport", "decision_answers")
    _drop_index_if_exists("ix_decision_answers_status", "decision_answers")
    _drop_index_if_exists("ix_decision_answers_question_kind", "decision_answers")
    _drop_index_if_exists("ix_decision_answers_question_id", "decision_answers")
    _drop_index_if_exists("ix_decision_answers_issue_key", "decision_answers")
    _drop_index_if_exists("ix_decision_answers_project_id", "decision_answers")
    _drop_index_if_exists("ix_decision_answers_tenant_id", "decision_answers")
    _drop_index_if_exists("ix_decision_answers_cycle_id", "decision_answers")
    _drop_index_if_exists("ix_decision_answers_case_id", "decision_answers")
    if _table_exists("decision_answers"):
        op.drop_table("decision_answers")
