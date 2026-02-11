from __future__ import annotations

import json

from orchestrator.core.codex_runtime import CodexRuntime, CodexRuntimeError
from orchestrator.core.workflow.runner import (
    DevResult,
    PmPlan,
    ReviewResult,
    TestResult,
    WorkflowRequest,
)


class CodexWorkflowAgents:
    def __init__(self, *, runtime: CodexRuntime):
        self._runtime = runtime

    def pm(self, request: WorkflowRequest) -> PmPlan:
        payload = self._runtime.run_json(
            system_prompt=(
                "You are the PM stage agent in an orchestrated software workflow. "
                "Honor the enforcement context already supplied by the runtime. "
                "Return strict JSON only with keys: plan_steps, acceptance_criteria, risks."
            ),
            user_prompt=(
                "Stage: pm\n"
                f"Tenant ID: {request.tenant_id}\n"
                f"Run ID: {request.run_id}\n"
                f"Issue key: {request.issue_key}\n"
                f"Summary: {request.issue_summary}\n"
                f"Description:\n{request.issue_description}\n\n"
                "Output contract:\n"
                "- plan_steps: array of concrete execution steps.\n"
                "- acceptance_criteria: array of observable outcomes.\n"
                "- risks: array of risks.\n"
            ),
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
            system_prompt=(
                "You are the Dev stage agent. Honor the enforcement context already supplied by the runtime. "
                "Return strict JSON only with keys: change_summary, pr_url. "
                "If no PR exists yet set pr_url to null."
            ),
            user_prompt=(
                "Stage: dev\n"
                f"Tenant ID: {request.tenant_id}\n"
                f"Run ID: {request.run_id}\n"
                f"Issue key: {request.issue_key}\n"
                f"Attempt: {attempt}\n"
                f"Feedback from prior stage: {feedback or 'none'}\n"
                f"Plan: {json.dumps(plan.plan_steps)}\n"
                f"Acceptance criteria: {json.dumps(plan.acceptance_criteria)}\n"
                "Output contract:\n"
                "- change_summary: array of implemented changes this attempt.\n"
                "- pr_url: PR URL string or null.\n"
            ),
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
            system_prompt=(
                "You are the Test stage agent. Honor the enforcement context already supplied by the runtime. "
                "Return strict JSON only with keys: "
                "passed (boolean), guidance (array of strings), feedback (string|null)."
            ),
            user_prompt=(
                "Stage: test\n"
                f"Tenant ID: {request.tenant_id}\n"
                f"Run ID: {request.run_id}\n"
                f"Issue key: {request.issue_key}\n"
                f"Attempt: {attempt}\n"
                f"Dev summary: {json.dumps(dev_result.change_summary)}\n"
                f"PR URL: {dev_result.pr_url or 'none'}\n"
                f"Suggested test commands: {json.dumps(request.suggested_test_commands)}\n"
                "Output contract:\n"
                "- passed: boolean readiness signal.\n"
                "- guidance: array of concrete validation actions.\n"
                "- feedback: blocking reason string or null.\n"
            ),
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
            system_prompt=(
                "You are the Review stage agent. Honor the enforcement context already supplied by the runtime. "
                "Return strict JSON only with keys: "
                "approved (boolean), summary (array of strings), feedback (string|null), pr_url (string|null)."
            ),
            user_prompt=(
                "Stage: review\n"
                f"Tenant ID: {request.tenant_id}\n"
                f"Run ID: {request.run_id}\n"
                f"Issue key: {request.issue_key}\n"
                f"Attempt: {attempt}\n"
                f"Plan steps: {json.dumps(plan.plan_steps)}\n"
                f"Acceptance criteria: {json.dumps(plan.acceptance_criteria)}\n"
                f"Dev summary: {json.dumps(dev_result.change_summary)}\n"
                f"Test passed: {test_result.passed}\n"
                f"Test guidance: {json.dumps(test_result.guidance)}\n"
                f"Test feedback: {test_result.feedback or 'none'}\n"
                f"PR URL: {dev_result.pr_url or 'none'}\n"
                "Output contract:\n"
                "- approved: boolean decision.\n"
                "- summary: array of review findings/outcome notes.\n"
                "- feedback: blocking feedback string or null.\n"
                "- pr_url: PR URL string or null.\n"
            ),
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
) -> str:
    normalized_history = history or []
    payload = runtime.run_json(
        system_prompt=(
            "You answer Discord board questions for an engineering team. "
            "Honor the enforcement context already supplied by the runtime. "
            "Return strict JSON only with key 'message' (string). "
            "Be concise: max 5 lines, no markdown tables."
        ),
        user_prompt=(
            "Stage: discord-ask-answer\n"
            f"Question: {question}\n"
            f"Projects: {json.dumps(project_keys)}\n"
            f"Status counts: {json.dumps(status_counts)}\n"
            f"Recent conversation context: {json.dumps(normalized_history[:6])}\n"
            f"Issues (sample): {json.dumps(issues[:40])}\n"
            "Answer directly and include specific issue keys when helpful."
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
) -> dict:
    normalized_history = history or []
    payload = runtime.run_json(
        system_prompt=(
            "You route Discord /ask requests for an engineering orchestration bot. "
            "Honor the enforcement context already supplied by the runtime. "
            "Return strict JSON only with keys: mode, summary, command. "
            "mode must be either 'answer' or 'command'. "
            "If mode='command', command must be a single supported command string that starts with '!' "
            "and uses one of: !status, !runs, !run <ISSUE_KEY>, !retry <ISSUE_KEY|RUN_ID>, "
            "!cancel <RUN_ID>, !link <ISSUE_KEY>, !issues seed <markdown spec>. "
            "If mode='answer', leave command empty."
        ),
        user_prompt=(
            "Stage: discord-ask-intent\n"
            f"Question: {question}\n"
            f"Projects: {json.dumps(project_keys)}\n"
            f"Status counts: {json.dumps(status_counts)}\n"
            f"Recent conversation context: {json.dumps(normalized_history[:6])}\n"
            f"Issues (sample): {json.dumps(issues[:40])}\n"
            "Choose command mode only when the user is clearly requesting an operational action."
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
        system_prompt=(
            "You split product specs into Jira issue drafts. "
            "Honor the enforcement context already supplied by the runtime. "
            "Return strict JSON only with keys: project_key (string), issues (array), questions (array). "
            "Each issue item must include: summary (string), objective (string), "
            "scope_in (array of strings), scope_out (array of strings), "
            "acceptance_criteria (array of strings), tags (array of strings), "
            "labels (array of strings), issue_type (string), and optional issue_key (string). "
            "questions should contain concise clarification questions only if required details are missing."
        ),
        user_prompt=(
            "Stage: discord-issues-seed\n"
            f"Allowed Jira project keys: {json.dumps(allowed_project_keys)}\n"
            f"Markdown spec:\n{prompt_markdown}\n\n"
            "Output contract:\n"
            "- issues must be implementation-ready and concrete.\n"
            "- project_key must be one of allowed_project_keys.\n"
            "- tags/labels should be short normalized tokens when present.\n"
            "- issue_type should be Task, Bug, or Story.\n"
            "- description content should be detailed enough for Objective, Scope In/Out, and Acceptance Criteria.\n"
            "- when reseeding existing work, set issue_key if explicitly known from source context.\n"
        ),
    )
    if not isinstance(payload, dict):
        raise CodexRuntimeError("Codex did not return an issue-seeding JSON object")
    return payload
