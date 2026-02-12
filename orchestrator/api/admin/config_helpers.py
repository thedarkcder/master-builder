from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException, status

from orchestrator.core.enforcement_context import EnforcementAssetsError, validate_enforcement_assets


def with_managed_github_refs(*, raw_github_config: dict, settings) -> dict:  # noqa: ANN001
    github_config = dict(raw_github_config)
    github_config["app_id_ref"] = settings.github_app_id_ref
    github_config["private_key_ref"] = settings.github_private_key_ref
    return github_config


def validate_codex_assets_for_tenant_init(
    *,
    settings,
    module_file: str,
    validate_enforcement_assets_fn=validate_enforcement_assets,
) -> None:  # noqa: ANN001
    module_path = Path(module_file).resolve()
    repo_root = module_path.parent
    for candidate in [repo_root, *repo_root.parents]:
        if (candidate / ".codex").exists():
            repo_root = candidate
            break
    try:
        validate_enforcement_assets_fn(
            repo_root=repo_root,
            required_assets_version=settings.required_codex_assets_version,
        )
    except EnforcementAssetsError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Codex assets validation failed: {exc}",
        ) from exc


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
