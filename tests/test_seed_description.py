from __future__ import annotations

import json

from orchestrator.core.issue_fanout.description import (
    build_engineering_child_description,
    build_parent_feature_description,
)
from orchestrator.core.runtime.payload_models import TechnicalDecision
from orchestrator.tools.atlassian_oauth_issue_service import (
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
        child_text = " ".join(
            _flatten_text(item) for item in value.get("content", []) if item is not None
        ).strip()
        return " ".join(part for part in (text, child_text) if part).strip()
    if isinstance(value, list):
        return " ".join(
            _flatten_text(item) for item in value if item is not None
        ).strip()
    return ""


def _technical_decision() -> TechnicalDecision:
    return TechnicalDecision.from_payload(
        {
            "decision_id": "auth-boundary",
            "area": "security",
            "question": "Where should tenant authorization be enforced?",
            "options": [
                {
                    "option_id": "database-rls",
                    "title": "Database RLS",
                    "description": "Use database-owned row-level security as the enforcement backstop.",
                    "benefits": [
                        "Prevents app-layer omission from leaking tenant data"
                    ],
                    "risks": ["Requires migration and policy coverage"],
                }
            ],
            "selected_option_id": "database-rls",
            "rationale": "The database must own tenant isolation for high-risk tables",
            "evidence": ["Tenant-scoped tables have tenant_id"],
            "confidence": "high",
            "product_impact": "none",
        },
        context="test technical decision",
    )


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
    )

    headings = _heading_texts(doc)

    assert "PM Handoff" not in headings
    assert "Sync Status" not in headings
    assert "Architecture Context" not in headings
    assert "Architecture Diagram" not in headings
    assert "Objective" in headings
    assert "Architecture" not in headings
    assert "Open Questions" in headings
    assert "See Architecture:" not in _flatten_text(doc)


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
        serialized = json.dumps(
            bounded, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        assert len(serialized) <= MAX_JIRA_ADF_DOCUMENT_BYTES
        assert "Content truncated to fit Jira content size limit." not in _flatten_text(
            bounded
        )


def test_engineering_child_description_omits_architecture_section() -> None:
    doc = build_engineering_child_description(
        parent_issue_key="MAB-215",
        parent_summary="Decision engine rollout",
        parent_revision="normalized-parent-brief",
        capability="Introduce rollout gating",
        delivery="Engineering child inherits the canonical architecture document link.",
        expected_outcome="Implementation tickets use the same architecture reference as the epic.",
        acceptance_criteria=["Child ticket points at the architecture document."],
        how_to_test=[
            "Open the child ticket and verify the architecture link is present."
        ],
        done_means=[
            "Child descriptions reference the architecture doc instead of copying architecture context."
        ],
        dependencies_and_risks=[
            "Architecture document must exist before fanout completes."
        ],
        specialist_summary=["Architecture doc is the persistent source of truth."],
        planning_state="planning_completed",
    )

    headings = _heading_texts(doc)

    assert "Architecture" not in headings
    assert "Architecture Context" not in headings
    assert "Architecture Diagram" not in headings
    assert "See Architecture:" not in _flatten_text(doc)


def test_engineering_child_description_renders_typed_technical_decisions_at_description_boundary() -> (
    None
):
    doc = build_engineering_child_description(
        parent_issue_key="MAB-215",
        parent_summary="Decision engine rollout",
        parent_revision="normalized-parent-brief",
        capability="Introduce authorization boundary",
        delivery="Apply the selected tenant authorization design.",
        expected_outcome="Tenant data access is enforced consistently.",
        acceptance_criteria=["Authorization enforcement is covered by tests."],
        how_to_test=["Run tenant isolation regression tests."],
        done_means=["Tenant data cannot be read across tenant boundaries."],
        dependencies_and_risks=["Database policy migration must be applied."],
        specialist_summary=["Security selected an enforcement strategy."],
        technical_decisions=[_technical_decision()],
        planning_state="planning_completed",
    )

    text = _flatten_text(doc)

    assert "Technical Decisions" in _heading_texts(doc)
    assert "auth-boundary: Where should tenant authorization be enforced?" in text
    assert "Selected Database RLS" in text


def test_parent_feature_description_omits_architecture_section() -> None:
    doc = build_parent_feature_description(
        objective="Keep the parent description focused on execution.",
        user_value="Teams should use one canonical architecture source.",
        recommendation="Only render architecture when a canonical document exists.",
        scope_in=["Jira description cleanup"],
        scope_out=[],
        acceptance_criteria=["No placeholder architecture content appears."],
        ui_references=[],
        success_outcomes=[],
        dependencies_and_risks=[],
        open_questions=[],
        parent_revision="rev-1",
        sync_status="children_syncing",
    )

    headings = _heading_texts(doc)
    text = _flatten_text(doc)

    assert "Architecture" not in headings
    assert "See Architecture:" not in text
