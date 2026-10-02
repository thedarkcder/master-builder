"""Normalize existing Discord command references before exposing their schema."""

import re

from alembic import op
import sqlalchemy as sa

revision = "20261002_0137"
down_revision = "20261002_0136"
branch_labels = None
depends_on = None


def normalize_discord_config(config: dict | None) -> dict | None:
    if config is None:
        return None
    error = "Review Discord command references before migration 0137; malformed configuration cannot be inferred or discarded"
    if not isinstance(config, dict):
        raise ValueError(error)
    if "command_secret_ref" not in config:
        return dict(config)
    reference = config["command_secret_ref"]
    if reference is not None:
        if not isinstance(reference, str):
            raise ValueError(error)
        reference = reference.strip()
        # The ingress already trims strings and treats an empty reference as
        # unconfigured. Preserve that meaning without inventing a secret.
        if not reference:
            reference = None
        elif (
            len(reference) > 255
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:-]*", reference) is None
        ):
            raise ValueError(error)
    return dict(config, command_secret_ref=reference)


def upgrade() -> None:
    connection = op.get_bind()
    tenants = sa.Table("tenants", sa.MetaData(), autoload_with=connection)
    # Validate the complete batch before issuing any writes. Invalid records
    # require operator review; never partially erase authentication references.
    updates = []
    for tenant_id, config in connection.execute(
        sa.select(tenants.c.tenant_id, tenants.c.discord_config)
    ):
        normalized = normalize_discord_config(config)
        if normalized != config:
            updates.append((tenant_id, normalized))
    for tenant_id, config in updates:
        connection.execute(
            tenants.update()
            .where(tenants.c.tenant_id == tenant_id)
            .values(discord_config=config)
        )


def downgrade() -> None:
    raise RuntimeError(
        "Discord reference normalization is forward-only; restore a reviewed backup for rollback"
    )
