from types import SimpleNamespace

from fastapi import HTTPException

from orchestrator.api.admin.admin_project_service import AdminProjectService


class _Session:
    def __init__(self) -> None:
        self._objects: dict[tuple[type, str], object] = {}
        self.commits = 0
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

    def commit(self) -> None:
        self.commits += 1

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
