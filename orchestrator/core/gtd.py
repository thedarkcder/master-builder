from __future__ import annotations

from dataclasses import dataclass

from orchestrator.core.runtime.invocation import AgentInvocationContext, invoke_runtime_json
from orchestrator.core.runtime.runtime import CodexRuntimeError, build_codex_runtime
from orchestrator.core.config import get_settings
from orchestrator.core.prompt_templates import render_prompt


@dataclass(frozen=True)
class GoodToDoValidationResult:
    valid: bool
    missing_criteria: tuple[str, ...]
    clarification_questions: tuple[str, ...]


def _required_string_tuple(value: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise RuntimeError(f"Runtime GTD evaluation returned invalid {field}")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise RuntimeError(f"Runtime GTD evaluation returned invalid {field} item")
        stripped = item.strip()
        if stripped:
            normalized.append(stripped)
    return tuple(normalized)


def _validate_good_to_do_with_runtime(
    *,
    issue_summary: str,
    issue_description: str,
    tenant_id: str | None,
    project_id: str | None,
    issue_key: str | None,
    run_id: str | None,
) -> GoodToDoValidationResult:
    settings = get_settings()
    runtime = build_codex_runtime(session=None, settings=settings)
    try:
        payload = invoke_runtime_json(
            runtime=runtime,
            context=AgentInvocationContext(
                channel="system",
                tenant_id=tenant_id,
                project_id=project_id,
                command="policy",
                stage="good_to_do",
                working_dir=".",
                issue_key=issue_key,
                run_id=run_id,
                reasoning_effort="low",
                issue_description_chars=len(issue_description or ""),
            ),
            system_prompt=render_prompt("policy/gtd_system.j2"),
            user_prompt=render_prompt(
                "policy/gtd_user.j2",
                issue_summary=issue_summary,
                issue_description=issue_description,
            ),
        )
    except CodexRuntimeError as exc:
        raise RuntimeError(f"Runtime GTD evaluation failed: {exc}") from exc

    valid = bool(payload.get("valid"))
    missing_criteria = _required_string_tuple(payload.get("missing_criteria"), field="missing_criteria")
    clarification_questions = _required_string_tuple(
        payload.get("clarification_questions"),
        field="clarification_questions",
    )
    if not valid and not clarification_questions:
        raise RuntimeError("Runtime GTD evaluation returned invalid result without clarification questions")
    return GoodToDoValidationResult(
        valid=valid,
        missing_criteria=missing_criteria,
        clarification_questions=clarification_questions,
    )


def validate_good_to_do(
    *,
    issue_summary: str,
    issue_description: str,
    tenant_id: str | None = None,
    project_id: str | None = None,
    issue_key: str | None = None,
    run_id: str | None = None,
) -> GoodToDoValidationResult:
    return _validate_good_to_do_with_runtime(
        issue_summary=(issue_summary or "").strip(),
        issue_description=(issue_description or "").strip(),
        tenant_id=tenant_id,
        project_id=project_id,
        issue_key=issue_key,
        run_id=run_id,
    )
