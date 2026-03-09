from __future__ import annotations

from collections.abc import Callable
from difflib import SequenceMatcher
import logging
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.schemas import DiscordCommandRequest, DiscordCommandResponse
from orchestrator.core.codex_invocation import CodexInvocationContext, invoke_codex_json
from orchestrator.core.codex_runtime import CodexRuntimeError
from orchestrator.core.communications.command_pipeline import CommandScope
from orchestrator.core.communications.enqueue_reason_contract import (
    enqueue_reason_guidance,
    format_enqueue_conflict_detail,
)
from orchestrator.core.decision_engine import (
    DecisionEngineResult,
    DecisionEventInput,
    DecisionSource,
    evaluate_decision_event,
)
from orchestrator.core.pre_run_check import evaluate_pre_run_check
from orchestrator.core.precheck_decision import build_precheck_message
from orchestrator.core.precheck_question_lock import (
    build_precheck_questions_block,
    remove_precheck_questions_block,
    upsert_precheck_questions_block,
)
from orchestrator.core.project_policy import resolve_effective_policy
from orchestrator.core.run_gate_service import enqueue_issue_run_with_precheck, resolve_run_gate_block
from orchestrator.core.runs import cancel_run
from orchestrator.storage.models import Run, Tenant

logger = logging.getLogger(__name__)

DECISION_GATE_BLOCK_START = "<!-- decision-gate-clarifications:start -->"
DECISION_GATE_BLOCK_END = "<!-- decision-gate-clarifications:end -->"
KNOWLEDGE_AUTOFILL_BLOCK_START = "<!-- knowledge-autofill:start -->"
KNOWLEDGE_AUTOFILL_BLOCK_END = "<!-- knowledge-autofill:end -->"


def _oauth_context_value(oauth_context: Any, field: str) -> Any:
    if isinstance(oauth_context, dict):
        return oauth_context.get(field)
    return getattr(oauth_context, field, None)


def _evaluate_precheck_decision_with_labels(
    *,
    session: Session,
    tenant: Tenant,
    project: Any,  # noqa: ANN401
    source: DecisionSource,
    issue_key: str,
    issue_summary: str | None,
    issue_description: str | None,
    issue_labels: list[str] | None,
    settings_factory: Callable[[], Any],
    tenant_jira_oauth_context: Callable[..., Any],
    oauth_context: Any | None = None,
) -> DecisionEngineResult:
    settings = settings_factory()
    return evaluate_decision_event(
        session=session,
        tenant=tenant,
        project=project,
        event=DecisionEventInput(
            source=source,
            event_type=f"discord_{source}",
            idempotency_key=None,
            issue_key=issue_key,
            issue_summary=issue_summary,
            issue_description=issue_description,
            issue_labels=issue_labels,
        ),
        settings=settings,
        tenant_jira_oauth_context_fn=tenant_jira_oauth_context,
        oauth_context=oauth_context,
        publish_jira_comment_fn=None,
        evaluate_pre_run_check_fn=evaluate_pre_run_check,
    )


def _decision_gate_remaining_questions_message(*, issue_key: str, reason: str, questions: list[str]) -> str:
    lines = [
        f"Decision Gate still needs clarification for `{issue_key}`.",
        f"Reason: {reason}",
    ]
    if questions:
        lines.append("Please reply with:")
        lines.extend(f"- {question}" for question in questions[:5])
    return "\n".join(lines)


def _gtd_missing_message(*, issue_key: str, missing_criteria: tuple[str, ...], questions: tuple[str, ...]) -> str:
    lines = [f"Good To Do still needs clarification for `{issue_key}`."]
    cleaned_missing = [item.strip() for item in missing_criteria if item.strip()]
    cleaned_questions = [question.strip() for question in questions if question.strip()]
    if cleaned_missing:
        lines.append("Missing criteria: " + ", ".join(cleaned_missing))
    if cleaned_questions:
        lines.append("Please reply with:")
        lines.extend(f"- {question}" for question in cleaned_questions)
    return "\n".join(lines)


def _choose_updated_summary(*, issue_key: str, current_summary: str, suggested_summary: str) -> str:
    current = str(current_summary or "").strip()
    suggested = str(suggested_summary or "").strip()[:255]
    if not current:
        return suggested or f"{issue_key} - Decision Gate clarified"
    if not suggested or suggested.lower() == current.lower():
        return current
    similarity = SequenceMatcher(None, current.lower(), suggested.lower()).ratio()
    if similarity >= 0.7 or current.lower() in suggested.lower() or suggested.lower() in current.lower():
        return suggested
    if "DG clarified" in current:
        return current
    return f"{current} | DG clarified"[:255]


def _upsert_decision_gate_clarifications_block(*, current_description: str, block: str) -> str:
    current = str(current_description or "").strip()
    if not current:
        return block
    start_idx = current.find(DECISION_GATE_BLOCK_START)
    end_idx = current.find(DECISION_GATE_BLOCK_END)
    if start_idx >= 0 and end_idx > start_idx:
        end_of_marker = end_idx + len(DECISION_GATE_BLOCK_END)
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
        citation = getattr(value, "citation", {}) if value is not None else {}
        citation_title = str(citation.get("title") or "").strip()
        citation_asset_id = str(citation.get("asset_id") or "").strip()
        citation_ts = str(citation.get("source_timestamp") or "").strip()
        confidence = float(getattr(value, "confidence", 0.0) or 0.0)
        if not slot_value:
            continue
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


def _is_knowledge_enabled_for_project(*, tenant_policy: dict, project_overrides: dict) -> tuple[bool, str]:
    effective_policy = resolve_effective_policy(
        tenant_policy=tenant_policy,
        project_overrides=project_overrides,
    )
    enabled = bool(effective_policy.get("knowledge_base_enabled", True))
    mode = str(effective_policy.get("knowledge_auto_answer_mode") or "").strip().lower()
    if mode not in {"safe", "balanced", "aggressive"}:
        mode = "aggressive"
    return enabled, mode


def _persist_precheck_questions_block(
    *,
    oauth_client,  # noqa: ANN001
    oauth_access_token: str,
    cloud_id: str,
    issue_key: str,
    issue_summary: str,
    current_description: str,
    decision_gate_reason: str | None,
    decision_gate_questions: list[str],
    gtd_questions: list[str],
) -> str:
    block = build_precheck_questions_block(
        decision_gate_reason=decision_gate_reason,
        decision_gate_questions=decision_gate_questions,
        gtd_questions=gtd_questions,
    )
    next_description = (
        upsert_precheck_questions_block(current_description=current_description, block=block)
        if block
        else remove_precheck_questions_block(current_description=current_description)
    )
    if next_description.strip() == str(current_description or "").strip():
        return current_description
    oauth_client.update_issue_summary_and_description(
        access_token=oauth_access_token,
        cloud_id=cloud_id,
        issue_id_or_key=issue_key,
        summary=issue_summary,
        description=next_description,
    )
    return next_description


def _queue_run_from_issue_context(
    *,
    session: Session,
    tenant: Tenant,
    tenant_id: str,
    project: Any,  # noqa: ANN401
    source: DecisionSource,
    issue_key: str,
    issue_summary: str | None,
    issue_description: str | None,
    issue_labels: list[str] | None,
    settings_factory: Callable[[], Any],
    tenant_jira_oauth_context: Callable[..., Any],
    conflict_prefix: str,
    success_message: str,
) -> DiscordCommandResponse:
    decision_result = _evaluate_precheck_decision_with_labels(
        session=session,
        tenant=tenant,
        project=project,
        source=source,
        issue_key=issue_key,
        issue_summary=issue_summary,
        issue_description=issue_description,
        issue_labels=issue_labels,
        settings_factory=settings_factory,
        tenant_jira_oauth_context=tenant_jira_oauth_context,
    )
    precheck_decision = decision_result.decision
    gate_block = resolve_run_gate_block(decision_result=decision_result)
    if gate_block is not None and gate_block.reason == "policy_eval_failed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=enqueue_reason_guidance("policy_eval_failed"),
        )
    if gate_block is not None and gate_block.reason == "decision_gate_required":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_decision_gate_remaining_questions_message(
                issue_key=issue_key,
                reason=str(gate_block.decision_gate_reason or "").strip(),
                questions=list(gate_block.decision_gate_questions),
            ),
        )
    if gate_block is not None and gate_block.reason == "gtd_required":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_gtd_missing_message(
                issue_key=issue_key,
                missing_criteria=gate_block.gtd_missing_criteria,
                questions=gate_block.gtd_questions,
            ),
        )
    if gate_block is not None and gate_block.reason == "missing_ready_label":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{enqueue_reason_guidance('missing_ready_label')} ({str(gate_block.ready_label or '').strip()})",
        )
    enqueue_result = enqueue_issue_run_with_precheck(
        session,
        tenant_id=tenant_id,
        project_id=project.project_id,
        issue_key=issue_key,
        issue_summary=issue_summary,
        issue_description=issue_description,
        repo_url=project.github_repository,
        delivery_id=None,
        precheck_outcome=precheck_decision.pre_check.outcome if precheck_decision.pre_check is not None else None,
        max_concurrent_runs=resolve_effective_policy(
            tenant_policy=tenant.policy_config,
            project_overrides=project.policy_overrides,
        ).get("max_concurrent_runs"),
    )
    if not enqueue_result.enqueued:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=format_enqueue_conflict_detail(
                prefix=conflict_prefix,
                enqueue_reason=str(enqueue_result.reason),
                enqueue_run_obj=enqueue_result.run,
            ),
        )
    return DiscordCommandResponse(
        ok=True,
        command="retry" if source == "discord_retry" else "run",
        message=success_message.format(run_id=enqueue_result.run.run_id, issue_key=issue_key),
        data={"run_id": enqueue_result.run.run_id, "issue_key": issue_key},
    )


def _locked_decision_gate_reason(*, classification: str, decision_gate: Any | None) -> str | None:
    if classification not in {"decision_gate", "both"}:
        return None
    return str(getattr(decision_gate, "reason", "") or "").strip() or None


def _plan_decision_gate_jira_update(
    *,
    runtime,  # noqa: ANN001
    invocation_context: CodexInvocationContext,
    issue_key: str,
    current_summary: str,
    current_description: str,
    reply_text: str,
) -> tuple[str, str]:
    def _normalize_dependencies_and_risks(value: object) -> str:
        if isinstance(value, list):
            normalized = [str(item).strip() for item in value if str(item).strip()]
            return "; ".join(normalized)
        text = str(value or "").strip()
        return text

    payload = invoke_codex_json(
        runtime=runtime,
        context=invocation_context,
        system_prompt=(
            "You extract Decision Gate clarification fields from a user reply. "
            "Return strict JSON only with keys: "
            "summary, objective, scope, acceptance_criteria, how_to_test, nfr_intent, "
            "reliability_security_constraints, out_of_scope, rollout_constraints, decision_owner, "
            "dependencies_and_risks. "
            "Do not rewrite the full ticket body."
        ),
        user_prompt=(
            "Stage: decision-gate-reply-normalization\n"
            f"Issue key: {issue_key}\n"
            f"Current summary: {current_summary}\n"
            f"Current description:\n{current_description}\n\n"
            f"User reply text:\n{reply_text}\n\n"
            "Constraints:\n"
            "- Preserve existing ticket format by outputting only extracted field values.\n"
            "- Include field text only when supported by user reply.\n"
            "- Keep summary concise (<=255 chars), close to current summary wording.\n"
        ),
    )
    if not isinstance(payload, dict):
        raise CodexRuntimeError("Codex did not return JSON object for Decision Gate update")
    updated_summary = _choose_updated_summary(
        issue_key=issue_key,
        current_summary=current_summary,
        suggested_summary=str(payload.get("summary") or "").strip(),
    )
    objective = str(payload.get("objective") or "").strip() or "Provided in thread reply."
    scope = str(payload.get("scope") or "").strip() or "Provided in thread reply."
    acceptance_criteria = str(payload.get("acceptance_criteria") or "").strip() or "Provided in thread reply."
    how_to_test = str(payload.get("how_to_test") or "").strip() or "Provided in thread reply."
    nfr_intent = str(payload.get("nfr_intent") or "").strip() or "Provided in thread reply."
    reliability_security = str(payload.get("reliability_security_constraints") or "").strip() or "Not specified."
    out_of_scope = str(payload.get("out_of_scope") or "").strip() or "Not specified."
    rollout_constraints = str(payload.get("rollout_constraints") or "").strip() or "Not specified."
    decision_owner = str(payload.get("decision_owner") or "").strip() or "Not specified."
    dependencies_and_risks = _normalize_dependencies_and_risks(
        payload.get("dependencies_and_risks")
    ) or "Not specified."
    clarification_block = "\n".join(
        [
            DECISION_GATE_BLOCK_START,
            "## Decision Gate Clarifications",
            f"Objective: {objective}",
            f"Scope: {scope}",
            f"Acceptance Criteria: {acceptance_criteria}",
            f"How to test: {how_to_test}",
            f"NFR intent (MVP vs scale-ready): {nfr_intent}",
            f"Mandatory reliability/security constraints: {reliability_security}",
            f"Explicitly out of scope: {out_of_scope}",
            f"Rollout/migration constraints: {rollout_constraints}",
            f"Dependencies / Risks: {dependencies_and_risks}",
            f"Decision owner: {decision_owner}",
            DECISION_GATE_BLOCK_END,
        ]
    )
    updated_description = _upsert_decision_gate_clarifications_block(
        current_description=current_description,
        block=clarification_block,
    )
    return updated_summary, updated_description


def dispatch_run_control_command(
    *,
    session: Session,
    tenant: Tenant,
    tenant_id: str,
    payload: DiscordCommandRequest,
    command_name: str,
    arguments: list[str],
    scope: CommandScope,
    retryable_statuses: set[str],
    resolve_project_for_issue: Callable[..., Any],
    fetch_issue_preview: Callable[..., Any],
    fetch_issue_detail: Callable[..., Any],
    settings_factory: Callable[[], Any],
    build_codex_runtime: Callable[..., Any],
    tenant_jira_oauth_context: Callable[..., Any],
    ensure_issue_is_executable: Callable[..., Any],
    resolve_codex_working_dir: Callable[..., str],
) -> DiscordCommandResponse | None:
    if command_name == "run":
        if len(arguments) != 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Usage: !run <ISSUE_KEY>")
        issue_key = arguments[0].strip().upper()
        project = resolve_project_for_issue(
            session=session,
            tenant=tenant,
            issue_key=issue_key,
        )
        if scope.project_keys and project.jira_project_key not in set(scope.project_keys):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Issue {issue_key} is outside the mapped project scope",
            )
        issue_preview = fetch_issue_preview(session=session, tenant=tenant, issue_key=issue_key)
        ensure_issue_is_executable(
            issue_status=issue_preview.status,
            tenant=tenant,
            extra_executable_statuses=("In Progress",),
        )
        issue_description: str | None = None
        issue_labels: list[str] | None = None
        try:
            issue_detail = fetch_issue_detail(session=session, tenant=tenant, issue_key=issue_key)
            refreshed_description = str(getattr(issue_detail, "description", "") or "").strip()
            if refreshed_description:
                issue_description = refreshed_description
            labels_raw = getattr(issue_detail, "labels", None)
            if isinstance(labels_raw, list):
                issue_labels = [str(label).strip() for label in labels_raw if str(label).strip()]
        except HTTPException:
            issue_description = None
            issue_labels = None
        return _queue_run_from_issue_context(
            session=session,
            tenant=tenant,
            tenant_id=tenant_id,
            project=project,
            source="discord_run",
            issue_key=issue_key,
            issue_summary=issue_preview.summary,
            issue_description=issue_description,
            issue_labels=issue_labels,
            settings_factory=settings_factory,
            tenant_jira_oauth_context=tenant_jira_oauth_context,
            conflict_prefix="Run could not be queued",
            success_message="Queued run {run_id} for {issue_key}",
        )

    if command_name == "cancel":
        if len(arguments) != 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Usage: !cancel <RUN_ID>")
        run_id = arguments[0].strip()
        run = session.get(Run, run_id)
        if run is None or run.tenant_id != tenant_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run {run_id} was not found")
        if scope.project_id and run.project_id != scope.project_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Run {run_id} is outside the mapped project scope",
            )
        cancelled = cancel_run(session, run_id=run_id, cancelled_by=payload.user_id)
        return DiscordCommandResponse(
            ok=True,
            command=command_name,
            message=f"Cancelled run {cancelled.run_id}",
            data={"run_id": cancelled.run_id, "status": cancelled.status},
        )

    if command_name == "retry":
        if len(arguments) != 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Usage: !retry <ISSUE_KEY|RUN_ID>")
        target = arguments[0].strip()
        run = session.get(Run, target)
        if run is None:
            issue_key = target.upper()
            run = session.execute(
                select(Run)
                .where(Run.tenant_id == tenant_id, Run.issue_key == issue_key)
                .order_by(Run.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
        if run is None or run.tenant_id != tenant_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No run was found for '{target}'",
            )
        if scope.project_id and run.project_id != scope.project_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Run {run.run_id} is outside the mapped project scope",
            )
        if run.status not in retryable_statuses:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Run {run.run_id} is {run.status}; only failed/blocked/cancelled runs can be retried",
            )
        issue_preview = fetch_issue_preview(session=session, tenant=tenant, issue_key=run.issue_key)
        ensure_issue_is_executable(
            issue_status=issue_preview.status,
            tenant=tenant,
            extra_executable_statuses=("In Progress",),
        )
        issue_description = run.issue_description
        issue_labels: list[str] | None = None
        try:
            issue_detail = fetch_issue_detail(session=session, tenant=tenant, issue_key=run.issue_key)
            refreshed_description = str(getattr(issue_detail, "description", "") or "").strip()
            if refreshed_description:
                issue_description = refreshed_description
            labels_raw = getattr(issue_detail, "labels", None)
            if isinstance(labels_raw, list):
                issue_labels = [str(label).strip() for label in labels_raw if str(label).strip()]
        except HTTPException:
            issue_description = run.issue_description
            issue_labels = None
        project = resolve_project_for_issue(
            session=session,
            tenant=tenant,
            issue_key=run.issue_key,
        )
        if scope.project_keys and project.jira_project_key not in set(scope.project_keys):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Issue {run.issue_key} is outside the mapped project scope",
            )
        return _queue_run_from_issue_context(
            session=session,
            tenant=tenant,
            tenant_id=tenant_id,
            project=project,
            source="discord_retry",
            issue_key=run.issue_key,
            issue_summary=issue_preview.summary,
            issue_description=issue_description,
            issue_labels=issue_labels,
            settings_factory=settings_factory,
            tenant_jira_oauth_context=tenant_jira_oauth_context,
            conflict_prefix="Retry could not be queued",
            success_message="Queued retry run {run_id} for {issue_key}",
        )

    if command_name == "reply":
        command_params = payload.command_params if isinstance(payload.command_params, dict) else {}
        issue_key = str(command_params.get("issue_key") or "").strip().upper()
        reply_text = str(command_params.get("reply_text") or "").strip()
        if not issue_key:
            if arguments:
                issue_key = arguments[0].strip().upper()
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Usage: !reply <ISSUE_KEY> <clarification text>",
                )
        if not reply_text:
            if len(arguments) < 2:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Usage: !reply <ISSUE_KEY> <clarification text>",
                )
            reply_text = " ".join(arguments[1:]).strip()
        if not issue_key or not reply_text:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Usage: !reply <ISSUE_KEY> <clarification text>",
            )
        run = session.execute(
            select(Run)
            .where(
                Run.tenant_id == tenant_id,
                Run.issue_key == issue_key,
                Run.status.in_(retryable_statuses),
            )
            .order_by(Run.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        has_retryable_run = run is not None
        settings = settings_factory()
        issue_labels: list[str] | None = None
        try:
            oauth = tenant_jira_oauth_context(session=session, tenant=tenant, settings=settings)
            oauth_client = _oauth_context_value(oauth, "client")
            oauth_connection = _oauth_context_value(oauth, "connection")
            oauth_access_token = _oauth_context_value(oauth, "access_token")
            cloud_id = getattr(oauth_connection, "cloud_id", None)
            if oauth_client is None or oauth_access_token is None or not str(cloud_id or "").strip():
                raise RuntimeError("Tenant Jira OAuth context is incomplete")
            issue_detail = oauth_client.get_issue_detail(
                access_token=oauth_access_token,
                cloud_id=str(cloud_id),
                issue_id_or_key=issue_key,
            )
            runtime = build_codex_runtime(session=session, settings=settings)
            project = resolve_project_for_issue(
                session=session,
                tenant=tenant,
                issue_key=issue_key,
            )
            codex_working_dir = resolve_codex_working_dir(
                session=session,
                tenant=tenant,
                settings=settings,
                project_id=project.project_id,
                project_keys=[project.jira_project_key],
            )
            updated_summary, updated_description = _plan_decision_gate_jira_update(
                runtime=runtime,
                invocation_context=CodexInvocationContext(
                    channel="discord",
                    tenant_id=tenant.tenant_id,
                    project_id=project.project_id,
                    command="reply",
                    stage="decision_gate_normalize",
                    working_dir=codex_working_dir,
                    issue_key=issue_key,
                ),
                issue_key=issue_key,
                current_summary=issue_detail.summary,
                current_description=issue_detail.description,
                reply_text=reply_text,
            )
            oauth_client.update_issue_summary_and_description(
                access_token=oauth_access_token,
                cloud_id=str(cloud_id),
                issue_id_or_key=issue_key,
                summary=updated_summary,
                description=updated_description,
            )
            labels_raw = getattr(issue_detail, "labels", None)
            issue_labels = [str(label).strip() for label in labels_raw if str(label).strip()] if isinstance(labels_raw, list) else None
        except (HTTPException, CodexRuntimeError, RuntimeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to update Jira context for `{issue_key}`: {exc}",
            ) from exc

        decision_result = _evaluate_precheck_decision_with_labels(
            session=session,
            tenant=tenant,
            project=project,
            source="discord_reply",
            issue_key=issue_key,
            issue_summary=updated_summary,
            issue_description=updated_description,
            issue_labels=issue_labels,
            settings_factory=settings_factory,
            tenant_jira_oauth_context=tenant_jira_oauth_context,
            oauth_context=oauth,
        )
        precheck_decision = decision_result.decision
        if precheck_decision.pre_check is None:
            return DiscordCommandResponse(
                ok=True,
                command=command_name,
                message=enqueue_reason_guidance("policy_eval_failed"),
                data={
                    "issue_key": issue_key,
                    "recheck_required": True,
                    "policy_error": True,
                },
            )
        pre_check = precheck_decision.pre_check
        classification = decision_result.classification
        missing_slots = decision_result.missing_slots
        auto_resolved_slots = list(decision_result.auto_resolved_slots)

        if classification != "clear":
            decision_gate = getattr(pre_check, "decision_gate", None)
            locked_decision_gate_reason = _locked_decision_gate_reason(
                classification=classification,
                decision_gate=decision_gate,
            )
            decision_gate_questions = [
                question.strip()
                for question in getattr(decision_gate, "questions", ())
                if str(question).strip()
            ]
            gtd_questions = [
                question.strip()
                for question in getattr(pre_check, "gtd_clarification_questions", ())
                if str(question).strip()
            ]
            gtd_missing = [
                item.strip()
                for item in getattr(pre_check, "gtd_missing_criteria", ())
                if str(item).strip()
            ]
            try:
                updated_description = _persist_precheck_questions_block(
                    oauth_client=oauth_client,
                    oauth_access_token=str(oauth_access_token),
                    cloud_id=str(cloud_id),
                    issue_key=issue_key,
                    issue_summary=updated_summary,
                    current_description=str(updated_description or ""),
                    decision_gate_reason=locked_decision_gate_reason,
                    decision_gate_questions=decision_gate_questions,
                    gtd_questions=gtd_questions,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "discord_reply_precheck_questions_persist_failed tenant_id=%s issue_key=%s error=%s",
                    tenant.tenant_id,
                    issue_key,
                    exc,
                )
            message, generated_questions = build_precheck_message(
                runtime=runtime,
                invocation_context=CodexInvocationContext(
                    channel="discord",
                    tenant_id=tenant.tenant_id,
                    project_id=project.project_id,
                    command="reply",
                    stage="precheck_message",
                    working_dir=codex_working_dir,
                    issue_key=issue_key,
                ),
                issue_key=issue_key,
                classification=classification,
                decision_gate_reason=str(getattr(decision_gate, "reason", "") or "").strip(),
                decision_gate_questions=decision_gate_questions,
                gtd_missing_criteria=gtd_missing,
                gtd_questions=gtd_questions,
                missing_slots=missing_slots,
            )
            return DiscordCommandResponse(
                ok=True,
                command=command_name,
                message=message,
                data={
                    "issue_key": issue_key,
                    "recheck_required": True,
                    "classification": classification,
                    "decision_gate_reason": str(getattr(decision_gate, "reason", "") or "").strip() or None,
                    "gtd_missing_criteria": gtd_missing,
                    "questions": generated_questions or decision_gate_questions or gtd_questions,
                    "missing_slots": missing_slots,
                    "auto_resolved_slots": auto_resolved_slots,
                    "knowledge_mode": None,
                },
            )

        try:
            updated_description = _persist_precheck_questions_block(
                oauth_client=oauth_client,
                oauth_access_token=str(oauth_access_token),
                cloud_id=str(cloud_id),
                issue_key=issue_key,
                issue_summary=updated_summary,
                current_description=str(updated_description or ""),
                decision_gate_reason=None,
                decision_gate_questions=[],
                gtd_questions=[],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "discord_reply_precheck_questions_cleanup_failed tenant_id=%s issue_key=%s error=%s",
                tenant.tenant_id,
                issue_key,
                exc,
            )

        issue_preview = fetch_issue_preview(session=session, tenant=tenant, issue_key=issue_key)
        ensure_issue_is_executable(
            issue_status=issue_preview.status,
            tenant=tenant,
            extra_executable_statuses=("In Progress",),
        )
        return _queue_run_from_issue_context(
            session=session,
            tenant=tenant,
            tenant_id=tenant_id,
            project=project,
            source="discord_retry" if has_retryable_run else "discord_run",
            issue_key=issue_key,
            issue_summary=issue_preview.summary,
            issue_description=updated_description,
            issue_labels=issue_labels,
            settings_factory=settings_factory,
            tenant_jira_oauth_context=tenant_jira_oauth_context,
            conflict_prefix="Retry could not be queued" if has_retryable_run else "Run could not be queued",
            success_message=(
                "Queued retry run {run_id} for {issue_key}"
                if has_retryable_run
                else "Queued run {run_id} for {issue_key}"
            ),
        )

    return None
