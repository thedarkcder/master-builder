from __future__ import annotations

from orchestrator.core.codex_runtime import CodexRuntime, CodexRuntimeError
from orchestrator.core.prompt_templates import render_prompt
from orchestrator.core.runtime_invocation import AgentInvocationContext, invoke_runtime_json
from orchestrator.core.runtime_payload_models import JiraIssueIntakeRoutePayload


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
) -> JiraIssueIntakeRoutePayload:
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
    try:
        return JiraIssueIntakeRoutePayload.from_payload(payload)
    except RuntimeError as exc:
        raise CodexRuntimeError(str(exc)) from exc
