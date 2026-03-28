from __future__ import annotations

from tempfile import TemporaryDirectory
import unittest

import pytest

from orchestrator.core.followup_context_service import (
    close_followup_contexts,
    resolve_discord_command_subject_key,
    resolve_discord_interaction_subject_scope,
    resolve_issue_followup_context,
    resolve_followup_context,
    resolve_followup_context_match,
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
                tenant_id="route25",
                project_id="route25-default",
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
                tenant_id="route25",
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
                tenant_id="route25",
                project_id="route25-default",
                context_type="decision_gate",
                channel_id="discord-channel-1",
                thread_channel_id="thread-1",
                issue_key="GP-124",
            )
            session.commit()

        with self.session_factory() as session:
            closed = close_followup_contexts(
                session=session,
                tenant_id="route25",
                context_type="decision_gate",
                issue_key="GP-124",
            )
            session.commit()
            self.assertEqual(closed, 1)

        with self.session_factory() as session:
            context = session.query(FollowupContext).filter_by(tenant_id="route25").one()
            self.assertEqual(context.status, "closed")
            self.assertIsNotNone(context.closed_at)
            self.assertIsNone(
                resolve_followup_context(
                    session=session,
                    tenant_id="route25",
                    channel_id="thread-1",
                )
            )

    def test_resolve_followup_context_prefers_root_message_over_parent_channel(self) -> None:
        with self.session_factory() as session:
            upsert_followup_context(
                session=session,
                tenant_id="route25",
                project_id="route25-default",
                context_type="ask_thread",
                channel_id="discord-channel-1",
                root_message_id="message-1",
                request_id="req-1",
            )
            upsert_followup_context(
                session=session,
                tenant_id="route25",
                project_id="route25-default",
                context_type="decision_gate",
                channel_id="discord-channel-1",
                root_message_id="message-2",
                issue_key="GP-124",
            )
            session.commit()

        with self.session_factory() as session:
            context = resolve_followup_context(
                session=session,
                tenant_id="route25",
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
                tenant_id="route25",
                project_id="route25-default",
                context_type="engineering_clarification",
                issue_key="TP-42",
                request_id="engineering-clarification:TP-42",
                metadata={"source": "jira"},
            )
            session.commit()

        with self.session_factory() as session:
            context = resolve_issue_followup_context(
                session=session,
                tenant_id="route25",
                issue_key="TP-42",
                context_type="engineering_clarification",
            )

            self.assertIsNotNone(context)
            assert context is not None
            self.assertEqual(context.issue_key, "TP-42")
            self.assertEqual(context.context_type, "engineering_clarification")

    def test_resolve_followup_context_match_requires_explicit_thread_or_message_identity(self) -> None:
        with self.session_factory() as session:
            upsert_followup_context(
                session=session,
                tenant_id="route25",
                project_id="route25-default",
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
                tenant_id="route25",
                channel_id="discord-channel-1",
            )
            self.assertEqual(root_channel_only.status, "no_match")
            self.assertIsNone(root_channel_only.context)

            thread_match = resolve_followup_context_match(
                session=session,
                tenant_id="route25",
                channel_id="thread-1",
            )
            self.assertEqual(thread_match.status, "matched")
            assert thread_match.context is not None
            self.assertEqual(thread_match.context.thread_channel_id, "thread-1")

            message_match = resolve_followup_context_match(
                session=session,
                tenant_id="route25",
                channel_id="discord-channel-1",
                root_message_id="message-1",
            )
            self.assertEqual(message_match.status, "matched")
            assert message_match.context is not None
            self.assertEqual(message_match.context.root_message_id, "message-1")

    def test_resolve_followup_context_match_returns_ambiguous_instead_of_raising(self) -> None:
        with self.session_factory() as session:
            upsert_followup_context(
                session=session,
                tenant_id="route25",
                project_id="route25-default",
                context_type="decision_gate",
                channel_id="discord-channel-1",
                thread_channel_id="thread-1",
                root_message_id="message-1",
                issue_key="GP-124",
            )
            upsert_followup_context(
                session=session,
                tenant_id="route25",
                project_id="route25-default",
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
                tenant_id="route25",
                channel_id="discord-channel-1",
                root_message_id="message-1",
            )

            self.assertEqual(resolution.status, "ambiguous")
            self.assertIsNone(resolution.context)
            self.assertEqual(len(resolution.matches), 2)

    def test_resolve_discord_command_subject_key_ignores_existing_followup_contexts(self) -> None:
        with self.session_factory() as session:
            upsert_followup_context(
                session=session,
                tenant_id="route25",
                project_id="route25-default",
                context_type="decision_gate",
                channel_id="discord-channel-1",
                thread_channel_id="thread-1",
                issue_key="GP-124",
            )
            session.commit()

        with self.session_factory() as session:
            subject_key = resolve_discord_command_subject_key(
                session=session,
                tenant_id="route25",
                channel_id="discord-channel-1",
                user_id="u-1",
            )

            self.assertEqual(subject_key, "discord_channel:route25:discord-channel-1")

    def test_resolve_interaction_subject_scope_fresh_application_command_ignores_active_followups(self) -> None:
        with self.session_factory() as session:
            upsert_followup_context(
                session=session,
                tenant_id="route25",
                project_id="route25-default",
                context_type="ask_thread",
                channel_id="discord-channel-1",
                thread_channel_id="thread-1",
                root_message_id="message-1",
                request_id="req-1",
            )
            upsert_followup_context(
                session=session,
                tenant_id="route25",
                project_id="route25-default",
                context_type="seed_followup",
                channel_id="discord-channel-1",
                thread_channel_id="thread-2",
                root_message_id="message-2",
                request_id="req-2",
            )
            session.commit()

        def _find_tenant_for_discord_channel(*, session, channel_id: str):  # noqa: ANN001
            if channel_id == "discord-channel-1":
                return type("TenantRef", (), {"tenant_id": "route25"})()
            return None

        with self.session_factory() as session:
            tenant_id, project_id, subject_key = resolve_discord_interaction_subject_scope(
                session=session,
                payload={
                    "type": 2,
                    "channel_id": "discord-channel-1",
                    "data": {"name": "pm"},
                    "member": {"user": {"id": "u-1"}},
                },
                find_tenant_for_discord_channel=_find_tenant_for_discord_channel,
            )

            self.assertEqual(tenant_id, "route25")
            self.assertIsNone(project_id)
            self.assertEqual(subject_key, "discord_channel:route25:discord-channel-1")
