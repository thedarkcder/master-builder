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
    cors_origin_regex: str = ""
    admin_ui_base_url: str = "http://localhost:4100"
    github_install_state_secret: str = "local-dev-change-me"
    github_app_slug: str = ""
    public_api_base_url: str = "http://localhost:4000"
    jira_oauth_state_secret: str = "local-dev-change-me"
    secrets_encryption_key: str = ""
    discord_guild_id: str = ""
    discord_channel_name_template: str = "{project_name}"
    discord_channel_category_id: str = ""
    voice_transcription_provider: Literal["disabled", "openai", "whisper"] = "disabled"
    voice_transcription_model: str = "gpt-4o-mini-transcribe"
    voice_transcription_language: str = ""
    voice_transcription_openai_api_key: str = ""
    voice_transcription_device: str = "auto"
    voice_transcription_compute_type: str = "int8"
    voice_attachment_max_bytes: int = 25000000
    voice_reply_provider: Literal["disabled", "pocket_tts"] = "disabled"
    voice_reply_enabled_default: bool = False
    pocket_tts_base_url: str = ""
    pocket_tts_voice: str = ""
    discord_live_voice_transport_command: str = "/usr/local/bin/live-voice-transport"
    discord_live_voice_transport_startup_timeout_seconds: int = 10
    discord_live_voice_transport_request_timeout_seconds: int = 10
    codex_cli_command: str = "codex"
    codex_sandbox_mode: str = "workspace-write"
    codex_model: str = "gpt-5.4"
    codex_supported_models: str = "gpt-5.4,gpt-5.3-codex,gpt-5.3-codex-spark"
    codex_tool_database_url: str = ""
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
    knowledge_injection_enabled: bool = True
    knowledge_base_enabled_default: bool = True
    knowledge_auto_answer_mode_default: Literal["safe", "balanced", "aggressive"] = "aggressive"
    knowledge_context_top_k: int = 5
    knowledge_context_max_chars: int = 3200
    knowledge_embedding_model: str = "BAAI/bge-small-en-v1.5"
    knowledge_jira_auto_sync_enabled: bool = True
    knowledge_jira_sync_interval_seconds: int = 3600
    knowledge_jira_sync_poll_seconds: int = 30
    knowledge_jira_sync_lock_key: int = 947102033128
    knowledge_jira_sync_max_issues: int = 500
    knowledge_jira_sync_invalid_token_backoff_seconds: int = 21600
    workflow_orchestrated_run_timeout_minutes: int = 90
    run_events_initial_limit: int = 100
    run_logs_initial_limit: int = 200
    project_repo_checkout_base_dir: str = "/tmp/master-builder-project-repos"
    worker_poll_interval_seconds: int = 5
    worker_run_heartbeat_interval_seconds: int = 30
    worker_run_stale_timeout_seconds: int = 300
    worker_stale_sweep_interval_seconds: int = 60
    discord_gateway_lock_key: int = 947102033127
    discord_gateway_poll_seconds: int = 3
    discord_live_voice_lock_key: int = 947102033129
    discord_live_voice_poll_seconds: int = 3
    auto_migrate_on_startup: bool = True
    sentry_dsn: str = ""
    sentry_environment: str = "dev"
    sentry_release: str = ""
    sentry_traces_sample_rate: float = 0.0
    agent_id: str = "worker-linux-local"
    worker_capabilities: str = "linux"
    worker_workspace_key: str = ""

    model_config = SettingsConfigDict(
        env_prefix="ORCHESTRATOR_",
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
