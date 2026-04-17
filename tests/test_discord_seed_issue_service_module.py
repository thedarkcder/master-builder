from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import HTTPException

from orchestrator.api.discord.seed.issue_service import seed_issues_with_runtime, seed_parent_issues_with_runtime
from orchestrator.tools.jira_oauth import JiraIssueCreateResult, JiraOAuthError


def _seed_payload(*, project_key: str = "GP", parent_issue_type: str = "Story", child_count: int = 1) -> dict:
    children = [
        {
            "summary": "Instrument checkout retry telemetry",
            "issue_type": "Sub-task",
            "behavior_slice": "Track retry attempts and recovery outcomes.",
            "technical_objective": "Emit bounded retry telemetry from checkout recovery flow.",
            "implementation_plan": ["Add retry attempt events", "Capture terminal recovery outcome"],
            "technical_dependencies": ["Telemetry schema review"],
            "risks": ["Event volume could be noisy"],
            "how_to_test": ["Run checkout retry integration test"],
            "done_criteria": ["Retry metrics appear in analytics dashboard"],
            "labels": ["engineering"],
        }
    ]
    if child_count > 1:
        children.append(
            {
                "summary": "Persist checkout recovery UI state",
                "issue_type": "Sub-task",
                "behavior_slice": "Keep customer context while recovery decisions change.",
                "technical_objective": "Persist retry state across view transitions.",
                "implementation_plan": ["Store retry state", "Restore state on render"],
                "technical_dependencies": [],
                "risks": [],
                "how_to_test": ["Run recovery state UI test"],
                "done_criteria": ["State persists during retry flow"],
                "labels": ["engineering"],
            }
        )
    return {
        "project_key": project_key,
        "parent_issue": {
            "summary": "Improve checkout recovery",
            "issue_type": parent_issue_type,
            "objective": "Reduce failed checkouts from transient errors.",
            "user_value": "Customers can complete checkout after a recoverable failure.",
            "recommendation": "Ship a tighter retry and fallback experience.",
            "scope_in": ["Retry UX", "Checkout telemetry"],
            "scope_out": ["Payments provider migration"],
            "acceptance_criteria": ["Customers can retry without losing cart state"],
            "ui_references": ["Figma: checkout-recovery-v2"],
            "risks": ["Telemetry coverage is incomplete"],
            "dependencies": ["Design copy approval"],
            "open_questions": [],
            "success_outcomes": ["Reduce recoverable checkout drop-off"],
            "labels": ["product"],
        },
        "engineering_children": children,
        "questions": [],
    }


def _planning_package(*, planning_state: str, child_issues: list[dict] | None = None) -> dict:
    children = child_issues if child_issues is not None else _seed_payload()["engineering_children"]
    return {
        "planning_state": planning_state,
        "specialist_outputs": {
            "architecture": {
                "findings": ["Architectural boundaries should stay modular."],
                "recommendations": ["Use a dedicated planning package before Jira write."],
                "required_tasks": ["Implement shared planning package merge"],
                "open_behavior_questions": [],
                "acceptance_impacts": ["Parent stays PM-only until planning completes."],
                "mermaid_diagram": "flowchart TD\n  Parent[Parent brief] --> Planner[Planning runtime]",
            },
            "security": {
                "findings": ["Security review must be explicit."],
                "recommendations": ["Keep sensitive data out of the parent brief."],
                "required_tasks": ["Add security verification child"],
                "open_behavior_questions": [],
                "acceptance_impacts": ["Security tasks should stay technical."],
            },
            "testing": {
                "findings": ["Test coverage must prove the gate."],
                "recommendations": ["Add regression coverage for the handoff."],
                "required_tasks": ["Add planning-to-Jira regression tests"],
                "open_behavior_questions": [],
                "acceptance_impacts": ["Child creation waits for planning completion."],
            },
        },
        "architecture_summary": [
            "Architectural boundaries should stay modular.",
            "Use a dedicated planning package before Jira write.",
        ],
        "architecture_diagram": "flowchart TD\n  Parent[Parent brief] --> Planner[Planning runtime]",
        "child_issues": children,
    }


def _adf_text(value: object) -> str:
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"]
        parts = [_adf_text(item) for item in value.get("content", []) if item is not None]
        return "\n".join(part for part in parts if part)
    if isinstance(value, list):
        parts = [_adf_text(item) for item in value if item is not None]
        return "\n".join(part for part in parts if part)
    return ""


def test_seed_issues_scopes_allowed_project_keys() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-a")
    with __import__("pytest").raises(HTTPException) as exc_ctx:
        seed_issues_with_runtime(
            session=MagicMock(),
            tenant=tenant,
            prompt_markdown="seed issues",
            scoped_project_id="project-a",
            force_issue_keys=None,
            allow_create=True,
            scoped_project_keys=["GP"],
            codex_working_dir="/tmp",
            tenant_project_keys_fn=lambda **_kwargs: ["GP", "example"],
            get_settings_fn=lambda: SimpleNamespace(),
            build_runtime_fn=lambda **_kwargs: object(),
            plan_seed_issues_with_runtime_fn=lambda **_kwargs: _seed_payload(project_key="example"),
            codex_runtime_error_type=RuntimeError,
            build_seed_issue_description_fn=lambda **_kwargs: "",
            issue_key_pattern=__import__("re").compile(r"^[A-Z]+-\d+$"),
            tenant_jira_oauth_context_fn=lambda **_kwargs: {},
            select_seed_match_fn=lambda **_kwargs: None,
        )
    assert exc_ctx.value.status_code == 409
    assert "unsupported Jira project key 'example'" in str(exc_ctx.value.detail)


def test_seed_issues_creates_parent_and_engineering_child() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-a")
    created: list = []

    class _FakeClient:
        def list_project_issue_types_for_create(self, **_kwargs):  # type: ignore[no-untyped-def]
            return ["Epic", "Story", "Task", "Issue"]

        def search_issues_by_jql(self, **_kwargs):  # type: ignore[no-untyped-def]
            return []

        def create_issue(self, **kwargs):  # type: ignore[no-untyped-def]
            created.append(kwargs["issue"])
            issue = kwargs["issue"]
            if issue.parent_issue_key:
                return JiraIssueCreateResult(key="GP-2", issue_id="2")
            return JiraIssueCreateResult(key="GP-1", issue_id="1")

        def update_issue_fields(self, **_kwargs):  # type: ignore[no-untyped-def]
            return None

        def add_issue_link(self, **_kwargs):  # type: ignore[no-untyped-def]
            return {}

    message, data = seed_issues_with_runtime(
        session=MagicMock(),
        tenant=tenant,
        prompt_markdown="seed issues",
        scoped_project_id="project-a",
        force_issue_keys=None,
        allow_create=True,
        scoped_project_keys=["GP"],
        codex_working_dir="/tmp",
        tenant_project_keys_fn=lambda **_kwargs: ["GP"],
        get_settings_fn=lambda: SimpleNamespace(),
        build_runtime_fn=lambda **_kwargs: object(),
        plan_seed_issues_with_runtime_fn=lambda **_kwargs: _seed_payload(),
        codex_runtime_error_type=RuntimeError,
        build_seed_issue_description_fn=lambda **_kwargs: {},
        issue_key_pattern=__import__("re").compile(r"^[A-Z]+-\d+$"),
        tenant_jira_oauth_context_fn=lambda **_kwargs: {
            "client": _FakeClient(),
            "access_token": "token",
            "connection": SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
        },
        select_seed_match_fn=lambda **_kwargs: None,
    )

    assert "Issue upsert complete." in message
    assert data["parent_issue_key"] == "GP-1"
    assert data["created_parent"] == "GP-1"
    assert data["created_children"] == ["GP-2"]
    assert data["created_issue_keys"] == ["GP-1", "GP-2"]
    assert data["children_sync_status"] == "children_current"
    assert created[0].issue_type == "Story"
    assert created[1].issue_type == "Sub-task"
    assert created[1].parent_issue_key == "GP-1"


def test_seed_issues_falls_back_to_linked_task_when_subtasks_unavailable() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-a")
    created: list = []
    linked: list[tuple[str, str]] = []

    class _FakeClient:
        def list_project_issue_types_for_create(self, **_kwargs):  # type: ignore[no-untyped-def]
            return ["Epic", "Story", "Task"]

        def search_issues_by_jql(self, **_kwargs):  # type: ignore[no-untyped-def]
            return []

        def create_issue(self, **kwargs):  # type: ignore[no-untyped-def]
            issue = kwargs["issue"]
            created.append(issue)
            if issue.summary == "Improve checkout recovery":
                return JiraIssueCreateResult(key="GP-10", issue_id="10")
            if issue.parent_issue_key:
                raise JiraOAuthError("Subtask issue type is not available for project GP")
            return JiraIssueCreateResult(key="GP-11", issue_id="11")

        def update_issue_fields(self, **_kwargs):  # type: ignore[no-untyped-def]
            return None

        def add_issue_link(self, **kwargs):  # type: ignore[no-untyped-def]
            linked.append((kwargs["inward_issue_key"], kwargs["outward_issue_key"]))
            return {}

    message, data = seed_issues_with_runtime(
        session=MagicMock(),
        tenant=tenant,
        prompt_markdown="seed issues",
        scoped_project_id="project-a",
        force_issue_keys=None,
        allow_create=True,
        scoped_project_keys=["GP"],
        codex_working_dir="/tmp",
        tenant_project_keys_fn=lambda **_kwargs: ["GP"],
        get_settings_fn=lambda: SimpleNamespace(),
        build_runtime_fn=lambda **_kwargs: object(),
        plan_seed_issues_with_runtime_fn=lambda **_kwargs: _seed_payload(),
        codex_runtime_error_type=RuntimeError,
        build_seed_issue_description_fn=lambda **_kwargs: {},
        issue_key_pattern=__import__("re").compile(r"^[A-Z]+-\d+$"),
        tenant_jira_oauth_context_fn=lambda **_kwargs: {
            "client": _FakeClient(),
            "access_token": "token",
            "connection": SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
        },
        select_seed_match_fn=lambda **_kwargs: None,
    )

    assert "Issue upsert complete." in message
    assert data["created_children"] == ["GP-11"]
    assert created[1].issue_type == "Sub-task"
    assert created[2].issue_type == "Task"
    assert linked == [("GP-11", "GP-10")]


def test_seed_issues_with_incomplete_oauth_context_returns_controlled_502() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-a")
    with __import__("pytest").raises(HTTPException) as exc_ctx:
        seed_issues_with_runtime(
            session=MagicMock(),
            tenant=tenant,
            prompt_markdown="seed issues",
            scoped_project_id="project-a",
            force_issue_keys=None,
            allow_create=True,
            scoped_project_keys=["GP"],
            codex_working_dir="/tmp",
            tenant_project_keys_fn=lambda **_kwargs: ["GP"],
            get_settings_fn=lambda: SimpleNamespace(),
            build_runtime_fn=lambda **_kwargs: object(),
            plan_seed_issues_with_runtime_fn=lambda **_kwargs: _seed_payload(),
            codex_runtime_error_type=RuntimeError,
            build_seed_issue_description_fn=lambda **_kwargs: {},
            issue_key_pattern=__import__("re").compile(r"^[A-Z]+-\d+$"),
            tenant_jira_oauth_context_fn=lambda **_kwargs: {"access_token": "tok-only"},
            select_seed_match_fn=lambda **_kwargs: None,
        )
    assert exc_ctx.value.status_code == 502
    assert str(exc_ctx.value.detail) == "Failed to seed Jira issues: Jira OAuth context is incomplete"
    assert "tok-only" not in str(exc_ctx.value.detail)


def test_seed_issues_normalizes_blank_parent_issue_type_to_project_supported_story() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-a")
    created: list = []

    class _FakeClient:
        def list_project_issue_types_for_create(self, **_kwargs):  # type: ignore[no-untyped-def]
            return ["Story", "Task", "Issue"]

        def search_issues_by_jql(self, **_kwargs):  # type: ignore[no-untyped-def]
            return []

        def create_issue(self, **kwargs):  # type: ignore[no-untyped-def]
            created.append(kwargs["issue"])
            issue = kwargs["issue"]
            if issue.parent_issue_key:
                return JiraIssueCreateResult(key="GP-2", issue_id="2")
            return JiraIssueCreateResult(key="GP-1", issue_id="1")

        def update_issue_fields(self, **_kwargs):  # type: ignore[no-untyped-def]
            return None

        def add_issue_link(self, **_kwargs):  # type: ignore[no-untyped-def]
            return {}

    _, data = seed_issues_with_runtime(
        session=MagicMock(),
        tenant=tenant,
        prompt_markdown="seed issues",
        scoped_project_id="project-a",
        force_issue_keys=None,
        allow_create=True,
        scoped_project_keys=["GP"],
        codex_working_dir="/tmp",
        tenant_project_keys_fn=lambda **_kwargs: ["GP"],
        get_settings_fn=lambda: SimpleNamespace(),
        build_runtime_fn=lambda **_kwargs: object(),
        plan_seed_issues_with_runtime_fn=lambda **_kwargs: _seed_payload(parent_issue_type=""),
        codex_runtime_error_type=RuntimeError,
        build_seed_issue_description_fn=lambda **_kwargs: {},
        issue_key_pattern=__import__("re").compile(r"^[A-Z]+-\d+$"),
        tenant_jira_oauth_context_fn=lambda **_kwargs: {
            "client": _FakeClient(),
            "access_token": "token",
            "connection": SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
        },
        select_seed_match_fn=lambda **_kwargs: None,
    )

    assert data["created_parent"] == "GP-1"
    assert created[0].issue_type == "Story"


def test_seed_issues_keeps_single_behavior_parent_at_story_when_multiple_children_exist() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-a")
    created: list = []

    class _FakeClient:
        def list_project_issue_types_for_create(self, **_kwargs):  # type: ignore[no-untyped-def]
            return ["Epic", "Story", "Task"]

        def search_issues_by_jql(self, **_kwargs):  # type: ignore[no-untyped-def]
            return []

        def create_issue(self, **kwargs):  # type: ignore[no-untyped-def]
            created.append(kwargs["issue"])
            issue = kwargs["issue"]
            if issue.parent_issue_key:
                return JiraIssueCreateResult(key=f"GP-{len(created)}", issue_id=str(len(created)))
            return JiraIssueCreateResult(key="GP-1", issue_id="1")

        def update_issue_fields(self, **_kwargs):  # type: ignore[no-untyped-def]
            return None

        def add_issue_link(self, **_kwargs):  # type: ignore[no-untyped-def]
            return {}

    _, data = seed_issues_with_runtime(
        session=MagicMock(),
        tenant=tenant,
        prompt_markdown="seed issues",
        scoped_project_id="project-a",
        force_issue_keys=None,
        allow_create=True,
        scoped_project_keys=["GP"],
        codex_working_dir="/tmp",
        tenant_project_keys_fn=lambda **_kwargs: ["GP"],
        get_settings_fn=lambda: SimpleNamespace(),
        build_runtime_fn=lambda **_kwargs: object(),
        plan_seed_issues_with_runtime_fn=lambda **_kwargs: _seed_payload(parent_issue_type="", child_count=2),
        codex_runtime_error_type=RuntimeError,
        build_seed_issue_description_fn=lambda **_kwargs: {},
        issue_key_pattern=__import__("re").compile(r"^[A-Z]+-\d+$"),
        tenant_jira_oauth_context_fn=lambda **_kwargs: {
            "client": _FakeClient(),
            "access_token": "token",
            "connection": SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
        },
        select_seed_match_fn=lambda **_kwargs: None,
    )

    assert data["created_parent"] == "GP-1"
    assert created[0].issue_type == "Story"


def test_seed_issues_promotes_parent_to_epic_when_pm_brief_signals_initiative_scope() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-a")
    created: list = []

    class _FakeClient:
        def list_project_issue_types_for_create(self, **_kwargs):  # type: ignore[no-untyped-def]
            return ["Epic", "Story", "Task"]

        def search_issues_by_jql(self, **_kwargs):  # type: ignore[no-untyped-def]
            return []

        def create_issue(self, **kwargs):  # type: ignore[no-untyped-def]
            created.append(kwargs["issue"])
            issue = kwargs["issue"]
            if issue.parent_issue_key:
                return JiraIssueCreateResult(key=f"GP-{len(created)}", issue_id=str(len(created)))
            return JiraIssueCreateResult(key="GP-1", issue_id="1")

        def update_issue_fields(self, **_kwargs):  # type: ignore[no-untyped-def]
            return None

        def add_issue_link(self, **_kwargs):  # type: ignore[no-untyped-def]
            return {}

    payload = _seed_payload(parent_issue_type="", child_count=2)
    payload["parent_issue"]["summary"] = "Checkout recovery initiative"
    payload["parent_issue"]["objective"] = "Coordinate a multi-story recovery initiative across checkout."

    _, data = seed_issues_with_runtime(
        session=MagicMock(),
        tenant=tenant,
        prompt_markdown="seed issues",
        scoped_project_id="project-a",
        force_issue_keys=None,
        allow_create=True,
        scoped_project_keys=["GP"],
        codex_working_dir="/tmp",
        tenant_project_keys_fn=lambda **_kwargs: ["GP"],
        get_settings_fn=lambda: SimpleNamespace(),
        build_runtime_fn=lambda **_kwargs: object(),
        plan_seed_issues_with_runtime_fn=lambda **_kwargs: payload,
        codex_runtime_error_type=RuntimeError,
        build_seed_issue_description_fn=lambda **_kwargs: {},
        issue_key_pattern=__import__("re").compile(r"^[A-Z]+-\d+$"),
        tenant_jira_oauth_context_fn=lambda **_kwargs: {
            "client": _FakeClient(),
            "access_token": "token",
            "connection": SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
        },
        select_seed_match_fn=lambda **_kwargs: None,
    )

    assert data["created_parent"] == "GP-1"
    assert created[0].issue_type == "Epic"


def test_seed_parent_issues_rejects_incomplete_pm_status_before_jira_write() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-a")
    create_issue_mock = MagicMock()

    class _FakeClient:
        def list_project_issue_types_for_create(self, **_kwargs):  # type: ignore[no-untyped-def]
            return ["Story", "Task"]

        def search_issues_by_jql(self, **_kwargs):  # type: ignore[no-untyped-def]
            return []

        def create_issue(self, **kwargs):  # type: ignore[no-untyped-def]
            create_issue_mock(kwargs)
            return JiraIssueCreateResult(key="GP-1", issue_id="1")

        def update_issue_fields(self, **_kwargs):  # type: ignore[no-untyped-def]
            return None

    with __import__("pytest").raises(HTTPException) as exc_ctx:
        seed_parent_issues_with_runtime(
            session=MagicMock(),
            tenant=tenant,
            prompt_markdown="pm batch",
            scoped_project_id="project-a",
            force_issue_keys=None,
            allow_create=True,
            scoped_project_keys=["GP"],
            codex_working_dir="/tmp",
            tenant_project_keys_fn=lambda **_kwargs: ["GP"],
            get_settings_fn=lambda: SimpleNamespace(),
            build_runtime_fn=lambda **_kwargs: object(),
            plan_pm_parent_issues_with_runtime_fn=lambda **_kwargs: {
                "project_key": "GP",
                "issues": [_seed_payload()["parent_issue"]],
                "questions": [],
                "pm_status": "drafting",
            },
            codex_runtime_error_type=RuntimeError,
            issue_key_pattern=__import__("re").compile(r"^[A-Z]+-\d+$"),
            tenant_jira_oauth_context_fn=lambda **_kwargs: {
                "client": _FakeClient(),
                "access_token": "token",
                "connection": SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
            },
            select_seed_match_fn=lambda **_kwargs: None,
            pm_status="drafting",
        )

    assert exc_ctx.value.status_code == 409
    assert "PM interview is not ready to write Jira parent issues yet" in str(exc_ctx.value.detail)
    assert create_issue_mock.call_count == 0


def test_seed_parent_issues_blocks_when_stage_spi_not_ready() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-a")

    with __import__("pytest").raises(HTTPException) as exc_ctx:
        seed_parent_issues_with_runtime(
            session=MagicMock(),
            tenant=tenant,
            prompt_markdown="pm batch",
            scoped_project_id="project-a",
            force_issue_keys=None,
            allow_create=True,
            scoped_project_keys=["GP"],
            codex_working_dir="/tmp",
            tenant_project_keys_fn=lambda **_kwargs: ["GP"],
            get_settings_fn=lambda: SimpleNamespace(stage_spi_enabled=True),
            build_runtime_fn=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("should not build runtime")),
            plan_pm_parent_issues_with_runtime_fn=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("should not plan")),
            codex_runtime_error_type=RuntimeError,
            issue_key_pattern=__import__("re").compile(r"^[A-Z]+-\d+$"),
            tenant_jira_oauth_context_fn=lambda **_kwargs: {},
            select_seed_match_fn=lambda **_kwargs: None,
            pm_status="ready_to_write",
            pm_interview_notes_json={
                "stage_spi": {
                    "stage_ready_for_implementation": False,
                    "stage_status": "stage_review_pending",
                }
            },
        )

    assert exc_ctx.value.status_code == 409
    assert "Stage plugin has not approved" in str(exc_ctx.value.detail)


def test_seed_issues_blocks_children_until_planning_completes_and_keeps_parent_pm_complete() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-a")
    created: list = []

    class _FakeClient:
        def list_project_issue_types_for_create(self, **_kwargs):  # type: ignore[no-untyped-def]
            return ["Epic", "Story", "Task", "Issue"]

        def search_issues_by_jql(self, **_kwargs):  # type: ignore[no-untyped-def]
            return []

        def create_issue(self, **kwargs):  # type: ignore[no-untyped-def]
            created.append(kwargs["issue"])
            issue = kwargs["issue"]
            return JiraIssueCreateResult(key="GP-1" if not issue.parent_issue_key else "GP-2", issue_id="1")

        def update_issue_fields(self, **_kwargs):  # type: ignore[no-untyped-def]
            return None

        def add_issue_link(self, **_kwargs):  # type: ignore[no-untyped-def]
            return {}

    message, data = seed_issues_with_runtime(
        session=MagicMock(),
        tenant=tenant,
        prompt_markdown="seed issues",
        scoped_project_id="project-a",
        force_issue_keys=None,
        allow_create=True,
        scoped_project_keys=["GP"],
        codex_working_dir="/tmp",
        tenant_project_keys_fn=lambda **_kwargs: ["GP"],
        get_settings_fn=lambda: SimpleNamespace(),
        build_runtime_fn=lambda **_kwargs: object(),
        plan_seed_issues_with_runtime_fn=lambda **_kwargs: _seed_payload(),
        codex_runtime_error_type=RuntimeError,
        build_seed_issue_description_fn=lambda **_kwargs: {},
        issue_key_pattern=__import__("re").compile(r"^[A-Z]+-\d+$"),
        tenant_jira_oauth_context_fn=lambda **_kwargs: {
            "client": _FakeClient(),
            "access_token": "token",
            "connection": SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
        },
        select_seed_match_fn=lambda **_kwargs: None,
        pm_status="pm_completed",
        planning_package=_planning_package(planning_state="planning_drafting"),
    )

    assert "Specialist planning is not complete yet" in message
    assert data["pm_status"] == "pm_completed"
    assert data["planning_state"] == "planning_drafting"
    assert data["children_sync_status"] == "planning_blocked"
    assert data["created_parent"] == "GP-1"
    assert data["created_children"] == []
    assert len(created) == 1
    parent_description = _adf_text(created[0].description)
    assert "PM Status" in parent_description
    assert "pm_completed" in parent_description
    assert "Planning State" in parent_description
    assert "planning_drafting" in parent_description
    assert "Architecture Context" in parent_description
    assert "Architectural boundaries should stay modular." in parent_description
    assert "Parent[Parent brief] --> Planner[Planning runtime]" in parent_description


def test_seed_issues_merges_planning_package_context_into_child_ticket_descriptions() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-a")
    created: list = []

    class _FakeClient:
        def list_project_issue_types_for_create(self, **_kwargs):  # type: ignore[no-untyped-def]
            return ["Epic", "Story", "Task", "Issue"]

        def search_issues_by_jql(self, **_kwargs):  # type: ignore[no-untyped-def]
            return []

        def create_issue(self, **kwargs):  # type: ignore[no-untyped-def]
            created.append(kwargs["issue"])
            issue = kwargs["issue"]
            return JiraIssueCreateResult(key="GP-1" if not issue.parent_issue_key else "GP-2", issue_id="1")

        def update_issue_fields(self, **_kwargs):  # type: ignore[no-untyped-def]
            return None

        def add_issue_link(self, **_kwargs):  # type: ignore[no-untyped-def]
            return {}

    _, data = seed_issues_with_runtime(
        session=MagicMock(),
        tenant=tenant,
        prompt_markdown="seed issues",
        scoped_project_id="project-a",
        force_issue_keys=None,
        allow_create=True,
        scoped_project_keys=["GP"],
        codex_working_dir="/tmp",
        tenant_project_keys_fn=lambda **_kwargs: ["GP"],
        get_settings_fn=lambda: SimpleNamespace(),
        build_runtime_fn=lambda **_kwargs: object(),
        plan_seed_issues_with_runtime_fn=lambda **_kwargs: _seed_payload(),
        codex_runtime_error_type=RuntimeError,
        build_seed_issue_description_fn=lambda **_kwargs: {},
        issue_key_pattern=__import__("re").compile(r"^[A-Z]+-\d+$"),
        tenant_jira_oauth_context_fn=lambda **_kwargs: {
            "client": _FakeClient(),
            "access_token": "token",
            "connection": SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
        },
        select_seed_match_fn=lambda **_kwargs: None,
        pm_status="pm_completed",
        planning_package=_planning_package(
            planning_state="planning_completed",
            child_issues=[
                {
                    "summary": "Implement checkout planner merge",
                    "issue_type": "Sub-task",
                    "behavior_slice": "Merge the specialist outputs into one child plan.",
                    "technical_objective": "Combine specialist recommendations into the Jira child draft.",
                    "implementation_plan": ["Load specialist outputs", "Build merged child description"],
                    "technical_dependencies": ["Planning package schema"],
                    "risks": ["Descriptions may grow too large"],
                    "how_to_test": ["Assert merged planning context appears in the child description"],
                    "done_criteria": ["Child ticket reflects specialist planning context"],
                    "implementation_decisions": [
                        "Decision owner: Engineering child team.",
                        "Approval path: child PR review and architecture review when boundaries or platform risk change.",
                    ],
                    "labels": ["engineering"],
                }
            ],
        ),
    )

    assert data["children_sync_status"] == "children_current"
    assert data["created_children"] == ["GP-2"]
    assert len(created) == 2
    child_description = _adf_text(created[1].description)
    assert "Implementation Decisions" in child_description
    assert "Decision owner: Engineering child team." in child_description
    assert "Specialist Planning Context" in child_description
    assert "Architecture Findings: Architectural boundaries should stay modular." in child_description
    assert "Security Findings: Security review must be explicit." in child_description
    assert "Testing Recommendations: Add regression coverage for the handoff." in child_description
