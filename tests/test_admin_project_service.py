from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from orchestrator.api.admin.project_normalization import (
    normalize_project_architecture_docs_config,
    normalize_project_discord_config,
    with_preserved_discord_system_fields,
)
from orchestrator.api.admin.project_service import AdminProjectService
from orchestrator.storage.models import Project
from orchestrator.tools.atlassian_oauth import AtlassianOAuthError
from orchestrator.tools.project_repo_checkout import ProjectRepoCheckoutError


class _Session:
    def __init__(self) -> None:
        self._objects: dict[tuple[type, str], object] = {}
        self.commits = 0
        self.rollbacks = 0
        self.deleted: list[object] = []
        self.added: list[object] = []
        self._projects = []

    def get(self, cls, key):  # noqa: ANN001
        return self._objects.get((cls, key))

    def set(self, cls, key: str, value: object) -> None:  # noqa: ANN001
        self._objects[(cls, key)] = value

    def execute(self, _query):  # noqa: ANN001
        return SimpleNamespace(
            all=lambda: self._projects,
            scalars=lambda: SimpleNamespace(
                all=lambda: self._projects,
                first=lambda: self._projects[0] if self._projects else None,
            )
        )

    def add(self, obj):  # noqa: ANN001
        self.added.append(obj)
        obj_id = getattr(obj, "project_id", None) or getattr(obj, "tenant_id", None)
        if isinstance(obj_id, str):
            self._objects[(type(obj), obj_id)] = obj

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def delete(self, obj):  # noqa: ANN001
        self.deleted.append(obj)
        obj_id = getattr(obj, "project_id", None) or getattr(obj, "tenant_id", None)
        if isinstance(obj_id, str):
            self._objects.pop((type(obj), obj_id), None)

    def refresh(self, _obj) -> None:  # noqa: ANN001
        return None


def _resolve_project_discord_channel_binding(
    *,
    session,
    settings,
    tenant,
    project,
    discord_config: dict,
) -> dict:  # noqa: ANN001
    _ = session, settings, tenant, project
    return discord_config


def _sync_tenant_jira_project_keys(session, *, tenant) -> None:  # noqa: ANN001
    _ = session, tenant


def _ensure_project_repository_checkout(*, session, tenant, project) -> None:  # noqa: ANN001
    _ = session, tenant, project


def _resolve_project_run_board_id(*, session, tenant, jira_project_key: str, settings) -> int | None:  # noqa: ANN001
    _ = session, tenant, jira_project_key, settings
    return None


def _project_to_schema(project, *, tenant_policy: dict) -> dict:  # noqa: ANN001
    _ = tenant_policy
    return {
        "project_id": project.project_id,
        "name": project.name,
        "architecture_docs": getattr(project, "architecture_docs_config", {}),
    }


def _service() -> AdminProjectService:
    return AdminProjectService(
        normalize_project_repo=lambda value: value.strip(),
        normalize_project_key=lambda value: value.strip().upper(),
        normalize_project_policy_overrides=lambda value: value or {},
        normalize_string_map=lambda value: value or {},
        normalize_project_architecture_docs_config=lambda value: value or {},
        normalize_project_discord_config=lambda value: value or {},
        with_preserved_discord_system_fields=lambda existing, proposed: {**existing, **proposed},
        resolve_project_discord_channel_binding=_resolve_project_discord_channel_binding,
        sync_tenant_jira_project_keys=_sync_tenant_jira_project_keys,
        ensure_project_repository_checkout=_ensure_project_repository_checkout,
        resolve_project_run_board_id=_resolve_project_run_board_id,
        project_to_schema=_project_to_schema,
        settings_factory=lambda: SimpleNamespace(),
    )


def test_list_projects_raises_when_tenant_missing() -> None:
    session = _Session()
    service = _service()
    try:
        service.list_projects(session=session, tenant_id="missing")
        assert False, "expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 404


def test_list_project_navigation_returns_lightweight_project_rows() -> None:
    from orchestrator.storage.models import Tenant

    session = _Session()
    session.set(Tenant, "t1", SimpleNamespace(tenant_id="t1", policy_config={}))
    session._projects = [
        SimpleNamespace(
            project_id="p1",
            tenant_id="t1",
            name="Alpha",
            jira_project_key="ALPHA",
            is_archived=False,
        )
    ]

    items = _service().list_project_navigation(session=session, tenant_id="t1")

    assert [item.model_dump() for item in items] == [
        {
            "project_id": "p1",
            "tenant_id": "t1",
            "name": "Alpha",
            "jira_project_key": "ALPHA",
            "is_archived": False,
        }
    ]


def test_get_project_raises_when_project_missing() -> None:
    from orchestrator.storage.models import Tenant

    session = _Session()
    session.set(Tenant, "t1", SimpleNamespace(policy_config={}))
    service = _service()
    try:
        service.get_project(session=session, tenant_id="t1", project_id="missing")
        assert False, "expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 404


def test_create_project_clones_repository_after_commit() -> None:
    from orchestrator.storage.models import Tenant

    session = _Session()
    session.set(Tenant, "t1", SimpleNamespace(tenant_id="t1", policy_config={}, updated_at=None))
    checkout_calls: list[tuple[str, str, int]] = []
    service = AdminProjectService(
        normalize_project_repo=lambda value: value.strip(),
        normalize_project_key=lambda value: value.strip().upper(),
        normalize_project_policy_overrides=lambda value: value or {},
        normalize_string_map=lambda value: value or {},
        normalize_project_architecture_docs_config=lambda value: value or {},
        normalize_project_discord_config=lambda value: value or {},
        with_preserved_discord_system_fields=lambda existing, proposed: {**existing, **proposed},
        resolve_project_discord_channel_binding=_resolve_project_discord_channel_binding,
        sync_tenant_jira_project_keys=_sync_tenant_jira_project_keys,
        ensure_project_repository_checkout=lambda *, session, tenant, project: checkout_calls.append(
            (tenant.tenant_id, project.github_repository, session.commits)
        ),
        resolve_project_run_board_id=lambda *, session, tenant, jira_project_key, settings: 11,
        project_to_schema=_project_to_schema,
        settings_factory=lambda: SimpleNamespace(),
    )
    payload = SimpleNamespace(
        name="Sample",
        github_repository="https://github.com/example/repo",
        jira_project_key="tp",
        policy_overrides=None,
        environment=None,
        secret_refs=None,
        architecture_docs=None,
        discord=None,
    )

    result = service.create_project(session=session, tenant_id="t1", payload=payload)

    assert result["name"] == "Sample"
    assert checkout_calls == [("t1", "https://github.com/example/repo", 1)]
    created_project = session.added[0]
    assert isinstance(created_project, Project)
    assert created_project.project_id.startswith("proj_")
    assert "t1" not in created_project.project_id
    assert "run_board_id" not in created_project.policy_overrides


def test_create_project_returns_502_and_deletes_project_when_clone_fails() -> None:
    from orchestrator.storage.models import Tenant

    session = _Session()
    session.set(Tenant, "t1", SimpleNamespace(tenant_id="t1", policy_config={}, updated_at=None))
    service = AdminProjectService(
        normalize_project_repo=lambda value: value.strip(),
        normalize_project_key=lambda value: value.strip().upper(),
        normalize_project_policy_overrides=lambda value: value or {},
        normalize_string_map=lambda value: value or {},
        normalize_project_architecture_docs_config=lambda value: value or {},
        normalize_project_discord_config=lambda value: value or {},
        with_preserved_discord_system_fields=lambda existing, proposed: {**existing, **proposed},
        resolve_project_discord_channel_binding=_resolve_project_discord_channel_binding,
        sync_tenant_jira_project_keys=_sync_tenant_jira_project_keys,
        ensure_project_repository_checkout=lambda *, session, tenant, project: (_ for _ in ()).throw(
            ProjectRepoCheckoutError("clone failed")
        ),
        resolve_project_run_board_id=lambda *, session, tenant, jira_project_key, settings: 22,
        project_to_schema=_project_to_schema,
        settings_factory=lambda: SimpleNamespace(),
    )
    payload = SimpleNamespace(
        name="Sample",
        github_repository="https://github.com/example/repo",
        jira_project_key="tp",
        policy_overrides=None,
        environment=None,
        secret_refs=None,
        architecture_docs=None,
        discord=None,
    )

    try:
        service.create_project(session=session, tenant_id="t1", payload=payload)
        assert False, "expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 502
        assert "Unable to clone project repository" in str(exc.detail)
    assert session.rollbacks == 1
    assert len(session.deleted) == 1
    deleted = session.deleted[0]
    assert isinstance(deleted, Project)


def test_create_project_auto_binds_discord_channel_when_tenant_discord_is_installed() -> None:
    from orchestrator.storage.models import Tenant

    session = _Session()
    tenant = SimpleNamespace(
        tenant_id="t1",
        policy_config={},
        updated_at=None,
        discord_config={"guild_id": "guild-123"},
    )
    session.set(Tenant, "t1", tenant)
    resolve_calls: list[tuple[str, str]] = []

    def _resolve_channel(*, session, settings, tenant, project, discord_config: dict) -> dict:  # noqa: ANN001
        _ = session, settings
        resolve_calls.append((tenant.tenant_id, project.project_id))
        return {**discord_config, "channel_id": "discord-channel-123"}

    service = AdminProjectService(
        normalize_project_repo=lambda value: value.strip(),
        normalize_project_key=lambda value: value.strip().upper(),
        normalize_project_policy_overrides=lambda value: value or {},
        normalize_string_map=lambda value: value or {},
        normalize_project_architecture_docs_config=lambda value: value or {},
        normalize_project_discord_config=lambda value: value or {},
        with_preserved_discord_system_fields=lambda existing, proposed: {**existing, **proposed},
        resolve_project_discord_channel_binding=_resolve_channel,
        sync_tenant_jira_project_keys=_sync_tenant_jira_project_keys,
        ensure_project_repository_checkout=_ensure_project_repository_checkout,
        resolve_project_run_board_id=_resolve_project_run_board_id,
        project_to_schema=_project_to_schema,
        settings_factory=lambda: SimpleNamespace(),
    )
    payload = SimpleNamespace(
        name="Sample",
        github_repository="https://github.com/example/repo",
        jira_project_key="tp",
        policy_overrides=None,
        environment=None,
        secret_refs=None,
        architecture_docs=None,
        discord=None,
    )

    service.create_project(session=session, tenant_id="t1", payload=payload)

    created_project = session.added[0]
    assert isinstance(created_project, Project)
    assert created_project.discord_config == {"channel_id": "discord-channel-123"}
    assert resolve_calls == [("t1", created_project.project_id)]


def test_create_project_persists_architecture_docs_configuration() -> None:
    from orchestrator.storage.models import Tenant

    session = _Session()
    session.set(Tenant, "t1", SimpleNamespace(tenant_id="t1", policy_config={}, updated_at=None, discord_config={}))
    service = AdminProjectService(
        normalize_project_repo=lambda value: value.strip(),
        normalize_project_key=lambda value: value.strip().upper(),
        normalize_project_policy_overrides=lambda value: value or {},
        normalize_string_map=lambda value: value or {},
        normalize_project_architecture_docs_config=normalize_project_architecture_docs_config,
        normalize_project_discord_config=lambda value: value or {},
        with_preserved_discord_system_fields=lambda existing, proposed: {**existing, **proposed},
        resolve_project_discord_channel_binding=_resolve_project_discord_channel_binding,
        sync_tenant_jira_project_keys=_sync_tenant_jira_project_keys,
        ensure_project_repository_checkout=_ensure_project_repository_checkout,
        resolve_project_run_board_id=_resolve_project_run_board_id,
        project_to_schema=_project_to_schema,
        settings_factory=lambda: SimpleNamespace(),
    )
    payload = SimpleNamespace(
        name="Sample",
        github_repository="https://github.com/example/repo",
        jira_project_key="tp",
        policy_overrides=None,
        environment=None,
        secret_refs=None,
        architecture_docs={
            "provider": "confluence",
            "space_key": "ARCH",
        },
        discord=None,
    )

    result = service.create_project(session=session, tenant_id="t1", payload=payload)

    created_project = session.added[0]
    assert isinstance(created_project, Project)
    assert created_project.architecture_docs_config == {
        "provider": "confluence",
        "space_key": "ARCH",
    }
    assert result["architecture_docs"] == {
        "provider": "confluence",
        "space_key": "ARCH",
    }


def test_create_project_rejects_confluence_architecture_config_without_space_key() -> None:
    from orchestrator.storage.models import Tenant

    session = _Session()
    session.set(Tenant, "t1", SimpleNamespace(tenant_id="t1", policy_config={}, updated_at=None, discord_config={}))
    service = AdminProjectService(
        normalize_project_repo=lambda value: value.strip(),
        normalize_project_key=lambda value: value.strip().upper(),
        normalize_project_policy_overrides=lambda value: value or {},
        normalize_string_map=lambda value: value or {},
        normalize_project_architecture_docs_config=normalize_project_architecture_docs_config,
        normalize_project_discord_config=lambda value: value or {},
        with_preserved_discord_system_fields=lambda existing, proposed: {**existing, **proposed},
        resolve_project_discord_channel_binding=_resolve_project_discord_channel_binding,
        sync_tenant_jira_project_keys=_sync_tenant_jira_project_keys,
        ensure_project_repository_checkout=_ensure_project_repository_checkout,
        resolve_project_run_board_id=_resolve_project_run_board_id,
        project_to_schema=_project_to_schema,
        settings_factory=lambda: SimpleNamespace(),
    )
    payload = SimpleNamespace(
        name="Sample",
        github_repository="https://github.com/example/repo",
        jira_project_key="tp",
        policy_overrides=None,
        environment=None,
        secret_refs=None,
        architecture_docs={"provider": "confluence"},
        discord=None,
    )

    try:
        service.create_project(session=session, tenant_id="t1", payload=payload)
        assert False, "expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 400
        assert "require a space key" in str(exc.detail)


def test_resolve_project_jira_run_board_returns_502_when_oauth_fails() -> None:
    from orchestrator.storage.models import Tenant

    session = _Session()
    session.set(Tenant, "t1", SimpleNamespace(tenant_id="t1", policy_config={}, updated_at=None))
    service = AdminProjectService(
        normalize_project_repo=lambda value: value.strip(),
        normalize_project_key=lambda value: value.strip().upper(),
        normalize_project_policy_overrides=lambda value: value or {},
        normalize_string_map=lambda value: value or {},
        normalize_project_architecture_docs_config=lambda value: value or {},
        normalize_project_discord_config=lambda value: value or {},
        with_preserved_discord_system_fields=lambda existing, proposed: {**existing, **proposed},
        resolve_project_discord_channel_binding=_resolve_project_discord_channel_binding,
        sync_tenant_jira_project_keys=_sync_tenant_jira_project_keys,
        ensure_project_repository_checkout=_ensure_project_repository_checkout,
        resolve_project_run_board_id=lambda *, session, tenant, jira_project_key, settings: (_ for _ in ()).throw(AtlassianOAuthError("oauth unavailable")),
        project_to_schema=_project_to_schema,
        settings_factory=lambda: SimpleNamespace(),
    )
    existing_project = Project(
        project_id="p1",
        tenant_id="t1",
        name="Existing",
        github_repository="https://github.com/example/repo",
        jira_project_key="TP",
        policy_overrides={},
        environment={},
        secret_refs={},
        discord_config={},
        is_archived=False,
        created_at=None,  # type: ignore[arg-type]
        updated_at=None,  # type: ignore[arg-type]
    )
    session.set(Project, "p1", existing_project)

    try:
        service.resolve_project_jira_run_board(session=session, tenant_id="t1", project_id="p1")
        assert False, "expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 502
        assert "Unable to resolve Jira board for project TP: oauth unavailable" == str(exc.detail)


def test_update_project_configuration_does_not_resolve_jira_board() -> None:
    from orchestrator.storage.models import Tenant

    session = _Session()
    tenant = SimpleNamespace(tenant_id="t1", policy_config={}, updated_at=None)
    session.set(Tenant, "t1", tenant)
    existing_project = Project(
        project_id="p1",
        tenant_id="t1",
        name="Existing",
        github_repository="https://github.com/example/repo",
        jira_project_key="TP",
        policy_overrides={},
        environment={},
        secret_refs={},
        discord_config={},
        is_archived=False,
        created_at=None,  # type: ignore[arg-type]
        updated_at=None,  # type: ignore[arg-type]
    )
    session.set(Project, "p1", existing_project)
    service = AdminProjectService(
        normalize_project_repo=lambda value: value.strip(),
        normalize_project_key=lambda value: value.strip().upper(),
        normalize_project_policy_overrides=lambda value: value or {},
        normalize_string_map=lambda value: value or {},
        normalize_project_architecture_docs_config=lambda value: value or {},
        normalize_project_discord_config=lambda value: value or {},
        with_preserved_discord_system_fields=lambda existing, proposed: {**existing, **proposed},
        resolve_project_discord_channel_binding=_resolve_project_discord_channel_binding,
        sync_tenant_jira_project_keys=_sync_tenant_jira_project_keys,
        ensure_project_repository_checkout=_ensure_project_repository_checkout,
        resolve_project_run_board_id=lambda *, session, tenant, jira_project_key, settings: (_ for _ in ()).throw(AtlassianOAuthError("token revoked")),
        project_to_schema=_project_to_schema,
        settings_factory=lambda: SimpleNamespace(),
    )
    payload = SimpleNamespace(
        name="Updated",
        github_repository="https://github.com/example/repo-2",
        jira_project_key="tp",
        policy_overrides=None,
        environment=None,
        secret_refs=None,
        architecture_docs=None,
        discord=None,
        is_archived=False,
    )

    service.update_project_configuration(session=session, tenant_id="t1", project_id="p1", payload=payload)

    assert session.commits == 1
    assert existing_project.name == "Updated"
    assert existing_project.github_repository == "https://github.com/example/repo-2"
    assert existing_project.jira_project_key == "TP"
    assert "run_board_id" not in existing_project.policy_overrides


def test_update_project_migrates_inline_secret_values_to_project_managed_refs() -> None:
    from orchestrator.storage.models import Tenant

    session = _Session()
    tenant = SimpleNamespace(tenant_id="t1", policy_config={}, updated_at=None)
    session.set(Tenant, "t1", tenant)
    existing_project = Project(
        project_id="p1",
        tenant_id="t1",
        name="Existing",
        github_repository="https://github.com/example/repo",
        jira_project_key="TP",
        policy_overrides={},
        environment={},
        secret_refs={"SUPABASE_URL": "https://example.supabase.co"},
        discord_config={},
        is_archived=False,
        created_at=None,  # type: ignore[arg-type]
        updated_at=None,  # type: ignore[arg-type]
    )
    session.set(Project, "p1", existing_project)
    service = AdminProjectService(
        normalize_project_repo=lambda value: value.strip(),
        normalize_project_key=lambda value: value.strip().upper(),
        normalize_project_policy_overrides=lambda value: value or {},
        normalize_string_map=lambda value: value or {},
        normalize_project_architecture_docs_config=lambda value: value or {},
        normalize_project_discord_config=lambda value: value or {},
        with_preserved_discord_system_fields=lambda existing, proposed: {**existing, **proposed},
        resolve_project_discord_channel_binding=_resolve_project_discord_channel_binding,
        sync_tenant_jira_project_keys=_sync_tenant_jira_project_keys,
        ensure_project_repository_checkout=_ensure_project_repository_checkout,
        resolve_project_run_board_id=_resolve_project_run_board_id,
        project_to_schema=_project_to_schema,
        settings_factory=lambda: SimpleNamespace(secrets_encryption_key="enc-key"),
    )
    payload = SimpleNamespace(
        name="Updated",
        github_repository="https://github.com/example/repo",
        jira_project_key="tp",
        policy_overrides=None,
        environment=None,
        secret_refs={
            "SUPABASE_URL": "https://example.supabase.co",
            "APPLE_TEST_PASSWORD": "Ft6ygA&aYkf%hy",
        },
        architecture_docs=None,
        discord=None,
        is_archived=False,
    )

    upsert_calls: list[tuple[str, str]] = []
    with patch(
        "orchestrator.core.platform.tenant_secret_service._upsert_managed_secret",
        side_effect=lambda session, *, secret_ref, plaintext_value, encryption_key, scope, tenant_id: upsert_calls.append(
            (secret_ref, plaintext_value)
        )
        or SimpleNamespace(secret_ref=secret_ref, source="managed", updated_at=None),
    ):
        service.update_project_secret_refs(session=session, tenant_id="t1", project_id="p1", payload=payload)

    assert existing_project.secret_refs == {
        "SUPABASE_URL": "project/t1/p1/SUPABASE_URL",
        "APPLE_TEST_PASSWORD": "project/t1/p1/APPLE_TEST_PASSWORD",
    }
    assert ("project/t1/p1/SUPABASE_URL", "https://example.supabase.co") in upsert_calls
    assert ("project/t1/p1/APPLE_TEST_PASSWORD", "Ft6ygA&aYkf%hy") in upsert_calls


def test_update_project_auto_binds_discord_channel_when_tenant_discord_is_installed() -> None:
    from orchestrator.storage.models import Tenant

    session = _Session()
    tenant = SimpleNamespace(
        tenant_id="t1",
        policy_config={},
        updated_at=None,
        discord_config={"guild_id": "guild-123"},
    )
    session.set(Tenant, "t1", tenant)
    existing_project = Project(
        project_id="p1",
        tenant_id="t1",
        name="Existing",
        github_repository="https://github.com/example/repo",
        jira_project_key="TP",
        policy_overrides={},
        environment={},
        secret_refs={},
        discord_config={},
        is_archived=False,
        created_at=None,  # type: ignore[arg-type]
        updated_at=None,  # type: ignore[arg-type]
    )
    session.set(Project, "p1", existing_project)
    resolve_calls: list[tuple[str, str]] = []

    def _resolve_channel(*, session, settings, tenant, project, discord_config: dict) -> dict:  # noqa: ANN001
        _ = session, settings
        resolve_calls.append((tenant.tenant_id, project.project_id))
        return {**discord_config, "channel_id": "discord-channel-999"}

    service = AdminProjectService(
        normalize_project_repo=lambda value: value.strip(),
        normalize_project_key=lambda value: value.strip().upper(),
        normalize_project_policy_overrides=lambda value: value or {},
        normalize_string_map=lambda value: value or {},
        normalize_project_architecture_docs_config=lambda value: value or {},
        normalize_project_discord_config=lambda value: value or {},
        with_preserved_discord_system_fields=lambda existing, proposed: {**existing, **proposed},
        resolve_project_discord_channel_binding=_resolve_channel,
        sync_tenant_jira_project_keys=_sync_tenant_jira_project_keys,
        ensure_project_repository_checkout=_ensure_project_repository_checkout,
        resolve_project_run_board_id=_resolve_project_run_board_id,
        project_to_schema=_project_to_schema,
        settings_factory=lambda: SimpleNamespace(),
    )
    payload = SimpleNamespace(
        name="Updated",
        github_repository="https://github.com/example/repo",
        jira_project_key="tp",
        policy_overrides=None,
        environment=None,
        secret_refs=None,
        architecture_docs=None,
        discord={},
        is_archived=False,
    )

    service.update_project_discord(session=session, tenant_id="t1", project_id="p1", payload=payload)

    assert existing_project.discord_config == {"channel_id": "discord-channel-999"}
    assert resolve_calls == [("t1", "p1")]


def test_update_project_preserves_upstream_secret_refs_without_copying() -> None:
    from orchestrator.storage.models import Tenant

    session = _Session()
    tenant = SimpleNamespace(tenant_id="t1", policy_config={}, updated_at=None)
    session.set(Tenant, "t1", tenant)
    existing_project = Project(
        project_id="p1",
        tenant_id="t1",
        name="Existing",
        github_repository="https://github.com/example/repo",
        jira_project_key="TP",
        policy_overrides={},
        environment={},
        secret_refs={},
        discord_config={},
        is_archived=False,
        created_at=None,  # type: ignore[arg-type]
        updated_at=None,  # type: ignore[arg-type]
    )
    session.set(Project, "p1", existing_project)
    service = _service()
    payload = SimpleNamespace(
        name="Updated",
        github_repository="https://github.com/example/repo",
        jira_project_key="tp",
        policy_overrides=None,
        environment=None,
        secret_refs={
            "RAILWAY_TOKEN": "platform/RAILWAY_TOKEN",
            "SUPABASE_SERVICE_ROLE_KEY": "tenant/t1/SUPABASE_SERVICE_ROLE_KEY",
        },
        architecture_docs=None,
        discord=None,
        is_archived=False,
    )

    with patch("orchestrator.core.platform.tenant_secret_service._upsert_managed_secret") as upsert_mock:
        service.update_project_secret_refs(session=session, tenant_id="t1", project_id="p1", payload=payload)

    assert existing_project.secret_refs == {
        "RAILWAY_TOKEN": "platform/RAILWAY_TOKEN",
        "SUPABASE_SERVICE_ROLE_KEY": "tenant/t1/SUPABASE_SERVICE_ROLE_KEY",
    }
    upsert_mock.assert_not_called()


def test_get_project_does_not_mutate_existing_secret_refs() -> None:
    from orchestrator.storage.models import Tenant

    session = _Session()
    tenant = SimpleNamespace(tenant_id="t1", policy_config={}, updated_at=None)
    session.set(Tenant, "t1", tenant)
    existing_project = Project(
        project_id="p1",
        tenant_id="t1",
        name="Existing",
        github_repository="https://github.com/example/repo",
        jira_project_key="TP",
        policy_overrides={},
        environment={},
        secret_refs={
            "SERVICE_ID": "SUPABASE_APPLE_SERVICE_ID",
            "CALLBACK_URL": "tenant/t1/SUPABASE_APPLE_CALLBACK_URL",
        },
        discord_config={},
        is_archived=False,
        created_at=None,  # type: ignore[arg-type]
        updated_at=None,  # type: ignore[arg-type]
    )
    session.set(Project, "p1", existing_project)
    service = AdminProjectService(
        normalize_project_repo=lambda value: value.strip(),
        normalize_project_key=lambda value: value.strip().upper(),
        normalize_project_policy_overrides=lambda value: value or {},
        normalize_string_map=lambda value: value or {},
        normalize_project_architecture_docs_config=lambda value: value or {},
        normalize_project_discord_config=lambda value: value or {},
        with_preserved_discord_system_fields=lambda existing, proposed: {**existing, **proposed},
        resolve_project_discord_channel_binding=_resolve_project_discord_channel_binding,
        sync_tenant_jira_project_keys=_sync_tenant_jira_project_keys,
        ensure_project_repository_checkout=_ensure_project_repository_checkout,
        resolve_project_run_board_id=_resolve_project_run_board_id,
        project_to_schema=_project_to_schema,
        settings_factory=lambda: SimpleNamespace(secrets_encryption_key="enc-key"),
    )

    result = service.get_project(session=session, tenant_id="t1", project_id="p1")

    assert result["project_id"] == "p1"
    assert existing_project.secret_refs == {
        "SERVICE_ID": "SUPABASE_APPLE_SERVICE_ID",
        "CALLBACK_URL": "tenant/t1/SUPABASE_APPLE_CALLBACK_URL",
    }


def test_normalize_project_discord_config_preserves_persona_maps() -> None:
    normalized = normalize_project_discord_config(
        {
            "channel_id": "  channel-1  ",
            "live_voice_enabled": "true",
            "live_voice_room_links": {
                " voice-room-1 ": " text-room-1 ",
                " voice-room-2 ": " thread-room-2 ",
            },
            "persona_names": {"pm": " Ava ", "security": " June "},
            "persona_voices": {"pm": " marius ", "security": " fantine "},
            "voice_room_channel_ids": [" voice-room-1 ", ""],
            "pm_room_channel_ids": [" room-1 ", ""],
        }
    )

    assert normalized["channel_id"] == "channel-1"
    assert normalized["live_voice_enabled"] is True
    assert normalized["live_voice_room_links"] == {
        "voice-room-1": "text-room-1",
        "voice-room-2": "thread-room-2",
    }
    assert normalized["persona_names"] == {"pm": "Ava", "security": "June"}
    assert normalized["persona_voices"] == {"pm": "marius", "security": "fantine"}
    assert normalized["voice_room_channel_ids"] == ["voice-room-1"]
    assert normalized["pm_room_channel_ids"] == ["room-1"]


def test_with_preserved_discord_system_fields_keeps_only_system_fields_when_user_fields_omitted() -> None:
    merged = with_preserved_discord_system_fields(
        existing={
            "ask_history": [],
            "persona_room_history": [{"room_id": "room-1", "text": "hello"}],
            "persona_names": {"pm": "Ava"},
            "persona_voices": {"pm": "marius"},
            "live_voice_enabled": True,
            "live_voice_room_links": {"voice-room-1": "text-room-1"},
            "voice_room_channel_ids": ["voice-room-1"],
        },
        proposed={"channel_id": "channel-1"},
    )

    assert merged is not None
    assert merged["channel_id"] == "channel-1"
    assert "persona_names" not in merged
    assert "persona_voices" not in merged
    assert merged["persona_room_history"] == [{"room_id": "room-1", "text": "hello"}]
    assert "live_voice_enabled" not in merged
    assert "live_voice_room_links" not in merged
    assert "voice_room_channel_ids" not in merged
