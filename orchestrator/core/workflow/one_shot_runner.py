from __future__ import annotations

import json
from collections.abc import Callable

from orchestrator.core.codex_invocation import CodexInvocationContext, invoke_codex_json
from orchestrator.core.codex_runtime import CodexRuntime
from orchestrator.core.config import get_settings
from orchestrator.core.prompt_templates import render_prompt
from orchestrator.core.agent_tools import allowed_tools_for_stage
from orchestrator.core.workflow.runner import PmPlan, WorkflowRequest, WorkflowResult


class OneShotWorkflowExecutor:
    def __init__(self, *, runtime: CodexRuntime, log_sink: Callable[[dict], None] | None = None):
        self._runtime = runtime
        self._log_sink = log_sink

    def execute(
        self,
        request: WorkflowRequest,
        *,
        test_feedback_hook: Callable[[int, str], None] | None = None,
    ) -> WorkflowResult:
        _ = test_feedback_hook
        payload = invoke_codex_json(
            runtime=self._runtime,
            context=CodexInvocationContext(
                channel="worker",
                tenant_id=request.tenant_id,
                project_id=request.project_id,
                command="workflow",
                stage="one_shot",
                working_dir=request.execution_repo_dir or ".",
                issue_key=request.issue_key,
                run_id=request.run_id,
                attempt=1,
                reasoning_effort="medium",
                issue_description_chars=len(request.issue_description or ""),
            ),
            system_prompt=render_prompt("workflow/one_shot_system.j2"),
            user_prompt=render_prompt(
                "workflow/one_shot_user.j2",
                tenant_id=request.tenant_id,
                project_id=request.project_id or "",
                run_id=request.run_id,
                issue_key=request.issue_key,
                issue_summary=request.issue_summary,
                issue_description=request.issue_description,
                suggested_test_commands_json=json.dumps(request.suggested_test_commands),
                base_branch=request.base_branch or "main",
                integration_branch=request.integration_branch or f"feature/{request.issue_key}",
                pr_target_branch=request.pr_target_branch or request.base_branch or "main",
                max_parallel_workstreams=min(
                    5,
                    max(1, int(get_settings().workflow_max_parallel_workstreams)),
                ),
                current_worker_capability=request.current_worker_capability,
                available_worker_capabilities_json=json.dumps(request.available_worker_capabilities),
                pr_number=request.pr_number or 0,
                trigger_context_json=json.dumps(request.trigger_context or {}),
                allowed_tools_json=json.dumps(sorted(allowed_tools_for_stage("orchestrator"))),
                agent_tool_command=(
                    "python -m orchestrator agent-tool "
                    f"--tenant {request.tenant_id} "
                    f"--project {request.project_id or ''} "
                    f"--run {request.run_id} "
                    f"--issue {request.issue_key} "
                    "--stage orchestrator --tool <tool_name> --args '<json-object>'"
                ),
            ),
            extra_on_log_line=self._stage_log_sink(request=request),
            require_json=True,
        )
        return _payload_to_result(request=request, payload=payload)

    def _stage_log_sink(
        self,
        *,
        request: WorkflowRequest,
    ) -> Callable[[str, str], None] | None:
        if self._log_sink is None:
            return None

        def _emit(stream: str, message: str) -> None:
            self._log_sink(
                {
                    "tenant_id": request.tenant_id,
                    "project_id": request.project_id,
                    "run_id": request.run_id,
                    "issue_key": request.issue_key,
                    "stage": "one_shot",
                    "attempt": 1,
                    "stream": stream,
                    "message": message,
                }
            )

        return _emit


def _payload_to_result(*, request: WorkflowRequest, payload: dict) -> WorkflowResult:
    status = str(payload.get("status") or "").strip().lower()
    summary = _string_list(payload.get("summary"), fallback=["One-shot workflow completed"])
    pr_url = _optional_non_empty(payload.get("pr_url"))
    review_findings = _string_list(payload.get("review_findings"), fallback=[])
    workstreams = payload.get("workstreams")
    plan_steps = _string_list(payload.get("plan_steps"), fallback=[])
    acceptance_criteria = _string_list(payload.get("acceptance_criteria"), fallback=[])
    risks = _string_list(payload.get("risks"), fallback=[])
    merge_order = _string_list(payload.get("merge_order"), fallback=[])
    test_guidance = _string_list(
        payload.get("test_guidance"),
        fallback=request.suggested_test_commands or ["Run relevant project tests"],
    )
    plan = PmPlan(
        plan_steps=plan_steps or ["Execute one-shot multi-agent workflow"],
        acceptance_criteria=acceptance_criteria or ["Deliver validated PR for ticket scope"],
        risks=risks,
        next_stage="dev",
        execution_worker_capability=request.current_worker_capability,
    )

    if status == "approved" and pr_url:
        return WorkflowResult(
            succeeded=True,
            plan=plan,
            pr_url=pr_url,
            summary=summary,
            test_guidance=test_guidance,
            attempts=1,
            dev_rationale=_extract_workstream_summaries(workstreams),
            review_summary=review_findings or summary,
            review_feedback=None,
        )

    feedback = _optional_non_empty(payload.get("feedback")) or (
        "One-shot workflow did not produce an approved PR result."
    )
    diagnostics_history: list[dict[str, str]] = []
    if merge_order:
        diagnostics_history.append({"stage": "integrator", "attempt": "1", "event": f"merge_order={','.join(merge_order)}"})
    if review_findings:
        diagnostics_history.extend(
            {"stage": "review", "attempt": "1", "event": finding} for finding in review_findings
        )
    return WorkflowResult(
        succeeded=False,
        plan=plan,
        pr_url=None,
        summary=[],
        test_guidance=test_guidance,
        attempts=1,
        dev_rationale=_extract_workstream_summaries(workstreams),
        review_summary=review_findings,
        review_feedback=feedback,
        diagnostics=None if status == "approved" else _build_diagnostics(status=status, feedback=feedback, history=diagnostics_history),
    )


def _build_diagnostics(*, status: str, feedback: str, history: list[dict[str, str]]):
    from orchestrator.core.workflow.runner import WorkflowDiagnostics

    normalized_status = status if status in {"needs_changes", "blocked"} else "workflow"
    return WorkflowDiagnostics(
        stage=normalized_status,
        message=feedback,
        attempts=1,
        history=history,
    )


def _extract_workstream_summaries(workstreams: object) -> list[str]:
    if not isinstance(workstreams, list):
        return []
    summaries: list[str] = []
    for item in workstreams:
        if not isinstance(item, dict):
            continue
        name = _optional_non_empty(item.get("name")) or _optional_non_empty(item.get("branch")) or "workstream"
        item_summary = _optional_non_empty(item.get("summary")) or _optional_non_empty(item.get("result"))
        if item_summary:
            summaries.append(f"{name}: {item_summary}")
        else:
            summaries.append(name)
    return summaries


def _string_list(value: object, *, fallback: list[str]) -> list[str]:
    if isinstance(value, list):
        normalized = [str(item).strip() for item in value if str(item).strip()]
        if normalized:
            return normalized
    return list(fallback)


def _optional_non_empty(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None
