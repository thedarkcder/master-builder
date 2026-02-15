from __future__ import annotations

from collections.abc import Iterable

DEFAULT_WORKER_CAPABILITY = "linux"
WORKER_CAPABILITY_LABEL_PREFIX = "worker:"

KNOWN_WORKER_CAPABILITIES = {
    "linux",
    "macos",
}

_MACOS_HINTS = (
    "ios",
    "iphone",
    "ipad",
    "swift",
    "swiftui",
    "xcode",
    "xctest",
    "storekit",
    "testflight",
    "cocoapods",
)


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
) -> str:
    for label in issue_labels or []:
        normalized_label = str(label or "").strip().lower()
        if not normalized_label.startswith(WORKER_CAPABILITY_LABEL_PREFIX):
            continue
        explicit = normalize_worker_capability(
            normalized_label[len(WORKER_CAPABILITY_LABEL_PREFIX) :]
        )
        if explicit is not None:
            return explicit

    combined = f"{issue_summary or ''}\n{issue_description or ''}".lower()
    if any(token in combined for token in _MACOS_HINTS):
        return "macos"
    return DEFAULT_WORKER_CAPABILITY


def required_worker_capability_for_run(run) -> str:  # noqa: ANN001
    plan = run.plan if isinstance(run.plan, dict) else {}
    plan_required = normalize_worker_capability(plan.get("required_worker_capability"))
    if plan_required is not None:
        return plan_required
    return infer_required_worker_capability(
        issue_summary=run.issue_summary,
        issue_description=run.issue_description,
        issue_labels=None,
    )
