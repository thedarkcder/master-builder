from __future__ import annotations

from dataclasses import dataclass
import json

from orchestrator.core.codex_invocation import CodexInvocationContext, invoke_codex_json
from orchestrator.core.codex_runtime import CodexRuntimeError, build_codex_runtime
from orchestrator.core.config import get_settings
from orchestrator.core.prompt_templates import render_prompt
from orchestrator.tools.github_app import WorkflowCheckSuite


@dataclass(frozen=True)
class PrReadinessResult:
    ready: bool
    state: str
    reason: str
    missing_workflows: tuple[str, ...]
    pending_workflows: tuple[str, ...]
    failing_workflows: tuple[str, ...]
    missing_review_sections: tuple[str, ...] = ()


def _string_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _workflow_checks_payload(workflow_checks: list[WorkflowCheckSuite]) -> list[dict[str, str | None]]:
    return [
        {
            "name": check.name,
            "status": check.status,
            "conclusion": check.conclusion,
        }
        for check in workflow_checks
    ]


def _evaluate_pr_readiness_with_codex(
    *,
    review_summary_markdown: str | None,
    required_workflows: tuple[str, ...],
    workflow_checks: list[WorkflowCheckSuite],
    tenant_id: str | None,
    project_id: str | None,
) -> PrReadinessResult:
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
                stage="pr_ready",
                working_dir=".",
            ),
            system_prompt=render_prompt("policy/pr_ready_system.j2"),
            user_prompt=render_prompt(
                "policy/pr_ready_user.j2",
                review_summary_markdown=(review_summary_markdown or ""),
                required_workflows_json=json.dumps(required_workflows),
                workflow_checks_json=json.dumps(_workflow_checks_payload(workflow_checks)),
            ),
        )
    except CodexRuntimeError as exc:
        raise RuntimeError(f"Codex PR readiness evaluation failed: {exc}") from exc

    ready = bool(payload.get("ready"))
    state = str(payload.get("state") or "").strip()
    reason = str(payload.get("reason") or "").strip()
    missing_workflows = _string_list(payload.get("missing_workflows"))
    pending_workflows = _string_list(payload.get("pending_workflows"))
    failing_workflows = _string_list(payload.get("failing_workflows"))
    missing_review_sections = _string_list(payload.get("missing_review_sections"))

    if not state or not reason:
        raise RuntimeError("Codex PR readiness evaluation returned incomplete payload")

    return PrReadinessResult(
        ready=ready,
        state=state,
        reason=reason,
        missing_workflows=missing_workflows,
        pending_workflows=pending_workflows,
        failing_workflows=failing_workflows,
        missing_review_sections=missing_review_sections,
    )


def evaluate_pr_readiness(
    *,
    review_summary_markdown: str | None,
    required_workflows: tuple[str, ...],
    workflow_checks: list[WorkflowCheckSuite],
    tenant_id: str | None = None,
    project_id: str | None = None,
) -> PrReadinessResult:
    return _evaluate_pr_readiness_with_codex(
        review_summary_markdown=review_summary_markdown,
        required_workflows=required_workflows,
        workflow_checks=workflow_checks,
        tenant_id=tenant_id,
        project_id=project_id,
    )
