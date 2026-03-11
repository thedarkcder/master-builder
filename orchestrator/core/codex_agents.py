from __future__ import annotations

import json
import re
from collections.abc import Callable

from orchestrator.core.codex_invocation import CodexInvocationContext, invoke_codex_json
from orchestrator.core.codex_runtime import CodexRuntime, CodexRuntimeError
from orchestrator.core.prompt_templates import render_prompt
from orchestrator.core.agent_tools import allowed_tools_for_stage
from orchestrator.core.worker_capabilities import normalize_worker_capability
from orchestrator.core.workflow.runner import (
    DevResult,
    PmPlan,
    ReviewResult,
    TestResult,
    WorkflowRequest,
)


class CodexWorkflowAgents:
    def __init__(self, *, runtime: CodexRuntime, log_sink: Callable[[dict], None] | None = None):
        self._runtime = runtime
        self._log_sink = log_sink

    def _stage_log_sink(
        self,
        *,
        request: WorkflowRequest,
        stage: str,
        attempt: int | None,
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
                    "stage": stage,
                    "attempt": attempt,
                    "stream": stream,
                    "message": message,
                }
            )

        return _emit

    def pm(
        self,
        request: WorkflowRequest,
        attempt: int,
        feedback: str | None,
        history: list[dict[str, str]],
        last_dev_result: DevResult | None,
        last_test_result: TestResult | None,
        last_review_result: ReviewResult | None,
    ) -> PmPlan:
        payload = invoke_codex_json(
            runtime=self._runtime,
            context=CodexInvocationContext(
                channel="worker",
                tenant_id=request.tenant_id,
                project_id=request.project_id,
                command="workflow",
                stage="pm",
                working_dir=request.execution_repo_dir or ".",
                issue_key=request.issue_key,
                run_id=request.run_id,
                attempt=attempt,
                reasoning_effort="medium",
                issue_description_chars=len(request.issue_description or ""),
            ),
            system_prompt=render_prompt("workflow/pm_system.j2"),
            user_prompt=render_prompt(
                "workflow/pm_user.j2",
                tenant_id=request.tenant_id,
                run_id=request.run_id,
                issue_key=request.issue_key,
                issue_summary=request.issue_summary,
                issue_description=request.issue_description,
                attempt=attempt,
                feedback=feedback or "none",
                history_json=json.dumps(history[-25:]),
                last_dev_summary_json=json.dumps(last_dev_result.change_summary if last_dev_result else []),
                last_dev_pr_url=last_dev_result.pr_url if last_dev_result and last_dev_result.pr_url else "none",
                last_test_passed=(
                    "none"
                    if last_test_result is None
                    else ("true" if last_test_result.passed else "false")
                ),
                last_test_feedback=last_test_result.feedback if last_test_result and last_test_result.feedback else "none",
                last_test_guidance_json=json.dumps(last_test_result.guidance if last_test_result else []),
                last_review_approved=(
                    "none"
                    if last_review_result is None
                    else ("true" if last_review_result.approved else "false")
                ),
                last_review_feedback=(
                    last_review_result.feedback
                    if last_review_result and last_review_result.feedback
                    else "none"
                ),
                last_review_summary_json=json.dumps(last_review_result.summary if last_review_result else []),
                current_worker_capability=request.current_worker_capability,
                available_worker_capabilities_json=json.dumps(request.available_worker_capabilities),
                allowed_tools_json=json.dumps(sorted(allowed_tools_for_stage("pm"))),
                agent_tool_command=(
                    "python -m orchestrator agent-tool "
                    f"--tenant {request.tenant_id} "
                    f"--project {request.project_id or ''} "
                    f"--run {request.run_id} "
                    f"--issue {request.issue_key} "
                    "--stage pm --tool <tool_name> --args '<json-object>'"
                ),
            ),
            extra_on_log_line=self._stage_log_sink(request=request, stage="pm", attempt=attempt),
            require_json=False,
        )
        raw_response = _extract_raw_response(payload)
        next_stage = str(payload.get("next_stage") or _extract_next_stage(raw_response) or "dev").strip().lower()
        if next_stage not in {"dev", "test"}:
            next_stage = "dev"
        execution_worker_capability = (
            normalize_worker_capability(payload.get("execution_worker_capability"))
            or _extract_worker_capability(raw_response)
            or normalize_worker_capability(request.current_worker_capability)
            or "linux"
        )
        return PmPlan(
            plan_steps=_string_list(
                payload.get("plan_steps"),
                fallback=_extract_marked_items(raw_response, max_items=5) or ["Analyze scope", "Implement", "Validate"],
            ),
            acceptance_criteria=_string_list(
                payload.get("acceptance_criteria"),
                fallback=(
                    _extract_marked_items(raw_response, max_items=4)
                    or ["Behavior implemented", "Tests and verification provided"]
                ),
            ),
            risks=_string_list(
                payload.get("risks"),
                fallback=_extract_marked_items(raw_response, max_items=3),
            ),
            next_stage=next_stage,
            execution_worker_capability=execution_worker_capability,
            missing_evidence_sources=_string_list(
                payload.get("missing_evidence_sources"),
                fallback=[],
            ),
            confirmed_external_blockers=_string_list(
                payload.get("confirmed_external_blockers"),
                fallback=[],
            ),
            resolved_prerequisites=_string_list(
                payload.get("resolved_prerequisites"),
                fallback=[],
            ),
            unresolved_prerequisites=_string_list(
                payload.get("unresolved_prerequisites"),
                fallback=[],
            ),
        )

    def dev(
        self,
        request: WorkflowRequest,
        plan: PmPlan,
        attempt: int,
        feedback: str | None,
    ) -> DevResult:
        payload = invoke_codex_json(
            runtime=self._runtime,
            context=CodexInvocationContext(
                channel="worker",
                tenant_id=request.tenant_id,
                project_id=request.project_id,
                command="workflow",
                stage="dev",
                working_dir=request.execution_repo_dir or ".",
                issue_key=request.issue_key,
                run_id=request.run_id,
                attempt=attempt,
                reasoning_effort="medium",
                issue_description_chars=len(request.issue_description or ""),
            ),
            system_prompt=render_prompt("workflow/dev_system.j2"),
            user_prompt=render_prompt(
                "workflow/dev_user.j2",
                tenant_id=request.tenant_id,
                project_id=request.project_id or "",
                run_id=request.run_id,
                issue_key=request.issue_key,
                attempt=attempt,
                feedback=feedback or "none",
                plan_json=json.dumps(plan.plan_steps),
                acceptance_criteria_json=json.dumps(plan.acceptance_criteria),
                resolved_prerequisites_json=json.dumps(plan.resolved_prerequisites),
                unresolved_prerequisites_json=json.dumps(plan.unresolved_prerequisites),
                confirmed_external_blockers_json=json.dumps(plan.confirmed_external_blockers),
                missing_evidence_sources_json=json.dumps(plan.missing_evidence_sources),
                allowed_tools_json=json.dumps(sorted(allowed_tools_for_stage("dev"))),
                agent_tool_command=(
                    "python -m orchestrator agent-tool "
                    f"--tenant {request.tenant_id} "
                    f"--project {request.project_id or ''} "
                    f"--run {request.run_id} "
                    f"--issue {request.issue_key} "
                    "--stage dev --tool <tool_name> --args '<json-object>'"
                ),
            ),
            extra_on_log_line=self._stage_log_sink(request=request, stage="dev", attempt=attempt),
            require_json=False,
        )
        raw_response = _extract_raw_response(payload)
        pr_url_raw = payload.get("pr_url")
        pr_url = str(pr_url_raw).strip() if isinstance(pr_url_raw, str) and str(pr_url_raw).strip() else None
        hard_stop_raw = payload.get("hard_stop_reason")
        hard_stop_reason = (
            str(hard_stop_raw).strip()
            if isinstance(hard_stop_raw, str) and str(hard_stop_raw).strip()
            else None
        )
        if hard_stop_reason is None:
            hard_stop_reason = _extract_prefixed_value(raw_response, keys=("hard_stop_reason", "hard stop", "blocked"))
        return DevResult(
            change_summary=_string_list(
                payload.get("change_summary"),
                fallback=(
                    _extract_marked_items(raw_response, max_items=5)
                    or ["No change summary provided by Codex"]
                ),
            ),
            pr_url=pr_url,
            hard_stop_reason=hard_stop_reason,
        )

    def test(
        self,
        request: WorkflowRequest,
        plan: PmPlan,
        dev_result: DevResult,
        attempt: int,
    ) -> TestResult:
        payload = invoke_codex_json(
            runtime=self._runtime,
            context=CodexInvocationContext(
                channel="worker",
                tenant_id=request.tenant_id,
                project_id=request.project_id,
                command="workflow",
                stage="test",
                working_dir=request.execution_repo_dir or ".",
                issue_key=request.issue_key,
                run_id=request.run_id,
                attempt=attempt,
                reasoning_effort="medium",
                issue_description_chars=len(request.issue_description or ""),
            ),
            system_prompt=render_prompt("workflow/test_system.j2"),
            user_prompt=render_prompt(
                "workflow/test_user.j2",
                tenant_id=request.tenant_id,
                project_id=request.project_id or "",
                run_id=request.run_id,
                issue_key=request.issue_key,
                attempt=attempt,
                dev_summary_json=json.dumps(dev_result.change_summary),
                pr_url=dev_result.pr_url or "none",
                suggested_test_commands_json=json.dumps(request.suggested_test_commands),
                resolved_prerequisites_json=json.dumps(plan.resolved_prerequisites),
                unresolved_prerequisites_json=json.dumps(plan.unresolved_prerequisites),
                confirmed_external_blockers_json=json.dumps(plan.confirmed_external_blockers),
                missing_evidence_sources_json=json.dumps(plan.missing_evidence_sources),
                allowed_tools_json=json.dumps(sorted(allowed_tools_for_stage("test"))),
                agent_tool_command=(
                    "python -m orchestrator agent-tool "
                    f"--tenant {request.tenant_id} "
                    f"--project {request.project_id or ''} "
                    f"--run {request.run_id} "
                    f"--issue {request.issue_key} "
                    "--stage test --tool <tool_name> --args '<json-object>'"
                ),
            ),
            extra_on_log_line=self._stage_log_sink(request=request, stage="test", attempt=attempt),
            require_json=False,
        )

        raw_response = _extract_raw_response(payload)
        passed = _coerce_bool(
            value=payload.get("passed"),
            raw_response=raw_response,
            true_markers=("passed", "pass", "success", "passed.", "passes"),
            false_markers=("failed", "fail", "blocked", "not passed", "needs fixes", "needs fixing", "retry"),
        )
        feedback_raw = payload.get("feedback")
        feedback = str(feedback_raw).strip() if isinstance(feedback_raw, str) and str(feedback_raw).strip() else None
        if feedback is None:
            feedback = _extract_prefixed_value(raw_response, keys=("feedback", "reason", "summary"))
        guidance = _string_list(
            payload.get("guidance"),
            fallback=(
                _extract_marked_items(raw_response, max_items=5)
                or request.suggested_test_commands
                or ["Run project test suite"]
            ),
        )
        return TestResult(passed=passed, guidance=guidance, feedback=feedback)

    def review(
        self,
        request: WorkflowRequest,
        plan: PmPlan,
        dev_result: DevResult,
        test_result: TestResult,
        attempt: int,
    ) -> ReviewResult:
        payload = invoke_codex_json(
            runtime=self._runtime,
            context=CodexInvocationContext(
                channel="worker",
                tenant_id=request.tenant_id,
                project_id=request.project_id,
                command="workflow",
                stage="review",
                working_dir=request.execution_repo_dir or ".",
                issue_key=request.issue_key,
                run_id=request.run_id,
                attempt=attempt,
                reasoning_effort="medium",
                issue_description_chars=len(request.issue_description or ""),
            ),
            system_prompt=render_prompt("workflow/review_system.j2"),
            user_prompt=render_prompt(
                "workflow/review_user.j2",
                tenant_id=request.tenant_id,
                project_id=request.project_id or "",
                run_id=request.run_id,
                issue_key=request.issue_key,
                attempt=attempt,
                plan_steps_json=json.dumps(plan.plan_steps),
                acceptance_criteria_json=json.dumps(plan.acceptance_criteria),
                dev_summary_json=json.dumps(dev_result.change_summary),
                test_passed=str(test_result.passed).lower(),
                test_guidance_json=json.dumps(test_result.guidance),
                test_feedback=test_result.feedback or "none",
                pr_url=dev_result.pr_url or "none",
                resolved_prerequisites_json=json.dumps(plan.resolved_prerequisites),
                unresolved_prerequisites_json=json.dumps(plan.unresolved_prerequisites),
                confirmed_external_blockers_json=json.dumps(plan.confirmed_external_blockers),
                missing_evidence_sources_json=json.dumps(plan.missing_evidence_sources),
                allowed_tools_json=json.dumps(sorted(allowed_tools_for_stage("review"))),
                agent_tool_command=(
                    "python -m orchestrator agent-tool "
                    f"--tenant {request.tenant_id} "
                    f"--project {request.project_id or ''} "
                    f"--run {request.run_id} "
                    f"--issue {request.issue_key} "
                    "--stage review --tool <tool_name> --args '<json-object>'"
                ),
            ),
            extra_on_log_line=self._stage_log_sink(request=request, stage="review", attempt=attempt),
            require_json=False,
        )

        raw_response = _extract_raw_response(payload)
        approved = _coerce_bool(
            value=payload.get("approved"),
            raw_response=raw_response,
            true_markers=("approved", "pass", "acceptable", "looks good", "go"),
            false_markers=("rejected", "reject", "request changes", "not approved", "blocked", "do not approve"),
        )
        outcome_raw = str(payload.get("outcome") or "").strip().lower()
        if outcome_raw not in {"approved", "needs_changes", "blocked"}:
            fallback_text_parts = [
                raw_response,
                str(payload.get("feedback") or ""),
                " ".join(_string_list(payload.get("summary"), fallback=[])),
            ]
            lowered_response = " ".join(part for part in fallback_text_parts if part).lower()
            if approved:
                outcome_raw = "approved"
            elif any(
                marker in lowered_response
                for marker in (
                    "hard stop",
                    "blocked",
                    "cannot proceed",
                    "cannot approve yet",
                    "governed runtime unavailable",
                    "governed runtime was unavailable",
                    "missing approval",
                    "missing approvals",
                    "runtime unavailable",
                )
            ):
                outcome_raw = "blocked"
            else:
                outcome_raw = "needs_changes"
        feedback_raw = payload.get("feedback")
        feedback = str(feedback_raw).strip() if isinstance(feedback_raw, str) and str(feedback_raw).strip() else None
        if feedback is None:
            feedback = _extract_prefixed_value(raw_response, keys=("feedback", "summary", "reason"))
        pr_url_raw = payload.get("pr_url")
        pr_url = str(pr_url_raw).strip() if isinstance(pr_url_raw, str) and str(pr_url_raw).strip() else dev_result.pr_url
        return ReviewResult(
            approved=approved,
            summary=_string_list(
                payload.get("summary"),
                fallback=(
                    _extract_marked_items(raw_response, max_items=5)
                    or ["No review summary provided by Codex"]
                ),
            ),
            outcome=outcome_raw,
            feedback=feedback,
            pr_url=pr_url,
        )



def _string_list(value: object, *, fallback: list[str]) -> list[str]:
    if isinstance(value, list):
        normalized = [str(item).strip() for item in value if str(item).strip()]
        if normalized:
            return normalized
    return fallback


def _extract_raw_response(payload: dict) -> str:
    raw = payload.get("_raw_response")
    if isinstance(raw, str):
        return raw.strip()
    return ""


def _extract_marked_items(value: str, *, max_items: int = 6) -> list[str]:
    extracted: list[str] = []
    text = str(value or "").strip()
    if not text:
        return extracted
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = re.match(r"^[\-\*\+]\s+(.*)$", line)
        if match:
            item = match.group(1).strip()
        else:
            match = re.match(r"^\d+[.)]\s*(.*)$", line)
            if match:
                item = match.group(1).strip()
            else:
                item = line
        if item:
            extracted.append(item)
            if len(extracted) >= max_items:
                break
    return extracted


def _extract_prefixed_value(value: str, *, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        match = re.search(rf"{re.escape(key)}\s*[:=]\s*(.+)", value, flags=re.IGNORECASE)
        if match:
            candidate = match.group(1).strip()
            if candidate:
                return candidate.strip("\"'")
    return None


def _extract_next_stage(value: str) -> str | None:
    match = re.search(r"\bnext\s*stage\s*[:=]\s*(dev|test)\b", value, flags=re.IGNORECASE)
    return match.group(1).lower() if match else None


def _extract_worker_capability(value: str) -> str | None:
    lowered = str(value or "").lower()
    selected_match = re.search(
        r"\b(?:selected|required|target(?:ed)?|requested|planned)\s+(linux|macos|mac)\b",
        lowered,
    )
    if selected_match:
        normalized = normalize_worker_capability(selected_match.group(1))
        if normalized:
            return normalized

    worker_label_match = re.search(r"\bworker:(linux|macos|mac)\b", lowered)
    if worker_label_match:
        normalized = normalize_worker_capability(worker_label_match.group(1))
        if normalized:
            return normalized

    has_linux = "linux" in lowered
    has_macos = "macos" in lowered or bool(re.search(r"\bmac\b", lowered))
    if has_linux and has_macos:
        current_match = re.search(r"\bcurrent worker(?:\s+is|\s*:)?\s*(linux|macos|mac)\b", lowered)
        if current_match:
            current = normalize_worker_capability(current_match.group(1))
            if current == "linux":
                return "macos"
            if current == "macos":
                return "linux"
        return None
    if has_linux:
        return "linux"
    if has_macos:
        return "macos"
    return None


def _coerce_bool(
    *,
    value: object,
    raw_response: str,
    true_markers: tuple[str, ...],
    false_markers: tuple[str, ...],
) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1"}:
            return True
        if normalized in {"false", "no", "0"}:
            return False
    lowered = str(raw_response or "").lower()
    for marker in false_markers:
        if re.search(rf"\b{re.escape(marker)}\b", lowered):
            return False
    for marker in true_markers:
        if re.search(rf"\b{re.escape(marker)}\b", lowered):
            return True
    return False



def answer_board_question_with_codex(
    *,
    runtime: CodexRuntime,
    question: str,
    project_keys: list[str],
    issues: list[dict],
    status_counts: dict[str, int],
    invocation_context: CodexInvocationContext,
    history: list[dict] | None = None,
    github_context: dict | None = None,
) -> str:
    normalized_history: list[dict] = []
    normalized_github_context = github_context or {}
    payload = invoke_codex_json(
        runtime=runtime,
        context=invocation_context,
        system_prompt=render_prompt("discord/ask_answer_system.j2"),
        user_prompt=render_prompt(
            "discord/ask_answer_user.j2",
            question=question,
            project_keys_json=json.dumps(project_keys),
            status_counts_json=json.dumps(status_counts),
            github_context_json=json.dumps(normalized_github_context),
            history_json=json.dumps(normalized_history),
            issues_json=json.dumps(issues[:40]),
        ),
    )
    message = str(payload.get("message") or "").strip()
    if not message:
        raise CodexRuntimeError("Codex did not return an ask/board message")
    return message


def plan_discord_ask_intent_with_codex(
    *,
    runtime: CodexRuntime,
    question: str,
    project_keys: list[str],
    issues: list[dict],
    status_counts: dict[str, int],
    invocation_context: CodexInvocationContext,
    history: list[dict] | None = None,
    github_context: dict | None = None,
) -> dict:
    normalized_history: list[dict] = []
    normalized_github_context = github_context or {}
    payload = invoke_codex_json(
        runtime=runtime,
        context=invocation_context,
        system_prompt=render_prompt("discord/ask_intent_system.j2"),
        user_prompt=render_prompt(
            "discord/ask_intent_user.j2",
            question=question,
            project_keys_json=json.dumps(project_keys),
            status_counts_json=json.dumps(status_counts),
            github_context_json=json.dumps(normalized_github_context),
            history_json=json.dumps(normalized_history),
            issues_json=json.dumps(issues[:40]),
        ),
    )
    if not isinstance(payload, dict):
        raise CodexRuntimeError("Codex did not return an ask-intent JSON object")
    return payload


def plan_seed_issues_with_codex(
    *,
    runtime: CodexRuntime,
    prompt_markdown: str,
    allowed_project_keys: list[str],
    invocation_context: CodexInvocationContext,
) -> dict:
    payload = invoke_codex_json(
        runtime=runtime,
        context=invocation_context,
        system_prompt=render_prompt("discord/issues_seed_system.j2"),
        user_prompt=render_prompt(
            "discord/issues_seed_user.j2",
            allowed_project_keys_json=json.dumps(allowed_project_keys),
            prompt_markdown=prompt_markdown,
        ),
    )
    if not isinstance(payload, dict):
        raise CodexRuntimeError("Codex did not return an issue-seeding JSON object")
    return payload
