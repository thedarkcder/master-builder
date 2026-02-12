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
    github_app_slug: str = ""
    github_app_id_ref: str = "GITHUB_APP_ID"
    github_private_key_ref: str = "GITHUB_APP_PRIVATE_KEY"
    public_api_base_url: str = "http://localhost:4000"
    jira_oauth_state_secret: str = "local-dev-change-me"
    jira_oauth_client_id_ref: str = "JIRA_OAUTH_CLIENT_ID"
    jira_oauth_client_secret_ref: str = "JIRA_OAUTH_CLIENT_SECRET"
    secrets_encryption_key: str = ""
    required_codex_assets_version: str = ""
    discord_guild_id: str = ""
    discord_guild_id_secret_ref: str = "DISCORD_GUILD_ID"
    discord_channel_name_template: str = "{project_name}"
    discord_channel_category_id: str = ""
    discord_bot_token_secret_ref: str = "DISCORD_BOT_TOKEN"
    codex_cli_command: str = "codex"
    codex_model: str = "gpt-5-codex"
    codex_timeout_seconds: int = 120
    codex_max_output_tokens: int = 1800
    project_repo_checkout_base_dir: str = "/tmp/master-builder-project-repos"
    worker_poll_interval_seconds: int = 5
    auto_migrate_on_startup: bool = True
    sentry_dsn: str = ""
    sentry_environment: str = "dev"
    sentry_release: str = ""
    sentry_traces_sample_rate: float = 0.0
    agent_id: str = "worker-local"

    model_config = SettingsConfigDict(
        env_prefix="ORCHESTRATOR_",
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
