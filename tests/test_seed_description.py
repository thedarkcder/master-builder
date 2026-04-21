from __future__ import annotations

import json

from orchestrator.api.discord.seed.description import (
    build_engineering_child_description,
    build_parent_feature_description,
)
from orchestrator.tools.jira_oauth_issue_service import (
    MAX_JIRA_ADF_DOCUMENT_BYTES,
    _to_adf_description,
)


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


def _flatten_text(value: object) -> str:
    if isinstance(value, dict):
        text = str(value.get("text") or "").strip()
        child_text = " ".join(_flatten_text(item) for item in value.get("content", []) if item is not None).strip()
        return " ".join(part for part in (text, child_text) if part).strip()
    if isinstance(value, list):
        return " ".join(_flatten_text(item) for item in value if item is not None).strip()
    return ""


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


def test_seed_description_builders_budget_content_before_jira_transport_cap() -> None:
    huge_line = "Architecture context " + ("X" * 10_000)
    parent_doc = build_parent_feature_description(
        objective=huge_line,
        user_value=huge_line,
        recommendation=huge_line,
        scope_in=[huge_line] * 20,
        scope_out=[huge_line] * 20,
        acceptance_criteria=[huge_line] * 20,
        ui_references=[huge_line] * 20,
        success_outcomes=[huge_line] * 20,
        dependencies_and_risks=[huge_line] * 20,
        open_questions=[huge_line] * 20,
        parent_revision="normalized-parent-brief",
        sync_status="children_syncing",
        pm_status="pm_completed",
        planning_state="planning_completed",
        architecture_summary=[huge_line] * 20,
        architecture_diagram="flowchart TD\n" + ("Parent-->Planner\n" * 1000),
    )
    child_doc = build_engineering_child_description(
        parent_issue_key="MAB-215",
        parent_summary=huge_line,
        parent_revision="normalized-parent-brief",
        capability=huge_line,
        delivery=huge_line,
        expected_outcome=huge_line,
        acceptance_criteria=[huge_line] * 20,
        how_to_test=[huge_line] * 20,
        done_means=[huge_line] * 20,
        dependencies_and_risks=[huge_line] * 20,
        specialist_summary=[huge_line] * 20,
        planning_state="planning_completed",
    )

    for doc in (parent_doc, child_doc):
        bounded = _to_adf_description(doc)
        serialized = json.dumps(bounded, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        assert len(serialized) <= MAX_JIRA_ADF_DOCUMENT_BYTES
        assert "Content truncated to fit Jira content size limit." not in _flatten_text(bounded)
