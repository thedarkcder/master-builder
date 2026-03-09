from functools import lru_cache
from typing import Literal

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
    public_api_base_url: str = "http://localhost:4000"
    jira_oauth_state_secret: str = "local-dev-change-me"
    secrets_encryption_key: str = ""
    discord_guild_id: str = ""
    discord_channel_name_template: str = "{project_name}"
    discord_channel_category_id: str = ""
    codex_cli_command: str = "codex"
    codex_sandbox_mode: str = "workspace-write"
    codex_model: str = "gpt-5-codex"
    codex_reasoning_effort: Literal["low", "medium", "high"] = "medium"
    codex_stderr_log_mode: Literal["all", "errors_only", "off"] = "errors_only"
    codex_db_log_sampling_interval: int = 100
    codex_persist_turn_completed_usage: bool = True
    codex_raw_log_path: str = "/tmp/orchestrator-codex.ndjson"
    codex_max_output_tokens: int = 1800
    codex_hang_detection_quiet_seconds: int = 300
    codex_hang_detection_report_interval_seconds: int = 120
    codex_log_batch_size: int = 50
    codex_log_batch_flush_ms: int = 50
    workflow_max_parallel_workstreams: int = 3
    workflow_orchestrated_run_timeout_minutes: int = 90
    # Deprecated: retained for backward compatibility with existing env vars.
    workflow_one_shot_timeout_minutes: int = 90
    run_events_initial_limit: int = 100
    run_logs_initial_limit: int = 200
    project_repo_checkout_base_dir: str = "/tmp/master-builder-project-repos"
    worker_poll_interval_seconds: int = 5
    discord_gateway_lock_key: int = 947102033127
    discord_gateway_poll_seconds: int = 3
    auto_migrate_on_startup: bool = True
    sentry_dsn: str = ""
    sentry_environment: str = "dev"
    sentry_release: str = ""
    sentry_traces_sample_rate: float = 0.0
    agent_id: str = "worker-linux-local"
    worker_capabilities: str = "linux"

    model_config = SettingsConfigDict(
        env_prefix="ORCHESTRATOR_",
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
