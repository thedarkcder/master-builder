"""Persisted Discord command references match the explicit public schema."""

from copy import deepcopy

from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa


def migration():
    config = Config()
    config.set_main_option("script_location", "orchestrator/storage/migrations")
    return ScriptDirectory.from_config(config).get_revision("20261002_0137").module


@pytest.mark.parametrize(
    "reference, expected", [(" token-ref ", "token-ref"), (" ", None), (None, None)]
)
def test_reference_normalization_preserves_unrelated_configuration(reference, expected):
    original = {
        "command_secret_ref": reference,
        "channel_id": "test-channel",
        "ask_history": ["retained"],
    }
    saved = deepcopy(original)
    updated = migration().normalize_discord_config(original)
    assert updated["command_secret_ref"] == expected
    assert updated["channel_id"] == "test-channel"
    assert updated["ask_history"] == ["retained"]
    assert original == saved
    assert migration().normalize_discord_config(updated) == updated


@pytest.mark.parametrize("reference", [[], 123, "ref with spaces", "x" * 256])
def test_malformed_reference_blocks_without_disclosing_the_value(reference):
    with pytest.raises(ValueError, match="Review Discord command references"):
        migration().normalize_discord_config({"command_secret_ref": reference})


def test_upgrade_rewrites_actual_json_rows_and_does_not_invent_references():
    module = migration()
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    tenants = sa.Table(
        "tenants",
        metadata,
        sa.Column("tenant_id", sa.String(), primary_key=True),
        sa.Column("discord_config", sa.JSON()),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            tenants.insert(),
            [
                {
                    "tenant_id": "configured",
                    "discord_config": {
                        "command_secret_ref": " token-ref ",
                        "channel_id": "retained",
                    },
                },
                {"tenant_id": "missing", "discord_config": {"channel_id": "retained"}},
                {"tenant_id": "empty", "discord_config": None},
            ],
        )
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
            module.upgrade()
        rows = dict(
            connection.execute(
                sa.select(tenants.c.tenant_id, tenants.c.discord_config)
            ).all()
        )
        assert rows["configured"] == {
            "command_secret_ref": "token-ref",
            "channel_id": "retained",
        }
        assert rows["missing"] == {"channel_id": "retained"}
        assert rows["empty"] is None
    engine.dispose()


def test_malformed_later_tenant_prevents_partial_updates():
    module = migration()
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    tenants = sa.Table(
        "tenants",
        metadata,
        sa.Column("tenant_id", sa.String(), primary_key=True),
        sa.Column("discord_config", sa.JSON()),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            tenants.insert(),
            [
                {
                    "tenant_id": "first",
                    "discord_config": {"command_secret_ref": " token-ref "},
                },
                {
                    "tenant_id": "second",
                    "discord_config": {"command_secret_ref": ["invalid"]},
                },
            ],
        )
        with Operations.context(MigrationContext.configure(connection)):
            with pytest.raises(ValueError, match="Review Discord command references"):
                module.upgrade()
        first = connection.execute(
            sa.select(tenants.c.discord_config).where(tenants.c.tenant_id == "first")
        ).scalar_one()
        assert first == {"command_secret_ref": " token-ref "}
    engine.dispose()
