from __future__ import annotations

_ELLIPSIS = "..."

_PARENT_SECTION_ITEM_LIMITS: dict[str, int] = {
    "architecture_summary": 5,
    "scope_in": 8,
    "scope_out": 8,
    "acceptance_criteria": 10,
    "ui_references": 6,
    "success_outcomes": 8,
    "dependencies_and_risks": 8,
    "open_questions": 6,
}

_CHILD_SECTION_ITEM_LIMITS: dict[str, int] = {
    "implementation_plan": 10,
    "how_to_test": 8,
    "done_criteria": 8,
    "dependencies_and_risks": 8,
    "implementation_decisions": 6,
    "specialist_summary": 8,
}

_TEXT_LIMITS: dict[str, int] = {
    "objective": 500,
    "user_value": 500,
    "recommendation": 500,
    "architecture_item": 260,
    "scope_item": 180,
    "acceptance_item": 220,
    "ui_reference_item": 180,
    "success_item": 180,
    "dependency_item": 200,
    "question_item": 220,
    "behavior_slice": 420,
    "technical_objective": 500,
    "implementation_item": 220,
    "how_to_test_item": 180,
    "done_criteria_item": 180,
    "decision_item": 220,
    "specialist_item": 220,
    "planning_state": 120,
    "revision": 120,
}

_MAX_ARCHITECTURE_DIAGRAM_CHARS = 3_500
_MAX_ARCHITECTURE_DIAGRAM_LINES = 80


def _truncate_text(text: str, *, max_chars: int) -> str:
    normalized = " ".join(str(text or "").split()).strip()
    if not normalized:
        return ""
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max(0, max_chars - len(_ELLIPSIS))].rstrip() + _ELLIPSIS


def _budget_items(
    items: list[str],
    *,
    max_items: int,
    max_chars: int,
    empty_fallback: str,
) -> list[str]:
    normalized: list[str] = []
    for item in items:
        text = _truncate_text(str(item or ""), max_chars=max_chars)
        if text:
            normalized.append(text)
        if len(normalized) >= max_items:
            break
    return normalized if normalized else [empty_fallback]


def _budget_code_block(text: str) -> str | None:
    normalized = str(text or "").strip()
    if not normalized:
        return None
    lines = normalized.splitlines()[:_MAX_ARCHITECTURE_DIAGRAM_LINES]
    bounded = "\n".join(lines)
    if len(bounded) <= _MAX_ARCHITECTURE_DIAGRAM_CHARS:
        return bounded
    return bounded[: _MAX_ARCHITECTURE_DIAGRAM_CHARS - len(_ELLIPSIS)].rstrip() + _ELLIPSIS


def _heading(text: str) -> dict:
    return {
        "type": "heading",
        "attrs": {"level": 3},
        "content": [{"type": "text", "text": text}],
    }


def _bullet_list(items: list[str]) -> dict:
    normalized_items = items if items else ["Not specified"]
    return {
        "type": "bulletList",
        "content": [
            {
                "type": "listItem",
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": item}]}],
            }
            for item in normalized_items
        ],
    }


def _paragraph(text: str) -> dict:
    return {
        "type": "paragraph",
        "content": [{"type": "text", "text": text}],
    }


def _code_block(text: str, *, language: str) -> dict:
    return {
        "type": "codeBlock",
        "attrs": {"language": language},
        "content": [{"type": "text", "text": text}],
    }


def build_parent_feature_description(
    *,
    objective: str,
    user_value: str,
    recommendation: str,
    scope_in: list[str],
    scope_out: list[str],
    acceptance_criteria: list[str],
    ui_references: list[str],
    success_outcomes: list[str],
    dependencies_and_risks: list[str],
    open_questions: list[str],
    parent_revision: str,
    sync_status: str,
    pm_status: str | None = None,
    planning_state: str | None = None,
    architecture_summary: list[str] | None = None,
    architecture_diagram: str | None = None,
) -> dict:
    bounded_objective = _truncate_text(objective, max_chars=_TEXT_LIMITS["objective"]) or "No objective provided"
    bounded_user_value = _truncate_text(user_value, max_chars=_TEXT_LIMITS["user_value"]) or "User value was not provided"
    bounded_recommendation = (
        _truncate_text(recommendation, max_chars=_TEXT_LIMITS["recommendation"]) or "Recommendation was not provided"
    )
    bounded_architecture_summary = _budget_items(
        architecture_summary or [],
        max_items=_PARENT_SECTION_ITEM_LIMITS["architecture_summary"],
        max_chars=_TEXT_LIMITS["architecture_item"],
        empty_fallback="Architecture context was not provided",
    )
    bounded_scope_in = _budget_items(
        scope_in,
        max_items=_PARENT_SECTION_ITEM_LIMITS["scope_in"],
        max_chars=_TEXT_LIMITS["scope_item"],
        empty_fallback="Not specified",
    )
    bounded_scope_out = _budget_items(
        scope_out,
        max_items=_PARENT_SECTION_ITEM_LIMITS["scope_out"],
        max_chars=_TEXT_LIMITS["scope_item"],
        empty_fallback="Not specified",
    )
    bounded_acceptance_criteria = _budget_items(
        acceptance_criteria,
        max_items=_PARENT_SECTION_ITEM_LIMITS["acceptance_criteria"],
        max_chars=_TEXT_LIMITS["acceptance_item"],
        empty_fallback="Criteria were not provided",
    )
    bounded_ui_references = _budget_items(
        ui_references,
        max_items=_PARENT_SECTION_ITEM_LIMITS["ui_references"],
        max_chars=_TEXT_LIMITS["ui_reference_item"],
        empty_fallback="Not specified",
    )
    bounded_success_outcomes = _budget_items(
        success_outcomes,
        max_items=_PARENT_SECTION_ITEM_LIMITS["success_outcomes"],
        max_chars=_TEXT_LIMITS["success_item"],
        empty_fallback="Not specified",
    )
    bounded_dependencies_and_risks = _budget_items(
        dependencies_and_risks,
        max_items=_PARENT_SECTION_ITEM_LIMITS["dependencies_and_risks"],
        max_chars=_TEXT_LIMITS["dependency_item"],
        empty_fallback="No explicit dependencies or risks were provided",
    )
    bounded_open_questions = _budget_items(
        open_questions,
        max_items=_PARENT_SECTION_ITEM_LIMITS["open_questions"],
        max_chars=_TEXT_LIMITS["question_item"],
        empty_fallback="No open questions remain",
    )
    bounded_architecture_diagram = _budget_code_block(architecture_diagram)

    content = [
        _heading("Objective"),
        _bullet_list([bounded_objective]),
        _heading("User / Business Value"),
        _bullet_list([bounded_user_value]),
        _heading("Recommendation"),
        _bullet_list([bounded_recommendation]),
        _heading("Architecture Context"),
        _bullet_list(bounded_architecture_summary),
    ]
    if bounded_architecture_diagram:
        content.extend(
            [
                _heading("Architecture Diagram"),
                _paragraph("Mermaid diagram generated during backlog planning."),
                _code_block(bounded_architecture_diagram, language="mermaid"),
            ]
        )
    content.extend(
        [
        _heading("Scope In"),
        _bullet_list(bounded_scope_in),
        _heading("Scope Out"),
        _bullet_list(bounded_scope_out),
        _heading("Acceptance Criteria"),
        _bullet_list(bounded_acceptance_criteria),
        _heading("UI / Design / References"),
        _bullet_list(bounded_ui_references),
        _heading("Success Outcomes"),
        _bullet_list(bounded_success_outcomes),
        _heading("Dependencies / Risks"),
        _bullet_list(bounded_dependencies_and_risks),
        _heading("Open Questions"),
        _bullet_list(bounded_open_questions),
        _heading("Good To Do Checklist"),
        _bullet_list(
            [
                "[ ] Objective is clear",
                "[ ] Scope is explicit (in/out)",
                "[ ] Acceptance criteria are testable",
                "[ ] UI/design references are attached or explicitly not needed",
                "[ ] Outcomes are defined for users and the business",
                "[ ] Dependencies and risks are identified",
            ]
        ),
        _heading("Notes / Links"),
        _bullet_list(["Owned by Product Management", "Reported via Discord PM flow"]),
        ]
    )
    return {"type": "doc", "version": 1, "content": content}


def build_engineering_child_description(
    *,
    parent_issue_key: str,
    parent_summary: str,
    parent_revision: str,
    behavior_slice: str,
    technical_objective: str,
    implementation_plan: list[str],
    how_to_test: list[str],
    done_criteria: list[str],
    dependencies_and_risks: list[str],
    implementation_decisions: list[str] | None = None,
    specialist_summary: list[str] | None = None,
    planning_state: str | None = None,
) -> dict:
    bounded_technical_objective = (
        _truncate_text(technical_objective, max_chars=_TEXT_LIMITS["technical_objective"])
        or "Technical objective was not provided"
    )
    bounded_parent_link = _truncate_text(
        f"{parent_issue_key}: {parent_summary}".strip(": "),
        max_chars=_TEXT_LIMITS["technical_objective"],
    ) or "Parent link was not provided"
    bounded_behavior_slice = (
        _truncate_text(behavior_slice, max_chars=_TEXT_LIMITS["behavior_slice"]) or "Behavior slice was not provided"
    )
    bounded_implementation_plan = _budget_items(
        implementation_plan,
        max_items=_CHILD_SECTION_ITEM_LIMITS["implementation_plan"],
        max_chars=_TEXT_LIMITS["implementation_item"],
        empty_fallback="Implementation details were not provided",
    )
    bounded_how_to_test = _budget_items(
        how_to_test,
        max_items=_CHILD_SECTION_ITEM_LIMITS["how_to_test"],
        max_chars=_TEXT_LIMITS["how_to_test_item"],
        empty_fallback="How-to-test steps were not provided",
    )
    bounded_done_criteria = _budget_items(
        done_criteria,
        max_items=_CHILD_SECTION_ITEM_LIMITS["done_criteria"],
        max_chars=_TEXT_LIMITS["done_criteria_item"],
        empty_fallback="Done criteria were not provided",
    )
    bounded_dependencies_and_risks = _budget_items(
        dependencies_and_risks,
        max_items=_CHILD_SECTION_ITEM_LIMITS["dependencies_and_risks"],
        max_chars=_TEXT_LIMITS["dependency_item"],
        empty_fallback="No explicit technical dependencies or risks were provided",
    )
    bounded_implementation_decisions = _budget_items(
        implementation_decisions
        if implementation_decisions
        else [
            "Decision owner: Engineering child team.",
            "Approval path: child PR review unless product behavior or non-functional requirements change.",
            "No implementation decisions have been recorded yet.",
        ],
        max_items=_CHILD_SECTION_ITEM_LIMITS["implementation_decisions"],
        max_chars=_TEXT_LIMITS["decision_item"],
        empty_fallback="No implementation decisions have been recorded yet.",
    )
    bounded_specialist_summary = _budget_items(
        specialist_summary if specialist_summary else ["No specialist planning context was provided"],
        max_items=_CHILD_SECTION_ITEM_LIMITS["specialist_summary"],
        max_chars=_TEXT_LIMITS["specialist_item"],
        empty_fallback="No specialist planning context was provided",
    )
    bounded_planning_state = _truncate_text(
        planning_state if isinstance(planning_state, str) else "",
        max_chars=_TEXT_LIMITS["planning_state"],
    ) or "Planning state was not provided"
    bounded_parent_revision = _truncate_text(parent_revision or "unknown", max_chars=_TEXT_LIMITS["revision"]) or "unknown"

    content = [
        _heading("Technical Objective"),
        _bullet_list([bounded_technical_objective]),
        _heading("Parent Feature Link"),
        _bullet_list([bounded_parent_link]),
        _heading("Behavior Slice"),
        _bullet_list([bounded_behavior_slice]),
        _heading("Implementation Plan"),
        _bullet_list(bounded_implementation_plan),
        _heading("How to Test"),
        _bullet_list(bounded_how_to_test),
        _heading("Done Criteria"),
        _bullet_list(bounded_done_criteria),
        _heading("Technical Dependencies / Risks"),
        _bullet_list(bounded_dependencies_and_risks),
        _heading("Implementation Decisions"),
        _bullet_list(bounded_implementation_decisions),
        _heading("Specialist Planning Context"),
        _bullet_list(bounded_specialist_summary),
        _heading("Planning State"),
        _bullet_list([bounded_planning_state]),
        _heading("Synced From Parent Revision"),
        _bullet_list([bounded_parent_revision]),
        _heading("Notes / Links"),
        _bullet_list(["Owned by Engineering", f"Parent issue: {parent_issue_key}"]),
    ]
    return {"type": "doc", "version": 1, "content": content}


def build_seed_issue_description(
    *,
    objective: str,
    scope_in: list[str],
    scope_out: list[str],
    acceptance_criteria: list[str],
    how_to_test: list[str],
    nfr_intent: str,
    dependencies_and_risks: list[str],
) -> dict:
    return build_engineering_child_description(
        parent_issue_key="PARENT-UNKNOWN",
        parent_summary="Parent feature not linked",
        parent_revision=nfr_intent.strip() or "Not specified (MVP or scale-ready decision required)",
        behavior_slice=objective,
        technical_objective=objective,
        implementation_plan=[*scope_in, *scope_out] or ["Implementation scope was not provided"],
        how_to_test=how_to_test,
        done_criteria=acceptance_criteria,
        dependencies_and_risks=dependencies_and_risks,
        implementation_decisions=None,
    )
