from __future__ import annotations

import json
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from orchestrator.core.pm_interview_service import (
    PM_INTERVIEW_STATUS_ABANDONED,
    PM_INTERVIEW_STATUS_PM_COMPLETED,
    PM_INTERVIEW_SOURCE_KIND_PARENT_BRIEF_SNAPSHOT,
    PM_INTERVIEW_STATUS_READY_TO_WRITE,
    PM_INTERVIEW_STATUS_QUESTION_PENDING,
    assess_pm_interview_brief,
    format_pm_interview_question,
    mark_pm_interview_case_abandoned,
    mark_pm_interview_case_completed,
    normalize_pm_interview_evidence,
    normalize_parent_feature_brief_with_runtime,
    plan_pm_interview_with_codex,
    resolve_pm_interview_case,
    resolve_pm_interview_case_match,
    select_next_pm_interview_question,
    upsert_pm_interview_case,
)
from orchestrator.core.parent_feature_brief_store import (
    persist_parent_feature_brief_snapshot,
    resolve_parent_feature_brief,
)
from orchestrator.core.runtime_invocation import AgentInvocationContext
from orchestrator.storage.models import PMInterviewCase

try:
    from tests.production_path_support import (
        clear_runtime_environment,
        configure_runtime_environment,
        seed_core_runtime_state,
        session_factory_for,
    )
except ModuleNotFoundError:  # pragma: no cover - local test runner path quirk
    from production_path_support import (  # type: ignore[no-redef]
        clear_runtime_environment,
        configure_runtime_environment,
        seed_core_runtime_state,
        session_factory_for,
    )


pytestmark = pytest.mark.contract


class PMInterviewServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url, _ = configure_runtime_environment(
            temp_dir=self.temp_dir,
            database_name="pm_interview_service.db",
        )
        self.session_factory = session_factory_for(self.database_url)
        seed_core_runtime_state(self.session_factory)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        clear_runtime_environment()

    def test_assess_pm_interview_brief_selects_next_question_with_examples(self) -> None:
        assessment = assess_pm_interview_brief(brief={"objective": "Share the app with friends"})

        self.assertEqual(assessment.status, PM_INTERVIEW_STATUS_QUESTION_PENDING)
        self.assertGreater(len(assessment.missing_slots), 0)
        self.assertIsNotNone(assessment.next_question)
        assert assessment.next_question is not None
        self.assertEqual(assessment.next_question.slot_key, "user_value")
        question_text = format_pm_interview_question(assessment.next_question)
        self.assertIn("Why does the user want this feature?", question_text)
        self.assertIn("Examples:", question_text)
        self.assertIn("friction during onboarding", question_text)

    def test_assess_pm_interview_brief_uses_evidence_updates_and_reaches_ready_to_write(self) -> None:
        evidence = normalize_pm_interview_evidence(
            [
                {
                    "evidence_id": "e-file",
                    "evidence_type": "file",
                    "source_ref": "drive://share-spec",
                    "metadata": {
                        "slot_values": {
                            "objective": "Share the app with friends",
                            "user_value": "Help users invite friends without friction",
                            "target_user": "New users",
                            "primary_journey": "From onboarding",
                            "acceptance_criteria": [
                                "Users can copy or send a share link",
                                "The link opens the right store page if the app is not installed",
                            ],
                            "scope_in": ["Share link", "Invite link copy"],
                            "scope_out": ["Rewards", "Referral tracking"],
                            "ui_references": ["Onboarding screen"],
                            "constraints": ["iOS and Android"],
                            "risks": ["Spam and abuse"],
                            "success_outcomes": ["More invites sent"],
                        }
                    },
                },
                {
                    "evidence_id": "e-link",
                    "evidence_type": "link",
                    "source_ref": "https://example.com/product",
                    "metadata": {"slot_values": {"scope_in": ["Profile entry point"]}},
                },
                {
                    "evidence_id": "e-research",
                    "evidence_type": "web_research",
                    "source_ref": "https://example.com/research",
                    "metadata": {"slot_values": {"risks": ["Platform policy"]}},
                },
            ]
        )
        assessment = assess_pm_interview_brief(brief={}, evidence=evidence)

        self.assertEqual(assessment.status, PM_INTERVIEW_STATUS_READY_TO_WRITE)
        self.assertTrue(assessment.ready_to_write)
        self.assertEqual(assessment.missing_slots, ())
        self.assertIsNone(assessment.next_question)
        self.assertEqual(assessment.brief.objective, "Share the app with friends")
        self.assertIn("Profile entry point", assessment.brief.scope_in)
        self.assertIn("Platform policy", assessment.brief.risks)

    def test_pm_interview_case_round_trips_and_resolves_by_explicit_identity(self) -> None:
        with self.session_factory() as session:
            case = upsert_pm_interview_case(
                session=session,
                tenant_id="example",
                project_id="example-default",
                request_id="pm-req-1",
                source_kind="voice_note",
                channel_id="discord-channel-1",
                thread_channel_id="thread-1",
                root_message_id="message-1",
                owner_user_id="u-1",
                source_text="share feature",
                brief={"objective": "Share the app with friends"},
                evidence=[
                    {
                        "evidence_id": "e-1",
                        "evidence_type": "stakeholder_answer",
                        "metadata": {
                            "slot_values": {
                                "user_value": "Users can bring in friends easily",
                                "target_user": "New users",
                                "primary_journey": "From profile",
                                "acceptance_criteria": ["Users can send a share link"],
                                "scope_in": ["Share link"],
                                "scope_out": ["Rewards"],
                                "ui_references": ["Profile page"],
                                "constraints": ["iOS"],
                                "risks": ["Abuse"],
                                "success_outcomes": ["More invites"],
                            }
                        },
                    }
                ],
            )
            session.commit()
            self.assertEqual(case.status, PM_INTERVIEW_STATUS_READY_TO_WRITE)

        with self.session_factory() as session:
            resolved = resolve_pm_interview_case(
                session=session,
                tenant_id="example",
                request_id="pm-req-1",
            )
            self.assertIsNotNone(resolved)
            assert resolved is not None
            self.assertEqual(resolved.request_id, "pm-req-1")

            by_thread = resolve_pm_interview_case_match(
                session=session,
                tenant_id="example",
                thread_channel_id="thread-1",
            )
            self.assertEqual(by_thread.status, "matched")

            by_root_message = resolve_pm_interview_case_match(
                session=session,
                tenant_id="example",
                root_message_id="message-1",
            )
            self.assertEqual(by_root_message.status, "matched")

            by_channel_only = resolve_pm_interview_case_match(
                session=session,
                tenant_id="example",
                channel_id="discord-channel-1",
            )
            self.assertEqual(by_channel_only.status, "no_match")

    def test_pm_interview_case_terminal_transitions_close_the_case(self) -> None:
        with self.session_factory() as session:
            upsert_pm_interview_case(
                session=session,
                tenant_id="example",
                project_id="example-default",
                request_id="pm-req-2",
                source_kind="command",
                channel_id="discord-channel-1",
                source_text="create onboarding flow",
                brief={"objective": "Improve onboarding"},
            )
            mark_pm_interview_case_completed(
                session=session,
                tenant_id="example",
                request_id="pm-req-2",
                parent_issue_key="TP-123",
            )
            session.commit()

        with self.session_factory() as session:
            completed = resolve_pm_interview_case(
                session=session,
                tenant_id="example",
                request_id="pm-req-2",
            )
            self.assertIsNone(completed)

            row = session.query(PMInterviewCase).filter_by(request_id="pm-req-2").one()
            self.assertEqual(row.status, PM_INTERVIEW_STATUS_PM_COMPLETED)
            self.assertEqual(row.parent_issue_key, "TP-123")
            self.assertIsNotNone(row.closed_at)

        with self.session_factory() as session:
            abandoned = upsert_pm_interview_case(
                session=session,
                tenant_id="example",
                project_id="example-default",
                request_id="pm-req-3",
                source_kind="command",
                channel_id="discord-channel-1",
                source_text="cancel this",
                brief={"objective": "Abort"},
            )
            mark_pm_interview_case_abandoned(
                session=session,
                tenant_id="example",
                request_id="pm-req-3",
            )
            session.commit()
            self.assertEqual(abandoned.status, PM_INTERVIEW_STATUS_ABANDONED)

    def test_resolve_parent_feature_brief_uses_latest_case_for_parent_issue(self) -> None:
        with self.session_factory() as session:
            persist_parent_feature_brief_snapshot(
                session=session,
                tenant_id="example",
                project_id="example-default",
                parent_issue_key="TP-500",
                source_text="Legacy parent description",
                brief={
                    "objective": "Legacy objective",
                    "user_value": "Legacy value",
                },
                notes={"source": "compat"},
            )
            upsert_pm_interview_case(
                session=session,
                tenant_id="example",
                project_id="example-default",
                request_id="pm-req-parent-1",
                source_kind="command",
                channel_id="discord-channel-1",
                source_text="create runtime reset",
                parent_issue_key="TP-500",
                status=PM_INTERVIEW_STATUS_PM_COMPLETED,
                brief={
                    "objective": "Canonical objective",
                    "user_value": "Canonical value",
                    "acceptance_criteria": ["Canonical acceptance"],
                },
            )
            session.commit()

        with self.session_factory() as session:
            brief = resolve_parent_feature_brief(
                session=session,
                tenant_id="example",
                parent_issue_key="TP-500",
            )

        self.assertIsNotNone(brief)
        assert brief is not None
        self.assertEqual(brief.objective, "Canonical objective")
        self.assertEqual(brief.user_value, "Canonical value")
        self.assertEqual(brief.acceptance_criteria, ("Canonical acceptance",))

    def test_persist_parent_feature_brief_snapshot_stores_parent_brief_record(self) -> None:
        with self.session_factory() as session:
            row = persist_parent_feature_brief_snapshot(
                session=session,
                tenant_id="example",
                project_id="example-default",
                parent_issue_key="TP-501",
                source_text="Objective\nFallback parent brief",
                brief={
                    "objective": "Fallback parent brief",
                    "user_value": "Fallback value",
                },
                notes={"source": "jira_parent_brief_normalization"},
            )
            session.commit()

            stored = session.query(PMInterviewCase).filter_by(request_id="parent-brief:TP-501").one()

        self.assertEqual(row.source_kind, PM_INTERVIEW_SOURCE_KIND_PARENT_BRIEF_SNAPSHOT)
        self.assertEqual(stored.parent_issue_key, "TP-501")
        self.assertEqual(stored.status, PM_INTERVIEW_STATUS_PM_COMPLETED)
        self.assertEqual(stored.channel_id, "jira-parent-sync")
        self.assertEqual(stored.brief_json["objective"], "Fallback parent brief")
        self.assertEqual(stored.notes_json["source"], "jira_parent_brief_normalization")
        self.assertTrue(stored.notes_json["parent_brief_snapshot"])

    def test_resolve_parent_feature_brief_excludes_incomplete_snapshots_by_default(self) -> None:
        with self.session_factory() as session:
            persist_parent_feature_brief_snapshot(
                session=session,
                tenant_id="example",
                project_id="example-default",
                parent_issue_key="TP-502",
                source_text="Loose parent brief",
                brief={
                    "objective": "Needs clarification",
                    "user_value": "Still incomplete",
                },
                notes={"source": "jira_parent_brief_normalization"},
                status=PM_INTERVIEW_STATUS_QUESTION_PENDING,
            )
            session.commit()

        with self.session_factory() as session:
            completed_only = resolve_parent_feature_brief(
                session=session,
                tenant_id="example",
                parent_issue_key="TP-502",
            )
            latest_any_status = resolve_parent_feature_brief(
                session=session,
                tenant_id="example",
                parent_issue_key="TP-502",
                include_incomplete=True,
            )

        self.assertIsNone(completed_only)
        self.assertIsNotNone(latest_any_status)
        assert latest_any_status is not None
        self.assertEqual(latest_any_status.objective, "Needs clarification")

    def test_normalize_parent_feature_brief_with_runtime_returns_typed_brief(self) -> None:
        captured: dict[str, object] = {}

        def _render_prompt(template_name: str, **kwargs):  # noqa: ANN001
            captured[template_name] = kwargs
            return template_name

        with (
            patch(
                "orchestrator.core.runtime_stage_session.invoke_runtime_json",
                return_value={
                    "brief": {
                        "objective": "Refactor the orchestration stack",
                        "user_value": "Runtime behavior is easier to reason about and verify",
                        "target_user": "Platform engineers",
                        "primary_journey": "Plan and execute orchestration changes from Jira parents",
                        "acceptance_criteria": ["One canonical decision state machine exists"],
                        "scope_in": ["Decision state machine refactor"],
                        "scope_out": ["Unrelated UI redesign"],
                        "ui_references": ["Current runtime architecture doc"],
                        "constraints": ["Keep the runtime-agnostic contract stable"],
                        "risks": ["Planning drift across runtimes"],
                        "success_outcomes": ["Parent planning produces stable child tickets"],
                        "recommendation": "Normalize the brief before planning child tickets",
                    },
                    "open_questions": [],
                },
            ),
            patch("orchestrator.core.pm_interview_service.render_prompt", side_effect=_render_prompt),
        ):
            payload = normalize_parent_feature_brief_with_runtime(
                runtime=SimpleNamespace(),
                parent_issue_key="MAB-200",
                parent_summary="Complete Runtime Architecture and Verification Reset",
                parent_description="Long Jira parent description",
                invocation_context=SimpleNamespace(),
            )

        self.assertTrue(payload["ready_to_write"])
        self.assertEqual(payload["open_questions"], [])
        self.assertEqual(payload["brief"]["objective"], "Refactor the orchestration stack")
        self.assertIn("workflow/pm_parent_brief_normalization_system.j2", captured)
        user_kwargs = captured["workflow/pm_parent_brief_normalization_user.j2"]
        self.assertEqual(user_kwargs["parent_issue_key"], "MAB-200")
        self.assertIn("Complete Runtime Architecture and Verification Reset", user_kwargs["parent_summary"])
        self.assertIn("governed_tools_json", user_kwargs)
        self.assertIn("native_tools_json", user_kwargs)

    def test_normalize_parent_feature_brief_with_runtime_uses_tool_bridge_when_session_available(self) -> None:
        captured: dict[str, object] = {}

        def _render_prompt(template_name: str, **kwargs):  # noqa: ANN001
            if template_name == "workflow/pm_parent_brief_normalization_user.j2":
                captured["governed_tools_json"] = kwargs["governed_tools_json"]
                captured["native_tools_json"] = kwargs["native_tools_json"]
            return template_name

        def _invoke_runtime_json_with_tools(*, context, user_prompt, allowed_tools, **kwargs):  # noqa: ANN001
            _ = kwargs
            captured["stage"] = context.stage
            captured["user_prompt"] = user_prompt
            captured["allowed_tools"] = set(allowed_tools)
            return {
                "brief": {
                    "objective": "Normalized objective",
                    "user_value": "Clear business outcome",
                    "target_user": "Operators",
                    "primary_journey": "Create parent and review subtasks",
                    "acceptance_criteria": ["Parent planning creates child tickets"],
                    "scope_in": ["Parent normalization"],
                    "scope_out": ["Runtime migration"],
                    "ui_references": ["Current Jira parent workflow"],
                    "constraints": ["Keep planning reviewable in Jira and Discord"],
                    "risks": ["Parent planning can drift without clear product answers"],
                    "success_outcomes": ["Consistent parent structure"],
                    "recommendation": "Normalize before decomposition",
                    "open_questions": [],
                    "next_steps": ["Create engineering subtasks after normalization"],
                },
                "open_questions": [],
            }

        with (
            patch("orchestrator.core.pm_interview_service.render_prompt", side_effect=_render_prompt),
            patch("orchestrator.core.runtime_stage_session.invoke_runtime_json_with_tools", side_effect=_invoke_runtime_json_with_tools),
        ):
            payload = normalize_parent_feature_brief_with_runtime(
                session=object(),  # type: ignore[arg-type]
                settings=object(),
                runtime=SimpleNamespace(command="codex"),
                parent_issue_key="MAB-200",
                parent_summary="Complete Runtime Architecture and Verification Reset",
                parent_description="Stakeholder parent description",
                invocation_context=AgentInvocationContext(
                    channel="jira",
                    tenant_id="example",
                    project_id="example-default",
                    command="pm",
                    stage="pm_parent_brief_normalization",
                    working_dir=".",
                    issue_key="MAB-200",
                ),
            )

        self.assertTrue(payload["ready_to_write"])
        self.assertEqual(captured["stage"], "pm_parent_brief_normalization")
        self.assertIn("knowledge.read", captured["allowed_tools"])
        self.assertIn("jira.get_issue", captured["allowed_tools"])
        self.assertNotIn("web.search", captured["allowed_tools"])
        self.assertIn("tool_name", str(captured["governed_tools_json"]))
        self.assertIn("knowledge.read", str(captured["governed_tools_json"]))
        self.assertIn("web.search", str(captured["native_tools_json"]))

    def test_plan_pm_interview_with_codex_uses_question_examples_and_json_contract(self) -> None:
        captured: dict[str, object] = {}

        def _render_prompt(template_name: str, **kwargs):  # noqa: ANN001
            captured[template_name] = kwargs
            return template_name

        with (
            patch(
                "orchestrator.core.pm_interview_service._invoke_discord_json_maybe_tools",
                return_value={"message": "What user group?", "brief": {"objective": "Share the app with friends"}},
            ),
            patch("orchestrator.core.pm_interview_service.render_prompt", side_effect=_render_prompt),
        ):
            payload = plan_pm_interview_with_codex(
                runtime=SimpleNamespace(),
                request_text="create a share feature",
                brief={"objective": "Share the app with friends"},
                evidence=[],
                missing_slots=["user_value", "target_user"],
                next_question=select_next_pm_interview_question(missing_slots=["user_value", "target_user"]),
                project_keys=["TP"],
                issues=[],
                status_counts={},
                invocation_context=SimpleNamespace(),
                history=[{"speaker": "user", "text": "create a share feature"}],
                github_context={"repository": "org/repo"},
            )

        self.assertEqual(payload["message"], "What user group?")
        self.assertEqual(payload["status"], PM_INTERVIEW_STATUS_QUESTION_PENDING)
        self.assertEqual(payload["missing_slots"], ["user_value", "target_user"])
        self.assertIn("discord/pm_interview_system.j2", captured)
        user_kwargs = captured["discord/pm_interview_user.j2"]
        self.assertIn("next_question_examples_json", user_kwargs)
        self.assertTrue(json.loads(user_kwargs["next_question_examples_json"]))
        self.assertIn("brief_json", user_kwargs)
        self.assertEqual(json.loads(user_kwargs["brief_json"])["objective"], "Share the app with friends")


if __name__ == "__main__":
    unittest.main()
