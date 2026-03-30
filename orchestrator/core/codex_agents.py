from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from orchestrator.core.agent_tools import allowed_tools_for_stage, execute_agent_tool
from orchestrator.core.runtime_invocation import (
    AgentInvocationContext,
    invoke_runtime_json,
    invoke_runtime_json_with_tools,
)
from orchestrator.core.codex_runtime import CodexRuntime, CodexRuntimeError
from orchestrator.core.discord.personas import get_voice_room_persona_definition
from orchestrator.core.prompt_templates import render_prompt
from orchestrator.core.worker_capabilities import normalize_worker_capability
from orchestrator.core.workflow.runner import (
    DevResult,
    PmPlan,
    ReviewResult,
    TestResult,
    WorkflowRequest,
)


def _discord_tool_bridge_suffix(*, tool_stage: str) -> str:
    allowed = allowed_tools_for_stage(tool_stage)
    if not allowed:
        return ""
    return "\n\n" + render_prompt(
        "discord/codex_tool_bridge_suffix.j2",
        allowed_tools_json=json.dumps(sorted(allowed)),
    )


def _codex_discord_execute_tool(
    *,
    session: Session,
    settings: Any,
    invocation_context: AgentInvocationContext,
    tool_stage: str,
) -> Callable[[str, dict[str, object]], dict[str, object]]:
    tenant_id = str(invocation_context.tenant_id or "").strip()
    if not tenant_id:
        raise RuntimeError("tenant_id is required for Discord tool execution")

    def _run(tool_name: str, tool_args: dict[str, object]) -> dict[str, object]:
        raw = execute_agent_tool(
            session=session,
            settings=settings,
            tenant_id=tenant_id,
            project_id=str(invocation_context.project_id or "").strip() or None,
            run_id=None,
            issue_key=str(invocation_context.issue_key or "").strip(),
            stage=tool_stage,
            tool_name=tool_name,
            tool_args=dict(tool_args),
        )
        return dict(raw)

    return _run


def _invoke_discord_json_maybe_tools(
    *,
    runtime: CodexRuntime,
    context: AgentInvocationContext,
    system_prompt: str,
    user_prompt: str,
    tool_stage: str,
    sqlalchemy_session: Session | None,
    settings: Any | None,
    max_tool_hops: int = 8,
) -> dict:
    allowed = allowed_tools_for_stage(tool_stage)
    if (
        sqlalchemy_session is None
        or settings is None
        or not allowed
        or not str(context.tenant_id or "").strip()
    ):
        return invoke_runtime_json(
            runtime=runtime,
            context=context,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
    bridged_user = user_prompt + _discord_tool_bridge_suffix(tool_stage=tool_stage)
    return invoke_runtime_json_with_tools(
        runtime=runtime,
        context=context,
        system_prompt=system_prompt,
        user_prompt=bridged_user,
        allowed_tools=allowed,
        execute_tool=_codex_discord_execute_tool(
            session=sqlalchemy_session,
            settings=settings,
            invocation_context=context,
            tool_stage=tool_stage,
        ),
        max_tool_hops=max_tool_hops,
    )


class CodexWorkflowAgents:
    def __init__(
        self,
        *,
        runtime: CodexRuntime,
        runtime_resolver: Callable[[str, WorkflowRequest], CodexRuntime] | None = None,
        log_sink: Callable[[dict], None] | None = None,
        execute_tool: Callable[[AgentInvocationContext, str, dict[str, object]], dict[str, object]] | None = None,
    ):
        self._runtime = runtime
        self._runtime_resolver = runtime_resolver
        self._log_sink = log_sink
        self._execute_tool = execute_tool

    def _runtime_for_stage(self, *, stage: str, request: WorkflowRequest) -> CodexRuntime:
        if self._runtime_resolver is None:
            return self._runtime
        return self._runtime_resolver(stage, request)

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

    def _resume_session_id_for_stage(self, *, request: WorkflowRequest, stage: str) -> str | None:
        if str(request.resume_mode or "").strip().lower() != "resume":
            return None
        resume_stage = str(request.resume_stage or "").strip().lower()
        session_id = str(request.resume_session_id or "").strip() or None
        if not session_id:
            return None
        if resume_stage == stage:
            return session_id
        if resume_stage == "orchestrated" and stage == "pm":
            return session_id
        return None

    def _resume_source_state(self, *, request: WorkflowRequest) -> dict[str, Any]:
        trigger_context = request.trigger_context if isinstance(request.trigger_context, dict) else {}
        payload = trigger_context.get("resume_source_state")
        return dict(payload) if isinstance(payload, dict) else {}

    def _invoke_stage_payload(
        self,
        *,
        request: WorkflowRequest,
        stage: str,
        attempt: int,
        system_prompt: str,
        user_prompt: str,
        reasoning_effort: str = "medium",
    ) -> dict[str, Any]:
        context = AgentInvocationContext(
            channel="worker",
            tenant_id=request.tenant_id,
            project_id=request.project_id,
            command="workflow",
            stage=stage,
            working_dir=request.execution_repo_dir or ".",
            issue_key=request.issue_key,
            run_id=request.run_id,
            attempt=attempt,
            reasoning_effort=reasoning_effort,
            issue_description_chars=len(request.issue_description or ""),
            codex_session_id=self._resume_session_id_for_stage(request=request, stage=stage),
        )
        allowed_tools = sorted(allowed_tools_for_stage(stage))
        return invoke_runtime_json_with_tools(
            runtime=self._runtime_for_stage(stage=stage, request=request),
            context=context,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            allowed_tools=set(allowed_tools),
            execute_tool=lambda tool_name, tool_args: self._execute_stage_tool(
                context=context,
                tool_name=tool_name,
                tool_args=tool_args,
            ),
            extra_on_log_line=self._stage_log_sink(request=request, stage=stage, attempt=attempt),
            require_json=False,
        )

    def _execute_stage_tool(
        self,
        *,
        context: AgentInvocationContext,
        tool_name: str,
        tool_args: dict[str, object],
    ) -> dict[str, object]:
        if self._execute_tool is None:
            raise RuntimeError("Codex workflow stage requested a tool but no tool executor is configured")
        return self._execute_tool(context, tool_name, tool_args)

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
        payload = self._invoke_stage_payload(
            request=request,
            stage="pm",
            attempt=attempt,
            system_prompt=render_prompt("workflow/pm_system.j2"),
            user_prompt=render_prompt(
                "workflow/pm_user.j2",
                tenant_id=request.tenant_id,
                project_id=request.project_id or "",
                project_name=request.project_name or "",
                github_repository=request.github_repository or "",
                jira_project_key=request.jira_project_key or "",
                run_id=request.run_id,
                issue_key=request.issue_key,
                execution_repo_dir=request.execution_repo_dir or "",
                execution_branch=request.execution_branch or "",
                base_branch=request.base_branch or "",
                integration_branch=request.integration_branch or "",
                pr_target_branch=request.pr_target_branch or "",
                allow_pr_creation="true" if request.allow_pr_creation else "false",
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
                human_inputs_json=json.dumps(request.human_inputs),
                allowed_tools_json=json.dumps(sorted(allowed_tools_for_stage("pm"))),
            ),
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
        payload = self._invoke_stage_payload(
            request=request,
            stage="dev",
            attempt=attempt,
            system_prompt=render_prompt("workflow/dev_system.j2"),
            user_prompt=render_prompt(
                "workflow/dev_user.j2",
                tenant_id=request.tenant_id,
                project_id=request.project_id or "",
                project_name=request.project_name or "",
                github_repository=request.github_repository or "",
                jira_project_key=request.jira_project_key or "",
                run_id=request.run_id,
                issue_key=request.issue_key,
                execution_repo_dir=request.execution_repo_dir or "",
                execution_branch=request.execution_branch or "",
                base_branch=request.base_branch or "",
                integration_branch=request.integration_branch or "",
                pr_target_branch=request.pr_target_branch or "",
                allow_pr_creation="true" if request.allow_pr_creation else "false",
                attempt=attempt,
                feedback=feedback or "none",
                plan_json=json.dumps(plan.plan_steps),
                acceptance_criteria_json=json.dumps(plan.acceptance_criteria),
                resolved_prerequisites_json=json.dumps(plan.resolved_prerequisites),
                unresolved_prerequisites_json=json.dumps(plan.unresolved_prerequisites),
                confirmed_external_blockers_json=json.dumps(plan.confirmed_external_blockers),
                missing_evidence_sources_json=json.dumps(plan.missing_evidence_sources),
                human_inputs_json=json.dumps(request.human_inputs),
                allowed_tools_json=json.dumps(sorted(allowed_tools_for_stage("dev"))),
            ),
        )
        raw_response = _extract_raw_response(payload)
        pr_url_raw = payload.get("pr_url")
        pr_url = str(pr_url_raw).strip() if isinstance(pr_url_raw, str) and str(pr_url_raw).strip() else None
        blocker_category_raw = payload.get("blocker_category")
        blocker_category = (
            str(blocker_category_raw).strip()
            if isinstance(blocker_category_raw, str) and str(blocker_category_raw).strip()
            else None
        )
        blocker_message_raw = payload.get("blocker_message")
        blocker_message = (
            str(blocker_message_raw).strip()
            if isinstance(blocker_message_raw, str) and str(blocker_message_raw).strip()
            else None
        )
        if blocker_message is None:
            blocker_message = _extract_prefixed_value(
                raw_response,
                keys=("blocker_message", "blocked", "hard stop"),
            )
        return DevResult(
            change_summary=_string_list(
                payload.get("change_summary"),
                fallback=(
                    _extract_marked_items(raw_response, max_items=5)
                    or ["No change summary provided by Codex"]
                ),
            ),
            pr_url=pr_url,
            blocker_category=blocker_category,
            blocker_message=blocker_message,
        )

    def test(
        self,
        request: WorkflowRequest,
        plan: PmPlan,
        dev_result: DevResult,
        attempt: int,
    ) -> TestResult:
        payload = self._invoke_stage_payload(
            request=request,
            stage="test",
            attempt=attempt,
            system_prompt=render_prompt("workflow/test_system.j2"),
            user_prompt=render_prompt(
                "workflow/test_user.j2",
                tenant_id=request.tenant_id,
                project_id=request.project_id or "",
                project_name=request.project_name or "",
                github_repository=request.github_repository or "",
                jira_project_key=request.jira_project_key or "",
                run_id=request.run_id,
                issue_key=request.issue_key,
                execution_repo_dir=request.execution_repo_dir or "",
                execution_branch=request.execution_branch or "",
                base_branch=request.base_branch or "",
                integration_branch=request.integration_branch or "",
                pr_target_branch=request.pr_target_branch or "",
                allow_pr_creation="true" if request.allow_pr_creation else "false",
                attempt=attempt,
                dev_summary_json=json.dumps(dev_result.change_summary),
                pr_url=dev_result.pr_url or "none",
                suggested_test_commands_json=json.dumps(request.suggested_test_commands),
                resolved_prerequisites_json=json.dumps(plan.resolved_prerequisites),
                unresolved_prerequisites_json=json.dumps(plan.unresolved_prerequisites),
                confirmed_external_blockers_json=json.dumps(plan.confirmed_external_blockers),
                missing_evidence_sources_json=json.dumps(plan.missing_evidence_sources),
                human_inputs_json=json.dumps(request.human_inputs),
                allowed_tools_json=json.dumps(sorted(allowed_tools_for_stage("test"))),
            ),
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
        blocker_category_raw = payload.get("blocker_category")
        blocker_category = (
            str(blocker_category_raw).strip()
            if isinstance(blocker_category_raw, str) and str(blocker_category_raw).strip()
            else None
        )
        blocker_message_raw = payload.get("blocker_message")
        blocker_message = (
            str(blocker_message_raw).strip()
            if isinstance(blocker_message_raw, str) and str(blocker_message_raw).strip()
            else None
        )
        if blocker_message is None and passed is False:
            blocker_message = _extract_prefixed_value(raw_response, keys=("blocker_message", "blocked"))
        guidance = _string_list(
            payload.get("guidance"),
            fallback=(
                _extract_marked_items(raw_response, max_items=5)
                or request.suggested_test_commands
                or ["Run project test suite"]
            ),
        )
        return TestResult(
            passed=passed,
            guidance=guidance,
            feedback=feedback,
            blocker_category=blocker_category,
            blocker_message=blocker_message,
        )

    def review(
        self,
        request: WorkflowRequest,
        plan: PmPlan,
        dev_result: DevResult,
        test_result: TestResult,
        attempt: int,
    ) -> ReviewResult:
        resume_source_state = self._resume_source_state(request=request)
        payload = self._invoke_stage_payload(
            request=request,
            stage="review",
            attempt=attempt,
            system_prompt=render_prompt("workflow/review_system.j2"),
            user_prompt=render_prompt(
                "workflow/review_user.j2",
                tenant_id=request.tenant_id,
                project_id=request.project_id or "",
                project_name=request.project_name or "",
                github_repository=request.github_repository or "",
                jira_project_key=request.jira_project_key or "",
                run_id=request.run_id,
                issue_key=request.issue_key,
                execution_repo_dir=request.execution_repo_dir or "",
                execution_branch=request.execution_branch or "",
                base_branch=request.base_branch or "",
                integration_branch=request.integration_branch or "",
                pr_target_branch=request.pr_target_branch or "",
                allow_pr_creation="true" if request.allow_pr_creation else "false",
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
                human_inputs_json=json.dumps(request.human_inputs),
                previous_review_summary_json=json.dumps(resume_source_state.get("review_summary") or []),
                previous_review_feedback=str(resume_source_state.get("review_feedback") or "").strip() or "none",
                allowed_tools_json=json.dumps(sorted(allowed_tools_for_stage("review"))),
            ),
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
        blocker_category_raw = payload.get("blocker_category")
        blocker_category = (
            str(blocker_category_raw).strip()
            if isinstance(blocker_category_raw, str) and str(blocker_category_raw).strip()
            else None
        )
        blocker_message_raw = payload.get("blocker_message")
        blocker_message = (
            str(blocker_message_raw).strip()
            if isinstance(blocker_message_raw, str) and str(blocker_message_raw).strip()
            else None
        )
        if blocker_message is None and outcome_raw == "blocked":
            blocker_message = feedback or _extract_prefixed_value(raw_response, keys=("blocker_message", "blocked"))
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
            blocker_category=blocker_category,
            blocker_message=blocker_message,
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



def answer_board_question_with_runtime(
    *,
    runtime: CodexRuntime,
    question: str,
    project_keys: list[str],
    issues: list[dict],
    status_counts: dict[str, int],
    invocation_context: AgentInvocationContext,
    history: list[dict] | None = None,
    github_context: dict | None = None,
    sqlalchemy_session: Session | None = None,
    settings: Any | None = None,
    answer_persona_id: str | None = None,
) -> str:
    normalized_history = history if isinstance(history, list) else []
    normalized_github_context = github_context or {}
    history_slice = normalized_history[-25:] if normalized_history else []
    persona = str(answer_persona_id or "").strip().lower() or "pm"
    user_prompt = render_prompt(
        "discord/ask_answer_user.j2",
        question=question,
        persona_id=persona,
        project_keys_json=json.dumps(project_keys),
        status_counts_json=json.dumps(status_counts),
        github_context_json=json.dumps(normalized_github_context),
        history_json=json.dumps(history_slice),
        issues_json=json.dumps(issues[:40]),
    )
    payload = _invoke_discord_json_maybe_tools(
        runtime=runtime,
        context=invocation_context,
        system_prompt=render_prompt("discord/ask_answer_system.j2", persona_id=persona),
        user_prompt=user_prompt,
        tool_stage="discord_ask_answer",
        sqlalchemy_session=sqlalchemy_session,
        settings=settings,
        max_tool_hops=8,
    )
    message = str(payload.get("message") or "").strip()
    if not message:
        raise CodexRuntimeError("Codex did not return an ask/board message")
    return message


def answer_pm_question_with_codex(
    *,
    runtime: CodexRuntime,
    question: str,
    action: str | None,
    project_keys: list[str],
    issues: list[dict],
    status_counts: dict[str, int],
    invocation_context: AgentInvocationContext,
    history: list[dict] | None = None,
    github_context: dict | None = None,
) -> dict:
    normalized_action = str(action or "ask").strip().lower()
    if normalized_action not in {"ask", "approve"}:
        normalized_action = "ask"
    normalized_history = history if isinstance(history, list) else []
    normalized_github_context = github_context or {}
    payload = invoke_runtime_json(
        runtime=runtime,
        context=invocation_context,
        system_prompt=render_prompt("discord/pm_answer_system.j2"),
        user_prompt=render_prompt(
            "discord/pm_answer_user.j2",
            action=normalized_action,
            question=question,
            project_keys_json=json.dumps(project_keys),
            status_counts_json=json.dumps(status_counts),
            github_context_json=json.dumps(normalized_github_context),
            history_json=json.dumps(normalized_history[-25:]),
            issues_json=json.dumps(issues[:40]),
        ),
    )
    if not isinstance(payload, dict):
        raise CodexRuntimeError("Codex did not return a pm JSON object")
    message = str(payload.get("message") or "").strip()
    if not message:
        raise CodexRuntimeError("Codex did not return a pm message")
    brief = payload.get("brief")
    if brief is None:
        brief = {}
    if not isinstance(brief, dict):
        raise CodexRuntimeError("Codex did not return a pm brief object")
    normalized_payload = dict(payload)
    normalized_payload["message"] = message
    normalized_payload["brief"] = brief
    return normalized_payload


def classify_engineering_clarification_with_codex(
    *,
    runtime: CodexRuntime,
    parent_issue_key: str,
    parent_summary: str,
    parent_description: str,
    child_issue_key: str,
    child_summary: str,
    child_description: str,
    question: str,
    invocation_context: AgentInvocationContext,
) -> dict:
    payload = invoke_runtime_json(
        runtime=runtime,
        context=invocation_context,
        system_prompt=render_prompt("jira/engineering_clarification_system.j2"),
        user_prompt=render_prompt(
            "jira/engineering_clarification_user.j2",
            parent_issue_key=parent_issue_key,
            parent_summary=parent_summary,
            parent_description=parent_description,
            child_issue_key=child_issue_key,
            child_summary=child_summary,
            child_description=child_description,
            question=question,
        ),
    )
    if not isinstance(payload, dict):
        raise CodexRuntimeError("Codex did not return an engineering clarification JSON object")
    return payload


def route_voice_entry_with_runtime(
    *,
    runtime: CodexRuntime,
    transcript: str,
    invocation_context: AgentInvocationContext,
    entry_source: str,
    history: list[dict] | None = None,
    room_context: dict | None = None,
    sqlalchemy_session: Session | None = None,
    settings: Any | None = None,
) -> dict:
    """Route voice transcript to ask vs interview (strict JSON from agent runtime)."""
    normalized_history = history if isinstance(history, list) else []
    user_prompt = render_prompt(
        "discord/voice_entry_router_user.j2",
        transcript=transcript,
        entry_source=entry_source,
        history_json=json.dumps(normalized_history[-25:]),
        room_context_json=json.dumps(room_context or {}),
    )
    payload = _invoke_discord_json_maybe_tools(
        runtime=runtime,
        context=invocation_context,
        system_prompt=render_prompt("discord/voice_entry_router_system.j2"),
        user_prompt=user_prompt,
        tool_stage="voice_entry_router",
        sqlalchemy_session=sqlalchemy_session,
        settings=settings,
        max_tool_hops=6,
    )
    if not isinstance(payload, dict):
        raise CodexRuntimeError("Codex did not return a voice-entry router JSON object")
    lane = str(payload.get("lane") or "").strip().lower()
    if lane == "pm":
        lane = "interview"
    if lane == "persona":
        lane = "ask"
    if lane not in {"ask", "interview"}:
        lane = "ask"
    persona_id = str(payload.get("persona") or "").strip().lower()
    valid_personas = {"pm", "architect", "engineer", "qa", "security"}
    if persona_id not in valid_personas:
        persona_id = "pm"
    try:
        confidence = float(payload.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    reason = str(payload.get("reason") or "").strip()
    if lane == "interview":
        persona_id = "pm"
    return {
        "lane": lane,
        "persona": persona_id,
        "confidence": confidence,
        "reason": reason,
    }


def answer_voice_room_persona_with_codex(
    *,
    runtime: CodexRuntime,
    persona_id: str,
    transcript: str,
    project_keys: list[str],
    issues: list[dict],
    status_counts: dict[str, int],
    invocation_context: AgentInvocationContext,
    history: list[dict] | None = None,
    github_context: dict | None = None,
    room_context: dict | None = None,
    sqlalchemy_session: Session | None = None,
    settings: Any | None = None,
) -> dict:
    persona = get_voice_room_persona_definition(persona_id)
    normalized_history = history if isinstance(history, list) else []
    system_prompt = render_prompt(persona.system_prompt_template)
    if sqlalchemy_session is not None and settings is not None and allowed_tools_for_stage("discord_voice_room_persona"):
        system_prompt += (
            "\n\nWhen the user message includes an Allowed tools section, use tool_request then final_response. "
            "final_response.result must match the same JSON shape required above (same keys as without tools)."
        )
    user_prompt = render_prompt(
        persona.user_prompt_template,
        transcript=transcript,
        history_json=json.dumps(normalized_history[-25:]),
        room_context_json=json.dumps(
            room_context
            or {
                "project_keys": project_keys,
                "status_counts": status_counts,
                "github_context": github_context or {},
                "issues": issues[:40],
            }
        ),
    )
    payload = _invoke_discord_json_maybe_tools(
        runtime=runtime,
        context=invocation_context,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        tool_stage="discord_voice_room_persona",
        sqlalchemy_session=sqlalchemy_session,
        settings=settings,
        max_tool_hops=6,
    )
    if not isinstance(payload, dict):
        raise CodexRuntimeError("Codex did not return a voice-room persona JSON object")
    message = str(payload.get("message") or "").strip()
    if not message:
        raise CodexRuntimeError("Codex did not return a voice-room persona message")
    brief = payload.get("brief")
    if brief is None:
        brief = {}
    if not isinstance(brief, dict):
        raise CodexRuntimeError("Codex did not return a voice-room brief object")
    return {
        "message": message,
        "brief": brief,
    }


def plan_discord_ask_intent_with_codex(
    *,
    runtime: CodexRuntime,
    question: str,
    project_keys: list[str],
    issues: list[dict],
    status_counts: dict[str, int],
    invocation_context: AgentInvocationContext,
    history: list[dict] | None = None,
    github_context: dict | None = None,
) -> dict:
    normalized_history: list[dict] = []
    normalized_github_context = github_context or {}
    payload = invoke_runtime_json(
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
    invocation_context: AgentInvocationContext,
) -> dict:
    last_error: CodexRuntimeError | None = None
    for attempt in range(2):
        try:
            payload = invoke_runtime_json(
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
        except CodexRuntimeError as exc:
            last_error = exc
            error_text = str(exc).lower()
            retryable_empty_output = (
                "no last agent message" in error_text
                or "returned empty output" in error_text
                or "empty response" in error_text
                or "wrote empty content" in error_text
            )
            if not retryable_empty_output or attempt > 0:
                raise
    assert last_error is not None
    raise last_error


def plan_pm_parent_issues_with_codex(
    *,
    runtime: CodexRuntime,
    prompt_markdown: str,
    allowed_project_keys: list[str],
    invocation_context: AgentInvocationContext,
) -> dict:
    last_error: CodexRuntimeError | None = None
    for attempt in range(2):
        try:
            payload = invoke_runtime_json(
                runtime=runtime,
                context=invocation_context,
                system_prompt=render_prompt("discord/pm_seed_batch_system.j2"),
                user_prompt=render_prompt(
                    "discord/pm_seed_batch_user.j2",
                    allowed_project_keys_json=json.dumps(allowed_project_keys),
                    prompt_markdown=prompt_markdown,
                ),
            )
            if not isinstance(payload, dict):
                raise CodexRuntimeError("Codex did not return a PM batch issue-seeding JSON object")
            return payload
        except CodexRuntimeError as exc:
            last_error = exc
            error_text = str(exc).lower()
            retryable_empty_output = (
                "no last agent message" in error_text
                or "returned empty output" in error_text
                or "empty response" in error_text
                or "wrote empty content" in error_text
            )
            if not retryable_empty_output or attempt > 0:
                raise
    assert last_error is not None
    raise last_error
