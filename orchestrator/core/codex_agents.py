from __future__ import annotations

import json
from collections.abc import Callable

from orchestrator.core.codex_runtime import CodexRuntime, CodexRuntimeError
from orchestrator.core.prompt_templates import render_prompt
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

    def pm(self, request: WorkflowRequest) -> PmPlan:
        payload = self._runtime.run_json(
            system_prompt=render_prompt("workflow/pm_system.j2"),
            user_prompt=render_prompt(
                "workflow/pm_user.j2",
                tenant_id=request.tenant_id,
                run_id=request.run_id,
                issue_key=request.issue_key,
                issue_summary=request.issue_summary,
                issue_description=request.issue_description,
            ),
            working_dir=request.execution_repo_dir,
            on_log_line=self._stage_log_sink(request=request, stage="pm", attempt=0),
        )
        return PmPlan(
            plan_steps=_string_list(payload.get("plan_steps"), fallback=["Analyze scope", "Implement", "Validate"]),
            acceptance_criteria=_string_list(
                payload.get("acceptance_criteria"),
                fallback=["Behavior implemented", "Tests and verification provided"],
            ),
            risks=_string_list(payload.get("risks"), fallback=[]),
        )

    def dev(
        self,
        request: WorkflowRequest,
        plan: PmPlan,
        attempt: int,
        feedback: str | None,
    ) -> DevResult:
        payload = self._runtime.run_json(
            system_prompt=render_prompt("workflow/dev_system.j2"),
            user_prompt=render_prompt(
                "workflow/dev_user.j2",
                tenant_id=request.tenant_id,
                project_id=request.project_id or "unknown",
                run_id=request.run_id,
                issue_key=request.issue_key,
                attempt=attempt,
                feedback=feedback or "none",
                plan_json=json.dumps(plan.plan_steps),
                acceptance_criteria_json=json.dumps(plan.acceptance_criteria),
            ),
            working_dir=request.execution_repo_dir,
            on_log_line=self._stage_log_sink(request=request, stage="dev", attempt=attempt),
        )
        pr_url_raw = payload.get("pr_url")
        pr_url = str(pr_url_raw).strip() if isinstance(pr_url_raw, str) and str(pr_url_raw).strip() else None
        return DevResult(
            change_summary=_string_list(payload.get("change_summary"), fallback=["No change summary provided by Codex"]),
            pr_url=pr_url,
        )

    def test(
        self,
        request: WorkflowRequest,
        plan: PmPlan,
        dev_result: DevResult,
        attempt: int,
    ) -> TestResult:
        payload = self._runtime.run_json(
            system_prompt=render_prompt("workflow/test_system.j2"),
            user_prompt=render_prompt(
                "workflow/test_user.j2",
                tenant_id=request.tenant_id,
                project_id=request.project_id or "unknown",
                run_id=request.run_id,
                issue_key=request.issue_key,
                attempt=attempt,
                dev_summary_json=json.dumps(dev_result.change_summary),
                pr_url=dev_result.pr_url or "none",
                suggested_test_commands_json=json.dumps(request.suggested_test_commands),
            ),
            working_dir=request.execution_repo_dir,
            on_log_line=self._stage_log_sink(request=request, stage="test", attempt=attempt),
        )

        passed = bool(payload.get("passed"))
        feedback_raw = payload.get("feedback")
        feedback = str(feedback_raw).strip() if isinstance(feedback_raw, str) and str(feedback_raw).strip() else None
        guidance = _string_list(
            payload.get("guidance"),
            fallback=request.suggested_test_commands or ["Run project test suite"],
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
        payload = self._runtime.run_json(
            system_prompt=render_prompt("workflow/review_system.j2"),
            user_prompt=render_prompt(
                "workflow/review_user.j2",
                tenant_id=request.tenant_id,
                project_id=request.project_id or "unknown",
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
            ),
            working_dir=request.execution_repo_dir,
            on_log_line=self._stage_log_sink(request=request, stage="review", attempt=attempt),
        )

        approved = bool(payload.get("approved"))
        feedback_raw = payload.get("feedback")
        feedback = str(feedback_raw).strip() if isinstance(feedback_raw, str) and str(feedback_raw).strip() else None
        pr_url_raw = payload.get("pr_url")
        pr_url = str(pr_url_raw).strip() if isinstance(pr_url_raw, str) and str(pr_url_raw).strip() else dev_result.pr_url
        return ReviewResult(
            approved=approved,
            summary=_string_list(payload.get("summary"), fallback=["No review summary provided by Codex"]),
            feedback=feedback,
            pr_url=pr_url,
        )



def _string_list(value: object, *, fallback: list[str]) -> list[str]:
    if isinstance(value, list):
        normalized = [str(item).strip() for item in value if str(item).strip()]
        if normalized:
            return normalized
    return fallback



def answer_board_question_with_codex(
    *,
    runtime: CodexRuntime,
    question: str,
    project_keys: list[str],
    issues: list[dict],
    status_counts: dict[str, int],
    history: list[dict] | None = None,
    github_context: dict | None = None,
) -> str:
    normalized_history = history or []
    normalized_github_context = github_context or {}
    payload = runtime.run_json(
        system_prompt=render_prompt("discord/ask_answer_system.j2"),
        user_prompt=render_prompt(
            "discord/ask_answer_user.j2",
            question=question,
            project_keys_json=json.dumps(project_keys),
            status_counts_json=json.dumps(status_counts),
            github_context_json=json.dumps(normalized_github_context),
            history_json=json.dumps(normalized_history[:6]),
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
    history: list[dict] | None = None,
    github_context: dict | None = None,
) -> dict:
    normalized_history = history or []
    normalized_github_context = github_context or {}
    payload = runtime.run_json(
        system_prompt=render_prompt("discord/ask_intent_system.j2"),
        user_prompt=render_prompt(
            "discord/ask_intent_user.j2",
            question=question,
            project_keys_json=json.dumps(project_keys),
            status_counts_json=json.dumps(status_counts),
            github_context_json=json.dumps(normalized_github_context),
            history_json=json.dumps(normalized_history[:6]),
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
) -> dict:
    payload = runtime.run_json(
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
