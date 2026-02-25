from __future__ import annotations

from collections.abc import Iterable

DEFAULT_WORKER_CAPABILITY = "linux"
WORKER_CAPABILITY_LABEL_PREFIX = "worker:"

KNOWN_WORKER_CAPABILITIES = {
    "linux",
    "macos",
}


def normalize_worker_capability(value: object) -> str | None:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return None
    if normalized in {"mac", "darwin", "osx", "macos"}:
        return "macos"
    if normalized in {"linux", "ubuntu", "debian", "alpine"}:
        return "linux"
    if normalized in KNOWN_WORKER_CAPABILITIES:
        return normalized
    return None


def worker_label_for_capability(capability: str) -> str:
    normalized = normalize_worker_capability(capability) or DEFAULT_WORKER_CAPABILITY
    return f"{WORKER_CAPABILITY_LABEL_PREFIX}{normalized}"


def parse_worker_capabilities(raw_value: object) -> set[str]:
    if isinstance(raw_value, str):
        values = [item.strip() for item in raw_value.split(",")]
    elif isinstance(raw_value, Iterable):
        values = [str(item).strip() for item in raw_value]
    else:
        values = []
    normalized = {
        cap
        for cap in (normalize_worker_capability(item) for item in values)
        if cap is not None
    }
    if not normalized:
        normalized.add(DEFAULT_WORKER_CAPABILITY)
    return normalized


def infer_required_worker_capability(
    *,
    issue_summary: str | None,
    issue_description: str | None,
    issue_labels: list[str] | None,
    tenant_id: str | None = None,
    project_id: str | None = None,
    issue_key: str | None = None,
    run_id: str | None = None,
) -> str:
    _ = issue_summary
    _ = issue_description
    _ = tenant_id
    _ = project_id
    _ = issue_key
    _ = run_id
    normalized_labels = [str(label).strip() for label in (issue_labels or []) if str(label).strip()]
    for label in normalized_labels:
        if not label.lower().startswith(f"{WORKER_CAPABILITY_LABEL_PREFIX}"):
            continue
        requested = normalize_worker_capability(label.split(":", 1)[1])
        if requested is not None:
            return requested
    return DEFAULT_WORKER_CAPABILITY


def required_worker_capability_for_run(run) -> str | None:  # noqa: ANN001
    plan = run.plan if isinstance(run.plan, dict) else {}
    plan_required = normalize_worker_capability(plan.get("required_worker_capability"))
    if plan_required is not None:
        return plan_required
    return None
