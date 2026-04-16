from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy.orm import Session

from orchestrator.core.specialist_planning import PLANNING_STATE_COMPLETED

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ParentFeaturePlanningWorkflowResult:
    handled: bool
    reason: str | None = None
    extra: dict[str, object] | None = None


@dataclass(frozen=True)
class ParentFeaturePlanningWorkflowDeps:
    issue_gateway: Any
    brief_planner: Any
    child_sync_gateway: Any
    project_key_for_issue_fn: Callable[[str], str]
    extract_changed_fields_fn: Callable[..., list[str]]
    extract_status_transition_fn: Callable[..., tuple[str | None, str | None]]
    material_parent_changed_fields_fn: Callable[..., list[str]]
    parent_board_entry_target_status_fn: Callable[..., str | None]


class ParentFeaturePlanningWorkflow:
    def __init__(self, *, deps: ParentFeaturePlanningWorkflowDeps) -> None:
        self._deps = deps

    def handle(
        self,
        *,
        context,
        session: Session,
        settings,  # noqa: ANN001
    ) -> ParentFeaturePlanningWorkflowResult:
        normalized_labels = {str(label).strip().casefold() for label in context.issue_labels or []}
        if context.webhook_event not in {"issue_created", "issue_updated"} or "pm-parent" not in normalized_labels:
            return ParentFeaturePlanningWorkflowResult(handled=False)
        routed_from_backlog = bool(context.payload.get("_mb_pm_parent_routed_from_backlog"))
        if context.webhook_event == "issue_created" or routed_from_backlog:
            return self._handle_issue_created(context=context, session=session, settings=settings)
        return self._handle_issue_updated(context=context, session=session, settings=settings)

    def _handle_issue_created(self, *, context, session: Session, settings) -> ParentFeaturePlanningWorkflowResult:  # noqa: ANN001
        issue_gateway = self._deps.issue_gateway
        brief_planner = self._deps.brief_planner
        child_sync_gateway = self._deps.child_sync_gateway
        parent_detail = issue_gateway.load_parent_detail(context.issue_key)
        project_key = self._deps.project_key_for_issue_fn(context.issue_key)
        product_brief, normalization_questions = brief_planner.resolve_product_brief(
            parent_detail=parent_detail,
            refresh=False,
        )
        issue_gateway.rewrite_parent_issue_from_brief(
            parent_detail=parent_detail,
            brief_payload=product_brief,
            sync_status="sync-blocked" if normalization_questions else "children_syncing",
            planning_state="brief_normalized",
            open_questions=normalization_questions or None,
        )
        if normalization_questions:
            return self._block_parent_brief(
                context=context,
                session=session,
                settings=settings,
                parent_detail=parent_detail,
                questions=normalization_questions,
                body_prefix="Parent feature was created in backlog, but brief normalization is blocked pending clarification.",
                reason="pm_parent_issue_created_brief_blocked",
            )

        planning_result, planning_package = brief_planner.plan_backlog_parent(
            parent_detail=parent_detail,
            product_brief=product_brief,
            project_key=project_key,
        )
        try:
            seed_data = child_sync_gateway.seed_parent_backlog_children(
                parent_detail=parent_detail,
                project_key=project_key,
                planning_package=planning_package,
                planning_state=planning_result.planning_state,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "jira_parent_issue_created_seed_failed request_id=%s tenant_id=%s issue_key=%s error=%s",
                context.request_id,
                context.tenant_id,
                context.issue_key,
                exc,
            )
            issue_gateway.update_issue_sync_label(
                issue_detail=parent_detail,
                target_label="sync-blocked",
            )
            issue_gateway.post_sync_note(
                issue_key=context.issue_key,
                body=f"Parent feature was created in backlog, but engineering child planning failed. Error: {exc}",
            )
            return ParentFeaturePlanningWorkflowResult(
                handled=True,
                reason="pm_parent_issue_created_seed_failed",
                extra={"webhook_event": context.webhook_event},
            )

        updated_children, created_children, changed_children = child_sync_gateway.combined_child_updates(seed_data=seed_data)
        if planning_result.planning_state != PLANNING_STATE_COMPLETED or bool(seed_data.get("requires_input")):
            questions = list(planning_result.open_behavior_questions) or [
                str(value).strip() for value in seed_data.get("questions", []) if str(value).strip()
            ]
            question_block = " ".join(questions) if questions else "More product detail is required."
            issue_gateway.update_issue_sync_label(
                issue_detail=parent_detail,
                target_label="sync-blocked",
            )
            issue_gateway.post_sync_note(
                issue_key=context.issue_key,
                body=(
                    "Parent feature was created in backlog, but engineering child planning is blocked pending clarification. "
                    f"{question_block}"
                ),
            )
            return ParentFeaturePlanningWorkflowResult(
                handled=True,
                reason="pm_parent_issue_created_seed_blocked",
                extra={
                    "questions": questions,
                    "parent_revision": seed_data.get("parent_revision"),
                    "children_sync_status": seed_data.get("children_sync_status"),
                    "webhook_event": context.webhook_event,
                },
            )

        issue_gateway.post_sync_note(
            issue_key=context.issue_key,
            body=(
                "Backlog parent feature planning complete. "
                f"{child_sync_gateway.sync_completion_note(updated_children=updated_children, created_children=created_children)}"
            ),
        )
        for child_key in changed_children:
            issue_gateway.post_sync_note(
                issue_key=child_key,
                body=f"Created or refreshed from parent feature {context.issue_key} during backlog planning.",
            )
        return ParentFeaturePlanningWorkflowResult(
            handled=True,
            reason="pm_parent_issue_created_seed_completed",
            extra={
                "updated_children": changed_children,
                "parent_revision": seed_data.get("parent_revision"),
                "children_sync_status": seed_data.get("children_sync_status"),
                "webhook_event": context.webhook_event,
            },
        )

    def _handle_issue_updated(self, *, context, session: Session, settings) -> ParentFeaturePlanningWorkflowResult:  # noqa: ANN001
        issue_gateway = self._deps.issue_gateway
        brief_planner = self._deps.brief_planner
        child_sync_gateway = self._deps.child_sync_gateway
        material_changed_fields = self._deps.material_parent_changed_fields_fn(
            payload=context.payload,
            extract_changed_fields_fn=self._deps.extract_changed_fields_fn,
        )
        if not material_changed_fields:
            board_entry_target_status = self._deps.parent_board_entry_target_status_fn(
                payload=context.payload,
                extract_status_transition_fn=self._deps.extract_status_transition_fn,
            )
            if board_entry_target_status:
                return self._handle_board_entry(
                    context=context,
                    target_status=board_entry_target_status,
                )
            return ParentFeaturePlanningWorkflowResult(
                handled=True,
                reason="pm_parent_non_material_change",
                extra={"changed_fields": [], "webhook_event": context.webhook_event},
            )

        parent_detail = issue_gateway.load_parent_detail(context.issue_key)
        project_key = self._deps.project_key_for_issue_fn(context.issue_key)
        child_details = issue_gateway.load_child_details(
            project_key=project_key,
            parent_issue_key=context.issue_key,
        )
        product_brief, normalization_questions = brief_planner.resolve_product_brief(
            parent_detail=parent_detail,
            refresh=True,
        )
        issue_gateway.rewrite_parent_issue_from_brief(
            parent_detail=parent_detail,
            brief_payload=product_brief,
            sync_status="sync-blocked" if normalization_questions else "children_syncing",
            planning_state="brief_normalized",
            open_questions=normalization_questions or None,
        )
        if normalization_questions:
            blocked_issue_keys = [context.issue_key, *[detail.key for detail in child_details]]
            issue_gateway.mark_issues_sync_blocked(issue_keys=blocked_issue_keys)
            return self._block_parent_brief(
                context=context,
                session=session,
                settings=settings,
                parent_detail=parent_detail,
                questions=normalization_questions,
                body_prefix="Parent feature changed but brief normalization is blocked pending clarification.",
                reason="pm_parent_sync_brief_blocked",
                extra={"changed_fields": material_changed_fields},
            )

        if not child_details:
            issue_gateway.post_sync_note(
                issue_key=context.issue_key,
                body="Parent feature changed, but there are no engineering child tickets to refresh.",
            )
            return ParentFeaturePlanningWorkflowResult(
                handled=True,
                reason="pm_parent_no_children",
                extra={"changed_fields": material_changed_fields, "webhook_event": context.webhook_event},
            )

        try:
            seed_data = child_sync_gateway.refresh_parent_children(
                parent_detail=parent_detail,
                child_details=child_details,
                changed_fields=material_changed_fields,
                project_key=project_key,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "jira_parent_sync_failed request_id=%s tenant_id=%s issue_key=%s error=%s",
                context.request_id,
                context.tenant_id,
                context.issue_key,
                exc,
            )
            blocked_issue_keys = [context.issue_key, *[detail.key for detail in child_details]]
            issue_gateway.mark_issues_sync_blocked(issue_keys=blocked_issue_keys)
            issue_gateway.post_sync_note(
                issue_key=context.issue_key,
                body=(
                    "Parent feature changed and child refresh failed. "
                    f"Blocked {len(child_details)} engineering child ticket(s). Error: {exc}"
                ),
            )
            for detail in child_details:
                issue_gateway.post_sync_note(
                    issue_key=detail.key,
                    body=f"Blocked because parent feature {context.issue_key} changed and refresh failed.",
                )
            return ParentFeaturePlanningWorkflowResult(
                handled=True,
                reason="pm_parent_sync_failed",
                extra={
                    "changed_fields": material_changed_fields,
                    "stale_child_keys": [detail.key for detail in child_details],
                    "webhook_event": context.webhook_event,
                },
            )

        updated_children, created_children, changed_children = child_sync_gateway.combined_child_updates(seed_data=seed_data)
        if bool(seed_data.get("requires_input")):
            blocked_issue_keys = [context.issue_key, *[detail.key for detail in child_details]]
            issue_gateway.mark_issues_sync_blocked(issue_keys=blocked_issue_keys)
            questions = [str(value).strip() for value in seed_data.get("questions", []) if str(value).strip()]
            question_block = " ".join(questions) if questions else "More product detail is required."
            issue_gateway.post_sync_note(
                issue_key=context.issue_key,
                body=f"Parent feature changed but child sync is blocked pending clarification. {question_block}",
            )
            for detail in child_details:
                issue_gateway.post_sync_note(
                    issue_key=detail.key,
                    body=f"Still blocked because parent feature {context.issue_key} needs clarification before refresh can complete.",
                )
            return ParentFeaturePlanningWorkflowResult(
                handled=True,
                reason="pm_parent_sync_blocked",
                extra={
                    "changed_fields": material_changed_fields,
                    "stale_child_keys": [detail.key for detail in child_details],
                    "questions": questions,
                    "webhook_event": context.webhook_event,
                },
            )

        issue_gateway.post_sync_note(
            issue_key=context.issue_key,
            body=(
                f"{child_sync_gateway.sync_completion_note(updated_children=updated_children, created_children=created_children)} "
                f"Changed fields: {', '.join(material_changed_fields)}."
            ),
        )
        for child_key in changed_children:
            issue_gateway.post_sync_note(
                issue_key=child_key,
                body=f"Refreshed from parent feature {context.issue_key} after Jira product update.",
            )
        return ParentFeaturePlanningWorkflowResult(
            handled=True,
            reason="pm_parent_sync_completed",
            extra={
                "changed_fields": material_changed_fields,
                "updated_children": changed_children,
                "parent_revision": seed_data.get("parent_revision"),
                "children_sync_status": seed_data.get("children_sync_status"),
                "webhook_event": context.webhook_event,
            },
        )

    def _handle_board_entry(
        self,
        *,
        context,
        target_status: str,
    ) -> ParentFeaturePlanningWorkflowResult:
        issue_gateway = self._deps.issue_gateway
        project_key = self._deps.project_key_for_issue_fn(context.issue_key)
        child_details = issue_gateway.load_child_details(project_key=project_key, parent_issue_key=context.issue_key)
        if not child_details:
            issue_gateway.post_sync_note(
                issue_key=context.issue_key,
                body="Parent feature moved onto the board, but there are no engineering child tickets to promote.",
            )
            return ParentFeaturePlanningWorkflowResult(
                handled=True,
                reason="pm_parent_board_entry_no_children",
                extra={"target_status": target_status, "webhook_event": context.webhook_event},
            )

        promoted_children: list[str] = []
        unchanged_children: list[str] = []
        skipped_children: list[str] = []
        failed_children: list[str] = []
        for child_detail in child_details:
            child_labels = {str(label).strip().casefold() for label in child_detail.labels}
            if "engineering-child" not in child_labels:
                skipped_children.append(child_detail.key)
                continue
            if str(child_detail.status or "").strip().casefold() in {"to do", "ready for agent", "in progress", "testing", "done"}:
                unchanged_children.append(child_detail.key)
                continue
            try:
                issue_gateway.transition_issue(issue_key=child_detail.key, target_status=target_status)
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "jira_parent_board_entry_child_transition_failed request_id=%s tenant_id=%s parent_issue_key=%s child_issue_key=%s target_status=%s error=%s",
                    context.request_id,
                    context.tenant_id,
                    context.issue_key,
                    child_detail.key,
                    target_status,
                    exc,
                )
                failed_children.append(child_detail.key)
                continue
            promoted_children.append(child_detail.key)

        issue_gateway.post_sync_note(
            issue_key=context.issue_key,
            body=self._deps.child_sync_gateway.fanout_completion_note(
                target_status=target_status,
                promoted_children=promoted_children,
                unchanged_children=unchanged_children,
                skipped_children=skipped_children,
                failed_children=failed_children,
            ),
        )
        return ParentFeaturePlanningWorkflowResult(
            handled=True,
            reason="pm_parent_board_entry_fanout_completed" if not failed_children else "pm_parent_board_entry_fanout_partial",
            extra={
                "target_status": target_status,
                "promoted_children": promoted_children,
                "unchanged_children": unchanged_children,
                "skipped_children": skipped_children,
                "failed_children": failed_children,
                "webhook_event": context.webhook_event,
            },
        )

    def _block_parent_brief(
        self,
        *,
        context,
        session: Session,
        settings,  # noqa: ANN001
        parent_detail,
        questions: list[str],
        body_prefix: str,
        reason: str,
        extra: dict[str, object] | None = None,
    ) -> ParentFeaturePlanningWorkflowResult:
        _ = (session, settings)
        issue_gateway = self._deps.issue_gateway
        issue_gateway.update_issue_sync_label(
            issue_detail=parent_detail,
            target_label="sync-blocked",
        )
        posted_to_discord = issue_gateway.post_parent_brief_questions(
            parent_issue_key=parent_detail.key,
            questions=questions,
        )
        issue_gateway.post_parent_brief_questions_jira(
            parent_issue_key=parent_detail.key,
            questions=questions,
            discord_failed=not posted_to_discord,
        )
        payload = dict(extra or {})
        payload.update({"questions": questions, "webhook_event": context.webhook_event})
        return ParentFeaturePlanningWorkflowResult(
            handled=True,
            reason=reason,
            extra=payload,
        )
