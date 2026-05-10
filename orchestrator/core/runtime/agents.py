from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from orchestrator.core.runtime.invocation import (
    AgentInvocationContext,
    invoke_runtime_json,
)
from orchestrator.core.runtime.runtime import CodexRuntime, CodexRuntimeError
from orchestrator.core.discord.personas import get_voice_room_persona_definition
from orchestrator.core.prompt_templates import render_prompt
from orchestrator.core.runtime.payload_models import (
    AskIntent,
    EngineeringClarification,
    EngineeringSeedPlan,
    PMMessageBrief,
    PmParentSeedPlan,
    RuntimeMessage,
    VoiceEntryRoute,
)
from orchestrator.core.runtime.stage_session import (
    RuntimeStageSession,
    build_governed_tool_executor,
    build_runtime_stage_tooling,
)
from orchestrator.core.worker.capability_normalization import parse_worker_capability
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.core.workflow.runner import (
    DevResult,
    PmPlan,
    ReviewResult,
    StageOutcome,
    TestResult,
    WorkflowRequest,
)


def _stage_prompt_tool_context(*, tool_stage: str, runtime_command: str | None) -> dict[str, str]:
    tooling = build_runtime_stage_tooling(policy_stage=tool_stage, runtime_command=runtime_command)
    return tooling.governed_native_prompt_context()


def _stage_has_governed_tools(*, tool_stage: str, runtime_command: str | None) -> bool:
    tooling = build_runtime_stage_tooling(policy_stage=tool_stage, runtime_command=runtime_command)
    return bool(tooling.governed_tools)


def _discord_tool_bridge_suffix(*, tool_stage: str, runtime_command: str | None = None) -> str:
    tooling = build_runtime_stage_tooling(policy_stage=tool_stage, runtime_command=runtime_command)
    if not tooling.governed_tools:
        return ""
    return "\n\n" + render_prompt(
        "discord/codex_tool_bridge_suffix.j2",
        allowed_tools_json=json.dumps(sorted(tooling.governed_tools)),
    )


def _codex_discord_execute_tool(
    *,
    session: Session,
    settings: Any,
    invocation_context: AgentInvocationContext,
    tool_stage: str,
) -> Callable[[str, dict[str, object]], dict[str, object]]:
    return build_governed_tool_executor(
        session=session,
        settings=settings,
        context=invocation_context,
        policy_stage=tool_stage,
    )


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
    runtime_command = str(getattr(runtime, "command", "") or "").strip()
    stage_session = RuntimeStageSession.create(
        runtime=runtime,
        context=context,
        policy_stage=tool_stage,
        execute_tool=(
            _codex_discord_execute_tool(
                session=sqlalchemy_session,
                settings=settings,
                invocation_context=context,
                tool_stage=tool_stage,
            )
            if sqlalchemy_session is not None and settings is not None and str(context.tenant_id or "").strip()
            else None
        ),
    )
    bridged_user = user_prompt
    if stage_session.tooling.governed_tools:
        bridged_user += _discord_tool_bridge_suffix(
            tool_stage=tool_stage,
            runtime_command=runtime_command,
        )
    return stage_session.invoke_json(
        system_prompt=system_prompt,
        user_prompt=bridged_user,
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
        if str(request.entry_mode or "").strip().lower() != "resume":
            return None
        checkpoint_kind = str(request.checkpoint_kind or "").strip().lower()
        session_id = str(request.checkpoint_session_id or "").strip() or None
        if not session_id:
            return None
        if checkpoint_kind == "orchestrated" and stage == "pm":
            return session_id
        if checkpoint_kind == "pm" and stage == "pm":
            return session_id
        if checkpoint_kind == "execution" and stage in {"dev", "test", "review"}:
            return session_id
        return None

    def _resume_source_state(self, *, request: WorkflowRequest) -> dict[str, Any]:
        payload = request.checkpoint_payload
        snapshot = ExecutionSnapshot.load(payload)
        if snapshot is None:
            return {}
        review_result = snapshot.review_result()
        if review_result is None:
            return {}
        return {
            "review_summary": list(review_result.summary),
            "review_feedback": review_result.feedback,
        }

    def _invoke_stage_payload(
        self,
        *,
        request: WorkflowRequest,
        stage: str,
        attempt: int,
        system_prompt: str,
        user_prompt: str,
        reasoning_effort: str = "medium",
        runtime_override: CodexRuntime | None = None,
    ) -> dict[str, Any]:
        runtime = runtime_override or self._runtime_for_stage(stage=stage, request=request)
        context = AgentInvocationContext(
            channel="worker",
            tenant_id=request.tenant_id,
            project_id=request.project_id,
            command="workflow",
            stage=stage,
            working_dir=request.execution_repo_dir or ".",
            workflow_id=request.workflow_id,
            issue_key=request.issue_key,
            run_id=request.run_id,
            attempt=attempt,
            reasoning_effort=reasoning_effort,
            issue_description_chars=len(request.issue_description or ""),
            codex_session_id=self._resume_session_id_for_stage(request=request, stage=stage),
        )
        stage_session = RuntimeStageSession.create(
            runtime=runtime,
            context=context,
            policy_stage=stage,
            execute_tool=lambda tool_name, tool_args: self._execute_stage_tool(
                context=context,
                tool_name=tool_name,
                tool_args=tool_args,
            ),
        )
        return stage_session.invoke_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            extra_on_log_line=self._stage_log_sink(request=request, stage=stage, attempt=attempt),
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
        runtime = self._runtime_for_stage(stage="pm", request=request)
        runtime_command = str(getattr(runtime, "command", "") or "")
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
                last_test_outcome=(
                    "none"
                    if last_test_result is None
                    else last_test_result.outcome
                ),
                last_test_feedback=last_test_result.feedback if last_test_result and last_test_result.feedback else "none",
                last_test_guidance_json=json.dumps(last_test_result.guidance if last_test_result else []),
                last_review_outcome=(
                    "none"
                    if last_review_result is None
                    else last_review_result.outcome
                ),
                last_review_feedback=(
                    last_review_result.feedback
                    if last_review_result and last_review_result.feedback
                    else "none"
                ),
                last_review_summary_json=json.dumps(last_review_result.summary if last_review_result else []),
                current_worker_capability=request.current_worker_capability.value,
                available_worker_capabilities_json=json.dumps(
                    [capability.value for capability in request.available_worker_capabilities]
                ),
                human_inputs_json=json.dumps(request.human_inputs),
                **_stage_prompt_tool_context(tool_stage="pm", runtime_command=runtime_command),
            ),
            runtime_override=runtime,
        )
        outcome = _require_stage_outcome(payload=payload, stage="pm")
        next_stage_raw = payload.get("next_stage")
        next_stage = next_stage_raw if isinstance(next_stage_raw, str) else None
        if next_stage not in {"dev", "test"}:
            raise CodexRuntimeError("Codex pm response missing valid required next_stage")
        execution_worker_capability = parse_worker_capability(payload.get("execution_worker_capability"))
        if execution_worker_capability is None:
            raise CodexRuntimeError("Codex pm response missing valid required execution_worker_capability")
        blocker_message = _optional_string(payload.get("blocker_message"))
        requeue_target_raw = payload.get("requeue_target")
        requeue_target = (
            parse_worker_capability(requeue_target_raw)
            if requeue_target_raw is not None
            else None
        )
        requeue_reason = _optional_string(payload.get("requeue_reason"))
        if outcome == "blocked" and blocker_message is None:
            raise CodexRuntimeError("Codex pm response missing blocker_message for blocked outcome")
        if outcome == "requeue" and requeue_target is None:
            raise CodexRuntimeError("Codex pm response missing requeue_target for requeue outcome")
        if outcome == "requeue" and requeue_reason is None:
            raise CodexRuntimeError("Codex pm response missing requeue_reason for requeue outcome")
        return PmPlan(
            plan_steps=_required_string_list(payload.get("plan_steps"), stage="pm", field="plan_steps"),
            acceptance_criteria=_required_string_list(
                payload.get("acceptance_criteria"),
                stage="pm",
                field="acceptance_criteria",
            ),
            risks=_string_list(payload.get("risks")),
            outcome=outcome,
            next_stage=next_stage,
            execution_worker_capability=execution_worker_capability.value,
            blocker_message=blocker_message,
            requeue_target=(requeue_target.value if requeue_target is not None else None),
            requeue_reason=requeue_reason,
            resolved_prerequisites=_string_list(payload.get("resolved_prerequisites")),
            unresolved_prerequisites=_string_list(payload.get("unresolved_prerequisites")),
        )

    def dev(
        self,
        request: WorkflowRequest,
        plan: PmPlan,
        attempt: int,
        feedback: str | None,
    ) -> DevResult:
        runtime = self._runtime_for_stage(stage="dev", request=request)
        runtime_command = str(getattr(runtime, "command", "") or "")
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
                pm_outcome=plan.outcome,
                human_inputs_json=json.dumps(request.human_inputs),
                **_stage_prompt_tool_context(tool_stage="dev", runtime_command=runtime_command),
            ),
            runtime_override=runtime,
        )
        pr_url_raw = payload.get("pr_url")
        pr_url = str(pr_url_raw).strip() if isinstance(pr_url_raw, str) and str(pr_url_raw).strip() else None
        outcome = _require_stage_outcome(payload=payload, stage="dev")
        blocker_message = _optional_string(payload.get("blocker_message"))
        if outcome == "blocked" and blocker_message is None:
            raise CodexRuntimeError("Codex dev response missing blocker_message for blocked outcome")
        return DevResult(
            change_summary=_required_string_list(payload.get("change_summary"), stage="dev", field="change_summary"),
            pr_url=pr_url,
            outcome=outcome,
            blocker_message=blocker_message,
        )

    def test(
        self,
        request: WorkflowRequest,
        plan: PmPlan,
        dev_result: DevResult,
        attempt: int,
    ) -> TestResult:
        runtime = self._runtime_for_stage(stage="test", request=request)
        runtime_command = str(getattr(runtime, "command", "") or "")
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
                dev_outcome=dev_result.outcome,
                human_inputs_json=json.dumps(request.human_inputs),
                **_stage_prompt_tool_context(tool_stage="test", runtime_command=runtime_command),
            ),
            runtime_override=runtime,
        )
        outcome = _require_stage_outcome(payload=payload, stage="test")
        feedback_raw = payload.get("feedback")
        feedback = str(feedback_raw).strip() if isinstance(feedback_raw, str) and str(feedback_raw).strip() else None
        blocker_message = _optional_string(payload.get("blocker_message"))
        if outcome == "blocked" and blocker_message is None:
            raise CodexRuntimeError("Codex test response missing blocker_message for blocked outcome")
        guidance = _required_string_list(payload.get("guidance"), stage="test", field="guidance")
        return TestResult(
            guidance=guidance,
            outcome=outcome,
            feedback=feedback,
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
        runtime = self._runtime_for_stage(stage="review", request=request)
        runtime_command = str(getattr(runtime, "command", "") or "")
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
                test_outcome=test_result.outcome,
                test_guidance_json=json.dumps(test_result.guidance),
                test_feedback=test_result.feedback or "none",
                pr_url=dev_result.pr_url or "none",
                resolved_prerequisites_json=json.dumps(plan.resolved_prerequisites),
                unresolved_prerequisites_json=json.dumps(plan.unresolved_prerequisites),
                human_inputs_json=json.dumps(request.human_inputs),
                previous_review_summary_json=json.dumps(resume_source_state.get("review_summary") or []),
                previous_review_feedback=str(resume_source_state.get("review_feedback") or "").strip() or "none",
                **_stage_prompt_tool_context(tool_stage="review", runtime_command=runtime_command),
            ),
            runtime_override=runtime,
        )
        feedback_raw = payload.get("feedback")
        feedback = str(feedback_raw).strip() if isinstance(feedback_raw, str) and str(feedback_raw).strip() else None
        pr_url_raw = payload.get("pr_url")
        pr_url = str(pr_url_raw).strip() if isinstance(pr_url_raw, str) and str(pr_url_raw).strip() else dev_result.pr_url
        outcome = _require_stage_outcome(payload=payload, stage="review")
        blocker_message = _optional_string(payload.get("blocker_message"))
        if outcome == "blocked" and blocker_message is None:
            raise CodexRuntimeError("Codex review response missing blocker_message for blocked outcome")
        return ReviewResult(
            summary=_required_string_list(payload.get("summary"), stage="review", field="summary"),
            outcome=outcome,
            feedback=feedback,
            pr_url=pr_url,
            blocker_message=blocker_message,
        )



def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    parsed: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return []
        if not item.strip():
            return []
        parsed.append(item)
    return parsed


def _required_string_list(value: object, *, stage: str, field: str) -> list[str]:
    normalized = _string_list(value)
    if normalized:
        return normalized
    raise CodexRuntimeError(f"Codex {stage} response missing required non-empty {field}")


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    if not value.strip():
        return None
    return value


def _normalize_stage_outcome(value: object) -> StageOutcome | None:
    if isinstance(value, str) and value in {"continue", "requeue", "waiting_for_input", "blocked", "failed"}:
        return value  # type: ignore[return-value]
    return None


def _require_stage_outcome(*, payload: dict[str, Any], stage: str) -> StageOutcome:
    outcome = _normalize_stage_outcome(payload.get("outcome"))
    if outcome is None:
        raise CodexRuntimeError(f"Codex {stage} response missing valid required outcome")
    return outcome



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
    try:
        return RuntimeMessage.from_payload(payload, context="Ask answer payload").message
    except RuntimeError as exc:
        raise CodexRuntimeError(str(exc)) from exc


def answer_pm_question_with_runtime(
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
    try:
        return PMMessageBrief.from_payload(payload, context="PM answer payload").to_payload()
    except RuntimeError as exc:
        raise CodexRuntimeError(str(exc)) from exc


def classify_engineering_clarification_with_runtime(
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
) -> EngineeringClarification:
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
    try:
        return EngineeringClarification.from_payload(payload)
    except RuntimeError as exc:
        raise CodexRuntimeError(str(exc)) from exc


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
) -> VoiceEntryRoute:
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
    try:
        return VoiceEntryRoute.from_payload(payload)
    except RuntimeError as exc:
        raise CodexRuntimeError(str(exc)) from exc


def answer_voice_room_persona_with_runtime(
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
    if sqlalchemy_session is not None and settings is not None and _stage_has_governed_tools(
        "discord_voice_room_persona",
        runtime_command=str(getattr(runtime, "command", "") or ""),
    ):
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
    try:
        return PMMessageBrief.from_payload(payload, context="Voice room persona payload").to_payload()
    except RuntimeError as exc:
        raise CodexRuntimeError(str(exc)) from exc


def plan_discord_ask_intent_with_runtime(
    *,
    runtime: CodexRuntime,
    question: str,
    project_keys: list[str],
    issues: list[dict],
    status_counts: dict[str, int],
    invocation_context: AgentInvocationContext,
    history: list[dict] | None = None,
    github_context: dict | None = None,
) -> AskIntent:
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
    try:
        return AskIntent.from_payload(payload)
    except RuntimeError as exc:
        raise CodexRuntimeError(str(exc)) from exc


def plan_seed_issues_with_runtime(
    *,
    runtime: CodexRuntime,
    prompt_markdown: str,
    allowed_project_keys: list[str],
    project_issue_types_by_key: dict[str, list[str]],
    invocation_context: AgentInvocationContext,
) -> EngineeringSeedPlan:
    payload = invoke_runtime_json(
        runtime=runtime,
        context=invocation_context,
        system_prompt=render_prompt("discord/issues_seed_system.j2"),
        user_prompt=render_prompt(
            "discord/issues_seed_user.j2",
            allowed_project_keys_json=json.dumps(allowed_project_keys),
            project_issue_types_json=json.dumps(project_issue_types_by_key, sort_keys=True),
            prompt_markdown=prompt_markdown,
        ),
    )
    try:
        return EngineeringSeedPlan.from_payload(payload)
    except RuntimeError as exc:
        raise CodexRuntimeError(str(exc)) from exc


def plan_pm_parent_issues_with_runtime(
    *,
    runtime: CodexRuntime,
    prompt_markdown: str,
    allowed_project_keys: list[str],
    project_issue_types_by_key: dict[str, list[str]],
    invocation_context: AgentInvocationContext,
) -> PmParentSeedPlan:
    payload = invoke_runtime_json(
        runtime=runtime,
        context=invocation_context,
        system_prompt=render_prompt("discord/pm_seed_batch_system.j2"),
        user_prompt=render_prompt(
            "discord/pm_seed_batch_user.j2",
            allowed_project_keys_json=json.dumps(allowed_project_keys),
            project_issue_types_json=json.dumps(project_issue_types_by_key, sort_keys=True),
            prompt_markdown=prompt_markdown,
        ),
    )
    try:
        return PmParentSeedPlan.from_payload(payload)
    except RuntimeError as exc:
        raise CodexRuntimeError(str(exc)) from exc
