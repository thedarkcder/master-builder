from types import SimpleNamespace

from fastapi import HTTPException

from orchestrator.api.admin.project_service import AdminProjectService
from orchestrator.storage.models import Project
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
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: self._projects))

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


def _service() -> AdminProjectService:
    return AdminProjectService(
        normalize_project_repo=lambda value: value.strip(),
        normalize_project_key=lambda value: value.strip().upper(),
        normalize_project_policy_overrides=lambda value: value or {},
        normalize_string_map=lambda value: value or {},
        normalize_project_discord_config=lambda value: value or {},
        with_preserved_discord_system_fields=lambda existing, proposed: {**existing, **proposed},
        resolve_project_discord_channel_binding=lambda **kwargs: kwargs["discord_config"],  # type: ignore[return-value]
        sync_tenant_jira_project_keys=lambda *_args, **_kwargs: None,
        ensure_project_repository_checkout=lambda **_kwargs: None,
        resolve_project_run_board_id=lambda **_kwargs: None,
        project_to_schema=lambda project, **_kwargs: {"project_id": project.project_id, "name": project.name},
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
        normalize_project_discord_config=lambda value: value or {},
        with_preserved_discord_system_fields=lambda existing, proposed: {**existing, **proposed},
        resolve_project_discord_channel_binding=lambda **kwargs: kwargs["discord_config"],  # type: ignore[return-value]
        sync_tenant_jira_project_keys=lambda *_args, **_kwargs: None,
        ensure_project_repository_checkout=lambda **kwargs: checkout_calls.append(
            (kwargs["tenant"].tenant_id, kwargs["project"].github_repository, session.commits)
        ),
        resolve_project_run_board_id=lambda **_kwargs: 11,
        project_to_schema=lambda project, **_kwargs: {"project_id": project.project_id, "name": project.name},
        settings_factory=lambda: SimpleNamespace(),
    )
    payload = SimpleNamespace(
        name="Sample",
        github_repository="https://github.com/example/repo",
        jira_project_key="tp",
        policy_overrides=None,
        environment=None,
        secret_refs=None,
        discord=None,
    )

    result = service.create_project(session=session, tenant_id="t1", payload=payload)

    assert result["name"] == "Sample"
    assert checkout_calls == [("t1", "https://github.com/example/repo", 1)]
    created_project = session.added[0]
    assert isinstance(created_project, Project)
    assert created_project.policy_overrides.get("run_board_id") == 11


def test_create_project_returns_502_and_deletes_project_when_clone_fails() -> None:
    from orchestrator.storage.models import Tenant

    session = _Session()
    session.set(Tenant, "t1", SimpleNamespace(tenant_id="t1", policy_config={}, updated_at=None))
    service = AdminProjectService(
        normalize_project_repo=lambda value: value.strip(),
        normalize_project_key=lambda value: value.strip().upper(),
        normalize_project_policy_overrides=lambda value: value or {},
        normalize_string_map=lambda value: value or {},
        normalize_project_discord_config=lambda value: value or {},
        with_preserved_discord_system_fields=lambda existing, proposed: {**existing, **proposed},
        resolve_project_discord_channel_binding=lambda **kwargs: kwargs["discord_config"],  # type: ignore[return-value]
        sync_tenant_jira_project_keys=lambda *_args, **_kwargs: None,
        ensure_project_repository_checkout=lambda **_kwargs: (_ for _ in ()).throw(
            ProjectRepoCheckoutError("clone failed")
        ),
        resolve_project_run_board_id=lambda **_kwargs: 22,
        project_to_schema=lambda project, **_kwargs: {"project_id": project.project_id, "name": project.name},
        settings_factory=lambda: SimpleNamespace(),
    )
    payload = SimpleNamespace(
        name="Sample",
        github_repository="https://github.com/example/repo",
        jira_project_key="tp",
        policy_overrides=None,
        environment=None,
        secret_refs=None,
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
