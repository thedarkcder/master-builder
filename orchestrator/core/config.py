from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "sqlite:///./orchestrator.db"
    log_level: str = "INFO"
    admin_username: str = "admin"
    admin_password: str = "change-me"

    model_config = SettingsConfigDict(
        env_prefix="ORCHESTRATOR_",
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
