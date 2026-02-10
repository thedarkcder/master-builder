from __future__ import annotations


def build_seed_issue_description(
    *,
    objective: str,
    scope_in: list[str],
    scope_out: list[str],
    acceptance_criteria: list[str],
) -> dict:
    def heading(text: str) -> dict:
        return {
            "type": "heading",
            "attrs": {"level": 3},
            "content": [{"type": "text", "text": text}],
        }

    def bullet_list(items: list[str]) -> dict:
        return {
            "type": "bulletList",
            "content": [
                {
                    "type": "listItem",
                    "content": [{"type": "paragraph", "content": [{"type": "text", "text": item}]}],
                }
                for item in items
            ],
        }

    scope_in_items = scope_in if scope_in else ["Not specified"]
    scope_out_items = scope_out if scope_out else ["Not specified"]
    acceptance_items = acceptance_criteria if acceptance_criteria else ["Criteria were not provided"]

    content = [
        heading("Objective"),
        bullet_list([objective.strip() or "No objective provided"]),
        heading("Scope In"),
        bullet_list(scope_in_items),
        heading("Scope Out"),
        bullet_list(scope_out_items),
        heading("Acceptance Criteria"),
        bullet_list(acceptance_items),
        heading("Good To Do Checklist"),
        bullet_list(
            [
                "[ ] Objective is clear",
                "[ ] Scope is explicit (in/out)",
                "[ ] Acceptance criteria are testable",
                "[ ] How-to-test is defined",
                "[ ] MVP vs scale-ready is decided",
            ]
        ),
        heading("Decision Gate Triggers"),
        bullet_list(
            [
                "[ ] Requirements are ambiguous",
                "[ ] Design choice impacts NFRs/reliability/cost/security",
            ]
        ),
        heading("Notes / Links"),
        bullet_list(["Reported via Discord issue seeding flow"]),
    ]
    return {"type": "doc", "version": 1, "content": content}
