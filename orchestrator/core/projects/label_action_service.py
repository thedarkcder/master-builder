from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from orchestrator.core.decision.engine import DecisionLabelAction
from orchestrator.core.projects.policy import resolve_effective_policy


@dataclass(frozen=True)
class LabelActionApplyResult:
    applied_labels: tuple[str, ...]
    skipped_reason: str | None


def apply_issue_label_actions(
    *,
    session,  # noqa: ANN001
    tenant,  # noqa: ANN001
    project_policy_overrides: dict[str, object] | None,
    issue_key: str,
    existing_labels: list[str] | None,
    actions: tuple[DecisionLabelAction, ...],
    settings,  # noqa: ANN001
    tenant_atlassian_oauth_context_fn: Callable[..., Any],
    oauth_context: Any | None = None,
    logger,  # noqa: ANN001
) -> LabelActionApplyResult:
    if not actions:
        return LabelActionApplyResult(applied_labels=(), skipped_reason=None)

    effective_policy = resolve_effective_policy(
        tenant_policy=tenant.policy_config,
        project_overrides=project_policy_overrides or {},
    )
    if not bool(effective_policy.get("allow_label_mutations", True)):
        return LabelActionApplyResult(applied_labels=(), skipped_reason="label_mutations_disabled")

    normalized_existing = {
        str(label).strip().casefold()
        for label in (existing_labels or [])
        if str(label).strip()
    }
    labels_to_apply: list[str] = []
    seen: set[str] = set()
    for action in actions:
        normalized = action.label.strip().casefold()
        if not normalized or normalized in normalized_existing or normalized in seen:
            continue
        labels_to_apply.append(action.label.strip())
        seen.add(normalized)
    if not labels_to_apply:
        return LabelActionApplyResult(applied_labels=(), skipped_reason=None)

    try:
        oauth = oauth_context or tenant_atlassian_oauth_context_fn(
            session=session,
            tenant=tenant,
            settings=settings,
        )
        oauth_client = _oauth_context_value(oauth, "client")
        oauth_connection = _oauth_context_value(oauth, "connection")
        oauth_access_token = _oauth_context_value(oauth, "access_token")
        cloud_id = getattr(oauth_connection, "cloud_id", None)
        if oauth_client is None or oauth_access_token is None or not str(cloud_id or "").strip():
            raise RuntimeError("Tenant Atlassian context is incomplete")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "label_actions_oauth_context_failed tenant_id=%s issue_key=%s error=%s",
            tenant.tenant_id,
            issue_key,
            exc,
        )
        return LabelActionApplyResult(applied_labels=(), skipped_reason="oauth_context_failed")

    applied_labels: list[str] = []
    for label in labels_to_apply:
        try:
            oauth_client.add_issue_labels(
                access_token=oauth_access_token,
                cloud_id=str(cloud_id),
                issue_id_or_key=issue_key,
                labels=[label],
            )
            applied_labels.append(label)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "label_actions_apply_failed tenant_id=%s issue_key=%s label=%s error=%s",
                tenant.tenant_id,
                issue_key,
                label,
                exc,
            )
    return LabelActionApplyResult(applied_labels=tuple(applied_labels), skipped_reason=None)


def _oauth_context_value(oauth_context: Any, field: str) -> Any:
    if isinstance(oauth_context, dict):
        return oauth_context.get(field)
    return getattr(oauth_context, field, None)

