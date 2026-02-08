from __future__ import annotations

import json

from orchestrator.core.codex_runtime import CodexRuntime, CodexRuntimeError
from orchestrator.core.workflow_runner import (
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
                "You are the PM agent in an orchestrated software workflow. "
                "Return strict JSON only with keys: plan_steps, acceptance_criteria, risks."
            ),
            user_prompt=(
                f"Issue key: {request.issue_key}\n"
                f"Summary: {request.issue_summary}\n"
                f"Description:\n{request.issue_description}\n\n"
                "Constraints:\n"
                "- Keep plan concise and executable.\n"
                "- plan_steps must be 3-8 concrete steps.\n"
                "- acceptance_criteria must map to observable outcomes.\n"
                "- risks should include technical and rollout risks when relevant.\n"
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
                "You are the Dev agent. Return strict JSON only with keys: change_summary, pr_url. "
                "If no PR exists yet set pr_url to null."
            ),
            user_prompt=(
                f"Issue key: {request.issue_key}\n"
                f"Attempt: {attempt}\n"
                f"Feedback from prior stage: {feedback or 'none'}\n"
                f"Plan: {json.dumps(plan.plan_steps)}\n"
                f"Acceptance criteria: {json.dumps(plan.acceptance_criteria)}\n"
                "Respond with what was implemented this attempt."
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
                "You are the Test agent. Return strict JSON only with keys: "
                "passed (boolean), guidance (array of strings), feedback (string|null)."
            ),
            user_prompt=(
                f"Issue key: {request.issue_key}\n"
                f"Attempt: {attempt}\n"
                f"Dev summary: {json.dumps(dev_result.change_summary)}\n"
                f"PR URL: {dev_result.pr_url or 'none'}\n"
                f"Suggested test commands: {json.dumps(request.suggested_test_commands)}\n"
                "Evaluate whether the implementation is ready for review."
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
                "You are the Review agent. Return strict JSON only with keys: "
                "approved (boolean), summary (array of strings), feedback (string|null), pr_url (string|null)."
            ),
            user_prompt=(
                f"Issue key: {request.issue_key}\n"
                f"Attempt: {attempt}\n"
                f"Plan steps: {json.dumps(plan.plan_steps)}\n"
                f"Acceptance criteria: {json.dumps(plan.acceptance_criteria)}\n"
                f"Dev summary: {json.dumps(dev_result.change_summary)}\n"
                f"Test passed: {test_result.passed}\n"
                f"Test guidance: {json.dumps(test_result.guidance)}\n"
                f"Test feedback: {test_result.feedback or 'none'}\n"
                f"PR URL: {dev_result.pr_url or 'none'}\n"
                "Approve only if change quality is sufficient and test result is acceptable."
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
) -> str:
    payload = runtime.run_json(
        system_prompt=(
            "You answer Discord board questions for an engineering team. "
            "Return strict JSON only with key 'message' (string). "
            "Be concise: max 5 lines, no markdown tables."
        ),
        user_prompt=(
            f"Question: {question}\n"
            f"Projects: {json.dumps(project_keys)}\n"
            f"Status counts: {json.dumps(status_counts)}\n"
            f"Issues (sample): {json.dumps(issues[:40])}\n"
            "Answer directly and include specific issue keys when helpful."
        ),
    )
    message = str(payload.get("message") or "").strip()
    if not message:
        raise CodexRuntimeError("Codex did not return an ask/board message")
    return message


def plan_seed_issues_with_codex(
    *,
    runtime: CodexRuntime,
    prompt_markdown: str,
    allowed_project_keys: list[str],
) -> dict:
    payload = runtime.run_json(
        system_prompt=(
            "You split product specs into Jira issue drafts. "
            "Return strict JSON only with keys: project_key (string), issues (array). "
            "Each issue item must include: summary (string), objective (string), "
            "acceptance_criteria (array of strings), labels (array of strings)."
        ),
        user_prompt=(
            f"Allowed Jira project keys: {json.dumps(allowed_project_keys)}\n"
            f"Markdown spec:\n{prompt_markdown}\n\n"
            "Rules:\n"
            "- Create 1-12 concrete implementation issues.\n"
            "- Keep each summary under 90 characters.\n"
            "- Choose project_key from allowed keys only.\n"
            "- Add useful labels (lowercase, kebab-case).\n"
            "- acceptance_criteria entries should be testable outcomes.\n"
        ),
    )
    if not isinstance(payload, dict):
        raise CodexRuntimeError("Codex did not return an issue-seeding JSON object")
    return payload
