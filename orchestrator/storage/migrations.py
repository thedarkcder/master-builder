from pathlib import Path

from alembic import command
from alembic.config import Config

from orchestrator.core.config import get_settings


def run_migrations(database_url: str | None = None) -> None:
    settings = get_settings()
    root = Path(__file__).resolve().parents[2]

    config = Config(str(root / "alembic.ini"))
    # Keep application logging configuration intact; Alembic's default fileConfig
    # would otherwise reset handlers/levels (which hides request logs).
    config.attributes["configure_logger"] = False
    config.set_main_option(
        "script_location",
        str(root / "orchestrator" / "storage" / "migrations"),
    )
    config.set_main_option("sqlalchemy.url", database_url or settings.database_url)
    command.upgrade(config, "head")
