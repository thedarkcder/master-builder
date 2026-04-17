from __future__ import annotations

from orchestrator.api.discord.seed.description import build_parent_feature_description


def _heading_texts(doc: dict) -> list[str]:
    headings: list[str] = []
    for node in doc.get("content", []):
        if node.get("type") != "heading":
            continue
        text = "".join(
            str(item.get("text") or "")
            for item in node.get("content", [])
            if isinstance(item, dict)
        ).strip()
        if text:
            headings.append(text)
    return headings


def test_parent_feature_description_omits_pm_handoff_and_sync_status_sections() -> None:
    doc = build_parent_feature_description(
        objective="Unify tenant authorization",
        user_value="Admins can manage access safely.",
        recommendation="Proceed with the identity redesign.",
        scope_in=["Memberships", "Groups", "Roles"],
        scope_out=["SSO overhaul"],
        acceptance_criteria=["Tenant admins can configure role assignments."],
        ui_references=["Existing admin settings"],
        success_outcomes=["Fewer support escalations"],
        dependencies_and_risks=["Migration sequencing"],
        open_questions=["What is the invite TTL?"],
        parent_revision="normalized-parent-brief",
        sync_status="children_syncing",
        pm_status="pm_completed",
        planning_state="planning_completed",
        architecture_summary=["Identity policy remains tenant-scoped."],
        architecture_diagram=None,
    )

    headings = _heading_texts(doc)

    assert "PM Handoff" not in headings
    assert "Sync Status" not in headings
    assert "Objective" in headings
    assert "Open Questions" in headings
