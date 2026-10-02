from __future__ import annotations

import unittest
from orchestrator.core.stages.plugins import evaluate_stage_plugin


class StagePluginsTests(unittest.TestCase):
    def test_design_stage_initial_plan_blocks_until_feedback(self) -> None:
        result = evaluate_stage_plugin(
            state=None,
            stakeholder_text="Need a landing page redesign for conversion.",
            assistant_summary="Drafted an initial design direction.",
            attachments=None,
        )
        self.assertEqual(result.stage_plugin, "design")
        self.assertEqual(result.stage_status, "stage_review_pending")
        self.assertFalse(result.stage_ready_for_implementation)
        self.assertTrue(result.stage_open_questions)

    def test_design_stage_approval_marks_ready(self) -> None:
        planned = evaluate_stage_plugin(
            state=None,
            stakeholder_text="Need a landing page redesign for conversion.",
            assistant_summary="Drafted an initial design direction.",
            attachments=None,
        )
        approved = evaluate_stage_plugin(
            state={
                "stage_plugin": planned.stage_plugin,
                "stage_status": planned.stage_status,
                "stage_artifacts": planned.stage_artifacts,
                "stage_open_questions": list(planned.stage_open_questions),
                "stage_feedback_log": [
                    dict(item) for item in planned.stage_feedback_log
                ],
                "stage_tool_outputs": [],
                "stage_ready_for_implementation": planned.stage_ready_for_implementation,
            },
            stakeholder_text="Approved. Looks good, ship it.",
            assistant_summary="Acknowledged approval.",
            attachments=None,
        )
        self.assertEqual(approved.stage_status, "stage_ready_for_implementation")
        self.assertTrue(approved.stage_ready_for_implementation)
        self.assertFalse(approved.stage_open_questions)

    def test_stitch_links_captured_as_tool_outputs(self) -> None:
        result = evaluate_stage_plugin(
            state=None,
            stakeholder_text="Reference this concept https://stitch.withgoogle.com/project/abc123",
            assistant_summary="Drafted an initial design direction.",
            attachments=None,
        )
        self.assertEqual(len(result.stage_tool_outputs), 1)
        self.assertEqual(result.stage_tool_outputs[0]["provider"], "stitch")

    def test_unknown_plugin_fails_closed(self) -> None:
        result = evaluate_stage_plugin(
            state=None,
            stakeholder_text="hello",
            assistant_summary="summary",
            attachments=None,
            plugin_id="marketing",
            tenant_id="tenant-x",
            project_id="proj-y",
            request_id="req-z",
        )
        self.assertEqual(result.stage_status, "stage_blocked")
        self.assertFalse(result.stage_ready_for_implementation)
        self.assertIsNotNone(result.block_reason)
        self.assertIn("Unknown stage plugin", str(result.block_reason))

    def test_llm_plan_payload_merges_after_plan(self) -> None:
        result = evaluate_stage_plugin(
            state=None,
            stakeholder_text="Stakeholder context",
            assistant_summary="PM summary",
            attachments=None,
            tenant_id="t1",
            request_id="r1",
            llm_plan_payload={
                "stage_open_questions": ["From LLM: confirm palette?"],
                "stage_artifacts": {"llm_hint": "warm palette"},
                "message": "LLM stakeholder-facing line.",
            },
        )
        self.assertEqual(result.stage_open_questions[0], "From LLM: confirm palette?")
        self.assertEqual(result.stage_artifacts.get("llm_hint"), "warm palette")
        self.assertEqual(result.message, "LLM stakeholder-facing line.")

    def test_reject_sets_revising(self) -> None:
        planned = evaluate_stage_plugin(
            state=None,
            stakeholder_text="We need a hero section",
            assistant_summary="Draft direction",
            attachments=None,
        )
        rejected = evaluate_stage_plugin(
            state={
                "stage_plugin": planned.stage_plugin,
                "stage_status": planned.stage_status,
                "stage_artifacts": dict(planned.stage_artifacts),
                "stage_open_questions": list(planned.stage_open_questions),
                "stage_feedback_log": [
                    dict(item) for item in planned.stage_feedback_log
                ],
                "stage_tool_outputs": [],
                "stage_ready_for_implementation": planned.stage_ready_for_implementation,
            },
            stakeholder_text="We reject this direction, start over",
            assistant_summary="ok",
            attachments=None,
        )
        self.assertEqual(rejected.stage_status, "stage_revising")

    def test_not_approved_is_not_treated_as_approval(self) -> None:
        planned = evaluate_stage_plugin(
            state=None,
            stakeholder_text="Design request",
            assistant_summary="Draft",
            attachments=None,
        )
        follow = evaluate_stage_plugin(
            state={
                "stage_plugin": planned.stage_plugin,
                "stage_status": planned.stage_status,
                "stage_artifacts": dict(planned.stage_artifacts),
                "stage_open_questions": list(planned.stage_open_questions),
                "stage_feedback_log": [
                    dict(item) for item in planned.stage_feedback_log
                ],
                "stage_tool_outputs": [],
                "stage_ready_for_implementation": planned.stage_ready_for_implementation,
            },
            stakeholder_text="This is not approved yet",
            assistant_summary="ack",
            attachments=None,
        )
        self.assertFalse(follow.stage_ready_for_implementation)

    def test_explicit_tool_outputs_are_appended(self) -> None:
        fake_row = {
            "provider": "stitch",
            "kind": "stitch_tool",
            "tool": "synthesize_screen",
            "html_url": "https://example.com/preview",
        }
        result = evaluate_stage_plugin(
            state=None,
            stakeholder_text="Landing page refresh",
            assistant_summary="Initial direction",
            attachments=None,
            tenant_id="t1",
            project_id="p1",
            request_id="r1",
            explicit_tool_outputs=[fake_row],
        )
        kinds = {str(o.get("kind") or "") for o in result.stage_tool_outputs}
        self.assertIn("stitch_tool", kinds)

    def test_decision_state_approved_commits_ready(self) -> None:
        planned = evaluate_stage_plugin(
            state=None,
            stakeholder_text="Design request",
            assistant_summary="Draft",
            attachments=None,
        )
        approved = evaluate_stage_plugin(
            state={
                "stage_plugin": planned.stage_plugin,
                "stage_status": planned.stage_status,
                "stage_artifacts": dict(planned.stage_artifacts),
                "stage_open_questions": list(planned.stage_open_questions),
                "stage_feedback_log": [
                    dict(item) for item in planned.stage_feedback_log
                ],
                "stage_tool_outputs": [],
                "stage_ready_for_implementation": planned.stage_ready_for_implementation,
            },
            stakeholder_text="still discussing",
            assistant_summary="ack",
            attachments=None,
            decision_state="approved",
        )
        self.assertTrue(approved.stage_ready_for_implementation)
        self.assertEqual(approved.stage_status, "stage_ready_for_implementation")

    def test_revision_request_replans_with_updated_artifacts(self) -> None:
        planned = evaluate_stage_plugin(
            state=None,
            stakeholder_text="Create a bright marketing hero with a long headline.",
            assistant_summary="Initial design direction",
            attachments=None,
        )
        revision_requested = evaluate_stage_plugin(
            state={
                "stage_plugin": planned.stage_plugin,
                "stage_status": planned.stage_status,
                "stage_artifacts": dict(planned.stage_artifacts),
                "stage_open_questions": list(planned.stage_open_questions),
                "stage_feedback_log": [
                    dict(item) for item in planned.stage_feedback_log
                ],
                "stage_tool_outputs": [],
                "stage_ready_for_implementation": planned.stage_ready_for_implementation,
            },
            stakeholder_text="Revisions required. Make it darker and much tighter.",
            assistant_summary="Acknowledged revision request.",
            attachments=None,
            decision_state="revisions_required",
        )
        replanned = evaluate_stage_plugin(
            state={
                "stage_plugin": revision_requested.stage_plugin,
                "stage_status": revision_requested.stage_status,
                "stage_artifacts": dict(revision_requested.stage_artifacts),
                "stage_open_questions": list(revision_requested.stage_open_questions),
                "stage_feedback_log": [
                    dict(item) for item in revision_requested.stage_feedback_log
                ],
                "stage_tool_outputs": [],
                "stage_ready_for_implementation": revision_requested.stage_ready_for_implementation,
            },
            stakeholder_text="Use a dark palette and a short proof-led headline.",
            assistant_summary="Updated design direction",
            attachments=None,
            llm_plan_payload={
                "stage_artifacts": {"llm_hint": "dark palette"},
                "message": "Updated direction ready for another review pass.",
            },
        )
        self.assertEqual(replanned.stage_status, "stage_review_pending")
        self.assertEqual(
            replanned.stage_artifacts.get("design_brief"), "Updated design direction"
        )
        self.assertEqual(
            replanned.stage_artifacts.get("design_direction"),
            "Use a dark palette and a short proof-led headline.",
        )
        self.assertEqual(replanned.stage_artifacts.get("llm_hint"), "dark palette")
        self.assertNotEqual(
            replanned.stage_artifacts.get("artifact_version"),
            planned.stage_artifacts.get("artifact_version"),
        )
        self.assertEqual(
            replanned.message, "Updated direction ready for another review pass."
        )


if __name__ == "__main__":
    unittest.main()
