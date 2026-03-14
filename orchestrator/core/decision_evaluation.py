from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
from typing import Any, Callable

from sqlalchemy.orm import Session

from orchestrator.core.decision_types import DecisionSource, IngressDecision
from orchestrator.core.pre_run_check import evaluate_pre_run_check
from orchestrator.storage.models import Project, Tenant

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EvaluationResult:
    decision: IngressDecision
    issue_labels: list[str]



def evaluate_with_labels(
    *,
    session: Session,
    tenant: Tenant,
    project: Project | None,
    source: DecisionSource,
    issue_key: str,
    issue_summary: str | None,
    issue_description: str | None,
    recorded_answers: list[dict[str, str]] | None,
    issue_labels: list[str] | None,
    settings,  # noqa: ANN001
    tenant_jira_oauth_context_fn: Callable[..., Any],
    evaluate_ingress_precheck_fn: Callable[..., IngressDecision],
    oauth_context: Any | None = None,
    evaluate_pre_run_check_fn: Callable[..., object] = evaluate_pre_run_check,
) -> EvaluationResult:
    from orchestrator.core.label_action_service import apply_issue_label_actions

    decision = evaluate_ingress_precheck_fn(
        source=source,
        tenant_id=tenant.tenant_id,
        project_id=project.project_id if project is not None else None,
        issue_key=issue_key,
        issue_summary=issue_summary,
        issue_description=issue_description,
        recorded_answers=recorded_answers,
        issue_labels=issue_labels,
        ready_label=(tenant.jira_config or {}).get("ready_label"),
        evaluate_pre_run_check_fn=evaluate_pre_run_check_fn,
    )
    normalized_labels = [str(label).strip() for label in (issue_labels or []) if str(label).strip()]
    if decision.pre_check is None:
        return EvaluationResult(decision=decision, issue_labels=normalized_labels)

    apply_result = apply_issue_label_actions(
        session=session,
        tenant=tenant,
        project_policy_overrides=project.policy_overrides if project is not None else {},
        issue_key=issue_key,
        existing_labels=normalized_labels,
        actions=decision.label_actions,
        settings=settings,
        tenant_jira_oauth_context_fn=tenant_jira_oauth_context_fn,
        oauth_context=oauth_context,
        logger=logger,
    )
    label_set = {label.casefold() for label in normalized_labels}
    for label in apply_result.applied_labels:
        if label.casefold() in label_set:
            continue
        normalized_labels.append(label)
        label_set.add(label.casefold())
    resolved = decision.with_applied_labels(list(apply_result.applied_labels))
    return EvaluationResult(decision=resolved, issue_labels=normalized_labels)



def issue_fingerprint(
    *,
    issue_summary: str | None,
    issue_description: str | None,
    issue_labels: list[str],
) -> str:
    payload = json.dumps(
        {
            "summary": str(issue_summary or "").strip(),
            "description": str(issue_description or "").strip(),
            "labels": sorted({label.casefold() for label in issue_labels}),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
