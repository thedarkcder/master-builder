from __future__ import annotations
import logging
import re
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.discord.seed.description import build_parent_feature_description
from orchestrator.core.runtime_invocation import AgentInvocationContext
from orchestrator.core.codex_runtime import CodexRuntimeError
from orchestrator.core.specialist_planning import (
    PLANNING_STATE_COMPLETED,
    SpecialistPlanningRequest,
    build_runtime_seed_planning_package,
    run_specialist_planning_fanout,
)
from orchestrator.core.followup_context_service import (
    FOLLOWUP_CONTEXT_PM_INTERVIEW,
    FOLLOWUP_CONTEXT_ENGINEERING_CLARIFICATION,
    close_followup_contexts,
    resolve_issue_followup_context,
    upsert_followup_context,
)
from orchestrator.core.parent_feature_brief_store import (
    persist_parent_feature_brief_snapshot,
    resolve_parent_feature_brief,
    resolve_parent_feature_case,
)
from orchestrator.core.clarification_projection_service import (
    ClarificationProjectionSpec,
    clarification_question_reason,
    clarification_question_text,
    has_matching_active_clarification_state,
    resolve_active_clarification_context,
    upsert_clarification_projection,
)
from orchestrator.core.pm_interview_service import (
    PM_INTERVIEW_PARENT_BRIEF_CHANNEL_ID,
    PM_INTERVIEW_SOURCE_KIND_PARENT_BRIEF_SNAPSHOT,
    PM_INTERVIEW_STATUS_PM_COMPLETED,
    PM_INTERVIEW_STATUS_QUESTION_PENDING,
    format_pm_interview_question,
    mark_pm_interview_case_completed,
    normalize_parent_feature_brief_with_runtime,
    upsert_pm_interview_case,
)
from orchestrator.core.pm_interview_followup_service import continue_pm_interview_from_followup
from orchestrator.core.platform_secret_service import (
    PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF,
    resolve_platform_secret_ref,
)
from orchestrator.core.parent_feature_planning_workflow import (
    ParentFeaturePlanningWorkflow,
    ParentFeaturePlanningWorkflowDeps,
)
from orchestrator.core.workflow_runtime import WorkflowAdvanceRequest, WorkflowAdvanceResult
from orchestrator.core.workflow_execution_projection import (
    classify_external_workflow_failure,
    ensure_issue_workflow_execution,
    resolve_latest_issue_workflow,
)
from orchestrator.core.workflow_type_catalog import get_workflow_type
from orchestrator.core.webhook_job_errors import RetryableWebhookJobError
from orchestrator.storage.models import FollowupContext, Project
from orchestrator.tools.discord_api import DiscordApiClient, DiscordApiError
from orchestrator.tools.jira_oauth import JiraIssueDetail

logger = logging.getLogger(__name__)

_SYSTEM_COMMENT_MARKER = "[mb-system]"
_SYNC_LABELS = {"sync-current", "sync-stale", "sync-blocked"}
_PARENT_BOARD_ENTRY_STATUSES = {"to do", "ready for agent"}
_CHILD_STATUSES_TO_LEAVE = {"to do", "ready for agent", "in progress", "testing", "done"}
_PARENT_SYNC_MATERIAL_FIELDS = {
    "summary",
    "description",
    "acceptance criteria",
    "acceptance_criteria",
    "scope",
    "scope in",
    "scope out",
    "ui / design / references",
    "ui references",
    "risk",
    "risks",
    "dependency",
    "dependencies",
    "open questions",
    "open question",
}
_ISSUE_KEY_PATTERN = re.compile(r"([A-Z][A-Z0-9_]+-\d+)")
_PM_INTERVIEW_JIRA_TRANSPORT = "jira_issue_comment"
_PM_INTERVIEW_JIRA_REPLY_SCOPE = "issue_comment_stream_from_root"


def _pm_interview_jira_followup_request_id(*, parent_issue_key: str) -> str:
    return f"pm-interview-jira:{str(parent_issue_key or '').strip().upper()}"


def _normalize_comment_id(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, int):
        return str(value)
    return None


def _extract_created_comment_id(comment: dict[str, Any] | None) -> str | None:
    if not isinstance(comment, dict):
        return None
    return _normalize_comment_id(comment.get("id"))

@dataclass(frozen=True)
class JiraParentChildSyncContext:
    request_id: str
    tenant_id: str
    tenant: Any
    project_id: str | None
    issue_key: str
    issue_labels: list[str]
    payload: dict[str, Any]
    webhook_event: str | None
    comment_command: str | None
    comment_command_argument: str | None


@dataclass(frozen=True)
class JiraParentChildSyncResult:
    handled: bool
    reason: str | None = None
    extra: dict[str, object] = field(default_factory=dict)


class ParentFeatureWorkflowAdvanceHandler:
    def __init__(
        self,
        *,
        integration_router,
        extract_changed_fields_fn,
        extract_status_transition_fn,
        build_runtime_for_selector_fn,
        seed_issues_with_runtime_fn,
        post_jira_comment_fn,
        create_jira_comment_fn,
    ) -> None:
        self._integration_router = integration_router
        self._extract_changed_fields_fn = extract_changed_fields_fn
        self._extract_status_transition_fn = extract_status_transition_fn
        self._build_runtime_for_selector_fn = build_runtime_for_selector_fn
        self._seed_issues_with_runtime_fn = seed_issues_with_runtime_fn
        self._post_jira_comment_fn = post_jira_comment_fn
        self._create_jira_comment_fn = create_jira_comment_fn

    def advance(
        self,
        *,
        session: Session,
        settings,  # noqa: ANN001
        workflow_type,
        request: WorkflowAdvanceRequest,
    ) -> WorkflowAdvanceResult:
        context = JiraParentChildSyncContext(
            request_id=str(request.payload.get("request_id") or "").strip() or f"workflow-advance:{request.issue_key}",
            tenant_id=request.tenant_id,
            tenant=request.tenant,
            project_id=request.project_id,
            issue_key=request.issue_key,
            issue_labels=list(request.issue_labels),
            payload=dict(request.payload),
            webhook_event=request.webhook_event,
            comment_command=request.comment_command,
            comment_command_argument=request.comment_command_argument,
        )
        issue_gateway = _JiraParentIssueGateway(
            session=session,
            settings=settings,
            context=context,
            integration_router=self._integration_router,
            post_jira_comment_fn=self._post_jira_comment_fn,
            create_jira_comment_fn=self._create_jira_comment_fn,
        )
        brief_planner = _ParentBriefPlanner(
            session=session,
            settings=settings,
            context=context,
            build_runtime_for_selector_fn=self._build_runtime_for_selector_fn,
        )
        child_sync_gateway = _ParentChildSyncGateway(
            session=session,
            context=context,
            seed_issues_with_runtime_fn=self._seed_issues_with_runtime_fn,
        )
        workflow = ParentFeaturePlanningWorkflow(
            deps=ParentFeaturePlanningWorkflowDeps(
                issue_gateway=issue_gateway,
                brief_planner=brief_planner,
                child_sync_gateway=child_sync_gateway,
                workflow_type=workflow_type,
                project_key_for_issue_fn=_project_key_for_issue,
                material_parent_changed_fields_fn=_material_parent_changed_fields,
                parent_board_entry_target_status_fn=_parent_board_entry_target_status,
                extract_changed_fields_fn=self._extract_changed_fields_fn,
                extract_status_transition_fn=self._extract_status_transition_fn,
            )
        )
        result = workflow.handle(
            context=context,
            session=session,
            settings=settings,
        )
        return WorkflowAdvanceResult(
            handled=result.handled,
            reason=result.reason,
            extra=dict(result.extra or {}),
        )


def build_workflow_advance_handler_resolver(
    *,
    integration_router,
    extract_changed_fields_fn,
    extract_status_transition_fn,
    build_runtime_for_selector_fn,
    seed_issues_with_runtime_fn,
    post_jira_comment_fn,
    create_jira_comment_fn,
):
    handlers = {
        "jira_parent_feature": ParentFeatureWorkflowAdvanceHandler(
            integration_router=integration_router,
            extract_changed_fields_fn=extract_changed_fields_fn,
            extract_status_transition_fn=extract_status_transition_fn,
            build_runtime_for_selector_fn=build_runtime_for_selector_fn,
            seed_issues_with_runtime_fn=seed_issues_with_runtime_fn,
            post_jira_comment_fn=post_jira_comment_fn,
            create_jira_comment_fn=create_jira_comment_fn,
        )
    }

    def _resolve(handler_key: str):
        normalized = str(handler_key or "").strip()
        handler = handlers.get(normalized)
        if handler is None:
            raise LookupError(f"No workflow advance handler is registered for {handler_key}")
        return handler

    return _resolve


class _JiraParentIssueGateway:
    def __init__(
        self,
        *,
        session: Session,
        settings,  # noqa: ANN001
        context: JiraParentChildSyncContext,
        integration_router,
        post_jira_comment_fn,
        create_jira_comment_fn,
    ) -> None:
        self._session = session
        self._settings = settings
        self._context = context
        self._integration_router = integration_router
        self._post_jira_comment_fn = post_jira_comment_fn
        self._create_jira_comment_fn = create_jira_comment_fn
        self._jira_adapter = None

    def _jira(self):
        if self._jira_adapter is None:
            self._jira_adapter = self._integration_router.jira(
                session=self._session,
                tenant=self._context.tenant,
                settings=self._settings,
            )
        return self._jira_adapter

    def _oauth_context(self):
        jira = self._jira()
        return SimpleNamespace(
            client=jira.client,
            access_token=jira.access_token,
            connection=SimpleNamespace(cloud_id=jira.cloud_id, site_url=jira.site_url),
        )

    def load_parent_detail(self, issue_key: str) -> JiraIssueDetail:
        return self._jira().get_issue_detail(issue_id_or_key=issue_key)

    def load_child_details(self, *, project_key: str, parent_issue_key: str) -> list[JiraIssueDetail]:
        previews = self._jira().list_child_issue_previews(
            project_key=project_key,
            parent_issue_key=parent_issue_key,
        )
        return [
            self._jira().get_issue_detail(issue_id_or_key=preview.key)
            for preview in previews
        ]

    def rewrite_parent_issue_from_brief(
        self,
        *,
        parent_detail: JiraIssueDetail,
        brief_payload: dict[str, object],
        sync_status: str,
        planning_state: str | None,
        open_questions: list[object] | None,
    ) -> None:
        _rewrite_parent_issue_from_brief(
            oauth=self._oauth_context(),
            parent_detail=parent_detail,
            brief_payload=brief_payload,
            sync_status=sync_status,
            planning_state=planning_state,
            open_questions=open_questions,
        )

    def update_issue_sync_label(self, *, issue_detail: JiraIssueDetail, target_label: str) -> None:
        _update_issue_sync_label(
            oauth=self._oauth_context(),
            issue_detail=issue_detail,
            target_label=target_label,
        )

    def post_parent_brief_questions(self, *, parent_issue_key: str, questions: list[object]) -> bool:
        return _post_parent_brief_questions_to_discord(
            session=self._session,
            settings=self._settings,
            tenant=self._context.tenant,
            project_id=self._context.project_id,
            parent_issue_key=parent_issue_key,
            questions=questions,
        )

    def post_parent_brief_questions_jira(
        self,
        *,
        parent_issue_key: str,
        questions: list[object],
    ) -> tuple[dict[str, Any] | None, str | None]:
        created_comment, error = _post_parent_brief_questions_to_jira(
            session=self._session,
            tenant=self._context.tenant,
            project_id=self._context.project_id,
            issue_key=parent_issue_key,
            payload=dict(self._context.payload or {}),
            questions=questions,
            settings=self._settings,
            create_jira_comment_fn=self._create_jira_comment_fn,
        )
        return created_comment, error

    def has_matching_active_pm_clarification_state(
        self,
        *,
        parent_issue_key: str,
        questions: list[object],
    ) -> bool:
        return has_matching_active_clarification_state(
            session=self._session,
            tenant_id=self._context.tenant_id,
            issue_key=parent_issue_key,
            context_type=FOLLOWUP_CONTEXT_PM_INTERVIEW,
            questions=questions,
            transport=_PM_INTERVIEW_JIRA_TRANSPORT,
            reply_scope=_PM_INTERVIEW_JIRA_REPLY_SCOPE,
        )

    def post_sync_note(self, *, issue_key: str, body: str) -> None:
        _post_sync_note(
            session=self._session,
            tenant=self._context.tenant,
            issue_key=issue_key,
            settings=self._settings,
            body=body,
            post_jira_comment_fn=self._post_jira_comment_fn,
        )

    def mark_issues_sync_blocked(self, *, issue_keys: list[str]) -> None:
        _mark_issues_sync_blocked(oauth=self._oauth_context(), issue_keys=issue_keys)

    def transition_issue(self, *, issue_key: str, target_status: str) -> None:
        oauth = self._oauth_context()
        oauth.client.transition_issue(
            access_token=oauth.access_token,
            cloud_id=oauth.connection.cloud_id,
            issue_id_or_key=issue_key,
            target_status=target_status,
        )


def _jira_adapter(*, integration_router, session: Session, tenant, settings):  # noqa: ANN001
    return integration_router.jira(
        session=session,
        tenant=tenant,
        settings=settings,
    )


def _jira_oauth_context(*, integration_router, session: Session, tenant, settings):  # noqa: ANN001
    jira = _jira_adapter(
        integration_router=integration_router,
        session=session,
        tenant=tenant,
        settings=settings,
    )
    return SimpleNamespace(
        client=jira.client,
        access_token=jira.access_token,
        connection=SimpleNamespace(cloud_id=jira.cloud_id, site_url=jira.site_url),
    )


class _ParentBriefPlanner:
    def __init__(
        self,
        *,
        session: Session,
        settings,  # noqa: ANN001
        context: JiraParentChildSyncContext,
        build_runtime_for_selector_fn,
    ) -> None:
        self._session = session
        self._settings = settings
        self._context = context
        self._build_runtime_for_selector_fn = build_runtime_for_selector_fn

    def resolve_product_brief(
        self,
        *,
        parent_detail: JiraIssueDetail,
        refresh: bool,
    ) -> tuple[dict[str, object], list[str]]:
        return _resolve_parent_product_brief(
            session=self._session,
            settings=self._settings,
            tenant_id=self._context.tenant_id,
            project_id=self._context.project_id,
            parent_detail=parent_detail,
            build_runtime_for_selector_fn=self._build_runtime_for_selector_fn,
            refresh=refresh,
        )

    def plan_backlog_parent(
        self,
        *,
        parent_detail: JiraIssueDetail,
        product_brief: dict[str, object],
        project_key: str,
    ) -> tuple[object, dict[str, Any]]:
        planning_runtime = self._build_runtime_for_selector_fn(
            session=self._session,
            settings=self._settings,
            tenant_id=self._context.tenant_id,
            project_id=self._context.project_id,
            selector="workflow.pm_planning_architect",
        )
        planning_result = run_specialist_planning_fanout(
            session=self._session,
            settings=self._settings,
            runtime=planning_runtime,
            request=SpecialistPlanningRequest(
                tenant_id=self._context.tenant_id,
                project_id=self._context.project_id,
                parent_issue_key=parent_detail.key,
                parent_summary=parent_detail.summary,
                parent_description=parent_detail.description,
                product_brief=product_brief,
                project_keys=(project_key,),
                related_issues=(),
                status_counts={parent_detail.status: 1},
                github_context={},
                conversation_history=(),
                working_dir=".",
            ),
            runtime_for_selector=lambda selector: self._build_runtime_for_selector_fn(
                session=self._session,
                settings=self._settings,
                tenant_id=self._context.tenant_id,
                project_id=self._context.project_id,
                selector=selector,
            ),
        )
        planning_package = build_runtime_seed_planning_package(
            result=planning_result,
            behavior_slice=str(product_brief.get("objective") or parent_detail.summary).strip() or parent_detail.summary,
        )
        return planning_result, planning_package


class _ParentChildSyncGateway:
    def __init__(
        self,
        *,
        session: Session,
        context: JiraParentChildSyncContext,
        seed_issues_with_runtime_fn,
    ) -> None:
        self._session = session
        self._context = context
        self._seed_issues_with_runtime_fn = seed_issues_with_runtime_fn

    def seed_parent_backlog_children(
        self,
        *,
        parent_detail: JiraIssueDetail,
        project_key: str,
        planning_package: dict[str, Any],
        planning_state: str,
    ) -> dict[str, Any]:
        _, seed_data = self._seed_issues_with_runtime_fn(
            session=self._session,
            tenant=self._context.tenant,
            prompt_markdown=_build_parent_seed_prompt(parent_detail=parent_detail),
            scoped_project_id=self._context.project_id,
            force_issue_keys=[self._context.issue_key],
            allow_create=True,
            allow_empty_children=planning_state != PLANNING_STATE_COMPLETED,
            scoped_project_keys=[project_key],
            codex_working_dir=".",
            planning_package=planning_package,
        )
        return seed_data

    def refresh_parent_children(
        self,
        *,
        parent_detail: JiraIssueDetail,
        child_details: list[JiraIssueDetail],
        changed_fields: list[str],
        project_key: str,
    ) -> dict[str, Any]:
        _, seed_data = self._seed_issues_with_runtime_fn(
            session=self._session,
            tenant=self._context.tenant,
            prompt_markdown=_build_parent_resync_prompt(
                parent_detail=parent_detail,
                child_details=child_details,
                changed_fields=changed_fields,
            ),
            scoped_project_id=self._context.project_id,
            force_issue_keys=[self._context.issue_key, *[detail.key for detail in child_details]],
            allow_create=True,
            allow_empty_children=True,
            scoped_project_keys=[project_key],
            codex_working_dir=".",
        )
        return seed_data

    @staticmethod
    def combined_child_updates(*, seed_data: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
        return _combined_child_updates(seed_data=seed_data)

    @staticmethod
    def sync_completion_note(*, updated_children: list[str], created_children: list[str]) -> str:
        return _sync_completion_note(updated_children=updated_children, created_children=created_children)

    @staticmethod
    def fanout_completion_note(
        *,
        target_status: str,
        promoted_children: list[str],
        unchanged_children: list[str],
        skipped_children: list[str],
        failed_children: list[str],
    ) -> str:
        return _fanout_completion_note(
            target_status=target_status,
            promoted_children=promoted_children,
            unchanged_children=unchanged_children,
            skipped_children=skipped_children,
            failed_children=failed_children,
        )


def is_system_generated_comment(*, text: str | None) -> bool:
    normalized_text = str(text or "").strip()
    if not normalized_text:
        return False
    return normalized_text.startswith(_SYSTEM_COMMENT_MARKER)


def _project_key_for_issue(issue_key: str) -> str:
    normalized = str(issue_key or "").strip().upper()
    if "-" not in normalized:
        return normalized
    return normalized.split("-", 1)[0]


def _normalize_sync_labels(labels: list[str], *, target_label: str) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []
    for raw_label in labels:
        label = str(raw_label or "").strip()
        if not label or label.casefold() in _SYNC_LABELS:
            continue
        lowered = label.casefold()
        if lowered in seen:
            continue
        seen.add(lowered)
        normalized.append(label)
    if target_label.casefold() not in seen:
        normalized.append(target_label)
    return normalized


def _update_issue_sync_label(
    *,
    oauth,
    issue_detail: JiraIssueDetail,
    target_label: str,
) -> None:
    oauth.client.replace_issue_labels(
        access_token=oauth.access_token,
        cloud_id=oauth.connection.cloud_id,
        issue_id_or_key=issue_detail.key,
        labels=_normalize_sync_labels(issue_detail.labels, target_label=target_label),
    )


def _extract_parent_issue_key(*, child_detail: JiraIssueDetail) -> str | None:
    for line in str(child_detail.description or "").splitlines():
        match = _ISSUE_KEY_PATTERN.search(line.strip().upper())
        if match:
            candidate = match.group(1).strip().upper()
            if candidate != child_detail.key.upper():
                return candidate
    for label in child_detail.labels:
        normalized = str(label or "").strip().lower()
        if not normalized.startswith("parent-"):
            continue
        raw_key = normalized[len("parent-") :].strip("-")
        if not raw_key:
            continue
        parts = [part for part in raw_key.split("-") if part]
        if len(parts) < 2:
            continue
        return f"{'-'.join(parts[:-1]).upper()}-{parts[-1]}"
    return None


def _material_parent_changed_fields(*, payload: dict[str, Any], extract_changed_fields_fn) -> list[str]:  # noqa: ANN001
    changed_fields = extract_changed_fields_fn(payload)
    material: list[str] = []
    seen: set[str] = set()
    for changed_field in changed_fields:
        normalized = str(changed_field or "").strip().casefold()
        if normalized in {"status", "labels"}:
            continue
        if normalized in _PARENT_SYNC_MATERIAL_FIELDS or normalized in {"summary", "description"}:
            if normalized not in seen:
                seen.add(normalized)
                material.append(normalized)
    return material


def _normalize_status_name(value: str | None) -> str:
    return str(value or "").strip().casefold()


def _parent_board_entry_target_status(*, payload: dict[str, Any], extract_status_transition_fn) -> str | None:  # noqa: ANN001
    from_status, to_status = extract_status_transition_fn(payload)
    normalized_to_status = _normalize_status_name(to_status)
    if normalized_to_status not in _PARENT_BOARD_ENTRY_STATUSES:
        return None
    if _normalize_status_name(from_status) == normalized_to_status:
        return None
    target_status = str(to_status or "").strip()
    return target_status or None


def _build_child_snapshot_markdown(child_detail: JiraIssueDetail) -> str:
    return (
        f"### {child_detail.key}: {child_detail.summary}\n"
        f"Labels: {', '.join(child_detail.labels) if child_detail.labels else 'none'}\n"
        f"Description:\n{child_detail.description.strip() or 'No description provided.'}"
    )


def _build_parent_resync_prompt(
    *,
    parent_detail: JiraIssueDetail,
    child_details: list[JiraIssueDetail],
    changed_fields: list[str],
) -> str:
    child_block = "\n\n".join(_build_child_snapshot_markdown(detail) for detail in child_details)
    changed_lines = "\n".join(f"- {field}" for field in changed_fields) or "- description"
    return (
        "Refresh the existing Jira parent/child hierarchy from the latest PM parent issue edit.\n\n"
        f"## Parent feature\n"
        f"- Key: {parent_detail.key}\n"
        f"- Summary: {parent_detail.summary}\n"
        f"- Changed fields in Jira webhook:\n{changed_lines}\n\n"
        f"Description:\n{parent_detail.description.strip() or 'No description provided.'}\n\n"
        "## Existing engineering children\n"
        f"{child_block}\n\n"
        "Instructions:\n"
        "- Reconstruct the PM-owned parent issue from the live parent description.\n"
        "- Return only the engineering child tickets materially impacted by the parent change.\n"
        "- If no engineering child tickets need changes, return engineering_children as an empty array.\n"
        "- Preserve existing issue keys when known from the source context.\n"
        "- Do not create duplicates.\n"
        "- Keep the parent non-technical and the children technical.\n"
    )


def _build_clarification_followup_prompt(
    *,
    parent_detail: JiraIssueDetail,
    child_details: list[JiraIssueDetail],
    metadata: dict[str, Any],
    reply_text: str,
) -> str:
    pending_questions = metadata.get("questions")
    pending_lines: list[str] = []
    if isinstance(pending_questions, list):
        for item in pending_questions:
            if not isinstance(item, dict):
                continue
            stakeholder_question = str(item.get("stakeholder_question") or "").strip()
            source_child_key = str(item.get("source_child_key") or "").strip().upper()
            original_question = str(item.get("original_question") or "").strip()
            if stakeholder_question:
                pending_lines.append(
                    f"- {source_child_key or 'child'} asked: {original_question}\n"
                    f"  Product clarification needed: {stakeholder_question}"
                )
    child_block = "\n\n".join(_build_child_snapshot_markdown(detail) for detail in child_details)
    questions_block = "\n".join(pending_lines) or "- No prior clarification prompts were captured."
    return (
        "Update the existing Jira parent/child hierarchy using the PM clarification reply below.\n\n"
        f"## Parent feature\n"
        f"- Key: {parent_detail.key}\n"
        f"- Summary: {parent_detail.summary}\n"
        f"Description:\n{parent_detail.description.strip() or 'No description provided.'}\n\n"
        "## Pending product-behavior clarification\n"
        f"{questions_block}\n\n"
        "## PM / stakeholder reply\n"
        f"{reply_text.strip()}\n\n"
        "## Affected engineering children (refresh all of these)\n"
        f"{child_block}\n\n"
        "Instructions:\n"
        "- Update the parent issue first using the clarified behavior.\n"
        "- Refresh all listed engineering child tickets from the updated parent.\n"
        "- If no engineering child tickets need changes, return engineering_children as an empty array.\n"
        "- Do not create duplicates.\n"
        "- Keep the parent product-focused and the children technical.\n"
    )


def _sync_note(*, body: str) -> str:
    return f"{_SYSTEM_COMMENT_MARKER} {body}".strip()


def _extract_jira_issue_mention_target(*, payload: dict[str, Any]) -> tuple[str | None, str | None]:
    issue = payload.get("issue") if isinstance(payload, dict) else None
    fields = issue.get("fields") if isinstance(issue, dict) and isinstance(issue.get("fields"), dict) else {}
    for field_name in ("reporter", "assignee"):
        actor = fields.get(field_name)
        if not isinstance(actor, dict):
            continue
        account_id = str(actor.get("accountId") or "").strip() or None
        display_name = str(actor.get("displayName") or "").strip() or None
        if account_id:
            return account_id, display_name
    return None, None


def _question_text(value: object) -> str:
    return clarification_question_text(value)


def _question_why_it_matters(value: object) -> str | None:
    return clarification_question_reason(value)


def _build_jira_question_list_items(*, questions: list[object]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for raw_question in questions:
        question_text = _question_text(raw_question)
        if not question_text:
            continue
        list_item_content: list[dict[str, Any]] = [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": question_text}],
            }
        ]
        why_it_matters = _question_why_it_matters(raw_question)
        if why_it_matters:
            list_item_content.append(
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": f"Why it matters: {why_it_matters}"}],
                }
            )
        items.append({"type": "listItem", "content": list_item_content})
    return items


def _jira_question_comment_adf(
    *,
    issue_key: str,
    questions: list[object],
    mention_account_id: str | None,
    mention_display_name: str | None,
    intro_text: str | None = None,
) -> dict[str, Any]:
    intro_content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": f"{_SYSTEM_COMMENT_MARKER} ",
        }
    ]
    if mention_account_id:
        intro_content.append(
            {
                "type": "mention",
                "attrs": {
                    "id": mention_account_id,
                    "text": f"@{mention_display_name or 'reporter'}",
                },
            }
        )
        intro_content.append({"type": "text", "text": " "})
    intro_content.append(
        {
            "type": "text",
            "text": intro_text
            or (
                f"Master Builder needs product clarification on {issue_key} before PM planning can continue. "
                "Please reply on this Jira issue with answers to the questions below."
            ),
        }
    )
    question_items = _build_jira_question_list_items(questions=questions)
    content: list[dict[str, Any]] = [
        {"type": "paragraph", "content": intro_content},
        {"type": "orderedList", "content": question_items},
    ]
    return {"type": "doc", "version": 1, "content": content}


def _post_parent_brief_questions_to_jira(
    *,
    session: Session,
    tenant,
    project_id: str | None,
    issue_key: str,
    payload: dict[str, Any],
    questions: list[object],
    settings,  # noqa: ANN001
    create_jira_comment_fn,
) -> tuple[dict[str, Any] | None, str | None]:  # noqa: ANN001
    mention_account_id, mention_display_name = _extract_jira_issue_mention_target(payload=payload)
    comment = _jira_question_comment_adf(
        issue_key=issue_key,
        questions=questions,
        mention_account_id=mention_account_id,
        mention_display_name=mention_display_name,
    )
    created_comment, error = create_jira_comment_fn(
        session=session,
        tenant=tenant,
        issue_key=issue_key,
        comment=comment,
        settings=settings,
    )
    if error is None:
        _persist_parent_brief_jira_followup(
            session=session,
            tenant=tenant,
            project_id=project_id,
            parent_issue_key=issue_key,
            questions=questions,
            posted_comment_id=_extract_created_comment_id(created_comment),
        )
    return created_comment, error


def _post_engineering_clarification_questions_to_jira(
    *,
    session: Session,
    tenant,
    issue_key: str,
    payload: dict[str, Any],
    questions: list[object],
    settings,  # noqa: ANN001
    create_jira_comment_fn,
) -> tuple[dict[str, Any] | None, str | None]:  # noqa: ANN001
    mention_account_id, mention_display_name = _extract_jira_issue_mention_target(payload=payload)
    comment = _jira_question_comment_adf(
        issue_key=issue_key,
        questions=questions,
        mention_account_id=mention_account_id,
        mention_display_name=mention_display_name,
        intro_text=(
            f"Engineering child tickets need product clarification on {issue_key} before engineering can resume. "
            "Please reply on this Jira issue with answers to the questions below."
        ),
    )
    return create_jira_comment_fn(
        session=session,
        tenant=tenant,
        issue_key=issue_key,
        comment=comment,
        settings=settings,
    )


def _persist_parent_brief_jira_followup(
    *,
    session: Session,
    tenant,
    project_id: str | None,
    parent_issue_key: str,
    questions: list[object],
    posted_comment_id: str | None,
) -> None:
    parent_case = resolve_parent_feature_case(
        session=session,
        tenant_id=str(getattr(tenant, "tenant_id", "") or ""),
        parent_issue_key=parent_issue_key,
    )
    existing_request_id = str(getattr(parent_case, "request_id", "") or "").strip() or None
    existing_source_kind = str(getattr(parent_case, "source_kind", "") or "").strip()
    pm_request_id = (
        existing_request_id
        if existing_request_id and existing_source_kind != PM_INTERVIEW_SOURCE_KIND_PARENT_BRIEF_SNAPSHOT
        else f"pm-parent-interview:{parent_issue_key}"
    )
    owner_user_id = str(getattr(parent_case, "owner_user_id", "") or "").strip() or None
    root_message_id = str(getattr(parent_case, "root_message_id", "") or "").strip() or posted_comment_id or None
    interview_case = upsert_pm_interview_case(
        session=session,
        tenant_id=str(getattr(tenant, "tenant_id", "") or ""),
        project_id=project_id,
        request_id=pm_request_id,
        source_kind="jira_parent",
        channel_id=str(getattr(parent_case, "channel_id", "") or "").strip() or PM_INTERVIEW_PARENT_BRIEF_CHANNEL_ID,
        thread_channel_id=str(getattr(parent_case, "thread_channel_id", "") or "").strip() or None,
        root_message_id=root_message_id,
        owner_user_id=owner_user_id,
        parent_issue_key=parent_issue_key,
        source_text=str(getattr(parent_case, "source_text", "") or "") or f"Parent issue {parent_issue_key} requires PM clarification.",
        status=PM_INTERVIEW_STATUS_QUESTION_PENDING,
        brief=resolve_parent_feature_brief(
            session=session,
            tenant_id=str(getattr(tenant, "tenant_id", "") or ""),
            parent_issue_key=parent_issue_key,
            include_incomplete=True,
        ),
        notes={
            "source": "jira_parent_brief_normalization",
            "normalization_questions": list(questions),
        },
    )
    upsert_clarification_projection(
        session=session,
        spec=ClarificationProjectionSpec(
            tenant_id=str(getattr(tenant, "tenant_id", "") or ""),
            project_id=project_id,
            context_type=FOLLOWUP_CONTEXT_PM_INTERVIEW,
            issue_key=parent_issue_key,
            request_id=_pm_interview_jira_followup_request_id(parent_issue_key=parent_issue_key),
            channel_id=parent_issue_key,
            thread_channel_id=None,
            root_message_id=posted_comment_id,
            owner_user_id=owner_user_id or None,
            origin_command="pm",
            transport=_PM_INTERVIEW_JIRA_TRANSPORT,
            reply_scope=_PM_INTERVIEW_JIRA_REPLY_SCOPE,
            questions=questions,
            metadata={
                "parent_issue_key": parent_issue_key,
                "questions": [item for item in questions if _question_text(item)],
                "source": "jira_parent_brief_normalization",
                "pm_request_id": str(getattr(interview_case, "request_id", "") or "").strip() or pm_request_id,
            },
        ),
    )


def _post_sync_note(
    *,
    session: Session,
    tenant,
    issue_key: str,
    settings,  # noqa: ANN001
    body: str,
    post_jira_comment_fn,
) -> tuple[bool, str | None]:  # noqa: ANN001
    return post_jira_comment_fn(
        session=session,
        tenant=tenant,
        issue_key=issue_key,
        comment=_sync_note(body=body),
        settings=settings,
    )


def _engineering_decision_note(
    *,
    question: str,
    owner: str,
    approval_path: str,
    rationale: str,
    status_line: str,
) -> str:
    lines = [
        "Implementation decision record",
        f"- Question: {question.strip()}",
        f"- Decision owner: {owner.strip()}",
        f"- Approval path: {approval_path.strip()}",
        f"- Status: {status_line.strip()}",
    ]
    if rationale.strip():
        lines.append(f"- Rationale: {rationale.strip()}")
    return "\n".join(lines)


def _active_issue_followup_contexts(
    *,
    session: Session,
    tenant_id: str,
    issue_key: str,
    context_type: str,
) -> list[FollowupContext]:
    normalized_tenant_id = str(tenant_id or "").strip()
    normalized_issue_key = str(issue_key or "").strip().upper()
    normalized_context_type = str(context_type or "").strip()
    if not normalized_tenant_id or not normalized_issue_key or not normalized_context_type:
        return []
    return session.execute(
        select(FollowupContext)
        .where(
            FollowupContext.tenant_id == normalized_tenant_id,
            FollowupContext.issue_key == normalized_issue_key,
            FollowupContext.context_type == normalized_context_type,
            FollowupContext.status == "active",
        )
        .order_by(FollowupContext.updated_at.desc())
    ).scalars().all()


def _mark_issues_sync_blocked(
    *,
    oauth,
    issue_keys: list[str],
) -> None:
    for issue_key in issue_keys:
        detail = oauth.client.get_issue_detail(
            access_token=oauth.access_token,
            cloud_id=oauth.connection.cloud_id,
            issue_id_or_key=issue_key,
        )
        _update_issue_sync_label(
            oauth=oauth,
            issue_detail=detail,
            target_label="sync-blocked",
        )


def _combined_child_updates(*, seed_data: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    updated_children = [
        str(value).strip().upper() for value in seed_data.get("updated_children", []) if str(value).strip()
    ]
    created_children = [
        str(value).strip().upper() for value in seed_data.get("created_children", []) if str(value).strip()
    ]
    combined: list[str] = []
    seen: set[str] = set()
    for issue_key in [*updated_children, *created_children]:
        if issue_key in seen:
            continue
        seen.add(issue_key)
        combined.append(issue_key)
    return updated_children, created_children, combined


def _sync_completion_note(*, updated_children: list[str], created_children: list[str]) -> str:
    parts = ["Parent feature sync complete after Jira edit."]
    if updated_children:
        parts.append(f"Refreshed engineering child tickets: {', '.join(updated_children)}.")
    if created_children:
        parts.append(f"Created engineering child tickets: {', '.join(created_children)}.")
    if not updated_children and not created_children:
        parts.append("No engineering child changes were required.")
    return " ".join(parts)


def _build_parent_seed_prompt(
    *,
    parent_detail: JiraIssueDetail,
) -> str:
    return (
        "Create or refresh the engineering child tickets needed to deliver this PM parent feature.\n\n"
        f"## Parent feature\n"
        f"- Key: {parent_detail.key}\n"
        f"- Summary: {parent_detail.summary}\n\n"
        f"Description:\n{parent_detail.description.strip() or 'No description provided.'}\n\n"
        "Instructions:\n"
        "- Keep the parent issue product-focused.\n"
        "- Create the technical engineering child tickets needed for implementation.\n"
        "- Provide concrete acceptance and how-to-test details for each child ticket.\n"
        "- Preserve the existing parent issue key.\n"
        "- Do not create duplicate child tickets.\n"
    )


def _resolve_parent_product_brief(
    *,
    session: Session,
    settings,  # noqa: ANN001
    tenant_id: str,
    project_id: str | None,
    parent_detail: JiraIssueDetail,
    build_runtime_for_selector_fn,
    refresh: bool = False,
) -> tuple[dict[str, object], list[object]]:
    canonical_brief = None if refresh else resolve_parent_feature_brief(
        session=session,
        tenant_id=tenant_id,
        parent_issue_key=parent_detail.key,
    )
    if canonical_brief is not None:
        return canonical_brief.to_payload(), []
    runtime = build_runtime_for_selector_fn(
        session=session,
        settings=settings,
        tenant_id=tenant_id,
        project_id=project_id,
        selector="workflow.pm_parent_brief_normalization",
    )
    normalization = normalize_parent_feature_brief_with_runtime(
        session=session,
        settings=settings,
        runtime=runtime,
        parent_issue_key=parent_detail.key,
        parent_summary=parent_detail.summary,
        parent_description=parent_detail.description,
        invocation_context=AgentInvocationContext(
            channel="jira",
            tenant_id=tenant_id,
            project_id=project_id,
            command="pm",
            stage="pm_parent_brief_normalization",
            working_dir=".",
            issue_key=parent_detail.key,
        ),
    )
    brief_payload = dict(normalization.get("brief") or {})
    open_questions = [value for value in normalization.get("open_questions", []) if _question_text(value)]
    persist_parent_feature_brief_snapshot(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        parent_issue_key=parent_detail.key,
        source_text=parent_detail.description,
        brief=brief_payload,
        status=PM_INTERVIEW_STATUS_QUESTION_PENDING if open_questions else PM_INTERVIEW_STATUS_PM_COMPLETED,
        notes={
            "source": "jira_parent_brief_normalization",
            "parent_summary": parent_detail.summary,
        },
    )
    return brief_payload, open_questions


def _rewrite_parent_issue_from_brief(
    *,
    oauth,
    parent_detail: JiraIssueDetail,
    brief_payload: dict[str, object],
    sync_status: str,
    planning_state: str | None = None,
    architecture_summary: list[str] | None = None,
    architecture_diagram: str | None = None,
    open_questions: list[object] | None = None,
) -> None:
    labels = list(parent_detail.labels or [])
    normalized_open_questions = [
        _question_text(value)
        for value in (open_questions if open_questions is not None else brief_payload.get("open_questions", []))
        if _question_text(value)
    ]
    description = build_parent_feature_description(
        objective=str(brief_payload.get("objective") or "").strip(),
        user_value=str(brief_payload.get("user_value") or "").strip(),
        recommendation=str(brief_payload.get("recommendation") or "").strip(),
        scope_in=[
            str(value).strip()
            for value in brief_payload.get("scope_in", [])
            if str(value).strip()
        ],
        scope_out=[
            str(value).strip()
            for value in brief_payload.get("scope_out", [])
            if str(value).strip()
        ],
        acceptance_criteria=[
            str(value).strip()
            for value in brief_payload.get("acceptance_criteria", [])
            if str(value).strip()
        ],
        ui_references=[
            str(value).strip()
            for value in brief_payload.get("ui_references", [])
            if str(value).strip()
        ],
        success_outcomes=[
            str(value).strip()
            for value in brief_payload.get("success_outcomes", [])
            if str(value).strip()
        ],
        dependencies_and_risks=[
            *[
                str(value).strip()
                for value in brief_payload.get("constraints", [])
                if str(value).strip()
            ],
            *[
                str(value).strip()
                for value in brief_payload.get("risks", [])
                if str(value).strip()
            ],
        ],
        open_questions=normalized_open_questions,
        parent_revision="normalized-parent-brief",
        sync_status=sync_status,
        pm_status="pm_completed",
        planning_state=planning_state,
        architecture_summary=architecture_summary,
        architecture_diagram=architecture_diagram,
    )
    if str(parent_detail.description or "") == description and labels == list(parent_detail.labels or []):
        return
    oauth.client.update_issue_fields(
        access_token=oauth.access_token,
        cloud_id=oauth.connection.cloud_id,
        issue_id_or_key=parent_detail.key,
        summary=parent_detail.summary,
        description=description,
        labels=labels,
    )


def _format_parent_brief_questions_for_discord(*, parent_issue_key: str, questions: list[object]) -> str:
    question_lines = "\n".join(f"- {_question_text(value)}" for value in questions if _question_text(value))
    return (
        f"Decision needed for `{parent_issue_key}` before I can finish backlog planning.\n"
        "Please reply in this thread with the missing product behavior:\n"
        f"{question_lines}"
    ).strip()


def _post_parent_brief_questions_to_discord(
    *,
    session: Session,
    settings,  # noqa: ANN001
    tenant,
    project_id: str | None,
    parent_issue_key: str,
    questions: list[object],
) -> bool:
    if not questions:
        return False
    parent_case = resolve_parent_feature_case(
        session=session,
        tenant_id=str(getattr(tenant, "tenant_id", "") or ""),
        parent_issue_key=parent_issue_key,
    )
    project = session.get(Project, project_id) if project_id else None
    project_discord_config = dict(getattr(project, "discord_config", None) or {})
    project_channel_id = str(project_discord_config.get("channel_id") or "").strip() or str(
        (getattr(tenant, "discord_config", None) or {}).get("channel_id") or ""
    ).strip()
    existing_request_id = str(getattr(parent_case, "request_id", "") or "").strip() or None
    existing_source_kind = str(getattr(parent_case, "source_kind", "") or "").strip()
    reusable_request_id = (
        existing_request_id
        if existing_request_id and existing_source_kind != PM_INTERVIEW_SOURCE_KIND_PARENT_BRIEF_SNAPSHOT
        else f"pm-parent-interview:{parent_issue_key}"
    )
    owner_user_id = str(getattr(parent_case, "owner_user_id", "") or "").strip() or None
    existing_root_channel_id = str(getattr(parent_case, "channel_id", "") or "").strip() or None
    existing_thread_channel_id = str(getattr(parent_case, "thread_channel_id", "") or "").strip() or None
    root_channel_id = existing_root_channel_id or project_channel_id or None
    if not root_channel_id:
        return False
    token_ref = PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF
    if not token_ref:
        return False
    bot_token = resolve_platform_secret_ref(
        session,
        secret_ref=token_ref,
        encryption_key=settings.secrets_encryption_key,
    )
    if not bot_token:
        return False
    mention_prefix = f"<@{owner_user_id}> " if owner_user_id else ""
    message = f"{mention_prefix}{_format_parent_brief_questions_for_discord(parent_issue_key=parent_issue_key, questions=questions)}".strip()
    try:
        client = DiscordApiClient(bot_token=bot_token)
        posted_root_message_id = None
        thread_channel_id = existing_thread_channel_id
        if thread_channel_id:
            client.post_message(channel_id=thread_channel_id, content=message)
        else:
            posted = client.post_message(
                channel_id=root_channel_id,
                content=f"Parent feature `{parent_issue_key}` needs PM clarification before backlog planning can continue.",
            )
            posted_root_message_id = str((posted or {}).get("id") or "").strip() or None
            if not posted_root_message_id:
                return False
            thread_name = f"{getattr(tenant, 'tenant_id', 'tenant')}-pm-{parent_issue_key}".replace(" ", "-")[:100]
            thread_channel_id = client.create_thread_from_message(
                channel_id=root_channel_id,
                message_id=posted_root_message_id,
                name=thread_name,
            )
            if project is not None:
                raw_thread_ids = project_discord_config.get("ask_thread_channel_ids")
                thread_ids = (
                    [str(value).strip() for value in raw_thread_ids if str(value).strip()]
                    if isinstance(raw_thread_ids, list)
                    else []
                )
                if thread_channel_id not in thread_ids:
                    thread_ids.append(thread_channel_id)
                ask_message_map = dict(project_discord_config.get("ask_thread_by_message_id") or {})
                ask_message_map[posted_root_message_id] = thread_channel_id
                project_discord_config["ask_thread_channel_ids"] = thread_ids[-200:]
                project_discord_config["ask_thread_by_message_id"] = dict(list(ask_message_map.items())[-500:])
                project.discord_config = project_discord_config
                project.updated_at = datetime.now(timezone.utc)
            client.post_message(
                channel_id=thread_channel_id,
                content=message,
            )
    except (DiscordApiError, ValueError):
        logger.warning(
            "jira_parent_brief_question_discord_post_failed tenant_id=%s parent_issue_key=%s channel_id=%s",
            getattr(tenant, "tenant_id", None),
            parent_issue_key,
            root_channel_id,
            exc_info=True,
        )
        return False
    interview_case = upsert_pm_interview_case(
        session=session,
        tenant_id=str(getattr(tenant, "tenant_id", "") or ""),
        project_id=project_id,
        request_id=reusable_request_id,
        source_kind="jira_parent",
        channel_id=root_channel_id,
        thread_channel_id=thread_channel_id if thread_channel_id != root_channel_id else None,
        root_message_id=posted_root_message_id or str(getattr(parent_case, "root_message_id", "") or "").strip() or None,
        owner_user_id=owner_user_id,
        parent_issue_key=parent_issue_key,
        source_text=str(getattr(parent_case, "source_text", "") or "") or f"Parent issue {parent_issue_key} requires PM clarification.",
        status=PM_INTERVIEW_STATUS_QUESTION_PENDING,
        brief=resolve_parent_feature_brief(
            session=session,
            tenant_id=str(getattr(tenant, "tenant_id", "") or ""),
            parent_issue_key=parent_issue_key,
            include_incomplete=True,
        ),
        notes={
            "source": "jira_parent_brief_normalization",
            "normalization_questions": list(questions),
        },
    )
    upsert_clarification_projection(
        session=session,
        spec=ClarificationProjectionSpec(
            tenant_id=str(getattr(tenant, "tenant_id", "") or ""),
            project_id=project_id,
            context_type=FOLLOWUP_CONTEXT_PM_INTERVIEW,
            issue_key=parent_issue_key,
            request_id=str(getattr(interview_case, "request_id", "") or "").strip() or None,
            channel_id=root_channel_id,
            thread_channel_id=thread_channel_id if thread_channel_id != root_channel_id else None,
            root_message_id=str(getattr(interview_case, "root_message_id", "") or "").strip() or None,
            owner_user_id=owner_user_id or None,
            origin_command="pm",
            questions=questions,
            metadata={
                "parent_issue_key": parent_issue_key,
                "questions": list(questions),
                "source": "jira_parent_brief_normalization",
            },
        ),
    )
    if project is not None:
        tenant.updated_at = datetime.now(timezone.utc)
    session.commit()
    return True


def _fanout_completion_note(
    *,
    target_status: str,
    promoted_children: list[str],
    unchanged_children: list[str],
    skipped_children: list[str],
    failed_children: list[str],
) -> str:
    parts = [f"Parent feature moved onto the board. Promoted engineering child tickets to {target_status}: {', '.join(promoted_children)}." if promoted_children else f"Parent feature moved onto the board. No engineering child tickets needed promotion to {target_status}."]
    if unchanged_children:
        parts.append(f"Already on board or terminal: {', '.join(unchanged_children)}.")
    if skipped_children:
        parts.append(f"Skipped non-engineering children: {', '.join(skipped_children)}.")
    if failed_children:
        parts.append(f"Failed to promote: {', '.join(failed_children)}.")
    return " ".join(parts)


def handle_parent_feature_sync(
    *,
    context: JiraParentChildSyncContext,
    session: Session,
    settings,  # noqa: ANN001
    integration_router,
    extract_changed_fields_fn,
    extract_status_transition_fn,
    build_workflow_runtime_fn,
    build_runtime_for_selector_fn,
    seed_issues_with_runtime_fn,
    post_jira_comment_fn,
    create_jira_comment_fn,
) -> JiraParentChildSyncResult:  # noqa: ANN001
    runtime = build_workflow_runtime_fn(
        session=session,
        settings=settings,
        process_claimed_run_fn=None,
        build_runner_fn=None,
        runtime_kwargs_fn=None,
        resolve_advance_handler_fn=build_workflow_advance_handler_resolver(
            integration_router=integration_router,
            extract_changed_fields_fn=extract_changed_fields_fn,
            extract_status_transition_fn=extract_status_transition_fn,
            build_runtime_for_selector_fn=build_runtime_for_selector_fn,
            seed_issues_with_runtime_fn=seed_issues_with_runtime_fn,
            post_jira_comment_fn=post_jira_comment_fn,
            create_jira_comment_fn=create_jira_comment_fn,
        ),
    )
    result = runtime.advance(
        request=WorkflowAdvanceRequest(
            workflow_handler_key="jira_parent_feature",
            tenant_id=context.tenant_id,
            tenant=context.tenant,
            project_id=context.project_id,
            issue_key=context.issue_key,
            issue_labels=tuple(context.issue_labels or []),
            payload={**dict(context.payload or {}), "request_id": context.request_id},
            webhook_event=context.webhook_event,
            comment_command=context.comment_command,
            comment_command_argument=context.comment_command_argument,
        )
    )
    return JiraParentChildSyncResult(
        handled=result.handled,
        reason=result.reason,
        extra=dict(result.extra or {}),
    )


def handle_engineering_clarification_command(
    *,
    context: JiraParentChildSyncContext,
    session: Session,
    settings,  # noqa: ANN001
    integration_router,
    build_runtime_for_selector_fn,
    classify_engineering_clarification_with_codex_fn,
    post_jira_comment_fn,
) -> JiraParentChildSyncResult:  # noqa: ANN001
    if context.comment_command != "clarify" or not context.project_id:
        return JiraParentChildSyncResult(handled=False)
    question = str(context.comment_command_argument or "").strip()
    if not question:
        return JiraParentChildSyncResult(handled=True, reason="invalid_comment_command")
    jira = _jira_adapter(
        integration_router=integration_router,
        session=session,
        tenant=context.tenant,
        settings=settings,
    )
    oauth = _jira_oauth_context(
        integration_router=integration_router,
        session=session,
        tenant=context.tenant,
        settings=settings,
    )
    child_detail = jira.get_issue_detail(issue_id_or_key=context.issue_key)
    child_labels = {str(label).strip().casefold() for label in child_detail.labels}
    if "engineering-child" not in child_labels:
        return JiraParentChildSyncResult(
            handled=True,
            reason="clarify_requires_engineering_child",
            extra={"webhook_event": context.webhook_event},
        )
    parent_issue_key = _extract_parent_issue_key(child_detail=child_detail)
    if not parent_issue_key:
        return JiraParentChildSyncResult(
            handled=True,
            reason="engineering_child_parent_missing",
            extra={"webhook_event": context.webhook_event},
        )
    parent_detail = jira.get_issue_detail(issue_id_or_key=parent_issue_key)
    runtime = build_runtime_for_selector_fn(
        session=session,
        settings=settings,
        tenant_id=context.tenant_id,
        project_id=context.project_id,
        selector="discord.pm_answer",
        agent_role="pm",
        agent_name="pm_primary",
    )
    try:
        translation = classify_engineering_clarification_with_codex_fn(
            runtime=runtime,
            parent_issue_key=parent_issue_key,
            parent_summary=parent_detail.summary,
            parent_description=parent_detail.description,
            child_issue_key=child_detail.key,
            child_summary=child_detail.summary,
            child_description=child_detail.description,
            question=question,
            invocation_context=AgentInvocationContext(
                channel="jira",
                tenant_id=context.tenant_id,
                project_id=context.project_id,
                command="clarify",
                stage="pm-translation",
                working_dir=".",
                issue_key=child_detail.key,
            ),
        )
    except CodexRuntimeError as exc:
        return JiraParentChildSyncResult(
            handled=True,
            reason="clarification_translation_failed",
            extra={"error": str(exc), "webhook_event": context.webhook_event},
        )
    classification = str(translation.get("classification") or "").strip().lower() or "technical_implementation"
    stakeholder_question = str(translation.get("stakeholder_question") or "").strip()
    child_block_note = str(translation.get("child_block_note") or "").strip()
    reason = str(translation.get("reason") or "").strip()
    if classification != "product_behavior" or not stakeholder_question:
        comment_text = _engineering_decision_note(
            question=question,
            owner="Engineering child team",
            approval_path="Child PR review and architecture review when boundaries or platform risk change",
            rationale=reason or child_block_note,
            status_line="Engineering owns this implementation decision. The parent PM brief does not reopen.",
        )
        _post_sync_note(
            session=session,
            tenant=context.tenant,
            issue_key=child_detail.key,
            settings=settings,
            body=comment_text,
            post_jira_comment_fn=post_jira_comment_fn,
        )
        return JiraParentChildSyncResult(
            handled=True,
            reason="clarification_not_product_behavior",
            extra={
                "classification": classification,
                "detail_reason": reason,
                "webhook_event": context.webhook_event,
            },
        )

    _update_issue_sync_label(
        oauth=oauth,
        issue_detail=child_detail,
        target_label="sync-blocked",
    )
    existing_context = resolve_issue_followup_context(
        session=session,
        tenant_id=context.tenant_id,
        issue_key=parent_issue_key,
        context_type=FOLLOWUP_CONTEXT_ENGINEERING_CLARIFICATION,
    )
    metadata = dict(getattr(existing_context, "metadata_json", {}) or {})
    existing_questions = metadata.get("questions")
    question_entries = list(existing_questions) if isinstance(existing_questions, list) else []
    question_entries.append(
        {
            "source_child_key": child_detail.key,
            "original_question": question,
            "stakeholder_question": stakeholder_question,
            "requested_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    clarification_questions = [
        {"question": str(item.get("stakeholder_question") or "").strip()}
        for item in question_entries
        if str(item.get("stakeholder_question") or "").strip()
    ]
    affected_child_keys = {
        *[str(value).strip().upper() for value in metadata.get("affected_child_keys", []) if str(value).strip()],
        child_detail.key.upper(),
    }
    projection = upsert_clarification_projection(
        session=session,
        spec=ClarificationProjectionSpec(
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            context_type=FOLLOWUP_CONTEXT_ENGINEERING_CLARIFICATION,
            issue_key=parent_issue_key,
            request_id=f"engineering-clarification:{parent_issue_key}",
            origin_command="clarify",
            questions=clarification_questions,
            metadata={
                **metadata,
                "parent_issue_key": parent_issue_key,
                "project_id": context.project_id,
                "project_key": _project_key_for_issue(parent_issue_key),
                "affected_child_keys": sorted(affected_child_keys),
                "questions": question_entries,
                "parent_updated": False,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        ),
    )
    session.commit()
    posted_to_discord = True
    if not projection.already_projected:
        _post_sync_note(
            session=session,
            tenant=context.tenant,
            issue_key=parent_issue_key,
            settings=settings,
            body=(
                f"Engineering needs a product clarification for child {child_detail.key}. "
                f"Reply on this parent issue with the decision: {stakeholder_question}"
            ),
            post_jira_comment_fn=post_jira_comment_fn,
        )
        posted_to_discord = _post_parent_brief_questions_to_discord(
            session=session,
            settings=settings,
            tenant=context.tenant,
            project_id=context.project_id,
            parent_issue_key=parent_issue_key,
            questions=[stakeholder_question],
        )
        if not posted_to_discord:
            _post_sync_note(
                session=session,
                tenant=context.tenant,
                issue_key=parent_issue_key,
                settings=settings,
                body=(
                    "Discord PM follow-up could not be created for this clarification. "
                    "Continue the decision on the parent Jira issue for now."
                ),
                post_jira_comment_fn=post_jira_comment_fn,
            )
    _post_sync_note(
        session=session,
        tenant=context.tenant,
        issue_key=child_detail.key,
        settings=settings,
        body=_engineering_decision_note(
            question=question,
            owner="Product via parent PM interview",
            approval_path=f"Parent feature {parent_issue_key} PM clarification thread",
            rationale=reason or child_block_note or stakeholder_question,
            status_line="Escalated to the parent PM thread because the answer changes product behavior or non-functional requirements.",
        ),
        post_jira_comment_fn=post_jira_comment_fn,
    )
    return JiraParentChildSyncResult(
        handled=True,
        reason="comment_command_clarify",
        extra={
            "classification": classification,
            "parent_issue_key": parent_issue_key,
            "affected_child_keys": sorted(affected_child_keys),
            "stakeholder_question": stakeholder_question,
            "webhook_event": context.webhook_event,
        },
    )


def handle_engineering_clarification_reply(
    *,
    context: JiraParentChildSyncContext,
    session: Session,
    settings,  # noqa: ANN001
    integration_router,
    seed_issues_with_runtime_fn,
    post_jira_comment_fn,
    create_jira_comment_fn,
    extract_jira_comment_text_fn,
) -> JiraParentChildSyncResult:  # noqa: ANN001
    if context.comment_command is not None:
        return JiraParentChildSyncResult(handled=False)
    if not context.project_id or context.webhook_event not in {"comment_created", "comment_updated"}:
        return JiraParentChildSyncResult(handled=False)
    followup_context = resolve_issue_followup_context(
        session=session,
        tenant_id=context.tenant_id,
        issue_key=context.issue_key,
        context_type=FOLLOWUP_CONTEXT_ENGINEERING_CLARIFICATION,
    )
    if followup_context is None:
        return JiraParentChildSyncResult(handled=False)
    comment = context.payload.get("comment")
    comment_body = ""
    if isinstance(comment, dict):
        body = comment.get("body")
        if isinstance(body, str):
            comment_body = body
    if not comment_body:
        comment_body = str(extract_jira_comment_text_fn(context.payload) or "")
    if not comment_body or is_system_generated_comment(text=comment_body):
        return JiraParentChildSyncResult(handled=False)

    metadata = dict(getattr(followup_context, "metadata_json", {}) or {})
    affected_child_keys = [
        str(value).strip().upper() for value in metadata.get("affected_child_keys", []) if str(value).strip()
    ]
    if not affected_child_keys:
        return JiraParentChildSyncResult(
            handled=True,
            reason="clarification_context_missing_children",
            extra={"webhook_event": context.webhook_event},
        )

    jira = _jira_adapter(
        integration_router=integration_router,
        session=session,
        tenant=context.tenant,
        settings=settings,
    )
    oauth = _jira_oauth_context(
        integration_router=integration_router,
        session=session,
        tenant=context.tenant,
        settings=settings,
    )
    parent_detail = jira.get_issue_detail(issue_id_or_key=context.issue_key)
    child_details = [
        jira.get_issue_detail(issue_id_or_key=child_key)
        for child_key in affected_child_keys
    ]
    prompt_markdown = _build_clarification_followup_prompt(
        parent_detail=parent_detail,
        child_details=child_details,
        metadata=metadata,
        reply_text=comment_body,
    )
    try:
        _, seed_data = seed_issues_with_runtime_fn(
            session=session,
            tenant=context.tenant,
            prompt_markdown=prompt_markdown,
            scoped_project_id=context.project_id,
            force_issue_keys=[context.issue_key, *affected_child_keys],
            allow_create=True,
            allow_empty_children=True,
            scoped_project_keys=[_project_key_for_issue(context.issue_key)],
            codex_working_dir=".",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "jira_engineering_clarification_reply_failed request_id=%s tenant_id=%s parent_issue_key=%s error=%s",
            context.request_id,
            context.tenant_id,
            context.issue_key,
            exc,
        )
        _mark_issues_sync_blocked(oauth=oauth, issue_keys=[context.issue_key, *affected_child_keys])
        _post_sync_note(
            session=session,
            tenant=context.tenant,
            issue_key=context.issue_key,
            settings=settings,
            body=f"Clarification reply was captured but parent/child refresh failed: {exc}",
            post_jira_comment_fn=post_jira_comment_fn,
        )
        return JiraParentChildSyncResult(
            handled=True,
            reason="engineering_clarification_refresh_failed",
            extra={"stale_child_keys": affected_child_keys, "webhook_event": context.webhook_event},
        )

    if bool(seed_data.get("requires_input")):
        questions = [value for value in seed_data.get("questions", []) if _question_text(value)]
        _mark_issues_sync_blocked(oauth=oauth, issue_keys=[context.issue_key, *affected_child_keys])
        metadata["parent_updated"] = bool(seed_data.get("updated_parent") or seed_data.get("created_parent"))
        metadata["updated_at"] = datetime.now(timezone.utc).isoformat()
        projection = upsert_clarification_projection(
            session=session,
            spec=ClarificationProjectionSpec(
                tenant_id=context.tenant_id,
                project_id=context.project_id,
                context_type=FOLLOWUP_CONTEXT_ENGINEERING_CLARIFICATION,
                issue_key=context.issue_key,
                request_id=f"engineering-clarification:{context.issue_key}",
                origin_command="clarify",
                questions=questions,
                metadata=metadata,
            ),
        )
        metadata = dict(projection.metadata)
        if not projection.already_projected:
            created_comment, error = _post_engineering_clarification_questions_to_jira(
                session=session,
                tenant=context.tenant,
                issue_key=context.issue_key,
                payload=dict(context.payload or {}),
                questions=questions,
                settings=settings,
                create_jira_comment_fn=create_jira_comment_fn,
            )
            if error is None:
                metadata["jira_comment_id"] = _extract_created_comment_id(created_comment)
                upsert_followup_context(
                    session=session,
                    tenant_id=context.tenant_id,
                    project_id=context.project_id,
                    context_type=FOLLOWUP_CONTEXT_ENGINEERING_CLARIFICATION,
                    origin_command="clarify",
                    issue_key=context.issue_key,
                    request_id=f"engineering-clarification:{context.issue_key}",
                    metadata=metadata,
                )
        session.commit()
        return JiraParentChildSyncResult(
            handled=True,
            reason="engineering_clarification_still_open",
            extra={
                "questions": questions,
                "stale_child_keys": affected_child_keys,
                "webhook_event": context.webhook_event,
            },
        )

    close_followup_contexts(
        session=session,
        tenant_id=context.tenant_id,
        context_type=FOLLOWUP_CONTEXT_ENGINEERING_CLARIFICATION,
        issue_key=context.issue_key,
    )
    session.commit()
    updated_children, created_children, changed_children = _combined_child_updates(seed_data=seed_data)
    _post_sync_note(
        session=session,
        tenant=context.tenant,
        issue_key=context.issue_key,
        settings=settings,
        body=(
            "Clarification reply applied to the parent feature and engineering child tickets are current again. "
            f"{'Refreshed children: ' + ', '.join(updated_children) + '.' if updated_children else ''} "
            f"{'Created children: ' + ', '.join(created_children) + '.' if created_children else ''} "
            f"{'No engineering child changes were required.' if not updated_children and not created_children else ''}"
        ).strip(),
        post_jira_comment_fn=post_jira_comment_fn,
    )
    for child_key in changed_children or affected_child_keys:
        _post_sync_note(
            session=session,
            tenant=context.tenant,
            issue_key=child_key,
            settings=settings,
            body=f"Product clarification from parent feature {context.issue_key} has been applied. Sync is current again.",
            post_jira_comment_fn=post_jira_comment_fn,
        )
    return JiraParentChildSyncResult(
        handled=True,
        reason="engineering_clarification_resolved",
        extra={
            "parent_issue_key": context.issue_key,
            "updated_children": changed_children,
            "parent_revision": seed_data.get("parent_revision"),
            "children_sync_status": seed_data.get("children_sync_status"),
            "webhook_event": context.webhook_event,
        },
    )


def handle_pm_interview_reply(
    *,
    context: JiraParentChildSyncContext,
    session: Session,
    settings,  # noqa: ANN001
    integration_router,
    build_runtime_for_selector_fn,
    seed_issues_with_runtime_fn,
    post_jira_comment_fn,
    create_jira_comment_fn,
    extract_jira_comment_text_fn,
    extract_jira_comment_id_fn,
) -> JiraParentChildSyncResult:  # noqa: ANN001
    if context.comment_command is not None:
        return JiraParentChildSyncResult(handled=False)
    if not context.project_id or context.webhook_event not in {"comment_created", "comment_updated"}:
        return JiraParentChildSyncResult(handled=False)

    comment_body = str(extract_jira_comment_text_fn(context.payload) or "").strip()
    if not comment_body or is_system_generated_comment(text=comment_body):
        return JiraParentChildSyncResult(handled=False)

    jira_followup = resolve_active_clarification_context(
        session=session,
        tenant_id=context.tenant_id,
        issue_key=context.issue_key,
        context_type=FOLLOWUP_CONTEXT_PM_INTERVIEW,
        transport=_PM_INTERVIEW_JIRA_TRANSPORT,
        reply_scope=_PM_INTERVIEW_JIRA_REPLY_SCOPE,
    )
    pm_request_id = None
    if jira_followup is not None:
        metadata = dict(getattr(jira_followup, "metadata_json", {}) or {})
        pm_request_id = str(metadata.get("pm_request_id") or "").strip() or None
    if not pm_request_id:
        legacy_case = resolve_parent_feature_case(
            session=session,
            tenant_id=context.tenant_id,
            parent_issue_key=context.issue_key,
        )
        if legacy_case is None or str(getattr(legacy_case, "status", "") or "").strip() != PM_INTERVIEW_STATUS_QUESTION_PENDING:
            return JiraParentChildSyncResult(handled=False)
        pm_request_id = str(getattr(legacy_case, "request_id", "") or "").strip() or None
    if not pm_request_id:
        return JiraParentChildSyncResult(handled=False)

    runtime = build_runtime_for_selector_fn(
        session=session,
        settings=settings,
        tenant_id=context.tenant_id,
        project_id=context.project_id,
        selector="discord.pm_answer",
        agent_role="pm",
        agent_name="pm_primary",
    )
    comment_id = extract_jira_comment_id_fn(context.payload)
    project_key = _project_key_for_issue(context.issue_key)
    try:
        followup_result = continue_pm_interview_from_followup(
            session=session,
            runtime=runtime,
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            request_id=pm_request_id,
            reply_text=comment_body,
            source_transport="jira_comment",
            source_ref=comment_id or context.delivery_id,
            actor_ref=str(context.payload.get("comment", {}).get("author", {}).get("accountId") or "").strip() or None,
            invocation_context=AgentInvocationContext(
                channel="jira",
                tenant_id=context.tenant_id,
                project_id=context.project_id,
                command="pm",
                stage="interview",
                working_dir=".",
                issue_key=context.issue_key,
            ),
            project_keys=[project_key],
            issues=[],
            status_counts={},
            settings=settings,
        )
    except CodexRuntimeError as exc:
        logger.warning(
            "jira_pm_interview_reply_retryable_failure request_id=%s tenant_id=%s issue_key=%s error=%s",
            context.request_id,
            context.tenant_id,
            context.issue_key,
            exc,
        )
        raise RetryableWebhookJobError(str(exc), retry_after_seconds=30) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "jira_pm_interview_reply_failed request_id=%s tenant_id=%s issue_key=%s error=%s",
            context.request_id,
            context.tenant_id,
            context.issue_key,
            exc,
        )
        return JiraParentChildSyncResult(
            handled=True,
            reason="pm_interview_reply_failed",
            extra={"error": str(exc), "webhook_event": context.webhook_event},
        )

    oauth = _jira_oauth_context(
        integration_router=integration_router,
        session=session,
        tenant=context.tenant,
        settings=settings,
    )
    parent_detail = _jira_adapter(
        integration_router=integration_router,
        session=session,
        tenant=context.tenant,
        settings=settings,
    ).get_issue_detail(issue_id_or_key=context.issue_key)
    existing_workflow = resolve_latest_issue_workflow(
        session=session,
        tenant_id=context.tenant_id,
        issue_key=parent_detail.key,
    )
    if existing_workflow is None:
        raise RuntimeError(f"No workflow execution exists for parent issue {parent_detail.key}")
    workflow_type = get_workflow_type(session, workflow_type_key=existing_workflow.workflow_type_key)
    workflow_projection = ensure_issue_workflow_execution(
        session=session,
        workflow_type=workflow_type,
        tenant_id=context.tenant_id,
        project_id=context.project_id,
        issue_key=parent_detail.key,
        issue_summary=parent_detail.summary,
        issue_description=parent_detail.description,
    )
    brief_payload = followup_result.assessment.brief.to_payload()
    next_questions: list[object] = []
    if followup_result.assessment.next_question is not None:
        next_questions.append(format_pm_interview_question(followup_result.assessment.next_question))
    if not next_questions:
        next_questions = [str(value).strip() for value in brief_payload.get("open_questions", []) if str(value).strip()]

    if not followup_result.assessment.ready_to_write:
        _update_issue_sync_label(
            oauth=oauth,
            issue_detail=parent_detail,
            target_label="sync-blocked",
        )
        workflow_projection.mark_waiting_for_input(
            operation_type="brief_normalization",
            summary="Parent brief still needs product clarification before planning can continue.",
        )
        error = None
        if not has_matching_active_clarification_state(
            session=session,
            tenant_id=context.tenant_id,
            issue_key=context.issue_key,
            context_type=FOLLOWUP_CONTEXT_PM_INTERVIEW,
            questions=next_questions,
            transport=_PM_INTERVIEW_JIRA_TRANSPORT,
            reply_scope=_PM_INTERVIEW_JIRA_REPLY_SCOPE,
        ):
            _post_parent_brief_questions_to_discord(
                session=session,
                settings=settings,
                tenant=context.tenant,
                project_id=context.project_id,
                parent_issue_key=context.issue_key,
                questions=next_questions,
            )
            _created_comment, error = _post_parent_brief_questions_to_jira(
                session=session,
                tenant=context.tenant,
                project_id=context.project_id,
                issue_key=context.issue_key,
                payload=dict(context.payload or {}),
                questions=next_questions,
                settings=settings,
                create_jira_comment_fn=create_jira_comment_fn,
            )
        session.commit()
        return JiraParentChildSyncResult(
            handled=True,
            reason="pm_interview_still_open",
            extra={
                "questions": next_questions,
                "comment_posted": error is None,
                "webhook_event": context.webhook_event,
            },
        )

    persist_parent_feature_brief_snapshot(
        session=session,
        tenant_id=context.tenant_id,
        project_id=context.project_id,
        parent_issue_key=context.issue_key,
        source_text=parent_detail.description or comment_body,
        brief=brief_payload,
        notes={"source": "jira_pm_interview_reply"},
        status=PM_INTERVIEW_STATUS_PM_COMPLETED,
    )
    _rewrite_parent_issue_from_brief(
        oauth=oauth,
        parent_detail=parent_detail,
        brief_payload=brief_payload,
        sync_status="children_syncing",
        planning_state="brief_normalized",
        open_questions=[],
    )
    workflow_projection.mark_operation_completed(
        operation_type="brief_normalization",
        summary="Parent brief normalized from PM clarification answers.",
    )
    workflow_projection.mark_operation_completed(
        operation_type="jira_parent_update",
        summary="Parent Jira issue synced with the confirmed brief.",
    )
    planner = _ParentBriefPlanner(
        session=session,
        settings=settings,
        context=context,
        build_runtime_for_selector_fn=build_runtime_for_selector_fn,
    )
    child_sync_gateway = _ParentChildSyncGateway(
        session=session,
        context=context,
        seed_issues_with_runtime_fn=seed_issues_with_runtime_fn,
    )
    try:
        planning_result, planning_package = planner.plan_backlog_parent(
            parent_detail=parent_detail,
            product_brief=brief_payload,
            project_key=project_key,
        )
        seed_data = child_sync_gateway.seed_parent_backlog_children(
            parent_detail=parent_detail,
            project_key=project_key,
            planning_package=planning_package,
            planning_state=planning_result.planning_state,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "jira_pm_interview_followup_seed_failed request_id=%s tenant_id=%s issue_key=%s error=%s",
            context.request_id,
            context.tenant_id,
            context.issue_key,
            exc,
        )
        category, retryable = classify_external_workflow_failure(error=exc)
        workflow_projection.mark_operation_failed(
            operation_type="jira_child_fanout",
            category=category,
            message=str(exc),
            retryable=retryable,
        )
        _update_issue_sync_label(
            oauth=oauth,
            issue_detail=parent_detail,
            target_label="sync-blocked",
        )
        _post_sync_note(
            session=session,
            tenant=context.tenant,
            issue_key=context.issue_key,
            settings=settings,
            body=f"PM clarification was recorded, but backlog planning failed: {exc}",
            post_jira_comment_fn=post_jira_comment_fn,
        )
        session.commit()
        return JiraParentChildSyncResult(
            handled=True,
            reason="pm_interview_followup_seed_failed",
            extra={"error": str(exc), "webhook_event": context.webhook_event},
        )

    if planning_result.planning_state != PLANNING_STATE_COMPLETED or bool(seed_data.get("requires_input")):
        workflow_projection.mark_waiting_for_input(
            operation_type="backlog_planning",
            summary="Backlog planning still needs clarification before child fanout can complete.",
        )
        _update_issue_sync_label(
            oauth=oauth,
            issue_detail=parent_detail,
            target_label="sync-blocked",
        )
        _post_sync_note(
            session=session,
            tenant=context.tenant,
            issue_key=context.issue_key,
            settings=settings,
            body=(
                "PM clarification was recorded, but backlog planning could not complete from the confirmed brief. "
                "Internal follow-up is required before engineering child tickets can be refreshed."
            ),
            post_jira_comment_fn=post_jira_comment_fn,
        )
        session.commit()
        return JiraParentChildSyncResult(
            handled=True,
            reason="pm_interview_followup_planning_blocked",
            extra={
                "parent_revision": seed_data.get("parent_revision"),
                "children_sync_status": seed_data.get("children_sync_status"),
                "webhook_event": context.webhook_event,
            },
        )

    mark_pm_interview_case_completed(
        session=session,
        tenant_id=context.tenant_id,
        request_id=pm_request_id,
        parent_issue_key=context.issue_key,
        notes={"source": "jira_pm_interview_reply"},
    )
    close_followup_contexts(
        session=session,
        tenant_id=context.tenant_id,
        context_type=FOLLOWUP_CONTEXT_PM_INTERVIEW,
        issue_key=context.issue_key,
    )
    workflow_projection.mark_operation_completed(
        operation_type="backlog_planning",
        summary="Backlog planning completed from the confirmed parent brief.",
    )
    workflow_projection.mark_operation_completed(
        operation_type="jira_child_fanout",
        summary="Engineering child tickets were created or refreshed from the confirmed brief.",
    )
    workflow_projection.mark_completed_if_ready()
    updated_children, created_children, changed_children = child_sync_gateway.combined_child_updates(seed_data=seed_data)
    _post_sync_note(
        session=session,
        tenant=context.tenant,
        issue_key=context.issue_key,
        settings=settings,
        body=(
            "Product clarification was applied and backlog planning is current again. "
            f"{child_sync_gateway.sync_completion_note(updated_children=updated_children, created_children=created_children)}"
        ),
        post_jira_comment_fn=post_jira_comment_fn,
    )
    for child_key in changed_children:
        _post_sync_note(
            session=session,
            tenant=context.tenant,
            issue_key=child_key,
            settings=settings,
            body=f"Updated from parent feature {context.issue_key} after PM clarification.",
            post_jira_comment_fn=post_jira_comment_fn,
        )
    session.commit()
    return JiraParentChildSyncResult(
        handled=True,
        reason="pm_interview_followup_resolved",
        extra={
            "updated_children": changed_children,
            "parent_revision": seed_data.get("parent_revision"),
            "children_sync_status": seed_data.get("children_sync_status"),
            "webhook_event": context.webhook_event,
        },
    )
