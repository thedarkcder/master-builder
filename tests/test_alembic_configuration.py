from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest


@pytest.mark.parametrize("offline", [True, False])
def test_direct_alembic_rejects_missing_explicit_database_url(offline):
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.attributes["configure_logger"] = False
    config.set_main_option(
        "script_location", str(root / "orchestrator/storage/migrations")
    )
    config.set_main_option("sqlalchemy.url", "")
    with pytest.raises(
        ValueError,
        match="Configure the database URL using the orchestrator migrate command",
    ):
        command.upgrade(config, "head", sql=offline)
