from __future__ import annotations

from datetime import datetime, timezone
import tempfile
import unittest
from unittest.mock import patch

from orchestrator.core.config import get_settings
from orchestrator.core.decision_reply_service import capture_decision_reply, serialize_recorded_answers_for_policy
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import DecisionAnswer, DecisionCase, DecisionCycle, Project, Tenant


class DecisionReplyServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.database_url = f"sqlite:///{self._tmp.name}/decision_reply.db"
        reset_db_engine_cache()
        run_migrations(database_url=self.database_url)
        self.session_factory = create_session_factory(database_url=self.database_url)
        self.settings = get_settings()

        with self.session_factory() as session:
            tenant = Tenant(
                tenant_id="tenant-reply",
                name="Tenant",
                is_enabled=True,
                jira_config={},
                github_config={},
                repos_config={},
                policy_config={},
                discord_config={},
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            project = Project(
                project_id="project-reply",
                tenant_id="tenant-reply",
                name="Project",
                github_repository="https://github.com/acme/project",
                jira_project_key="MAB",
                policy_overrides={},
                environment={},
                secret_refs={},
                discord_config={},
                is_archived=False,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            case = DecisionCase(
                case_id="case-1",
                tenant_id="tenant-reply",
                project_id="project-reply",
                issue_key="MAB-173",
                state="blocked_decision_gate",
                blocked_reason="decision_gate_required",
                classification="decision_gate",
                issue_fingerprint=None,
                active_cycle_id="cycle-1",
                last_source="discord_reply",
                last_event_type="discord_reply",
                last_event_at=datetime.now(timezone.utc),
                required_worker_capability=None,
                required_worker_label=None,
                ready_label=None,
                ready_label_present=False,
                metadata_json={},
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            cycle = DecisionCycle(
                cycle_id="cycle-1",
                case_id="case-1",
                tenant_id="tenant-reply",
                project_id="project-reply",
                issue_key="MAB-173",
                status="open",
                reason="Need config",
                classification="decision_gate",
                question_set_json=[
                    {
                        "id": "dg_1",
                        "kind": "decision_gate",
                        "text": "What config is approved?",
                    }
                ],
                unresolved_question_ids_json=["dg_1"],
                metadata_json={},
                opened_at=datetime.now(timezone.utc),
                closed_at=None,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            accepted = DecisionAnswer(
                answer_id="answer-1",
                case_id="case-1",
                cycle_id="cycle-1",
                tenant_id="tenant-reply",
                project_id="project-reply",
                issue_key="MAB-173",
                question_id="dg_1",
                question_kind="decision_gate",
                question_text="What config is approved?",
                status="accepted",
                normalized_answer="Accepted config answer.",
                source_transport="discord",
                source_ref=None,
                evidence_ids_json=[],
                metadata_json={},
                answered_at=datetime.now(timezone.utc),
                accepted_at=datetime.now(timezone.utc),
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            session.add_all((tenant, project, case, cycle, accepted))
            session.commit()

    def tearDown(self) -> None:
        self._tmp.cleanup()
        reset_db_engine_cache()

    def test_serialize_recorded_answers_for_policy_includes_answered_and_accepted_answers(self) -> None:
        answered = DecisionAnswer(
            answer_id="a1",
            case_id="c1",
            cycle_id="cy1",
            tenant_id="t1",
            project_id="p1",
            issue_key="MAB-173",
            question_id="dg_1",
            question_kind="decision_gate",
            question_text="What config is approved?",
            status="answered",
            normalized_answer="Production bundle ID is com.example.app.",
            source_transport="discord",
            source_ref=None,
            evidence_ids_json=[],
            metadata_json={},
        )
        accepted = DecisionAnswer(
            answer_id="a2",
            case_id="c1",
            cycle_id="cy1",
            tenant_id="t1",
            project_id="p1",
            issue_key="MAB-173",
            question_id="dg_2",
            question_kind="decision_gate",
            question_text="Who approved storage policy?",
            status="accepted",
            normalized_answer="Platform Security approved Keychain-only storage.",
            source_transport="discord",
            source_ref=None,
            evidence_ids_json=[],
            metadata_json={},
        )

        result = serialize_recorded_answers_for_policy([answered, accepted])

        self.assertEqual(
            result,
            [
                {
                    "question_id": "dg_1",
                    "question_kind": "decision_gate",
                    "question_text": "What config is approved?",
                    "status": "answered",
                    "answer": "Production bundle ID is com.example.app.",
                },
                {
                    "question_id": "dg_2",
                    "question_kind": "decision_gate",
                    "question_text": "Who approved storage policy?",
                    "status": "accepted",
                    "answer": "Platform Security approved Keychain-only storage.",
                },
            ],
        )

    def test_capture_decision_reply_does_not_downgrade_accepted_answer(self) -> None:
        with self.session_factory() as session, patch(
            "orchestrator.core.decision_reply_service._extract_reply_matches",
            return_value=[
                {
                    "question_id": "dg_1",
                    "status": "answered",
                    "answer": "A weaker follow-up answer.",
                    "notes": "partial follow-up",
                }
            ],
        ):
            tenant = session.get(Tenant, "tenant-reply")
            project = session.get(Project, "project-reply")
            assert tenant is not None and project is not None

            capture = capture_decision_reply(
                session=session,
                settings=self.settings,
                tenant=tenant,
                project=project,
                issue_key="MAB-173",
                reply_text="Follow-up reply",
                source_transport="discord",
                source_ref="msg-1",
            )
            session.commit()

            answer = session.get(DecisionAnswer, "answer-1")

        assert answer is not None
        self.assertEqual(capture.accepted_question_ids, ("dg_1",))
        self.assertEqual(capture.answered_question_ids, ())
        self.assertEqual(answer.status, "accepted")
        self.assertEqual(answer.normalized_answer, "Accepted config answer.")
