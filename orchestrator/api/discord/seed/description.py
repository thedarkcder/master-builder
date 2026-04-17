from __future__ import annotations


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
    content = [
        _heading("Objective"),
        _bullet_list([objective.strip() or "No objective provided"]),
        _heading("User / Business Value"),
        _bullet_list([user_value.strip() or "User value was not provided"]),
        _heading("Recommendation"),
        _bullet_list([recommendation.strip() or "Recommendation was not provided"]),
        _heading("PM Status"),
        _bullet_list([pm_status.strip() if isinstance(pm_status, str) and pm_status.strip() else "PM status was not provided"]),
        _heading("Planning State"),
        _bullet_list(
            [planning_state.strip() if isinstance(planning_state, str) and planning_state.strip() else "Planning state was not provided"]
        ),
        _heading("Architecture Context"),
        _bullet_list(
            architecture_summary
            if architecture_summary
            else ["Architecture context was not provided"]
        ),
    ]
    if isinstance(architecture_diagram, str) and architecture_diagram.strip():
        content.extend(
            [
                _heading("Architecture Diagram"),
                _paragraph("Mermaid diagram generated during backlog planning."),
                _code_block(architecture_diagram.strip(), language="mermaid"),
            ]
        )
    content.extend(
        [
        _heading("Scope In"),
        _bullet_list(scope_in),
        _heading("Scope Out"),
        _bullet_list(scope_out),
        _heading("Acceptance Criteria"),
        _bullet_list(acceptance_criteria if acceptance_criteria else ["Criteria were not provided"]),
        _heading("UI / Design / References"),
        _bullet_list(ui_references),
        _heading("Success Outcomes"),
        _bullet_list(success_outcomes),
        _heading("Dependencies / Risks"),
        _bullet_list(dependencies_and_risks if dependencies_and_risks else ["No explicit dependencies or risks were provided"]),
        _heading("Open Questions"),
        _bullet_list(open_questions if open_questions else ["No open questions remain"]),
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
    content = [
        _heading("Technical Objective"),
        _bullet_list([technical_objective.strip() or "Technical objective was not provided"]),
        _heading("Parent Feature Link"),
        _bullet_list([f"{parent_issue_key}: {parent_summary}".strip(": ")]),
        _heading("Behavior Slice"),
        _bullet_list([behavior_slice.strip() or "Behavior slice was not provided"]),
        _heading("Implementation Plan"),
        _bullet_list(implementation_plan if implementation_plan else ["Implementation details were not provided"]),
        _heading("How to Test"),
        _bullet_list(how_to_test if how_to_test else ["How-to-test steps were not provided"]),
        _heading("Done Criteria"),
        _bullet_list(done_criteria if done_criteria else ["Done criteria were not provided"]),
        _heading("Technical Dependencies / Risks"),
        _bullet_list(dependencies_and_risks if dependencies_and_risks else ["No explicit technical dependencies or risks were provided"]),
        _heading("Implementation Decisions"),
        _bullet_list(
            implementation_decisions
            if implementation_decisions
            else [
                "Decision owner: Engineering child team.",
                "Approval path: child PR review unless product behavior or non-functional requirements change.",
                "No implementation decisions have been recorded yet.",
            ]
        ),
        _heading("Specialist Planning Context"),
        _bullet_list(specialist_summary if specialist_summary else ["No specialist planning context was provided"]),
        _heading("Planning State"),
        _bullet_list([planning_state.strip() if isinstance(planning_state, str) and planning_state.strip() else "Planning state was not provided"]),
        _heading("Synced From Parent Revision"),
        _bullet_list([parent_revision or "unknown"]),
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
