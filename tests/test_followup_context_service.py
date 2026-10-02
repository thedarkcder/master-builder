from __future__ import annotations

from tempfile import TemporaryDirectory
import unittest

import pytest

from orchestrator.core.clarification.projection_service import (
    ClarificationProjectionSpec,
    upsert_clarification_projection,
)
from orchestrator.core.clarification.questions import ClarificationQuestionSet
from orchestrator.core.pm.followup_context_service import (
    close_followup_contexts,
    FOLLOWUP_CONTEXT_PM_INTERVIEW,
    resolve_discord_command_subject_key,
    resolve_discord_interaction_subject_scope,
    resolve_issue_followup_context,
    resolve_followup_context,
    resolve_followup_context_match,
    resolve_followup_reaction,
    upsert_followup_context,
)
from orchestrator.storage.models import FollowupContext

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


class FollowupContextServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url, _ = configure_runtime_environment(
            temp_dir=self.temp_dir,
            database_name="followup_context_service.db",
        )
        self.session_factory = session_factory_for(self.database_url)
        seed_core_runtime_state(self.session_factory)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        clear_runtime_environment()

    def test_upsert_and_resolve_decision_gate_context_by_thread_channel(self) -> None:
        with self.session_factory() as session:
            upsert_followup_context(
                session=session,
                tenant_id="example-workspace",
                project_id="example-workspace-default",
                context_type="decision_gate",
                channel_id="discord-channel-1",
                thread_channel_id="thread-1",
                root_message_id="message-1",
                issue_key="GP-124",
                metadata={"source": "test"},
            )
            session.commit()

        with self.session_factory() as session:
            context = resolve_followup_context(
                session=session,
                tenant_id="example-workspace",
                channel_id="thread-1",
            )

            self.assertIsNotNone(context)
            self.assertEqual(context.context_type, "decision_gate")
            self.assertEqual(context.issue_key, "GP-124")
            self.assertEqual(context.thread_channel_id, "thread-1")

    def test_close_followup_contexts_marks_matching_context_closed(self) -> None:
        with self.session_factory() as session:
            upsert_followup_context(
                session=session,
                tenant_id="example-workspace",
                project_id="example-workspace-default",
                context_type="decision_gate",
                channel_id="discord-channel-1",
                thread_channel_id="thread-1",
                issue_key="GP-124",
            )
            session.commit()

        with self.session_factory() as session:
            closed = close_followup_contexts(
                session=session,
                tenant_id="example-workspace",
                context_type="decision_gate",
                issue_key="GP-124",
            )
            session.commit()
            self.assertEqual(closed, 1)

        with self.session_factory() as session:
            context = (
                session.query(FollowupContext)
                .filter_by(tenant_id="example-workspace")
                .one()
            )
            self.assertEqual(context.status, "closed")
            self.assertIsNotNone(context.closed_at)
            self.assertIsNone(
                resolve_followup_context(
                    session=session,
                    tenant_id="example-workspace",
                    channel_id="thread-1",
                )
            )

    def test_clarification_projection_drops_stale_jira_comment_id_when_question_set_changes(
        self,
    ) -> None:
        with self.session_factory() as session:
            upsert_clarification_projection(
                session=session,
                spec=ClarificationProjectionSpec(
                    tenant_id="example-workspace",
                    project_id="example-workspace-default",
                    context_type=FOLLOWUP_CONTEXT_PM_INTERVIEW,
                    issue_key="MAB-243",
                    request_id="parent-planning-clarification:MAB-243",
                    origin_command="clarify",
                    questions=ClarificationQuestionSet.from_values(
                        ["What is the audit window?"]
                    ).questions,
                    metadata={
                        "questions": [{"question": "What is the audit window?"}],
                        "jira_comment_id": "comment-old",
                    },
                ),
            )
            projection = upsert_clarification_projection(
                session=session,
                spec=ClarificationProjectionSpec(
                    tenant_id="example-workspace",
                    project_id="example-workspace-default",
                    context_type=FOLLOWUP_CONTEXT_PM_INTERVIEW,
                    issue_key="MAB-243",
                    request_id="parent-planning-clarification:MAB-243",
                    origin_command="clarify",
                    questions=ClarificationQuestionSet.from_values(
                        ["What email verification policy should v1 use?"]
                    ).questions,
                    metadata={
                        "questions": [
                            {
                                "question": "What email verification policy should v1 use?"
                            }
                        ]
                    },
                ),
            )

            self.assertFalse(projection.already_projected)
            self.assertNotIn("jira_comment_id", projection.metadata)
            self.assertNotIn("created_comment_id", projection.metadata)

    def test_resolve_followup_context_prefers_root_message_over_parent_channel(
        self,
    ) -> None:
        with self.session_factory() as session:
            upsert_followup_context(
                session=session,
                tenant_id="example-workspace",
                project_id="example-workspace-default",
                context_type="ask_thread",
                channel_id="discord-channel-1",
                root_message_id="message-1",
                request_id="req-1",
            )
            upsert_followup_context(
                session=session,
                tenant_id="example-workspace",
                project_id="example-workspace-default",
                context_type="decision_gate",
                channel_id="discord-channel-1",
                root_message_id="message-2",
                issue_key="GP-124",
            )
            session.commit()

        with self.session_factory() as session:
            context = resolve_followup_context(
                session=session,
                tenant_id="example-workspace",
                channel_id="discord-channel-1",
                root_message_id="message-2",
            )

            self.assertIsNotNone(context)
            self.assertEqual(context.root_message_id, "message-2")
            self.assertEqual(context.context_type, "decision_gate")

    def test_resolve_issue_followup_context_by_issue_key(self) -> None:
        with self.session_factory() as session:
            upsert_followup_context(
                session=session,
                tenant_id="example-workspace",
                project_id="example-workspace-default",
                context_type="engineering_clarification",
                issue_key="TP-42",
                request_id="engineering-clarification:TP-42",
                metadata={"source": "jira"},
            )
            session.commit()

        with self.session_factory() as session:
            context = resolve_issue_followup_context(
                session=session,
                tenant_id="example-workspace",
                issue_key="TP-42",
                context_type="engineering_clarification",
            )

            self.assertIsNotNone(context)
            assert context is not None
            self.assertEqual(context.issue_key, "TP-42")
            self.assertEqual(context.context_type, "engineering_clarification")

    def test_clarification_projection_detects_changed_questions_before_metadata_update(
        self,
    ) -> None:
        first_questions = ClarificationQuestionSet.from_values(
            [{"question": "What should happen first?"}]
        )
        changed_questions = ClarificationQuestionSet.from_values(
            [{"question": "What should happen second?"}]
        )
        with self.session_factory() as session:
            first = upsert_clarification_projection(
                session=session,
                spec=ClarificationProjectionSpec(
                    tenant_id="example-workspace",
                    project_id="example-workspace-default",
                    context_type="engineering_clarification",
                    issue_key="TP-42",
                    request_id="engineering-clarification:TP-42",
                    questions=first_questions.questions,
                    metadata={"questions": first_questions.to_payload()},
                ),
            )
            changed = upsert_clarification_projection(
                session=session,
                spec=ClarificationProjectionSpec(
                    tenant_id="example-workspace",
                    project_id="example-workspace-default",
                    context_type="engineering_clarification",
                    issue_key="TP-42",
                    request_id="engineering-clarification:TP-42",
                    questions=changed_questions.questions,
                    metadata={"questions": changed_questions.to_payload()},
                ),
            )

        self.assertFalse(first.already_projected)
        self.assertFalse(changed.already_projected)

    def test_resolve_followup_context_match_requires_explicit_thread_or_message_identity(
        self,
    ) -> None:
        with self.session_factory() as session:
            upsert_followup_context(
                session=session,
                tenant_id="example-workspace",
                project_id="example-workspace-default",
                context_type="ask_thread",
                channel_id="discord-channel-1",
                thread_channel_id="thread-1",
                root_message_id="message-1",
                request_id="req-1",
            )
            session.commit()

        with self.session_factory() as session:
            root_channel_only = resolve_followup_context_match(
                session=session,
                tenant_id="example-workspace",
                channel_id="discord-channel-1",
            )
            self.assertEqual(root_channel_only.status, "no_match")
            self.assertIsNone(root_channel_only.context)

            thread_match = resolve_followup_context_match(
                session=session,
                tenant_id="example-workspace",
                channel_id="thread-1",
            )
            self.assertEqual(thread_match.status, "matched")
            assert thread_match.context is not None
            self.assertEqual(thread_match.context.thread_channel_id, "thread-1")

            message_match = resolve_followup_context_match(
                session=session,
                tenant_id="example-workspace",
                channel_id="discord-channel-1",
                root_message_id="message-1",
            )
            self.assertEqual(message_match.status, "matched")
            assert message_match.context is not None
            self.assertEqual(message_match.context.root_message_id, "message-1")

    def test_resolve_followup_context_match_returns_ambiguous_instead_of_raising(
        self,
    ) -> None:
        with self.session_factory() as session:
            upsert_followup_context(
                session=session,
                tenant_id="example-workspace",
                project_id="example-workspace-default",
                context_type="decision_gate",
                channel_id="discord-channel-1",
                thread_channel_id="thread-1",
                root_message_id="message-1",
                issue_key="GP-124",
            )
            upsert_followup_context(
                session=session,
                tenant_id="example-workspace",
                project_id="example-workspace-default",
                context_type="ask_thread",
                channel_id="discord-channel-1",
                thread_channel_id="thread-2",
                root_message_id="message-1",
                request_id="req-2",
            )
            session.commit()

        with self.session_factory() as session:
            resolution = resolve_followup_context_match(
                session=session,
                tenant_id="example-workspace",
                channel_id="discord-channel-1",
                root_message_id="message-1",
            )

            self.assertEqual(resolution.status, "ambiguous")
            self.assertIsNone(resolution.context)
            self.assertEqual(len(resolution.matches), 2)

    def test_resolve_discord_command_subject_key_ignores_existing_followup_contexts(
        self,
    ) -> None:
        with self.session_factory() as session:
            upsert_followup_context(
                session=session,
                tenant_id="example-workspace",
                project_id="example-workspace-default",
                context_type="decision_gate",
                channel_id="discord-channel-1",
                thread_channel_id="thread-1",
                issue_key="GP-124",
            )
            session.commit()

        with self.session_factory() as session:
            subject_key = resolve_discord_command_subject_key(
                session=session,
                tenant_id="example-workspace",
                channel_id="discord-channel-1",
                user_id="u-1",
            )

            self.assertEqual(
                subject_key, "discord_channel:example-workspace:discord-channel-1"
            )

    def test_resolve_followup_reaction_routes_pm_interview_replies_with_request_id(
        self,
    ) -> None:
        with self.session_factory() as session:
            context = upsert_followup_context(
                session=session,
                tenant_id="example-workspace",
                project_id="example-workspace-default",
                context_type=FOLLOWUP_CONTEXT_PM_INTERVIEW,
                channel_id="discord-channel-1",
                thread_channel_id="thread-1",
                request_id="pm-req-1",
                owner_user_id="u-1",
                origin_command="pm",
            )
            session.commit()

            reaction = resolve_followup_reaction(
                raw_text="simple share link only",
                source_ref="discord-message-1",
                followup_context=context,
                room_mode=False,
            )

            self.assertIsNotNone(reaction)
            assert reaction is not None
            self.assertEqual(reaction.kind, "command")
            self.assertEqual(reaction.command_text, "!pm simple share link only")
            self.assertEqual(reaction.command_params, {"request_id": "pm-req-1"})

    def test_resolve_interaction_subject_scope_fresh_application_command_ignores_active_followups(
        self,
    ) -> None:
        with self.session_factory() as session:
            upsert_followup_context(
                session=session,
                tenant_id="example-workspace",
                project_id="example-workspace-default",
                context_type="ask_thread",
                channel_id="discord-channel-1",
                thread_channel_id="thread-1",
                root_message_id="message-1",
                request_id="req-1",
            )
            upsert_followup_context(
                session=session,
                tenant_id="example-workspace",
                project_id="example-workspace-default",
                context_type="seed_followup",
                channel_id="discord-channel-1",
                thread_channel_id="thread-2",
                root_message_id="message-2",
                request_id="req-2",
            )
            session.commit()

        def _find_tenant_for_discord_channel(*, session, channel_id: str):  # noqa: ANN001
            if channel_id == "discord-channel-1":
                return type("TenantRef", (), {"tenant_id": "example-workspace"})()
            return None

        with self.session_factory() as session:
            tenant_id, project_id, subject_key = (
                resolve_discord_interaction_subject_scope(
                    session=session,
                    payload={
                        "type": 2,
                        "channel_id": "discord-channel-1",
                        "data": {"name": "pm"},
                        "member": {"user": {"id": "u-1"}},
                    },
                    find_tenant_for_discord_channel=_find_tenant_for_discord_channel,
                )
            )

            self.assertEqual(tenant_id, "example-workspace")
            self.assertIsNone(project_id)
            self.assertEqual(
                subject_key, "discord_channel:example-workspace:discord-channel-1"
            )

    def test_resolve_interaction_subject_scope_matches_active_thread_for_application_command(
        self,
    ) -> None:
        with self.session_factory() as session:
            context = upsert_followup_context(
                session=session,
                tenant_id="example-workspace",
                project_id="example-workspace-default",
                context_type="ask_thread",
                channel_id="discord-channel-1",
                thread_channel_id="thread-1",
                root_message_id="message-1",
                request_id="req-1",
            )
            context_id = context.context_id
            session.commit()

        def _find_tenant_for_discord_channel(*, session, channel_id: str):  # noqa: ANN001
            if channel_id == "thread-1":
                return type("TenantRef", (), {"tenant_id": "example-workspace"})()
            return None

        with self.session_factory() as session:
            tenant_id, project_id, subject_key = (
                resolve_discord_interaction_subject_scope(
                    session=session,
                    payload={
                        "type": 2,
                        "channel_id": "thread-1",
                        "data": {"name": "run"},
                        "member": {"user": {"id": "u-1"}},
                    },
                    find_tenant_for_discord_channel=_find_tenant_for_discord_channel,
                )
            )

            self.assertEqual(tenant_id, "example-workspace")
            self.assertEqual(project_id, "example-workspace-default")
            self.assertEqual(subject_key, f"discord_followup:{context_id}")

    def test_resolve_interaction_subject_scope_sets_project_for_project_channel(
        self,
    ) -> None:
        def _find_tenant_for_discord_channel(*, session, channel_id: str):  # noqa: ANN001
            if channel_id == "discord-channel-1":
                return type("TenantRef", (), {"tenant_id": "example-workspace"})()
            return None

        def _resolve_project_for_discord_channel(
            *, session, tenant_id: str, channel_id: str
        ):  # noqa: ANN001
            if tenant_id == "example-workspace" and channel_id == "discord-channel-1":
                return type(
                    "ProjectRef", (), {"project_id": "example-workspace-default"}
                )()
            return None

        with self.session_factory() as session:
            tenant_id, project_id, subject_key = (
                resolve_discord_interaction_subject_scope(
                    session=session,
                    payload={
                        "type": 2,
                        "channel_id": "discord-channel-1",
                        "data": {"name": "run"},
                        "member": {"user": {"id": "u-1"}},
                    },
                    find_tenant_for_discord_channel=_find_tenant_for_discord_channel,
                    resolve_project_for_discord_channel=_resolve_project_for_discord_channel,
                )
            )

            self.assertEqual(tenant_id, "example-workspace")
            self.assertEqual(project_id, "example-workspace-default")
            self.assertEqual(
                subject_key, "discord_channel:example-workspace:discord-channel-1"
            )
