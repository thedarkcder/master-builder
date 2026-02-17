from __future__ import annotations

from collections.abc import Iterable
import json

from orchestrator.core.codex_invocation import CodexInvocationContext, invoke_codex_json
from orchestrator.core.codex_runtime import CodexRuntimeError, build_codex_runtime
from orchestrator.core.config import get_settings
from orchestrator.core.prompt_templates import render_prompt

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


def _infer_required_worker_capability_with_codex(
    *,
    issue_summary: str,
    issue_description: str,
    issue_labels: list[str],
    tenant_id: str | None,
    project_id: str | None,
    issue_key: str | None,
    run_id: str | None,
) -> str:
    settings = get_settings()
    runtime = build_codex_runtime(session=None, settings=settings)
    try:
        payload = invoke_codex_json(
            runtime=runtime,
            context=CodexInvocationContext(
                channel="system",
                tenant_id=tenant_id,
                project_id=project_id,
                command="policy",
                stage="worker_capability",
                working_dir=".",
                issue_key=issue_key,
                run_id=run_id,
                reasoning_effort="low",
                issue_description_chars=len(issue_description or ""),
            ),
            system_prompt=render_prompt("policy/worker_capability_system.j2"),
            user_prompt=render_prompt(
                "policy/worker_capability_user.j2",
                issue_summary=issue_summary,
                issue_description=issue_description,
                issue_labels_json=json.dumps(issue_labels),
            ),
        )
    except CodexRuntimeError as exc:
        raise RuntimeError(f"Codex worker capability evaluation failed: {exc}") from exc

    capability = normalize_worker_capability(payload.get("required_worker_capability"))
    if capability is None:
        raise RuntimeError("Codex worker capability evaluation returned invalid required_worker_capability")
    return capability


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
    return _infer_required_worker_capability_with_codex(
        issue_summary=(issue_summary or "").strip(),
        issue_description=(issue_description or "").strip(),
        issue_labels=[str(label).strip() for label in (issue_labels or []) if str(label).strip()],
        tenant_id=tenant_id,
        project_id=project_id,
        issue_key=issue_key,
        run_id=run_id,
    )


def required_worker_capability_for_run(run) -> str:  # noqa: ANN001
    plan = run.plan if isinstance(run.plan, dict) else {}
    plan_required = normalize_worker_capability(plan.get("required_worker_capability"))
    if plan_required is not None:
        return plan_required
    return infer_required_worker_capability(
        issue_summary=run.issue_summary,
        issue_description=run.issue_description,
        issue_labels=None,
        tenant_id=run.tenant_id,
        project_id=run.project_id,
        issue_key=run.issue_key,
        run_id=run.run_id,
    )
