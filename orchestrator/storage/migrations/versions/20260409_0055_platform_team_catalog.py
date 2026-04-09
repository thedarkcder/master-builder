"""add platform team catalog tables

Revision ID: 20260409_0055
Revises: 20260407_0054
Create Date: 2026-04-09 12:10:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260409_0055"
down_revision = "20260407_0054"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    if not _table_exists("platform_personas"):
        op.create_table(
            "platform_personas",
            sa.Column("persona_id", sa.String(length=64), nullable=False),
            sa.Column("persona_key", sa.String(length=128), nullable=False),
            sa.Column("label", sa.String(length=255), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("default_display_name", sa.String(length=255), nullable=True),
            sa.Column("default_voice_id", sa.String(length=128), nullable=True),
            sa.Column("system_prompt_template", sa.String(length=255), nullable=True),
            sa.Column("user_prompt_template", sa.String(length=255), nullable=True),
            sa.Column("allowed_surfaces", sa.JSON(), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("persona_id"),
            sa.UniqueConstraint("persona_key", name="uq_platform_personas_persona_key"),
        )
        op.create_index("ix_platform_personas_persona_key", "platform_personas", ["persona_key"], unique=False)

    if not _table_exists("platform_agents"):
        op.create_table(
            "platform_agents",
            sa.Column("agent_id", sa.String(length=64), nullable=False),
            sa.Column("agent_key", sa.String(length=128), nullable=False),
            sa.Column("label", sa.String(length=255), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("persona_id", sa.String(length=64), nullable=False),
            sa.Column("runtime_role_key", sa.String(length=128), nullable=True),
            sa.Column("named_agent_key", sa.String(length=128), nullable=True),
            sa.Column("selector_key", sa.String(length=128), nullable=True),
            sa.Column("default_profile_name", sa.String(length=128), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["persona_id"], ["platform_personas.persona_id"], ondelete="RESTRICT"),
            sa.PrimaryKeyConstraint("agent_id"),
            sa.UniqueConstraint("agent_key", name="uq_platform_agents_agent_key"),
            sa.UniqueConstraint("named_agent_key", name="uq_platform_agents_named_agent_key"),
            sa.UniqueConstraint("selector_key", name="uq_platform_agents_selector_key"),
        )
        for index_name, columns in (
            ("ix_platform_agents_agent_key", ["agent_key"]),
            ("ix_platform_agents_persona_id", ["persona_id"]),
            ("ix_platform_agents_runtime_role_key", ["runtime_role_key"]),
            ("ix_platform_agents_named_agent_key", ["named_agent_key"]),
            ("ix_platform_agents_selector_key", ["selector_key"]),
        ):
            op.create_index(index_name, "platform_agents", columns, unique=False)

    if not _table_exists("platform_team_templates"):
        op.create_table(
            "platform_team_templates",
            sa.Column("template_id", sa.String(length=64), nullable=False),
            sa.Column("team_key", sa.String(length=128), nullable=False),
            sa.Column("label", sa.String(length=255), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("definition_version", sa.Integer(), nullable=False),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("template_id"),
            sa.UniqueConstraint("team_key", name="uq_platform_team_templates_team_key"),
        )
        op.create_index("ix_platform_team_templates_team_key", "platform_team_templates", ["team_key"], unique=False)

    if not _table_exists("platform_team_roles"):
        op.create_table(
            "platform_team_roles",
            sa.Column("role_id", sa.String(length=64), nullable=False),
            sa.Column("template_id", sa.String(length=64), nullable=False),
            sa.Column("role_key", sa.String(length=128), nullable=False),
            sa.Column("label", sa.String(length=255), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("persona_id", sa.String(length=64), nullable=False),
            sa.Column("agent_id", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["agent_id"], ["platform_agents.agent_id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["persona_id"], ["platform_personas.persona_id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["template_id"], ["platform_team_templates.template_id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("role_id"),
            sa.UniqueConstraint("template_id", "role_key", name="uq_platform_team_roles_template_role"),
        )
        op.create_index("ix_platform_team_roles_template_id", "platform_team_roles", ["template_id"], unique=False)
        op.create_index("ix_platform_team_roles_persona_id", "platform_team_roles", ["persona_id"], unique=False)
        op.create_index("ix_platform_team_roles_agent_id", "platform_team_roles", ["agent_id"], unique=False)
        op.create_index("ix_platform_team_roles_template_position", "platform_team_roles", ["template_id", "position"], unique=False)

    if not _table_exists("platform_team_tasks"):
        op.create_table(
            "platform_team_tasks",
            sa.Column("task_id", sa.String(length=64), nullable=False),
            sa.Column("template_id", sa.String(length=64), nullable=False),
            sa.Column("task_key", sa.String(length=128), nullable=False),
            sa.Column("label", sa.String(length=255), nullable=False),
            sa.Column("owner_role_key", sa.String(length=128), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("artifact_contract", sa.JSON(), nullable=False),
            sa.Column("approval_rule", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["template_id"], ["platform_team_templates.template_id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("task_id"),
            sa.UniqueConstraint("template_id", "task_key", name="uq_platform_team_tasks_template_task"),
        )
        op.create_index("ix_platform_team_tasks_template_id", "platform_team_tasks", ["template_id"], unique=False)
        op.create_index("ix_platform_team_tasks_owner_role_key", "platform_team_tasks", ["owner_role_key"], unique=False)
        op.create_index("ix_platform_team_tasks_template_position", "platform_team_tasks", ["template_id", "position"], unique=False)

    if not _table_exists("platform_team_edges"):
        op.create_table(
            "platform_team_edges",
            sa.Column("edge_id", sa.String(length=64), nullable=False),
            sa.Column("template_id", sa.String(length=64), nullable=False),
            sa.Column("from_task_key", sa.String(length=128), nullable=False),
            sa.Column("to_task_key", sa.String(length=128), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["template_id"], ["platform_team_templates.template_id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("edge_id"),
            sa.UniqueConstraint("template_id", "from_task_key", "to_task_key", name="uq_platform_team_edges_template_edge"),
        )
        op.create_index("ix_platform_team_edges_template_id", "platform_team_edges", ["template_id"], unique=False)
        op.create_index("ix_platform_team_edges_template_from", "platform_team_edges", ["template_id", "from_task_key"], unique=False)
        op.create_index("ix_platform_team_edges_template_to", "platform_team_edges", ["template_id", "to_task_key"], unique=False)


def downgrade() -> None:
    for table_name in (
        "platform_team_edges",
        "platform_team_tasks",
        "platform_team_roles",
        "platform_team_templates",
        "platform_agents",
        "platform_personas",
    ):
        if _table_exists(table_name):
            op.drop_table(table_name)
