from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "sqlite:///./orchestrator.db"
    log_level: str = "INFO"
    admin_username: str = "admin"
    admin_password: str = "change-me"
    admin_token_secret: str = "local-dev-admin-token-secret"
    admin_token_ttl_seconds: int = 28800
    cors_origins: str = "http://localhost:4100,http://127.0.0.1:4100"
    admin_ui_base_url: str = "http://localhost:4100"
    github_install_state_secret: str = "local-dev-change-me"
    public_api_base_url: str = "http://localhost:4000"
    jira_oauth_state_secret: str = "local-dev-change-me"
    secrets_encryption_key: str = ""
    required_codex_assets_version: str = ""

    model_config = SettingsConfigDict(
        env_prefix="ORCHESTRATOR_",
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
