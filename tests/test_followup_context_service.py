from __future__ import annotations

from tempfile import TemporaryDirectory
import unittest

import pytest

from orchestrator.core.followup_context_service import (
    close_followup_contexts,
    resolve_followup_context,
    upsert_followup_context,
)
from orchestrator.storage.models import FollowupContext
from tests.production_path_support import (
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
