from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from orchestrator.api.discord.seed.draft_assembly import normalize_planning_package
from orchestrator.api.discord.seed.issue_service import seed_issues_with_runtime, seed_parent_issues_with_runtime
from orchestrator.core.architecture_document_service import ArchitectureDocumentGate
from orchestrator.core.workflow_attempt_ref import WorkflowAttemptRef
from orchestrator.core.runtime_payload_models import EngineeringSeedPlanPayload, PmParentSeedPlanPayload
from orchestrator.storage.models import WorkflowExecution, WorkflowOperation, WorkflowOperationAttempt
from orchestrator.tools.atlassian_oauth import JiraIssueCreateResult, AtlassianOAuthError
from orchestrator.tools.atlassian_oauth_issue_service import MAX_JIRA_ADF_DOCUMENT_BYTES, _to_adf_description


class _JiraMetadataClientMixin:
    def update_issue_summary(self, **_kwargs):  # type: ignore[no-untyped-def]
        return None

    def replace_issue_labels(self, **_kwargs):  # type: ignore[no-untyped-def]
        return None

    def upsert_remote_issue_link(self, **_kwargs):  # type: ignore[no-untyped-def]
        return {}


def _seed_payload(*, project_key: str = "GP", parent_issue_type: str = "Story", child_count: int = 1) -> dict:
    children = [
        {
            "summary": "Instrument checkout retry telemetry",
            "issue_type": "Sub-task",
            "capability": "Checkout retry telemetry",
            "delivery": "Build checkout retry telemetry so each retry attempt and recovery outcome is captured in the system.",
            "expected_outcome": "Operators can see retry attempts and recovery outcomes for checkout failures.",
            "acceptance_criteria": ["Retry attempts are recorded", "Terminal recovery outcomes are visible in telemetry"],
            "dependencies": ["Telemetry schema review"],
            "risks": ["Event volume could be noisy"],
            "how_to_test": ["Run checkout retry integration test"],
            "done_means": ["Retry metrics appear in analytics dashboard"],
            "labels": ["engineering"],
        }
    ]
    if child_count > 1:
        children.append(
            {
                "summary": "Persist checkout recovery UI state",
                "issue_type": "Sub-task",
                "capability": "Checkout recovery UI state",
                "delivery": "Persist checkout recovery state across view transitions so the user does not lose context during recovery.",
                "expected_outcome": "Customers keep their recovery context while retry decisions change.",
                "acceptance_criteria": ["Recovery state survives view transitions"],
                "dependencies": [],
                "risks": [],
                "how_to_test": ["Run recovery state UI test"],
                "done_means": ["State persists during retry flow"],
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


def _engineering_seed_plan(*, project_key: str = "GP", parent_issue_type: str = "Story", child_count: int = 1) -> EngineeringSeedPlanPayload:
    return EngineeringSeedPlanPayload.from_payload(
        _seed_payload(project_key=project_key, parent_issue_type=parent_issue_type, child_count=child_count)
    )


def _pm_parent_seed_plan(*, project_key: str = "GP", parent_issue_type: str = "Story") -> PmParentSeedPlanPayload:
    return PmParentSeedPlanPayload.from_payload(
        {
            "project_key": project_key,
            "issues": [_seed_payload(parent_issue_type=parent_issue_type)["parent_issue"]],
            "questions": [],
        }
    )


def _planning_package(*, planning_state: str, child_issues: list[dict] | None = None) -> dict:
    children = child_issues if child_issues is not None else _seed_payload()["engineering_children"]
    return {
        "planning_state": planning_state,
        "specialist_outputs": {
            "architecture": {
                "findings": ["Architectural boundaries should stay modular."],
                "recommendations": ["Use a dedicated planning package before Jira write."],
                "required_tasks": ["Implement shared planning package merge"],
                "child_ticket_specs": [
                    {
                        "summary": "Merge planning package into Jira child draft",
                        "capability": "Planning package handoff",
                        "delivery": "Build the Jira child drafting path so specialist planning context is carried into each engineering child issue.",
                        "expected_outcome": "Engineering child issues include the planning package context they need to execute.",
                        "acceptance_criteria": ["Jira child draft includes specialist planning context"],
                        "how_to_test": ["Assert merged planning context appears in the child description"],
                        "done_means": ["Child ticket reflects specialist planning context"],
                        "dependencies": ["Planning package schema"],
                        "risks": ["Descriptions may grow too large"],
                        "labels": ["engineering"],
                    }
                ],
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
            plan_seed_issues_with_runtime_fn=lambda **_kwargs: _engineering_seed_plan(project_key="example"),
            codex_runtime_error_type=RuntimeError,
            build_seed_issue_description_fn=lambda **_kwargs: "",
            issue_key_pattern=__import__("re").compile(r"^[A-Z]+-\d+$"),
            tenant_atlassian_oauth_context_fn=lambda **_kwargs: {},
            select_seed_match_fn=lambda **_kwargs: None,
        )
    assert exc_ctx.value.status_code == 409
    assert "unsupported Jira project key 'example'" in str(exc_ctx.value.detail)


def test_normalize_planning_package_accepts_structured_stage_questions() -> None:
    package = _planning_package(planning_state="planning_completed")
    package["specialist_outputs"]["testing"]["open_behavior_questions"] = [
        {
            "question": "Which browsers must the regression suite cover in v1?",
            "why_it_matters": "QA needs a stable compatibility target.",
        }
    ]

    normalized = normalize_planning_package(package)

    assert any(
        "Testing Open behavior questions: Which browsers must the regression suite cover in v1?"
        == line
        for line in normalized.specialist_summary
    )


def test_seed_issues_creates_parent_and_engineering_child() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-a")
    created: list = []

    class _FakeClient(_JiraMetadataClientMixin):
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
        plan_seed_issues_with_runtime_fn=lambda **_kwargs: _engineering_seed_plan(),
        codex_runtime_error_type=RuntimeError,
        build_seed_issue_description_fn=lambda **_kwargs: {},
        issue_key_pattern=__import__("re").compile(r"^[A-Z]+-\d+$"),
        tenant_atlassian_oauth_context_fn=lambda **_kwargs: {
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


def test_seed_issues_passes_typed_attempt_ref_into_invocation_context() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-a")
    captured_context = None

    class _FakeClient(_JiraMetadataClientMixin):
        def list_project_issue_types_for_create(self, **_kwargs):  # type: ignore[no-untyped-def]
            return ["Epic", "Story", "Task", "Issue"]

        def search_issues_by_jql(self, **_kwargs):  # type: ignore[no-untyped-def]
            return []

        def create_issue(self, **kwargs):  # type: ignore[no-untyped-def]
            issue = kwargs["issue"]
            return JiraIssueCreateResult(key="GP-1" if not issue.parent_issue_key else "GP-2", issue_id="1")

        def update_issue_fields(self, **_kwargs):  # type: ignore[no-untyped-def]
            return None


        def add_issue_link(self, **_kwargs):  # type: ignore[no-untyped-def]
            return {}





    def _plan_seed_issues_with_runtime_fn(**kwargs):  # noqa: ANN001
        nonlocal captured_context
        captured_context = kwargs["invocation_context"]
        return _engineering_seed_plan()

    workflow = SimpleNamespace(
        workflow_id="wf-123",
        execution_id="exec-123",
        tenant_id="tenant-a",
        project_id="project-a",
        source_system="jira",
        source_ref="GP-1",
    )
    operation = SimpleNamespace(
        operation_id="op-456",
        operation_type="jira_child_fanout",
        workflow_id="wf-123",
        run_id="run-123",
    )
    attempt = SimpleNamespace(attempt_id="attempt-789", operation_id="op-456", attempt_number=7)
    session = MagicMock()
    session.get.side_effect = lambda model, _identity: {
        WorkflowOperation: operation,
        WorkflowOperationAttempt: attempt,
        WorkflowExecution: workflow,
    }.get(model)
    seed_issues_with_runtime(
        session=session,
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
        plan_seed_issues_with_runtime_fn=_plan_seed_issues_with_runtime_fn,
        codex_runtime_error_type=RuntimeError,
        build_seed_issue_description_fn=lambda **_kwargs: {},
        issue_key_pattern=__import__("re").compile(r"^[A-Z]+-\d+$"),
        tenant_atlassian_oauth_context_fn=lambda **_kwargs: {
            "client": _FakeClient(),
            "access_token": "token",
            "connection": SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
        },
        select_seed_match_fn=lambda **_kwargs: None,
        workflow_id="wf-123",
        operation_id="op-456",
        attempt_ref=WorkflowAttemptRef(
            workflow_id="wf-123",
            operation_id="op-456",
            attempt_id="attempt-789",
            number=7,
        ),
    )

    assert captured_context is not None
    assert captured_context.workflow_id == "wf-123"
    assert captured_context.operation_id == "op-456"
    assert captured_context.attempt == 7
    assert captured_context.attempt_id == "attempt-789"


def test_seed_issues_fails_when_subtasks_are_unavailable() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-a")
    created: list = []

    class _FakeClient(_JiraMetadataClientMixin):
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
                raise AtlassianOAuthError("Subtask issue type is not available for project GP")
            return JiraIssueCreateResult(key="GP-11", issue_id="11")

        def update_issue_fields(self, **_kwargs):  # type: ignore[no-untyped-def]
            return None

        def add_issue_link(self, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("linked-task fallback should not be used")
            return {}


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
            plan_seed_issues_with_runtime_fn=lambda **_kwargs: _engineering_seed_plan(),
            codex_runtime_error_type=RuntimeError,
            build_seed_issue_description_fn=lambda **_kwargs: {},
            issue_key_pattern=__import__("re").compile(r"^[A-Z]+-\d+$"),
            tenant_atlassian_oauth_context_fn=lambda **_kwargs: {
                "client": _FakeClient(),
                "access_token": "token",
                "connection": SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
            },
            select_seed_match_fn=lambda **_kwargs: None,
        )

    assert exc_ctx.value.status_code == 409
    assert "does not support subtasks" in str(exc_ctx.value.detail)
    assert created[0].summary == "Improve checkout recovery"
    assert created[1].issue_type == "Sub-task"


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
            plan_seed_issues_with_runtime_fn=lambda **_kwargs: _engineering_seed_plan(),
            codex_runtime_error_type=RuntimeError,
            build_seed_issue_description_fn=lambda **_kwargs: {},
            issue_key_pattern=__import__("re").compile(r"^[A-Z]+-\d+$"),
            tenant_atlassian_oauth_context_fn=lambda **_kwargs: {"access_token": "tok-only"},
            select_seed_match_fn=lambda **_kwargs: None,
        )
    assert exc_ctx.value.status_code == 502
    assert str(exc_ctx.value.detail) == "Failed to seed Jira issues: Atlassian context is incomplete"
    assert "tok-only" not in str(exc_ctx.value.detail)


def test_seed_issues_keeps_explicit_parent_issue_type_at_project_supported_story() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-a")
    created: list = []

    class _FakeClient(_JiraMetadataClientMixin):
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
        plan_seed_issues_with_runtime_fn=lambda **_kwargs: _engineering_seed_plan(parent_issue_type="Story"),
        codex_runtime_error_type=RuntimeError,
        build_seed_issue_description_fn=lambda **_kwargs: {},
        issue_key_pattern=__import__("re").compile(r"^[A-Z]+-\d+$"),
        tenant_atlassian_oauth_context_fn=lambda **_kwargs: {
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

    class _FakeClient(_JiraMetadataClientMixin):
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
        plan_seed_issues_with_runtime_fn=lambda **_kwargs: _engineering_seed_plan(parent_issue_type="Story", child_count=2),
        codex_runtime_error_type=RuntimeError,
        build_seed_issue_description_fn=lambda **_kwargs: {},
        issue_key_pattern=__import__("re").compile(r"^[A-Z]+-\d+$"),
        tenant_atlassian_oauth_context_fn=lambda **_kwargs: {
            "client": _FakeClient(),
            "access_token": "token",
            "connection": SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
        },
        select_seed_match_fn=lambda **_kwargs: None,
    )

    assert data["created_parent"] == "GP-1"
    assert created[0].issue_type == "Story"


def test_seed_issues_writes_explicit_epic_parent_issue_type_for_initiative_scope() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-a")
    created: list = []

    class _FakeClient(_JiraMetadataClientMixin):
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

    payload = _seed_payload(parent_issue_type="Epic", child_count=2)
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
        plan_seed_issues_with_runtime_fn=lambda **_kwargs: EngineeringSeedPlanPayload.from_payload(payload),
        codex_runtime_error_type=RuntimeError,
        build_seed_issue_description_fn=lambda **_kwargs: {},
        issue_key_pattern=__import__("re").compile(r"^[A-Z]+-\d+$"),
        tenant_atlassian_oauth_context_fn=lambda **_kwargs: {
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

    class _FakeClient(_JiraMetadataClientMixin):
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
            plan_pm_parent_issues_with_runtime_fn=lambda **_kwargs: _pm_parent_seed_plan(),
            codex_runtime_error_type=RuntimeError,
            issue_key_pattern=__import__("re").compile(r"^[A-Z]+-\d+$"),
            tenant_atlassian_oauth_context_fn=lambda **_kwargs: {
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
            tenant_atlassian_oauth_context_fn=lambda **_kwargs: {},
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

    class _FakeClient(_JiraMetadataClientMixin):
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
        plan_seed_issues_with_runtime_fn=lambda **_kwargs: _engineering_seed_plan(),
        codex_runtime_error_type=RuntimeError,
        build_seed_issue_description_fn=lambda **_kwargs: {},
        issue_key_pattern=__import__("re").compile(r"^[A-Z]+-\d+$"),
        tenant_atlassian_oauth_context_fn=lambda **_kwargs: {
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
    assert "PM status: pm_completed" in parent_description
    assert "Planning state: planning_drafting" in parent_description
    assert "Architecture Context" not in parent_description
    assert "Architectural boundaries should stay modular." not in parent_description
    assert "Parent[Parent brief] --> Planner[Planning runtime]" not in parent_description


def test_seed_issues_merges_planning_package_context_into_child_ticket_descriptions() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-a")
    created: list = []

    class _FakeClient(_JiraMetadataClientMixin):
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
        plan_seed_issues_with_runtime_fn=lambda **_kwargs: _engineering_seed_plan(),
        codex_runtime_error_type=RuntimeError,
        build_seed_issue_description_fn=lambda **_kwargs: {},
        issue_key_pattern=__import__("re").compile(r"^[A-Z]+-\d+$"),
        tenant_atlassian_oauth_context_fn=lambda **_kwargs: {
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
                    "summary": "Merge planning package into Jira child draft",
                    "issue_type": "Sub-task",
                    "capability": "Planning package handoff",
                    "delivery": "Build the Jira child drafting path so specialist planning context is carried into each engineering child issue.",
                    "expected_outcome": "Engineering child issues include the planning package context they need to execute.",
                    "acceptance_criteria": ["Jira child draft includes specialist planning context"],
                    "dependencies": ["Planning package schema"],
                    "risks": ["Descriptions may grow too large"],
                    "how_to_test": ["Assert merged planning context appears in the child description"],
                    "done_means": ["Child ticket reflects specialist planning context"],
                    "labels": ["engineering"],
                }
            ],
        ),
    )

    assert data["children_sync_status"] == "children_current"
    assert data["created_children"] == ["GP-2"]
    assert len(created) == 2
    child_description = _adf_text(created[1].description)
    assert "What to Build" in child_description
    assert "Expected Outcome" in child_description
    assert "Specialist Planning Context" in child_description
    assert "Architecture Findings: Architectural boundaries should stay modular." in child_description
    assert "Security Findings: Security review must be explicit." in child_description
    assert "Testing Recommendations: Add regression coverage for the handoff." in child_description


def test_seed_issues_truncates_large_jira_descriptions_before_write() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-a")
    created_payloads: list[object] = []
    huge_line = "Architecture context " + ("X" * 10_000)

    class _FakeClient(_JiraMetadataClientMixin):
        def list_project_issue_types_for_create(self, **_kwargs):  # type: ignore[no-untyped-def]
            return ["Epic", "Story", "Task", "Issue"]

        def search_issues_by_jql(self, **_kwargs):  # type: ignore[no-untyped-def]
            return []

        def create_issue(self, **kwargs):  # type: ignore[no-untyped-def]
            issue = kwargs["issue"]
            bounded = _to_adf_description(issue.description)
            serialized = __import__("json").dumps(
                bounded,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
            assert len(serialized) <= MAX_JIRA_ADF_DOCUMENT_BYTES
            created_payloads.append(bounded)
            return JiraIssueCreateResult(key="GP-1" if not issue.parent_issue_key else "GP-2", issue_id="1")

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
        plan_seed_issues_with_runtime_fn=lambda **_kwargs: _engineering_seed_plan(),
        codex_runtime_error_type=RuntimeError,
        build_seed_issue_description_fn=lambda **_kwargs: {},
        issue_key_pattern=__import__("re").compile(r"^[A-Z]+-\d+$"),
        tenant_atlassian_oauth_context_fn=lambda **_kwargs: {
            "client": _FakeClient(),
            "access_token": "token",
            "connection": SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
        },
        select_seed_match_fn=lambda **_kwargs: None,
        pm_status="pm_completed",
        planning_package={
            "planning_state": "planning_completed",
            "specialist_outputs": {
                "architecture": {
                    "findings": [huge_line, huge_line, huge_line],
                    "recommendations": [huge_line, huge_line],
                    "required_tasks": [huge_line],
                    "open_behavior_questions": [],
                    "acceptance_impacts": [huge_line],
                    "mermaid_diagram": "flowchart TD\n" + ("A-->B\n" * 5000),
                },
                "security": {
                    "findings": [huge_line],
                    "recommendations": [huge_line],
                    "required_tasks": [],
                    "open_behavior_questions": [],
                    "acceptance_impacts": [huge_line],
                },
                "testing": {
                    "findings": [huge_line],
                    "recommendations": [huge_line],
                    "required_tasks": [huge_line],
                    "open_behavior_questions": [],
                    "acceptance_impacts": [huge_line],
                },
            },
            "architecture_summary": [huge_line, huge_line, huge_line],
            "architecture_diagram": "flowchart TD\n" + ("Parent-->Planner\n" * 5000),
            "child_issues": [
                {
                    "summary": "Merge planning package into Jira child draft",
                    "issue_type": "Sub-task",
                    "capability": huge_line,
                    "delivery": huge_line,
                    "expected_outcome": huge_line,
                    "acceptance_criteria": [huge_line, huge_line],
                    "dependencies": [huge_line],
                    "risks": [huge_line],
                    "how_to_test": [huge_line],
                    "done_means": [huge_line, huge_line],
                    "labels": ["engineering"],
                }
            ],
        },
    )

    assert data["children_sync_status"] == "children_current"
    assert len(created_payloads) == 2
    created_text = _adf_text(created_payloads[0])
    child_text = _adf_text(created_payloads[1])
    assert "Content truncated to fit Jira content size limit." not in created_text
    assert "Content truncated to fit Jira content size limit." not in child_text


def test_seed_issues_blocks_child_fanout_when_architecture_document_is_still_draft() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-a")
    created: list[object] = []
    remote_links: list[dict[str, object]] = []

    class _FakeClient(_JiraMetadataClientMixin):
        def list_project_issue_types_for_create(self, **_kwargs):  # type: ignore[no-untyped-def]
            return ["Epic", "Story", "Task", "Issue"]

        def search_issues_by_jql(self, **_kwargs):  # type: ignore[no-untyped-def]
            return []

        def create_issue(self, **kwargs):  # type: ignore[no-untyped-def]
            created.append(kwargs["issue"])
            return JiraIssueCreateResult(key="GP-1", issue_id="1")

        def upsert_remote_issue_link(self, **kwargs):  # type: ignore[no-untyped-def]
            remote_links.append(kwargs)
            return {}

    payload = _seed_payload()
    payload["parent_issue"]["labels"] = ["product", "architecture-required"]
    with patch(
        "orchestrator.api.discord.seed.issue_service._architecture_gate_for_parent_issue",
        return_value=(
            ArchitectureDocumentGate(
                required=True,
                provider="internal",
                document=SimpleNamespace(
                    title="Decision Engine v2",
                    canonical_url="https://docs.example.com/decision-engine-v2",
                ),
                ready=False,
                block_reason="Architecture document is still draft",
            ),
            SimpleNamespace(
                title="Decision Engine v2",
                url="https://docs.example.com/decision-engine-v2",
            ),
        ),
    ):
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
            plan_seed_issues_with_runtime_fn=lambda **_kwargs: EngineeringSeedPlanPayload.from_payload(payload),
            codex_runtime_error_type=RuntimeError,
            build_seed_issue_description_fn=lambda **_kwargs: {},
            issue_key_pattern=__import__("re").compile(r"^[A-Z]+-\d+$"),
            tenant_atlassian_oauth_context_fn=lambda **_kwargs: {
                "client": _FakeClient(),
                "access_token": "token",
                "connection": SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
            },
            select_seed_match_fn=lambda **_kwargs: None,
            planning_package=_planning_package(planning_state="planning_completed"),
        )

    assert "Architecture is required for this epic" in message
    assert data["children_sync_status"] == "planning_blocked"
    assert data["created_children"] == []
    assert data["architecture_document_required"] is True
    assert len(created) == 1
    assert len(remote_links) == 1
    assert remote_links[0]["issue_id_or_key"] == "GP-1"
    assert remote_links[0]["relationship"] == "Architecture"
