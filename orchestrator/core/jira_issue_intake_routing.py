from __future__ import annotations

from orchestrator.core.codex_runtime import CodexRuntime, CodexRuntimeError
from orchestrator.core.prompt_templates import render_prompt
from orchestrator.core.runtime_invocation import AgentInvocationContext, invoke_runtime_json

_VALID_INTAKE_ROUTES = {"pm_parent", "engineering_child", "unclear"}
_VALID_CONFIDENCE_LEVELS = {"high", "medium", "low"}


def classify_jira_issue_intake_with_runtime(
    *,
    runtime: CodexRuntime,
    issue_key: str,
    issue_summary: str,
    issue_description: str,
    issue_status: str | None,
    issue_labels: list[str] | tuple[str, ...],
    webhook_event: str | None,
    invocation_context: AgentInvocationContext,
) -> dict[str, str]:
    payload = invoke_runtime_json(
        runtime=runtime,
        context=invocation_context,
        system_prompt=render_prompt("jira/issue_intake_routing_system.j2"),
        user_prompt=render_prompt(
            "jira/issue_intake_routing_user.j2",
            issue_key=issue_key,
            issue_summary=issue_summary,
            issue_description=issue_description,
            issue_status=issue_status or "",
            issue_labels=list(issue_labels or ()),
            webhook_event=webhook_event or "",
        ),
    )
    if not isinstance(payload, dict):
        raise CodexRuntimeError("Codex did not return a Jira issue intake routing JSON object")
    route = str(payload.get("route") or "").strip().lower()
    if route not in _VALID_INTAKE_ROUTES:
        raise CodexRuntimeError("Codex returned an invalid Jira issue intake route")
    reason = str(payload.get("reason") or "").strip()
    if not reason:
        raise CodexRuntimeError("Codex did not explain the Jira issue intake route")
    confidence = str(payload.get("confidence") or "").strip().lower()
    if confidence not in _VALID_CONFIDENCE_LEVELS:
        confidence = "medium"
    return {
        "route": route,
        "reason": reason,
        "confidence": confidence,
    }
