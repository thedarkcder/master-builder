from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy.orm import Session

from orchestrator.core.clarification_questions import ClarificationQuestion, ClarificationQuestionSet
from orchestrator.core.parent_planning_clarification_service import (
    ParentPlanningClarificationService,
)
from orchestrator.core.parent_planning_fanout_service import ParentPlanningFanoutSeedError, ParentPlanningFanoutService
from orchestrator.core.specialist_planning import PLANNING_STATE_COMPLETED
from orchestrator.core.workflow_runtime import WorkflowAdvanceOutcome, WorkflowTransitionPlanner
from orchestrator.core.workflow_execution_projection import classify_external_workflow_failure

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ParentFeaturePlanningWorkflowDeps:
    issue_gateway: Any
    brief_planner: Any
    child_sync_gateway: Any
    clarification_service: ParentPlanningClarificationService
    fanout_service: ParentPlanningFanoutService
    workflow_type: Any
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
    ) -> WorkflowAdvanceOutcome:
        lifecycle = WorkflowTransitionPlanner()
        if bool(context.payload.get("_mb_pm_interview_followup")):
            return self._handle_pm_interview_followup(
                context=context,
                session=session,
                settings=settings,
                lifecycle=lifecycle,
            )
        normalized_labels = {str(label).strip().casefold() for label in context.issue_labels or []}
        if context.webhook_event not in {"issue_created", "issue_updated"} or "pm-parent" not in normalized_labels:
            return lifecycle.build_outcome(handled=False)
        routed_from_backlog = bool(context.payload.get("_mb_pm_parent_routed_from_backlog"))
        if context.webhook_event == "issue_created" or routed_from_backlog:
            return self._handle_issue_created(context=context, session=session, settings=settings, lifecycle=lifecycle)
        return self._handle_issue_updated(context=context, session=session, settings=settings, lifecycle=lifecycle)

    def _handle_issue_created(self, *, context, session: Session, settings, lifecycle) -> WorkflowAdvanceOutcome:  # noqa: ANN001
        issue_gateway = self._deps.issue_gateway
        brief_planner = self._deps.brief_planner
        parent_detail = issue_gateway.load_parent_detail(context.issue_key)
        lifecycle.ensure_issue_execution(
            issue_summary=parent_detail.summary,
            issue_description=parent_detail.description,
        )
        project_key = self._deps.project_key_for_issue_fn(context.issue_key)
        product_brief, normalization_questions = brief_planner.resolve_product_brief(
            parent_detail=parent_detail,
            refresh=False,
        )
        normalization_questions = ClarificationQuestionSet.from_values(normalization_questions).questions
        architecture_gate = issue_gateway.resolve_architecture_gate(
            parent_issue_key=context.issue_key,
            issue_summary=parent_detail.summary,
            issue_labels=list(parent_detail.labels or []),
        )
        self._sync_parent_issue_references(
            issue_gateway=issue_gateway,
            parent_detail=parent_detail,
            architecture_gate=architecture_gate,
        )
        if normalization_questions:
            self._mark_parent_synced(lifecycle=lifecycle, draft=True)
            return self._block_parent_brief(
                context=context,
                session=session,
                settings=settings,
                parent_detail=parent_detail,
                lifecycle=lifecycle,
                questions=normalization_questions,
                body_prefix="Parent feature was created in backlog, but brief normalization is blocked pending clarification.",
                reason="pm_parent_issue_created_brief_blocked",
                waiting_operation_type="brief_normalization",
            )
        if architecture_gate.required and not architecture_gate.ready:
            self._mark_parent_synced(lifecycle=lifecycle, draft=True)
            return self._block_on_architecture(
                context=context,
                issue_gateway=issue_gateway,
                parent_detail=parent_detail,
                lifecycle=lifecycle,
                reason="pm_parent_issue_created_architecture_blocked",
                extra={
                    "architecture_document_title": architecture_gate.document.title if architecture_gate.document else None,
                    "architecture_document_url": (
                        str(architecture_gate.document.canonical_url or "").strip()
                        if architecture_gate.document is not None
                        else None
                    ),
                },
            )
        self._mark_brief_normalized(lifecycle=lifecycle, source="source issue")
        try:
            fanout = self._deps.fanout_service.plan_and_seed(
                parent_detail=parent_detail,
                product_brief=product_brief,
                project_key=project_key,
                planner=brief_planner,
                child_sync_gateway=self._deps.child_sync_gateway,
            )
        except ParentPlanningFanoutSeedError as exc:
            self._mark_planning_completed_if_completed(
                lifecycle=lifecycle,
                planning_result=exc.planning_result,
                planning_summary="Backlog planning completed from the normalized parent brief.",
            )
            return self._handle_fanout_failure(
                context=context,
                issue_gateway=issue_gateway,
                lifecycle=lifecycle,
                error=exc.error,
                failure_reason="pm_parent_issue_created_seed_failed",
            )
        except Exception as exc:  # noqa: BLE001
            return self._handle_fanout_failure(
                context=context,
                issue_gateway=issue_gateway,
                lifecycle=lifecycle,
                error=exc,
                failure_reason="pm_parent_issue_created_seed_failed",
            )
        self._mark_planning_completed_if_completed(
            lifecycle=lifecycle,
            planning_result=fanout.planning_result,
            planning_summary="Backlog planning completed from the normalized parent brief.",
        )

        if not fanout.completed:
            return self._block_parent_brief(
                context=context,
                session=session,
                settings=settings,
                parent_detail=parent_detail,
                lifecycle=lifecycle,
                questions=fanout.questions,
                body_prefix=(
                    "Parent feature was created in backlog, but engineering child planning is blocked pending clarification."
                ),
                reason="pm_parent_issue_created_seed_blocked",
                waiting_operation_type="backlog_planning",
                extra={
                    "parent_revision": fanout.seed_data.get("parent_revision"),
                    "children_sync_status": fanout.seed_data.get("children_sync_status"),
                },
            )
        self._mark_fanout_completed(
            lifecycle=lifecycle,
            fanout_summary="Engineering child tickets were created or refreshed from the parent planning package.",
        )

        return lifecycle.build_outcome(
            handled=True,
            reason="pm_parent_issue_created_seed_completed",
            extra={
                "updated_children": fanout.changed_children,
                "parent_revision": fanout.seed_data.get("parent_revision"),
                "children_sync_status": fanout.seed_data.get("children_sync_status"),
                "webhook_event": context.webhook_event,
            },
        )

    def _handle_issue_updated(self, *, context, session: Session, settings, lifecycle) -> WorkflowAdvanceOutcome:  # noqa: ANN001
        issue_gateway = self._deps.issue_gateway
        brief_planner = self._deps.brief_planner
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
                    session=session,
                    target_status=board_entry_target_status,
                    lifecycle=lifecycle,
                )
            return lifecycle.build_outcome(
                handled=True,
                reason="pm_parent_non_material_change",
                extra={"changed_fields": [], "webhook_event": context.webhook_event},
            )

        parent_detail = issue_gateway.load_parent_detail(context.issue_key)
        lifecycle.ensure_issue_execution(
            issue_summary=parent_detail.summary,
            issue_description=parent_detail.description,
        )
        project_key = self._deps.project_key_for_issue_fn(context.issue_key)
        child_details = issue_gateway.load_child_details(
            project_key=project_key,
            parent_issue_key=context.issue_key,
        )
        product_brief, normalization_questions = brief_planner.resolve_product_brief(
            parent_detail=parent_detail,
            refresh=True,
        )
        normalization_questions = ClarificationQuestionSet.from_values(normalization_questions).questions
        architecture_gate = issue_gateway.resolve_architecture_gate(
            parent_issue_key=context.issue_key,
            issue_summary=parent_detail.summary,
            issue_labels=list(parent_detail.labels or []),
        )
        self._sync_parent_issue_references(
            issue_gateway=issue_gateway,
            parent_detail=parent_detail,
            architecture_gate=architecture_gate,
        )
        if normalization_questions:
            self._mark_parent_synced(lifecycle=lifecycle, draft=True)
            blocked_issue_keys = [context.issue_key, *[detail.key for detail in child_details]]
            issue_gateway.mark_issues_sync_blocked(issue_keys=blocked_issue_keys)
            return self._block_parent_brief(
                context=context,
                session=session,
                settings=settings,
                parent_detail=parent_detail,
                lifecycle=lifecycle,
                questions=normalization_questions,
                body_prefix="Parent feature changed but brief normalization is blocked pending clarification.",
                reason="pm_parent_sync_brief_blocked",
                waiting_operation_type="brief_normalization",
                extra={"changed_fields": material_changed_fields},
            )
        if architecture_gate.required and not architecture_gate.ready:
            self._mark_parent_synced(lifecycle=lifecycle, draft=True)
            blocked_issue_keys = [context.issue_key, *[detail.key for detail in child_details]]
            issue_gateway.mark_issues_sync_blocked(issue_keys=blocked_issue_keys)
            return self._block_on_architecture(
                context=context,
                issue_gateway=issue_gateway,
                parent_detail=parent_detail,
                lifecycle=lifecycle,
                reason="pm_parent_sync_architecture_blocked",
                extra={
                    "changed_fields": material_changed_fields,
                    "architecture_document_title": architecture_gate.document.title if architecture_gate.document else None,
                    "architecture_document_url": (
                        str(architecture_gate.document.canonical_url or "").strip()
                        if architecture_gate.document is not None
                        else None
                    ),
                },
            )
        self._mark_brief_normalized(lifecycle=lifecycle, source="source issue")

        if not child_details:
            lifecycle.mark_completed_if_ready()
            return lifecycle.build_outcome(
                handled=True,
                reason="pm_parent_no_children",
                extra={"changed_fields": material_changed_fields, "webhook_event": context.webhook_event},
            )

        try:
            seed_data = self._deps.child_sync_gateway.refresh_parent_children(
                parent_detail=parent_detail,
                child_details=child_details,
                changed_fields=material_changed_fields,
                project_key=project_key,
            )
        except Exception as exc:  # noqa: BLE001
            category = classify_external_workflow_failure(error=exc)
            lifecycle.mark_operation_failed(
                operation_type="jira_child_fanout",
                category=category,
                message=str(exc),
            )
            blocked_issue_keys = [context.issue_key, *[detail.key for detail in child_details]]
            issue_gateway.mark_issues_sync_blocked(issue_keys=blocked_issue_keys)
            return lifecycle.build_outcome(
                handled=True,
                reason="pm_parent_sync_failed",
                extra={
                    "changed_fields": material_changed_fields,
                    "stale_child_keys": [detail.key for detail in child_details],
                    "webhook_event": context.webhook_event,
                },
            )

        updated_children, created_children, changed_children = self._deps.child_sync_gateway.combined_child_updates(seed_data=seed_data)
        if bool(seed_data.get("requires_input")):
            blocked_issue_keys = [context.issue_key, *[detail.key for detail in child_details]]
            issue_gateway.mark_issues_sync_blocked(issue_keys=blocked_issue_keys)
            questions = ClarificationQuestionSet.from_values(seed_data.get("questions", [])).questions
            lifecycle.mark_waiting_for_input(
                operation_type="backlog_planning",
                summary="Parent planning is waiting for product clarification before child refresh can complete.",
            )
            if not issue_gateway.has_matching_active_pm_clarification_state(
                parent_issue_key=context.issue_key,
                questions=questions,
            ):
                posted_to_discord = issue_gateway.post_parent_brief_questions(
                    parent_issue_key=context.issue_key,
                    questions=questions,
                )
                created_comment, error = issue_gateway.post_parent_brief_questions_jira(
                    parent_issue_key=context.issue_key,
                    questions=questions,
                )
                if posted_to_discord:
                    lifecycle.set_operation_completed(
                        operation_type="discord_followup_projection",
                        summary="Posted PM clarification follow-up to Discord.",
                    )
                if error is None and created_comment is not None:
                    lifecycle.set_operation_completed(
                        operation_type="jira_comment_projection",
                        summary="Posted PM clarification questions to Jira.",
                    )
            return lifecycle.build_outcome(
                handled=True,
                reason="pm_parent_sync_blocked",
                extra={
                    "changed_fields": material_changed_fields,
                    "stale_child_keys": [detail.key for detail in child_details],
                    "questions": ClarificationQuestionSet(questions=questions).to_payload(),
                    "webhook_event": context.webhook_event,
                },
            )
        self._mark_planning_completed(
            lifecycle=lifecycle,
            planning_summary="Backlog planning completed from the normalized parent brief.",
        )
        self._mark_fanout_completed(
            lifecycle=lifecycle,
            fanout_summary="Engineering child tickets were refreshed from the parent planning package.",
        )

        return lifecycle.build_outcome(
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
        session: Session,
        target_status: str,
        lifecycle,
    ) -> WorkflowAdvanceOutcome:
        issue_gateway = self._deps.issue_gateway
        parent_detail = issue_gateway.load_parent_detail(context.issue_key)
        lifecycle.ensure_issue_execution(
            issue_summary=parent_detail.summary,
            issue_description=parent_detail.description,
        )
        project_key = self._deps.project_key_for_issue_fn(context.issue_key)
        child_details = issue_gateway.load_child_details(project_key=project_key, parent_issue_key=context.issue_key)
        if not child_details:
            lifecycle.mark_operation_completed(
                operation_type="jira_child_promotion",
                summary=f"No engineering child tickets required promotion to {target_status}.",
            )
            lifecycle.mark_completed_if_ready()
            return lifecycle.build_outcome(
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

        if failed_children:
            lifecycle.mark_operation_failed(
                operation_type="jira_child_promotion",
                category="external_failure",
                message=(
                    f"Failed to promote engineering child tickets to {target_status}: "
                    f"{', '.join(failed_children)}"
                ),
            )
        else:
            lifecycle.mark_operation_completed(
                operation_type="jira_child_promotion",
                summary=(
                    f"Promoted engineering child tickets to {target_status}: "
                    f"{', '.join(promoted_children)}."
                    if promoted_children
                    else f"No engineering child tickets required promotion to {target_status}."
                ),
            )
            lifecycle.mark_completed_if_ready()

        return lifecycle.build_outcome(
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
        lifecycle,
        questions: tuple[ClarificationQuestion, ...],
        body_prefix: str,
        reason: str,
        waiting_operation_type: str,
        extra: dict[str, object] | None = None,
    ) -> WorkflowAdvanceOutcome:
        _ = (session, settings, body_prefix)
        question_set = ClarificationQuestionSet(questions=questions)
        issue_gateway = self._deps.issue_gateway
        issue_gateway.update_issue_sync_label(
            issue_detail=parent_detail,
            target_label="sync-blocked",
        )
        publication = self._deps.clarification_service.ensure_active_clarification(
            issue_key=parent_detail.key,
            questions=question_set.questions,
            publisher=issue_gateway,
        )
        if publication.discord_followup_created:
            lifecycle.set_operation_completed(
                operation_type="discord_followup_projection",
                summary="Posted PM clarification follow-up to Discord.",
            )
        if publication.jira_comment_created:
            lifecycle.set_operation_completed(
                operation_type="jira_comment_projection",
                summary="Posted PM clarification questions to Jira.",
            )
        if waiting_operation_type == "brief_normalization":
            lifecycle.set_operation_waiting_for_input(
                operation_type=waiting_operation_type,
                summary="Parent planning is waiting for product clarification.",
            )
        else:
            lifecycle.mark_waiting_for_input(
                operation_type=waiting_operation_type,
                summary="Parent planning is waiting for product clarification.",
            )
        payload = dict(extra or {})
        payload.update(
            {
                "questions": question_set.to_payload(),
                "webhook_event": context.webhook_event,
            }
        )
        return lifecycle.build_outcome(
            handled=True,
            reason=reason,
            extra=payload,
        )

    def _block_on_architecture(
        self,
        *,
        context,
        issue_gateway,
        parent_detail,
        lifecycle,
        reason: str,
        extra: dict[str, object] | None = None,
    ) -> WorkflowAdvanceOutcome:
        issue_gateway.update_issue_sync_label(
            issue_detail=parent_detail,
            target_label="sync-blocked",
        )
        lifecycle.mark_waiting_for_input(
            operation_type="backlog_planning",
            summary="Parent planning is waiting for the architecture document to be marked ready.",
        )
        payload = dict(extra or {})
        payload["webhook_event"] = context.webhook_event
        return lifecycle.build_outcome(
            handled=True,
            reason=reason,
            extra=payload,
        )

    def _handle_pm_interview_followup(
        self,
        *,
        context,
        session: Session,
        settings,  # noqa: ANN001
        lifecycle,
    ) -> WorkflowAdvanceOutcome:
        issue_gateway = self._deps.issue_gateway
        parent_detail = issue_gateway.load_parent_detail(context.issue_key)
        lifecycle.ensure_issue_execution(
            issue_summary=parent_detail.summary,
            issue_description=parent_detail.description,
        )
        project_key = self._deps.project_key_for_issue_fn(context.issue_key)
        brief_payload = dict(context.payload.get("brief_payload") or {})
        next_questions = ClarificationQuestionSet.from_values(context.payload.get("next_questions", [])).questions
        next_question_set = ClarificationQuestionSet(questions=next_questions)
        ready_to_write = bool(context.payload.get("ready_to_write"))

        if not ready_to_write:
            issue_gateway.update_issue_sync_label(
                issue_detail=parent_detail,
                target_label="sync-blocked",
            )
            lifecycle.set_operation_waiting_for_input(
                operation_type="brief_normalization",
                summary="Parent brief still needs product clarification before planning can continue.",
            )
            publication = self._deps.clarification_service.ensure_active_clarification(
                issue_key=context.issue_key,
                questions=next_question_set.questions,
                publisher=issue_gateway,
            )
            if publication.discord_followup_created:
                lifecycle.set_operation_completed(
                    operation_type="discord_followup_projection",
                    summary="Posted PM clarification follow-up to Discord.",
                )
            if publication.jira_comment_created:
                lifecycle.set_operation_completed(
                    operation_type="jira_comment_projection",
                    summary="Posted PM clarification questions to Jira.",
                )
            return lifecycle.build_outcome(
                handled=True,
                reason="pm_interview_still_open",
                extra={
                    "questions": next_question_set.to_payload(),
                    "comment_posted": publication.jira_comment_created,
                    "webhook_event": context.webhook_event,
                },
            )

        architecture_gate = issue_gateway.resolve_architecture_gate(
            parent_issue_key=context.issue_key,
            issue_summary=parent_detail.summary,
            issue_labels=list(parent_detail.labels or []),
        )
        self._sync_parent_issue_references(
            issue_gateway=issue_gateway,
            parent_detail=parent_detail,
            architecture_gate=architecture_gate,
        )
        if architecture_gate.required and not architecture_gate.ready:
            self._mark_parent_synced(lifecycle=lifecycle, draft=True)
            return self._block_on_architecture(
                context=context,
                issue_gateway=issue_gateway,
                parent_detail=parent_detail,
                lifecycle=lifecycle,
                reason="pm_interview_followup_architecture_blocked",
                extra={
                    "architecture_document_title": architecture_gate.document.title if architecture_gate.document else None,
                    "architecture_document_url": (
                        str(architecture_gate.document.canonical_url or "").strip()
                        if architecture_gate.document is not None
                        else None
                    ),
                },
            )
        self._mark_brief_normalized(lifecycle=lifecycle, source="PM clarification answers")

        try:
            fanout = self._deps.fanout_service.plan_and_seed(
                parent_detail=parent_detail,
                product_brief=brief_payload,
                project_key=project_key,
                planner=self._deps.brief_planner,
                child_sync_gateway=self._deps.child_sync_gateway,
            )
        except ParentPlanningFanoutSeedError as exc:
            self._mark_planning_completed_if_completed(
                lifecycle=lifecycle,
                planning_result=exc.planning_result,
                planning_summary="Backlog planning completed from the confirmed parent brief.",
            )
            return self._handle_fanout_failure(
                context=context,
                issue_gateway=issue_gateway,
                lifecycle=lifecycle,
                error=exc.error,
                failure_reason="pm_interview_followup_seed_failed",
            )
        except Exception as exc:  # noqa: BLE001
            return self._handle_fanout_failure(
                context=context,
                issue_gateway=issue_gateway,
                lifecycle=lifecycle,
                error=exc,
                failure_reason="pm_interview_followup_seed_failed",
            )
        self._mark_planning_completed_if_completed(
            lifecycle=lifecycle,
            planning_result=fanout.planning_result,
            planning_summary="Backlog planning completed from the confirmed parent brief.",
        )

        if not fanout.completed:
            return self._block_parent_brief(
                context=context,
                session=session,
                settings=settings,
                parent_detail=parent_detail,
                lifecycle=lifecycle,
                questions=fanout.questions,
                body_prefix="Backlog planning still needs clarification before child fanout can complete.",
                reason="pm_interview_followup_planning_blocked",
                waiting_operation_type="backlog_planning",
                extra={
                    "parent_revision": fanout.seed_data.get("parent_revision"),
                    "children_sync_status": fanout.seed_data.get("children_sync_status"),
                },
            )

        self._mark_fanout_completed(
            lifecycle=lifecycle,
            fanout_summary="Engineering child tickets were created or refreshed from the confirmed brief.",
        )
        return lifecycle.build_outcome(
            handled=True,
            reason="pm_interview_followup_resolved",
            extra={
                "updated_children": fanout.changed_children,
                "parent_revision": fanout.seed_data.get("parent_revision"),
                "children_sync_status": fanout.seed_data.get("children_sync_status"),
                "webhook_event": context.webhook_event,
            },
        )

    def _sync_parent_issue_references(
        self,
        *,
        issue_gateway,
        parent_detail,
        architecture_gate,
    ) -> None:
        issue_gateway.upsert_workflow_execution_link(issue_key=parent_detail.key)
        architecture_document = getattr(architecture_gate, "document", None)
        if architecture_document is None:
            return
        title = str(getattr(architecture_document, "title", "") or "").strip()
        url = str(getattr(architecture_document, "canonical_url", "") or "").strip()
        if not title or not url:
            raise ValueError(f"Architecture document link is incomplete for {parent_detail.key}")
        issue_gateway.upsert_architecture_document_link(
            issue_key=parent_detail.key,
            title=title,
            url=url,
        )

    def _mark_parent_synced(self, *, lifecycle, draft: bool) -> None:
        lifecycle.set_operation_completed(
            operation_type="jira_parent_update",
            summary=(
                "Parent Jira issue metadata synced without modifying the source description."
                if draft
                else "Parent Jira issue metadata synced without modifying the source description."
            ),
        )

    def _mark_brief_normalized(self, *, lifecycle, source: str) -> None:
        lifecycle.set_operation_completed(
            operation_type="brief_normalization",
            summary=f"Parent brief normalized from the {source}.",
        )
        self._mark_parent_synced(lifecycle=lifecycle, draft=False)

    def _mark_planning_completed(
        self,
        *,
        lifecycle,
        planning_summary: str,
    ) -> None:
        lifecycle.mark_operation_completed(
            operation_type="backlog_planning",
            summary=planning_summary,
        )

    def _mark_planning_completed_if_completed(
        self,
        *,
        lifecycle,
        planning_result,
        planning_summary: str,
    ) -> None:
        if planning_result.planning_state == PLANNING_STATE_COMPLETED:
            self._mark_planning_completed(
                lifecycle=lifecycle,
                planning_summary=planning_summary,
            )

    def _mark_fanout_completed(
        self,
        *,
        lifecycle,
        fanout_summary: str,
    ) -> None:
        lifecycle.mark_operation_completed(
            operation_type="jira_child_fanout",
            summary=fanout_summary,
        )
        lifecycle.mark_completed_if_ready()

    def _handle_fanout_failure(
        self,
        *,
        context,
        issue_gateway,
        lifecycle,
        error: Exception,
        failure_reason: str,
    ) -> WorkflowAdvanceOutcome:
        logger.exception(
            "parent_planning_fanout_failed request_id=%s tenant_id=%s issue_key=%s error=%s",
            context.request_id,
            context.tenant_id,
            context.issue_key,
            error,
        )
        category = classify_external_workflow_failure(error=error)
        lifecycle.mark_operation_failed(
            operation_type="jira_child_fanout",
            category=category,
            message=str(error),
        )
        issue_gateway.update_issue_sync_label(
            issue_detail=issue_gateway.load_parent_detail(context.issue_key),
            target_label="sync-blocked",
        )
        return lifecycle.build_outcome(
            handled=True,
            reason=failure_reason,
            extra={"error": str(error), "webhook_event": context.webhook_event},
        )
