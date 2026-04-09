from __future__ import annotations

DEPLOYMENT_RELEASE_ACTIVE_STATUSES = frozenset({"queued", "provisioning", "deploying"})
DEPLOYMENT_RELEASE_TERMINAL_STATUSES = frozenset({"live", "failed", "rolled_back"})

_COOLIFY_SUCCESS_STATUSES = {
    "completed",
    "complete",
    "done",
    "finished",
    "success",
    "succeeded",
    "healthy",
    "healthy_running",
}
_COOLIFY_FAILURE_STATUSES = {
    "crashed",
    "crash",
    "error",
    "failed",
    "failure",
    "unhealthy",
    "timeout",
}
_COOLIFY_CANCELLED_STATUSES = {
    "cancelled",
    "canceled",
    "cancelled_by_user",
    "rollback",
    "rolled_back",
}
_COOLIFY_PROGRESS_STATUSES = {
    "building",
    "created",
    "deploying",
    "in_progress",
    "pending",
    "preparing",
    "queued",
    "running",
    "starting",
    "started",
    "updating",
}


def normalize_deployment_status(value: object) -> str | None:
    normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return normalized or None


def classify_coolify_deployment_status(value: object) -> str | None:
    normalized = normalize_deployment_status(value)
    if normalized is None:
        return None
    if normalized in _COOLIFY_SUCCESS_STATUSES:
        return "success"
    if normalized in _COOLIFY_FAILURE_STATUSES:
        return "failure"
    if normalized in _COOLIFY_CANCELLED_STATUSES:
        return "cancelled"
    if normalized in _COOLIFY_PROGRESS_STATUSES:
        return "progress"
    return "unknown"


def resolve_polling_release_status(*, current_status: str, observed_status: object) -> str | None:
    normalized_current = normalize_deployment_status(current_status)
    if normalized_current in DEPLOYMENT_RELEASE_TERMINAL_STATUSES:
        return None

    coolify_status = classify_coolify_deployment_status(observed_status)
    if coolify_status == "success":
        return "live"
    if coolify_status == "failure":
        return "failed"
    if coolify_status == "cancelled":
        return "rolled_back"
    if coolify_status == "progress":
        if normalized_current == "queued":
            return "provisioning"
        if normalized_current == "provisioning":
            return "deploying"
        if normalized_current == "deploying":
            return None
    return None


def resolve_event_release_status(
    *,
    current_status: str,
    event_type: object,
    observed_status: object | None = None,
) -> str | None:
    normalized_event = normalize_deployment_status(event_type)
    if normalized_event is not None:
        if "success" in normalized_event:
            return "live"
        if "fail" in normalized_event or "error" in normalized_event:
            return "failed"
        if "rollback" in normalized_event or "cancel" in normalized_event:
            return "rolled_back"
        if "deploy" in normalized_event or "container" in normalized_event or "status" in normalized_event:
            normalized_current = normalize_deployment_status(current_status)
            if normalized_current == "queued":
                return "provisioning"
            if normalized_current == "provisioning":
                return "deploying"

    if observed_status is not None:
        return resolve_polling_release_status(current_status=current_status, observed_status=observed_status)
    return None
