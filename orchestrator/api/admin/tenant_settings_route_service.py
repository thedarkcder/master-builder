from __future__ import annotations


def update_tenant_configuration(
    *,
    session,
    tenant_id: str,
    payload,
    validate_codex_assets_for_tenant_init_fn,
    update_tenant_configuration_fn,
    tenant_to_schema_fn,
):  # noqa: ANN001
    validate_codex_assets_for_tenant_init_fn()
    return update_tenant_configuration_fn(
        session=session,
        tenant_id=tenant_id,
        payload=payload,
        tenant_to_schema_fn=tenant_to_schema_fn,
    )


def update_tenant_jira(
    *,
    session,
    tenant_id: str,
    payload,
    validate_codex_assets_for_tenant_init_fn,
    update_tenant_jira_fn,
    with_preserved_jira_system_fields_fn,
    reconcile_tenant_projects_fn,
    tenant_to_schema_fn,
):  # noqa: ANN001
    validate_codex_assets_for_tenant_init_fn()
    return update_tenant_jira_fn(
        session=session,
        tenant_id=tenant_id,
        payload=payload,
        with_preserved_jira_system_fields_fn=with_preserved_jira_system_fields_fn,
        reconcile_tenant_projects_fn=reconcile_tenant_projects_fn,
        tenant_to_schema_fn=tenant_to_schema_fn,
    )


def update_tenant_github(
    *,
    session,
    tenant_id: str,
    payload,
    validate_codex_assets_for_tenant_init_fn,
    update_tenant_github_fn,
    with_managed_github_refs_fn,
    tenant_to_schema_fn,
):  # noqa: ANN001
    validate_codex_assets_for_tenant_init_fn()
    return update_tenant_github_fn(
        session=session,
        tenant_id=tenant_id,
        payload=payload,
        with_managed_github_refs_fn=with_managed_github_refs_fn,
        tenant_to_schema_fn=tenant_to_schema_fn,
    )


def update_tenant_repos(
    *,
    session,
    tenant_id: str,
    payload,
    validate_codex_assets_for_tenant_init_fn,
    update_tenant_repos_fn,
    reconcile_tenant_projects_fn,
    tenant_to_schema_fn,
):  # noqa: ANN001
    validate_codex_assets_for_tenant_init_fn()
    return update_tenant_repos_fn(
        session=session,
        tenant_id=tenant_id,
        payload=payload,
        reconcile_tenant_projects_fn=reconcile_tenant_projects_fn,
        tenant_to_schema_fn=tenant_to_schema_fn,
    )


def update_tenant_policy(
    *,
    session,
    tenant_id: str,
    payload,
    validate_codex_assets_for_tenant_init_fn,
    update_tenant_policy_fn,
    tenant_to_schema_fn,
):  # noqa: ANN001
    validate_codex_assets_for_tenant_init_fn()
    return update_tenant_policy_fn(
        session=session,
        tenant_id=tenant_id,
        payload=payload,
        tenant_to_schema_fn=tenant_to_schema_fn,
    )


def update_tenant_observability(
    *,
    session,
    tenant_id: str,
    payload,
    validate_codex_assets_for_tenant_init_fn,
    update_tenant_observability_fn,
    tenant_to_schema_fn,
):  # noqa: ANN001
    validate_codex_assets_for_tenant_init_fn()
    return update_tenant_observability_fn(
        session=session,
        tenant_id=tenant_id,
        payload=payload,
        tenant_to_schema_fn=tenant_to_schema_fn,
    )


def update_tenant_discord(
    *,
    session,
    tenant_id: str,
    payload,
    validate_codex_assets_for_tenant_init_fn,
    update_tenant_discord_fn,
    with_preserved_discord_system_fields_fn,
    tenant_to_schema_fn,
):  # noqa: ANN001
    validate_codex_assets_for_tenant_init_fn()
    return update_tenant_discord_fn(
        session=session,
        tenant_id=tenant_id,
        payload=payload,
        with_preserved_discord_system_fields_fn=with_preserved_discord_system_fields_fn,
        tenant_to_schema_fn=tenant_to_schema_fn,
    )


def update_tenant_experience(
    *,
    session,
    tenant_id: str,
    payload,
    validate_codex_assets_for_tenant_init_fn,
    update_tenant_experience_fn,
    tenant_to_schema_fn,
):  # noqa: ANN001
    validate_codex_assets_for_tenant_init_fn()
    return update_tenant_experience_fn(
        session=session,
        tenant_id=tenant_id,
        payload=payload,
        tenant_to_schema_fn=tenant_to_schema_fn,
    )
