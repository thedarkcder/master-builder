from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.commands.entrypoint import execute_tenant_jira_comment_command
from orchestrator.api.discord.ask.context import remove_issue_key_from_tenant_ask_history
from orchestrator.core.observability import reset_log_context, set_log_context
from orchestrator.api.schemas import DiscordCommandRequest
from orchestrator.api.webhooks.payload_utils import read_json_payload as _read_json_payload
from orchestrator.api.webhooks.contracts import (
    JIRA_COMMENT_EVENTS,
    extract_delivery_id,
    extract_issue_payload,
    extract_jira_comment_author_account_id,
    extract_status_transition,
    normalize_jira_webhook_event,
    parse_jira_comment_command,
    post_jira_comment,
    record_jira_webhook_receipt,
    resolve_active_project_for_issue,
    validate_webhook_auth,
)
from orchestrator.core.pre_run_check import evaluate_pre_run_check
from orchestrator.core.communications.enqueue_reason_contract import enqueue_reason_guidance
from orchestrator.core.decision_engine import IngressDecision, evaluate_ingress_precheck
from orchestrator.core.knowledge_base import resolve_missing_slots_from_knowledge
from orchestrator.core.discord.notifications import send_tenant_discord_message
from orchestrator.core.label_action_service import apply_issue_label_actions
from orchestrator.core.precheck_decision import precheck_classification, precheck_missing_slots
from orchestrator.core.project_policy import resolve_effective_policy
from orchestrator.core.runs import (
    RUN_STATUS_BLOCKED,
    RUN_STATUS_CANCELLED,
    RUN_STATUS_FAILED,
    enqueue_run,
    resolve_precheck_outcome_for_enqueue,
)
from orchestrator.api.discord.shared.state import normalize_status_name, remove_issue_key_from_seed_followups
from orchestrator.api.jira_oauth.connection_service import tenant_jira_oauth_context
from orchestrator.storage.models import Run
from orchestrator.storage.models import Project, Tenant
from orchestrator.tools.jira_oauth import JiraOAuthError
from orchestrator.tools.jira_oauth_http import JiraOAuthHttpClient

logger = logging.getLogger(__name__)

execute_jira_comment_command = execute_tenant_jira_comment_command
TODO_STATUS = "to do"
DECISION_GATE_COOLDOWN = timedelta(minutes=10)
KNOWLEDGE_AUTOFILL_BLOCK_START = "<!-- knowledge-autofill:start -->"
KNOWLEDGE_AUTOFILL_BLOCK_END = "<!-- knowledge-autofill:end -->"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_todo_status(status_name: str) -> bool:
    return normalize_status_name(status_name) == TODO_STATUS


def _resolve_ready_trigger_mode_for_tenant(tenant: Tenant) -> str:
    raw_mode = tenant.jira_config.get("ready_trigger_mode")
    if isinstance(raw_mode, str):
        normalized_mode = raw_mode.strip().lower()
        if normalized_mode in {"status_recheck", "transition_only"}:
            return normalized_mode
    return "status_recheck"


def _latest_decision_gate_blocked_run(
    *,
    session: Session,
    tenant_id: str,
    issue_key: str,
) -> Run | None:
    return session.execute(
        select(Run)
        .where(
            Run.tenant_id == tenant_id,
            Run.issue_key == issue_key,
            Run.status == RUN_STATUS_BLOCKED,
            Run.last_error.is_not(None),
            Run.last_error.like("Decision Gate required:%"),
        )
        .order_by(Run.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _decision_gate_cooldown_remaining_seconds(*, blocked_run: Run, now: datetime) -> int:
    blocked_at = blocked_run.finished_at or blocked_run.created_at
    if blocked_at is None:
        return 0
    if blocked_at.tzinfo is None:
        blocked_at = blocked_at.replace(tzinfo=timezone.utc)
    elapsed = now - blocked_at
    remaining = DECISION_GATE_COOLDOWN - elapsed
    return max(0, int(remaining.total_seconds()))


def _notify_jira_enqueue_skipped(
    *,
    context: JiraWebhookContext,
    session: Session,
    settings,  # noqa: ANN001
    reason: str,
    extra_detail: str | None = None,
) -> None:
    detail = f" ({extra_detail})" if extra_detail else ""
    guidance = enqueue_reason_guidance(reason)
    message = (
        f"Jira webhook did not queue a run for `{context.issue_key}`.\n"
        f"Reason: `{reason}`{detail}\n"
        f"Guidance: {guidance}\n"
        f"Status: `{context.issue_status or 'unknown'}`"
    )
    send_tenant_discord_message(
        session=session,
        tenant=context.tenant,
        project=context.project,
        message=message,
        settings=settings,
    )


def _normalize_backlog_pre_run_check_text(raw_value: str | None, *, max_chars: int = 240) -> str | None:
    if not isinstance(raw_value, str):
        return None
    normalized = " ".join(raw_value.strip().split())
    if not normalized:
        return None
    if len(normalized) > max_chars:
        return f"{normalized[: max_chars - 1].rstrip()}..."
    return normalized


def _resolve_ready_label_for_tenant(tenant: Tenant) -> str | None:
    raw_ready_label = (tenant.jira_config or {}).get("ready_label")
    if not isinstance(raw_ready_label, str):
        return None
    ready_label = raw_ready_label.strip()
    if not ready_label:
        return None
    return ready_label


def _upsert_block(*, current_description: str, block: str, start_marker: str, end_marker: str) -> str:
    current = str(current_description or "").strip()
    if not current:
        return block
    start_idx = current.find(start_marker)
    end_idx = current.find(end_marker)
    if start_idx >= 0 and end_idx > start_idx:
        end_of_marker = end_idx + len(end_marker)
        prefix = current[:start_idx].rstrip()
        suffix = current[end_of_marker:].lstrip()
        if prefix and suffix:
            return f"{prefix}\n\n{block}\n\n{suffix}"
        if prefix:
            return f"{prefix}\n\n{block}"
        if suffix:
            return f"{block}\n\n{suffix}"
        return block
    return f"{current}\n\n{block}"


def _slot_display_name(slot_name: str) -> str:
    mapping = {
        "objective": "Objective",
        "scope": "Scope",
        "acceptance_criteria": "Acceptance Criteria",
        "how_to_test": "How to test",
        "nfr_intent": "NFR intent (MVP vs scale-ready)",
        "reliability_security_constraints": "Mandatory reliability/security constraints",
        "out_of_scope": "Explicitly out of scope",
        "rollout_constraints": "Rollout/migration constraints",
        "decision_owner": "Decision owner",
        "dependencies_and_risks": "Dependencies / Risks",
    }
    return mapping.get(slot_name, slot_name.replace("_", " ").title())


def _build_knowledge_autofill_block(*, slot_answers: dict[str, object]) -> str:
    lines = [
        KNOWLEDGE_AUTOFILL_BLOCK_START,
        "## Knowledge Base Auto-Resolved Clarifications",
    ]
    for slot_name in sorted(slot_answers.keys()):
        value = slot_answers.get(slot_name)
        if value is None:
            continue
        slot_value = str(getattr(value, "slot_value", "") or "").strip()
        if not slot_value:
            continue
        citation = getattr(value, "citation", {}) if value is not None else {}
        citation_title = str(citation.get("title") or "").strip()
        citation_asset_id = str(citation.get("asset_id") or "").strip()
        citation_ts = str(citation.get("source_timestamp") or "").strip()
        confidence = float(getattr(value, "confidence", 0.0) or 0.0)
        if citation_title or citation_asset_id or citation_ts:
            citation_parts = []
            if citation_title:
                citation_parts.append(citation_title)
            if citation_asset_id:
                citation_parts.append(f"asset:{citation_asset_id}")
            if citation_ts:
                citation_parts.append(f"dated:{citation_ts}")
            slot_value = f"{slot_value} (source: {', '.join(citation_parts)}; confidence={confidence:.2f})"
        lines.append(f"{_slot_display_name(slot_name)}: {slot_value}")
    lines.append(KNOWLEDGE_AUTOFILL_BLOCK_END)
    return "\n".join(lines)


def _build_backlog_pre_run_check(context: JiraWebhookContext) -> dict[str, object]:
    decision = evaluate_ingress_precheck(
        source="jira_webhook",
        tenant_id=context.tenant_id,
        project_id=context.project.project_id if context.project is not None else None,
        issue_key=context.issue_key,
        issue_summary=context.issue_summary,
        issue_description=context.issue_description,
        issue_labels=context.issue_labels,
        ready_label=_resolve_ready_label_for_tenant(context.tenant),
        evaluate_pre_run_check_fn=evaluate_pre_run_check,
    )
    pre_check = decision.pre_check
    if pre_check is None:
        return {
            "outcome": "policy_eval_failed",
            "ready_label": _resolve_ready_label_for_tenant(context.tenant),
            "ready_label_present": False,
            "required_worker_capability": "linux",
            "required_worker_label": "worker:linux",
            "required_worker_label_present": False,
            "decision_gate_triggered": False,
            "decision_gate_reason": _normalize_backlog_pre_run_check_text(decision.policy_error),
            "gtd_valid": False,
            "gtd_missing_criteria": [],
            "gtd_clarification_questions": [],
        }
    decision_gate_reason = _normalize_backlog_pre_run_check_text(pre_check.decision_gate_reason)

    return {
        "outcome": pre_check.outcome,
        "ready_label": pre_check.ready_label,
        "ready_label_present": pre_check.ready_label_present,
        "required_worker_capability": pre_check.required_worker_capability,
        "required_worker_label": pre_check.required_worker_label,
        "required_worker_label_present": pre_check.required_worker_label_present,
        "decision_gate_triggered": pre_check.decision_gate_triggered,
        "decision_gate_reason": decision_gate_reason,
        "gtd_valid": pre_check.gtd_valid,
        "gtd_missing_criteria": list(pre_check.gtd_missing_criteria),
        "gtd_clarification_questions": list(pre_check.gtd_clarification_questions),
    }


def _notify_backlog_pre_run_check(
    *,
    context: JiraWebhookContext,
    session: Session,
    settings,  # noqa: ANN001
    board_id: int,
    pre_run_check: dict[str, object],
) -> None:
    outcome = str(pre_run_check.get("outcome") or "").strip()
    ready_label = pre_run_check.get("ready_label")
    decision_gate_reason = _normalize_backlog_pre_run_check_text(
        pre_run_check.get("decision_gate_reason") if isinstance(pre_run_check.get("decision_gate_reason"), str) else None
    )
    gtd_missing_criteria = [
        str(item).strip()
        for item in (pre_run_check.get("gtd_missing_criteria") or [])
        if str(item).strip()
    ]

    lines = [
        f"New issue `{context.issue_key}` was added to the backlog on board `{board_id}`.",
        "Run was not started (backlog-only event).",
    ]
    if context.issue_status:
        lines.append(f"Issue status: `{context.issue_status}`")

    if outcome == "ready_for_agent":
        if isinstance(ready_label, str) and ready_label.strip():
            lines.append(f"Pre-run check: labeled `{ready_label.strip()}` and ready for agent.")
        else:
            lines.append("Pre-run check: ready for agent.")
    elif outcome == "decision_gate_required":
        lines.append("Pre-run check: Decision Gate required before execution.")
        if decision_gate_reason:
            lines.append(f"Decision Gate reason: {decision_gate_reason}")
    elif outcome == "gtd_required":
        lines.append("Pre-run check: Good To Do details are incomplete.")
        if gtd_missing_criteria:
            lines.append("Missing GTD criteria: " + ", ".join(gtd_missing_criteria))
    elif outcome == "missing_ready_label":
        if isinstance(ready_label, str) and ready_label.strip():
            lines.append(f"Pre-run check: missing ready label `{ready_label.strip()}`.")
        else:
            lines.append("Pre-run check: missing ready label.")

    required_worker_label = str(pre_run_check.get("required_worker_label") or "").strip()
    if required_worker_label:
        lines.append(f"Required worker capability: `{required_worker_label}`.")

    send_tenant_discord_message(
        session=session,
        tenant=context.tenant,
        project=context.project,
        message="\n".join(lines),
        settings=settings,
    )


def _evaluate_precheck_decision_with_labels(
    *,
    context: JiraWebhookContext,
    session: Session,
    settings,  # noqa: ANN001
    issue_description: str | None = None,
) -> tuple[IngressDecision, list[str]]:
    decision = evaluate_ingress_precheck(
        source="jira_webhook",
        tenant_id=context.tenant_id,
        project_id=context.project.project_id if context.project is not None else None,
        issue_key=context.issue_key,
        issue_summary=context.issue_summary,
        issue_description=context.issue_description if issue_description is None else issue_description,
        issue_labels=context.issue_labels,
        ready_label=_resolve_ready_label_for_tenant(context.tenant),
        evaluate_pre_run_check_fn=evaluate_pre_run_check,
    )
    if decision.pre_check is None:
        return decision, []
    apply_result = apply_issue_label_actions(
        session=session,
        tenant=context.tenant,
        project_policy_overrides=context.project.policy_overrides if context.project is not None else {},
        issue_key=context.issue_key,
        existing_labels=context.issue_labels,
        actions=decision.label_actions,
        settings=settings,
        tenant_jira_oauth_context_fn=tenant_jira_oauth_context,
        logger=logger,
    )
    applied_labels = list(apply_result.applied_labels)
    if applied_labels:
        context.issue_labels = [*context.issue_labels, *applied_labels]
        logger.info(
            "jira_webhook_labels_applied request_id=%s tenant_id=%s issue_key=%s labels=%s",
            context.request_id,
            context.tenant_id,
            context.issue_key,
            ",".join(applied_labels),
        )
    resolved_decision = decision.with_applied_labels(applied_labels)
    pre_check = resolved_decision.pre_check
    if (
        pre_check is not None
        and resolved_decision.block_reason in {"decision_gate_required", "gtd_required"}
        and context.project is not None
    ):
        effective_policy = resolve_effective_policy(
            tenant_policy=context.tenant.policy_config or {},
            project_overrides=context.project.policy_overrides or {},
        )
        knowledge_enabled = bool(effective_policy.get("knowledge_base_enabled", True))
        knowledge_mode = str(effective_policy.get("knowledge_auto_answer_mode") or "").strip().lower()
        if knowledge_mode not in {"safe", "balanced", "aggressive"}:
            knowledge_mode = "aggressive"
        missing_slots = precheck_missing_slots(pre_check)
        if knowledge_enabled and missing_slots:
            slot_answers = resolve_missing_slots_from_knowledge(
                session=session,
                tenant_id=context.tenant_id,
                project_id=context.project.project_id,
                missing_slots=missing_slots,
                mode=knowledge_mode,
            )
            if slot_answers:
                try:
                    oauth = tenant_jira_oauth_context(session=session, tenant=context.tenant, settings=settings)
                    oauth_client = getattr(oauth, "client", None)
                    oauth_connection = getattr(oauth, "connection", None)
                    oauth_access_token = getattr(oauth, "access_token", None)
                    cloud_id = getattr(oauth_connection, "cloud_id", None)
                    if oauth_client is None or oauth_access_token is None or not str(cloud_id or "").strip():
                        raise RuntimeError("Tenant Jira OAuth context is incomplete")
                    next_description = _upsert_block(
                        current_description=str(
                            context.issue_description if issue_description is None else issue_description or ""
                        ),
                        block=_build_knowledge_autofill_block(slot_answers=slot_answers),
                        start_marker=KNOWLEDGE_AUTOFILL_BLOCK_START,
                        end_marker=KNOWLEDGE_AUTOFILL_BLOCK_END,
                    )
                    oauth_client.update_issue_summary_and_description(
                        access_token=oauth_access_token,
                        cloud_id=str(cloud_id),
                        issue_id_or_key=context.issue_key,
                        summary=str(context.issue_summary or context.issue_key),
                        description=next_description,
                    )
                    context.issue_description = next_description
                    reevaluated_decision = evaluate_ingress_precheck(
                        source="jira_webhook",
                        tenant_id=context.tenant_id,
                        project_id=context.project.project_id if context.project is not None else None,
                        issue_key=context.issue_key,
                        issue_summary=context.issue_summary,
                        issue_description=next_description,
                        issue_labels=context.issue_labels,
                        ready_label=_resolve_ready_label_for_tenant(context.tenant),
                        evaluate_pre_run_check_fn=evaluate_pre_run_check,
                    )
                    if reevaluated_decision.pre_check is not None:
                        reapply_result = apply_issue_label_actions(
                            session=session,
                            tenant=context.tenant,
                            project_policy_overrides=context.project.policy_overrides if context.project is not None else {},
                            issue_key=context.issue_key,
                            existing_labels=context.issue_labels,
                            actions=reevaluated_decision.label_actions,
                            settings=settings,
                            tenant_jira_oauth_context_fn=tenant_jira_oauth_context,
                            oauth_context=oauth,
                            logger=logger,
                        )
                        re_applied = list(reapply_result.applied_labels)
                        if re_applied:
                            context.issue_labels = [*context.issue_labels, *re_applied]
                            applied_labels = [*applied_labels, *re_applied]
                        resolved_decision = reevaluated_decision.with_applied_labels(re_applied)
                        logger.info(
                            "jira_webhook_knowledge_autofill_applied request_id=%s tenant_id=%s issue_key=%s classification=%s resolved_slots=%s",
                            context.request_id,
                            context.tenant_id,
                            context.issue_key,
                            precheck_classification(resolved_decision.pre_check),
                            ",".join(sorted(slot_answers.keys())),
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "jira_webhook_knowledge_autofill_failed request_id=%s tenant_id=%s issue_key=%s error=%s",
                        context.request_id,
                        context.tenant_id,
                        context.issue_key,
                        exc,
                    )
    return resolved_decision, applied_labels


@dataclass
class JiraWebhookContext:
    request_id: str
    tenant_id: str
    tenant: Tenant
    payload: dict
    webhook_event: str | None
    issue_key: str
    issue_labels: list[str]
    issue_status: str | None
    issue_status_category_key: str | None
    issue_summary: str | None
    issue_description: str | None
    comment_command: str | None
    comment_command_argument: str | None
    comment_command_error: str | None
    delivery_id: str | None
    project: Project | None


def jira_webhook_response(
    context: JiraWebhookContext,
    *,
    enqueued: bool,
    reason: str | None,
    **extra: object,
) -> dict:
    payload: dict[str, object] = {
        "request_id": context.request_id,
        "tenant_id": context.tenant_id,
        "project_id": context.project.project_id if context.project is not None else None,
        "issue_key": context.issue_key,
        "enqueued": enqueued,
    }
    if reason is not None:
        payload["reason"] = reason
    payload.update(extra)
    return payload


async def stage_parse_jira_webhook_context(
    *,
    tenant_id: str,
    tenant: Tenant,
    request,
    request_id: str,
    session: Session,
    settings,  # noqa: ANN001
) -> JiraWebhookContext:
    validate_webhook_auth(
        tenant=tenant,
        request=request,
        request_id=request_id,
        session=session,
        settings=settings,
    )

    payload, _ = await _read_json_payload(request, request_id=request_id, source="jira")
    webhook_event = normalize_jira_webhook_event(payload.get("webhookEvent"))

    issue_key, issue_labels, issue_status, issue_status_category_key, issue_summary, issue_description = extract_issue_payload(payload)
    comment_command, comment_command_argument, comment_command_error = parse_jira_comment_command(payload)
    delivery_id = extract_delivery_id(request)
    record_jira_webhook_receipt(
        session=session,
        tenant=tenant,
        delivery_id=delivery_id,
        issue_key=issue_key,
        webhook_event=webhook_event,
    )
    logger.info(
        "jira_webhook_issue_parsed request_id=%s tenant_id=%s issue_key=%s delivery_id=%s webhook_event=%s comment_command=%s comment_command_error=%s",
        request_id,
        tenant_id,
        issue_key,
        delivery_id,
        webhook_event,
        comment_command,
        comment_command_error,
    )
    project = resolve_active_project_for_issue(
        session=session,
        tenant_id=tenant_id,
        issue_key=issue_key,
    )
    return JiraWebhookContext(
        request_id=request_id,
        tenant_id=tenant_id,
        tenant=tenant,
        payload=payload,
        webhook_event=webhook_event,
        issue_key=issue_key,
        issue_labels=issue_labels,
        issue_status=issue_status,
        issue_status_category_key=issue_status_category_key,
        issue_summary=issue_summary,
        issue_description=issue_description,
        comment_command=comment_command,
        comment_command_argument=comment_command_argument,
        comment_command_error=comment_command_error,
        delivery_id=delivery_id,
        project=project,
    )


def stage_handle_issue_deleted(
    *,
    context: JiraWebhookContext,
    session: Session,
) -> dict | None:
    if context.webhook_event != "issue_deleted":
        return None

    removed_entries = remove_issue_key_from_tenant_ask_history(
        session=session,
        tenant=context.tenant,
        issue_key=context.issue_key,
    )
    removed_seed_contexts, removed_seed_issue_refs = remove_issue_key_from_seed_followups(
        session=session,
        tenant=context.tenant,
        issue_key=context.issue_key,
    )
    logger.info(
        "jira_webhook_issue_deleted request_id=%s tenant_id=%s issue_key=%s removed_history_entries=%s removed_seed_contexts=%s removed_seed_issue_refs=%s",
        context.request_id,
        context.tenant_id,
        context.issue_key,
        removed_entries,
        removed_seed_contexts,
        removed_seed_issue_refs,
    )
    return jira_webhook_response(
        context,
        enqueued=False,
        reason="issue_deleted",
        removed_history_entries=removed_entries,
        removed_seed_contexts=removed_seed_contexts,
        removed_seed_issue_refs=removed_seed_issue_refs,
        webhook_event=context.webhook_event,
    )


def stage_handle_invalid_comment_command(
    *,
    context: JiraWebhookContext,
) -> dict | None:
    if not context.comment_command_error:
        return None
    logger.info(
        "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=invalid_comment_command",
        context.request_id,
        context.tenant_id,
        context.issue_key,
    )
    return jira_webhook_response(
        context,
        enqueued=False,
        reason="invalid_comment_command",
        webhook_event=context.webhook_event,
    )


def stage_handle_comment_event_memory(
    *,
    context: JiraWebhookContext,
    session: Session,
) -> int:
    if context.webhook_event not in JIRA_COMMENT_EVENTS:
        return 0
    removed_entries = remove_issue_key_from_tenant_ask_history(
        session=session,
        tenant=context.tenant,
        issue_key=context.issue_key,
    )
    logger.info(
        "jira_webhook_comment_event_memory_cleared request_id=%s tenant_id=%s issue_key=%s webhook_event=%s removed_history_entries=%s",
        context.request_id,
        context.tenant_id,
        context.issue_key,
        context.webhook_event,
        removed_entries,
    )
    return removed_entries


def stage_handle_comment_without_command(
    *,
    context: JiraWebhookContext,
    removed_history_entries: int,
) -> dict | None:
    if context.webhook_event not in JIRA_COMMENT_EVENTS or context.comment_command is not None:
        return None
    logger.info(
        "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=comment_without_command webhook_event=%s",
        context.request_id,
        context.tenant_id,
        context.issue_key,
        context.webhook_event,
    )
    return jira_webhook_response(
        context,
        enqueued=False,
        reason="comment_without_command",
        webhook_event=context.webhook_event,
        removed_history_entries=removed_history_entries,
    )


def stage_handle_comment_ask_command(
    *,
    context: JiraWebhookContext,
    session: Session,
    settings,  # noqa: ANN001
) -> dict | None:
    if context.comment_command != "ask":
        return None

    question = (context.comment_command_argument or "").strip()
    if not question:
        return jira_webhook_response(
            context,
            enqueued=False,
            reason="invalid_comment_command",
        )

    author_account_id = extract_jira_comment_author_account_id(context.payload) or "jira-user"
    try:
        ask_response = execute_jira_comment_command(
            session=session,
            tenant_id=context.tenant_id,
            payload=DiscordCommandRequest(
                user_id=author_account_id,
                channel_id=None,
                command=f"!ask @{context.issue_key} {question}",
            ),
        )
        response_text = ask_response.message.strip()
        if not response_text:
            response_text = "I processed your question but returned no response text."
    except HTTPException as exc:
        logger.exception(
            "jira_comment_ask_command_failed request_id=%s tenant_id=%s issue_key=%s detail=%s error=%s",
            context.request_id,
            context.tenant_id,
            context.issue_key,
            exc.detail,
            exc,
        )
        response_text = f"Unable to process `/mb ask`: {exc.detail}"

    posted, post_error = post_jira_comment(
        session=session,
        tenant=context.tenant,
        issue_key=context.issue_key,
        comment=response_text,
        settings=settings,
    )
    return jira_webhook_response(
        context,
        enqueued=False,
        reason="comment_command_ask",
        command=context.comment_command,
        question=question,
        comment_posted=posted,
        comment_error=post_error,
        webhook_event=context.webhook_event,
    )


def stage_handle_backlog_followup_issue_created(
    *,
    context: JiraWebhookContext,
) -> dict | None:
    if context.webhook_event != "issue_created":
        return None
    normalized_labels = {str(label).strip().casefold() for label in context.issue_labels}
    if "backlog-only" not in normalized_labels:
        return None
    logger.info(
        "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=backlog_followup_issue_created",
        context.request_id,
        context.tenant_id,
        context.issue_key,
    )
    return jira_webhook_response(
        context,
        enqueued=False,
        reason="backlog_followup_issue_created",
        webhook_event=context.webhook_event,
    )


def _issues_payload_contains_issue(*, payload: object, issue_key: str) -> bool:
    if not isinstance(payload, dict):
        return False
    issues = payload.get("issues")
    if not isinstance(issues, list):
        return False
    normalized_issue_key = issue_key.strip().upper()
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        candidate_key = str(issue.get("key") or "").strip().upper()
        if candidate_key == normalized_issue_key:
            return True
    return False


def _fetch_issue_board_location(
    *,
    context: JiraWebhookContext,
    session: Session,
    settings,  # noqa: ANN001
    board_id: int,
) -> tuple[str, str | None]:
    try:
        oauth_context = tenant_jira_oauth_context(
            session=session,
            tenant=context.tenant,
            settings=settings,
        )
    except HTTPException as exc:
        return "error", str(exc.detail)

    issue_jql = quote_plus(f'key = "{context.issue_key}"')
    base_url = f"https://api.atlassian.com/ex/jira/{oauth_context.connection.cloud_id}/rest/agile/1.0"
    backlog_url = f"{base_url}/board/{board_id}/backlog?jql={issue_jql}&maxResults=1"
    board_url = f"{base_url}/board/{board_id}/issue?jql={issue_jql}&maxResults=1"
    http_client = JiraOAuthHttpClient()

    backlog_error_detail: str | None = None
    try:
        backlog_payload = http_client.get_json(
            url=backlog_url,
            access_token=oauth_context.access_token,
        )
        if _issues_payload_contains_issue(payload=backlog_payload, issue_key=context.issue_key):
            return "backlog", None
    except (JiraOAuthError, ValueError) as exc:
        backlog_error_detail = f"backlog lookup failed: {exc}"

    try:
        board_payload = http_client.get_json(
            url=board_url,
            access_token=oauth_context.access_token,
        )
        if _issues_payload_contains_issue(payload=board_payload, issue_key=context.issue_key):
            return "board", None
    except (JiraOAuthError, ValueError) as exc:
        board_error_detail = f"board lookup failed: {exc}"
        if backlog_error_detail:
            return "error", f"{backlog_error_detail}; {board_error_detail}"
        return "error", board_error_detail

    if backlog_error_detail:
        return "not_on_board", backlog_error_detail
    return "not_on_board", None


def stage_handle_run_board_gate(
    *,
    context: JiraWebhookContext,
    session: Session,
    settings,  # noqa: ANN001
) -> dict | None:
    raw_board_id = None
    if context.project is not None:
        raw_board_id = (context.project.policy_overrides or {}).get("run_board_id")
    if raw_board_id is None:
        return None

    try:
        board_id = int(raw_board_id)
    except (TypeError, ValueError):
        board_id = 0
    if board_id <= 0:
        logger.info(
            "jira_webhook_not_started request_id=%s tenant_id=%s issue_key=%s reason=board_gate_unconfigured board_id=%s",
            context.request_id,
            context.tenant_id,
            context.issue_key,
            raw_board_id,
        )
        return jira_webhook_response(
            context,
            enqueued=False,
            reason="board_gate_unconfigured",
            guidance=enqueue_reason_guidance("board_gate_unconfigured"),
            board_id=raw_board_id,
            webhook_event=context.webhook_event,
        )

    location, detail = _fetch_issue_board_location(
        context=context,
        session=session,
        settings=settings,
        board_id=board_id,
    )
    if location == "board":
        return None

    if location == "backlog":
        reason = "issue_in_backlog"
        pre_run_check = _build_backlog_pre_run_check(context)
        if context.webhook_event == "issue_created":
            _notify_backlog_pre_run_check(
                context=context,
                session=session,
                settings=settings,
                board_id=board_id,
                pre_run_check=pre_run_check,
            )
        logger.info(
            "jira_webhook_backlog_pre_run_check request_id=%s tenant_id=%s issue_key=%s board_id=%s outcome=%s decision_gate_triggered=%s",
            context.request_id,
            context.tenant_id,
            context.issue_key,
            board_id,
            pre_run_check.get("outcome"),
            pre_run_check.get("decision_gate_triggered"),
        )
        return jira_webhook_response(
            context,
            enqueued=False,
            reason=reason,
            guidance=enqueue_reason_guidance(reason),
            board_id=board_id,
            webhook_event=context.webhook_event,
            detail=detail,
            pre_run_check=pre_run_check,
        )
    elif location == "not_on_board":
        reason = "issue_not_on_board"
    else:
        reason = "board_gate_check_failed"

    logger.info(
        "jira_webhook_not_started request_id=%s tenant_id=%s issue_key=%s reason=%s board_id=%s detail=%s",
        context.request_id,
        context.tenant_id,
        context.issue_key,
        reason,
        board_id,
        detail,
    )
    _notify_jira_enqueue_skipped(
        context=context,
        session=session,
        settings=settings,
        reason=reason,
        extra_detail=f"board_id={board_id}" if detail is None else f"board_id={board_id}; detail={detail}",
    )
    return jira_webhook_response(
        context,
        enqueued=False,
        reason=reason,
        guidance=enqueue_reason_guidance(reason),
        board_id=board_id,
        webhook_event=context.webhook_event,
        detail=detail,
    )


async def ingest_jira_webhook_event(
    *,
    tenant_id: str,
    request,
    session: Session,
    settings,  # noqa: ANN001
    request_id: str | None = None,
) -> dict:
    request_id = request_id or request.headers.get("X-Request-Id") or str(uuid4())
    context_tokens = set_log_context(correlation_id=request_id, tenant_id=tenant_id)
    try:
        logger.info("jira_webhook_received request_id=%s tenant_id=%s", request_id, tenant_id)

        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            logger.warning("jira_webhook_unknown_tenant request_id=%s tenant_id=%s", request_id, tenant_id)
            raise HTTPException(status_code=404, detail="Unknown tenant")
        if not tenant.is_enabled:
            logger.info(
                "jira_webhook_ignored request_id=%s tenant_id=%s reason=tenant_disabled",
                request_id,
                tenant_id,
            )
            return {
                "request_id": request_id,
                "tenant_id": tenant_id,
                "enqueued": False,
                "reason": "tenant_disabled",
            }

        context = await stage_parse_jira_webhook_context(
            tenant_id=tenant_id,
            tenant=tenant,
            request=request,
            request_id=request_id,
            session=session,
            settings=settings,
        )

        deleted_response = stage_handle_issue_deleted(context=context, session=session)
        if deleted_response is not None:
            return deleted_response

        invalid_comment_response = stage_handle_invalid_comment_command(context=context)
        if invalid_comment_response is not None:
            return invalid_comment_response

        removed_history_entries = stage_handle_comment_event_memory(context=context, session=session)
        comment_without_command_response = stage_handle_comment_without_command(
            context=context,
            removed_history_entries=removed_history_entries,
        )
        if comment_without_command_response is not None:
            return comment_without_command_response

        comment_ask_response = stage_handle_comment_ask_command(
            context=context,
            session=session,
            settings=settings,
        )
        if comment_ask_response is not None:
            return comment_ask_response

        backlog_followup_response = stage_handle_backlog_followup_issue_created(context=context)
        if backlog_followup_response is not None:
            return backlog_followup_response

        if context.issue_status is None:
            logger.info(
                "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=issue_status_missing",
                request_id,
                tenant_id,
                context.issue_key,
            )
            return jira_webhook_response(
                context,
                enqueued=False,
                reason="issue_status_missing",
                webhook_event=context.webhook_event,
            )

        if context.issue_status_category_key == "done":
            logger.info(
                "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=issue_done issue_status=%s",
                request_id,
                tenant_id,
                context.issue_key,
                context.issue_status,
            )
            return jira_webhook_response(
                context,
                enqueued=False,
                reason="issue_done",
                issue_status=context.issue_status,
                webhook_event=context.webhook_event,
            )

        if context.project is None:
            logger.info(
                "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=project_not_mapped",
                request_id,
                tenant_id,
                context.issue_key,
            )
            _notify_jira_enqueue_skipped(
                context=context,
                session=session,
                settings=settings,
                reason="project_not_mapped",
            )
            return jira_webhook_response(
                context,
                enqueued=False,
                reason="project_not_mapped",
                guidance=enqueue_reason_guidance("project_not_mapped"),
                command=context.comment_command,
                webhook_event=context.webhook_event,
            )

        initial_precheck_decision, _ = _evaluate_precheck_decision_with_labels(
            context=context,
            session=session,
            settings=settings,
        )
        if initial_precheck_decision.pre_check is None:
            logger.warning(
                "jira_webhook_not_started request_id=%s tenant_id=%s issue_key=%s reason=policy_eval_failed error=%s",
                request_id,
                tenant_id,
                context.issue_key,
                initial_precheck_decision.policy_error,
            )
            _notify_jira_enqueue_skipped(
                context=context,
                session=session,
                settings=settings,
                reason="policy_eval_failed",
                extra_detail=initial_precheck_decision.policy_error,
            )
            return jira_webhook_response(
                context,
                enqueued=False,
                reason="policy_eval_failed",
                guidance=enqueue_reason_guidance("policy_eval_failed"),
                trigger_reason="status_recheck",
                webhook_event=context.webhook_event,
            )

        board_gate_response = stage_handle_run_board_gate(
            context=context,
            session=session,
            settings=settings,
        )
        if board_gate_response is not None:
            return board_gate_response

        from_status, to_status = extract_status_transition(context.payload)
        ready_trigger_mode = _resolve_ready_trigger_mode_for_tenant(context.tenant)
        trigger_reason = "status_recheck"
        if context.comment_command == "run":
            trigger_reason = "comment_command_run"
        elif context.comment_command == "retry":
            trigger_reason = "comment_command_retry"
        elif context.webhook_event == "issue_created":
            trigger_reason = "issue_created"
        elif (
            to_status is not None
            and from_status is not None
            and from_status.casefold() != to_status.casefold()
        ):
            trigger_reason = "status_transition_to_todo" if _is_todo_status(to_status) else "status_transition_to_ready"
        if trigger_reason == "status_recheck" and ready_trigger_mode == "transition_only":
            logger.info(
                "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=ready_status_recheck_disabled trigger_mode=%s",
                request_id,
                tenant_id,
                context.issue_key,
                ready_trigger_mode,
            )
            return jira_webhook_response(
                context,
                enqueued=False,
                reason="ready_status_recheck_disabled",
                trigger_reason=trigger_reason,
                trigger_mode=ready_trigger_mode,
                issue_status=context.issue_status,
                webhook_event=context.webhook_event,
            )
        logger.info(
            "jira_webhook_trigger request_id=%s tenant_id=%s issue_key=%s trigger_reason=%s issue_status=%s from_status=%s to_status=%s",
            request_id,
            tenant_id,
            context.issue_key,
            trigger_reason,
            context.issue_status,
            from_status,
            to_status,
        )

        if not _is_todo_status(context.issue_status):
            logger.info(
                "jira_webhook_not_started request_id=%s tenant_id=%s issue_key=%s reason=ready_for_agent_backlog issue_status=%s",
                request_id,
                tenant_id,
                context.issue_key,
                context.issue_status,
            )
            return jira_webhook_response(
                context,
                enqueued=False,
                reason="ready_for_agent_backlog",
                ready_for_agent=True,
                trigger_reason=trigger_reason,
                webhook_event=context.webhook_event,
            )

        latest_decision_gate_block = _latest_decision_gate_blocked_run(
            session=session,
            tenant_id=tenant_id,
            issue_key=context.issue_key,
        )
        if latest_decision_gate_block is not None:
            remaining_seconds = _decision_gate_cooldown_remaining_seconds(
                blocked_run=latest_decision_gate_block,
                now=_utcnow(),
            )
            if remaining_seconds > 0:
                logger.info(
                    "jira_webhook_not_started request_id=%s tenant_id=%s issue_key=%s reason=decision_gate_cooldown_active remaining_seconds=%s run_id=%s",
                    request_id,
                    tenant_id,
                    context.issue_key,
                    remaining_seconds,
                    latest_decision_gate_block.run_id,
                )
                return jira_webhook_response(
                    context,
                    enqueued=False,
                    reason="decision_gate_cooldown_active",
                    guidance=(
                        "Decision Gate was recently required for this issue. "
                        "Wait for the cooldown to expire, then rerun."
                    ),
                    run_id=latest_decision_gate_block.run_id,
                    cooldown_seconds_remaining=remaining_seconds,
                    ready_for_agent=True,
                    trigger_reason=trigger_reason,
                    webhook_event=context.webhook_event,
                )

        retry_source_run = None
        resolved_issue_description = context.issue_description
        if context.comment_command == "retry":
            retryable_statuses = {RUN_STATUS_FAILED, RUN_STATUS_BLOCKED, RUN_STATUS_CANCELLED}
            retry_source_run = session.execute(
                select(Run)
                .where(
                    Run.tenant_id == tenant_id,
                    Run.issue_key == context.issue_key,
                    Run.status.in_(retryable_statuses),
                )
                .order_by(Run.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if retry_source_run is None:
                logger.info(
                    "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=no_retryable_run",
                    request_id,
                    tenant_id,
                    context.issue_key,
                )
                _notify_jira_enqueue_skipped(
                    context=context,
                    session=session,
                    settings=settings,
                    reason="no_retryable_run",
                )
                return jira_webhook_response(
                    context,
                    enqueued=False,
                    reason="no_retryable_run",
                    guidance=enqueue_reason_guidance("no_retryable_run"),
                    trigger_reason=trigger_reason,
                    webhook_event=context.webhook_event,
                )
            resolved_issue_description = retry_source_run.issue_description

        precheck_decision, _ = _evaluate_precheck_decision_with_labels(
            context=context,
            session=session,
            settings=settings,
            issue_description=resolved_issue_description,
        )
        if precheck_decision.pre_check is None:
            logger.warning(
                "jira_webhook_not_started request_id=%s tenant_id=%s issue_key=%s reason=policy_eval_failed error=%s",
                request_id,
                tenant_id,
                context.issue_key,
                precheck_decision.policy_error,
            )
            _notify_jira_enqueue_skipped(
                context=context,
                session=session,
                settings=settings,
                reason="policy_eval_failed",
                extra_detail=precheck_decision.policy_error,
            )
            return jira_webhook_response(
                context,
                enqueued=False,
                reason="policy_eval_failed",
                guidance=enqueue_reason_guidance("policy_eval_failed"),
                trigger_reason=trigger_reason,
                webhook_event=context.webhook_event,
            )
        pre_check = precheck_decision.pre_check
        if precheck_decision.block_reason in {"decision_gate_required", "gtd_required", "missing_ready_label"}:
            logger.info(
                "jira_webhook_not_started request_id=%s tenant_id=%s issue_key=%s reason=%s",
                request_id,
                tenant_id,
                context.issue_key,
                precheck_decision.block_reason,
            )
            _notify_jira_enqueue_skipped(
                context=context,
                session=session,
                settings=settings,
                reason=precheck_decision.block_reason,
                extra_detail=(
                    f"decision_gate_reason={pre_check.decision_gate_reason}"
                    if precheck_decision.block_reason == "decision_gate_required"
                    else (
                        "missing_gtd=" + ", ".join(pre_check.gtd_missing_criteria)
                        if precheck_decision.block_reason == "gtd_required"
                        else None
                    )
                ),
            )
            return jira_webhook_response(
                context,
                enqueued=False,
                reason=precheck_decision.block_reason,
                guidance=enqueue_reason_guidance(precheck_decision.block_reason),
                trigger_reason=trigger_reason,
                webhook_event=context.webhook_event,
                decision_gate_reason=pre_check.decision_gate_reason if precheck_decision.block_reason == "decision_gate_required" else None,
                gtd_missing_criteria=list(pre_check.gtd_missing_criteria) if precheck_decision.block_reason == "gtd_required" else None,
                gtd_questions=list(pre_check.gtd_clarification_questions) if precheck_decision.block_reason == "gtd_required" else None,
                ready_label=_resolve_ready_label_for_tenant(context.tenant) if precheck_decision.block_reason == "missing_ready_label" else None,
            )

        enqueue_result = enqueue_run(
            session,
            tenant_id=tenant_id,
            project_id=context.project.project_id,
            issue_key=context.issue_key,
            issue_summary=context.issue_summary,
            issue_description=resolved_issue_description,
            repo_url=context.project.github_repository,
            delivery_id=context.delivery_id,
            precheck_outcome=resolve_precheck_outcome_for_enqueue(
                precheck_outcome=pre_check.outcome
            ),
            max_concurrent_runs=tenant.policy_config.get("max_concurrent_runs"),
        )
        if not enqueue_result.enqueued:
            logger.info(
                "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=%s run_id=%s",
                request_id,
                tenant_id,
                context.issue_key,
                enqueue_result.reason,
                enqueue_result.run.run_id,
            )
            _notify_jira_enqueue_skipped(
                context=context,
                session=session,
                settings=settings,
                reason=enqueue_result.reason,
                extra_detail=f"run_id={enqueue_result.run.run_id}",
            )
            return jira_webhook_response(
                context,
                enqueued=False,
                reason=enqueue_result.reason,
                guidance=enqueue_reason_guidance(enqueue_result.reason),
                run_id=enqueue_result.run.run_id,
                trigger_reason=trigger_reason,
                command=context.comment_command,
                webhook_event=context.webhook_event,
            )
        logger.info(
            "jira_webhook_enqueued request_id=%s tenant_id=%s issue_key=%s run_id=%s",
            request_id,
            tenant_id,
            context.issue_key,
            enqueue_result.run.run_id,
        )
        return jira_webhook_response(
            context,
            enqueued=True,
            reason=None,
            run_id=enqueue_result.run.run_id,
            trigger_reason=trigger_reason,
            command=context.comment_command,
            webhook_event=context.webhook_event,
        )
    finally:
        reset_log_context(context_tokens)


__all__ = [
    "JiraWebhookContext",
    "execute_jira_comment_command",
    "extract_status_transition",
    "ingest_jira_webhook_event",
    "jira_webhook_response",
    "stage_handle_comment_ask_command",
    "stage_handle_comment_event_memory",
    "stage_handle_comment_without_command",
    "stage_handle_invalid_comment_command",
    "stage_handle_issue_deleted",
    "stage_parse_jira_webhook_context",
]
