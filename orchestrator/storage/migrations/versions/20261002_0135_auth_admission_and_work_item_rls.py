"""Persist anonymous admission budgets and enforce executable work-item isolation."""

from alembic.script import ScriptDirectory

from alembic import op
import sqlalchemy as sa

revision = "20261002_0135"
down_revision = "20261002_0134"
branch_labels = None
depends_on = None


def _admission_schema() -> None:
    inspector = sa.inspect(op.get_bind())
    table = "auth_request_budgets"
    if inspector.has_table(table):
        # Replayed migrations must match the exact owned schema, never guess or
        # accept an unrelated table simply because its name already exists.
        columns = {column["name"]: column for column in inspector.get_columns(table)}
        expected = {
            "bucket_key": "VARCHAR(64)",
            "window_start": "BIGINT",
            "expires_at": "BIGINT",
            "attempts": "INTEGER",
        }
        checks = inspector.get_check_constraints(table)
        if (
            set(columns) != set(expected)
            or any(
                str(columns[name]["type"]).upper() != kind or columns[name]["nullable"]
                for name, kind in expected.items()
            )
            or inspector.get_pk_constraint(table)["constrained_columns"]
            != ["bucket_key"]
            or not any(
                check["name"] == "ck_auth_budget_positive"
                and "".join(check["sqltext"].split()).strip("()") == "attempts>0"
                for check in checks
            )
        ):
            raise RuntimeError(
                "Existing auth admission schema does not satisfy migration 0135; review the schema before retrying."
            )
    else:
        op.create_table(
            table,
            sa.Column("bucket_key", sa.String(64), primary_key=True),
            sa.Column("window_start", sa.BigInteger(), nullable=False),
            sa.Column("expires_at", sa.BigInteger(), nullable=False),
            sa.Column("attempts", sa.Integer(), nullable=False),
            sa.CheckConstraint("attempts > 0", name="ck_auth_budget_positive"),
        )
    indexes = {
        index["name"]: index for index in sa.inspect(op.get_bind()).get_indexes(table)
    }
    name = "ix_auth_request_budgets_expires_at"
    if name in indexes:
        if indexes[name]["column_names"] != ["expires_at"] or indexes[name]["unique"]:
            raise RuntimeError("Existing auth admission schema expiry index is invalid")
    else:
        op.create_index(name, table, ["expires_at"])


def upgrade() -> None:
    _admission_schema()
    if op.get_bind().dialect.name != "postgresql":
        return
    policy_module = (
        ScriptDirectory.from_config(op.get_context().config)
        .get_revision("20260424_0093")
        .module
    )
    policies = (
        policy_module._direct_policy("workflow_executable_work_items"),
        policy_module._Policy(
            table_name="auth_request_budgets",
            policy_name="auth_request_budgets_identity_admission",
            using_expression="(current_setting('app.principal_type', true) IN ('identity_auth', 'platform_admin', 'platform_system'))",
        ),
    )
    op.execute(
        "COMMENT ON TABLE auth_request_budgets IS 'tenant_rls:protected; anonymous identity admission only'"
    )
    for policy in policies:
        table = policy_module._quote_identifier(policy.table_name)
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        for statement in policy_module._policy_sql(policy):
            op.execute(statement)


def downgrade() -> None:
    raise RuntimeError(
        "Security admission and RLS migration cannot be downgraded; deploy a reviewed forward migration."
    )
