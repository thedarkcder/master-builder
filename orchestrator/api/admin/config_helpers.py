from __future__ import annotations

from orchestrator.core.platform.secret_service import PLATFORM_SECRET_GITHUB_APP_ID_REF, PLATFORM_SECRET_GITHUB_PRIVATE_KEY_REF


def with_managed_github_refs(*, raw_github_config: dict, settings) -> dict:  # noqa: ANN001
    github_config = dict(raw_github_config)
    github_config["app_id_ref"] = PLATFORM_SECRET_GITHUB_APP_ID_REF
    github_config["private_key_ref"] = PLATFORM_SECRET_GITHUB_PRIVATE_KEY_REF
    return github_config


def validate_codex_assets_for_tenant_init(
    *,
    settings,
    module_file: str,
    validate_enforcement_assets_fn=None,
) -> None:  # noqa: ANN001
    _ = settings
    _ = module_file
    _ = validate_enforcement_assets_fn


def with_preserved_jira_system_fields(*, existing: dict, proposed: dict) -> dict:
    merged = dict(proposed)
    if "ready_trigger_mode" not in merged and "ready_trigger_mode" in existing:
        merged["ready_trigger_mode"] = existing.get("ready_trigger_mode")
    for key in (
        "managed_webhook_ids",
        "webhook_last_provisioned_at",
        "webhook_last_error",
        "webhook_last_received_at",
        "webhook_last_delivery_id",
        "webhook_last_issue_key",
    ):
        if key in existing:
            merged[key] = existing.get(key)
    return merged
