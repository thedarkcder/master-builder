from __future__ import annotations

from datetime import datetime, timezone
import tempfile
import unittest
import unittest.mock

from sqlalchemy import select

from orchestrator.core.config import get_settings
from orchestrator.core.decision.gate import DecisionGateResult
from orchestrator.core.decision.precheck_mapping import apply_frozen_cycle_to_precheck
from orchestrator.core.decision.reply_service import (
    capture_decision_reply,
    latest_recorded_answers_for_issue,
    serialize_recorded_answers_for_policy,
    sync_cycle_answers_from_planner,
    unresolved_question_feedback_for_cycle,
    unresolved_question_ids_for_cycle,
)
from orchestrator.core.decision.presentation import build_cycle_comment
from orchestrator.core.gtd import GoodToDoValidationResult
from orchestrator.core.precheck.pre_run_check import PreRunCheckResult
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import (
    DecisionAnswer,
    DecisionCase,
    DecisionCycle,
    Project,
    Tenant,
)


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

    def _create_gtd_dependencies_cycle(
        self, *, session, issue_key: str, question_text: str
    ) -> tuple[DecisionCase, DecisionCycle]:
        case = DecisionCase(
            case_id=f"case-{issue_key}",
            tenant_id="tenant-reply",
            project_id="project-reply",
            issue_key=issue_key,
            state="blocked_gtd",
            blocked_reason="gtd_required",
            classification="gtd",
            issue_fingerprint=None,
            active_cycle_id=f"cycle-{issue_key}",
            last_source="jira_webhook",
            last_event_type="comment_created",
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
            cycle_id=f"cycle-{issue_key}",
            case_id=case.case_id,
            tenant_id="tenant-reply",
            project_id="project-reply",
            issue_key=issue_key,
            status="open",
            reason="Need dependencies and risks",
            classification="gtd",
            question_set_json=[
                {
                    "id": "gtd_dependencies_risks",
                    "kind": "gtd",
                    "text": question_text,
                }
            ],
            unresolved_question_ids_json=["gtd_dependencies_risks"],
            metadata_json={},
            opened_at=datetime.now(timezone.utc),
            closed_at=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        session.add_all((case, cycle))
        session.flush()
        return case, cycle

    def test_serialize_recorded_answers_for_policy_includes_answered_and_accepted_answers(
        self,
    ) -> None:
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

    def test_capture_decision_reply_persists_accepted_answer_and_stages_effects(
        self,
    ) -> None:
        with (
            self.session_factory() as session,
            unittest.mock.patch(
                "orchestrator.core.decision.reply_service.build_codex_runtime",
                return_value=object(),
            ),
            unittest.mock.patch(
                "orchestrator.core.decision.reply_service.invoke_runtime_json",
                return_value={
                    "message": "Captured.",
                    "actions": [
                        {
                            "type": "capture_decision_answer",
                            "payload": {
                                "question_id": "dg_1",
                                "status": "accepted",
                                "answer": "Reject relink; device_id stays bound to one user only.",
                                "notes": "Cross-account relink policy confirmed.",
                            },
                        }
                    ],
                },
            ),
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
                reply_text="Reject relink; device_id stays bound to one user only.",
                source_transport="discord",
                source_ref="msg-1",
            )
            session.commit()

            answer = session.get(DecisionAnswer, "answer-1")
            latest_answers = latest_recorded_answers_for_issue(
                session=session,
                tenant_id="tenant-reply",
                issue_key="MAB-173",
            )

        assert answer is not None
        self.assertEqual(capture.accepted_question_ids, ("dg_1",))
        self.assertEqual(capture.answered_question_ids, ())
        self.assertEqual(answer.status, "accepted")
        self.assertEqual(
            answer.normalized_answer,
            "Reject relink; device_id stays bound to one user only.",
        )
        self.assertEqual(answer.evidence_ids_json, [capture.evidence_id])
        self.assertEqual(len(capture.effect_ids), 1)
        self.assertEqual(capture.unresolved_question_feedback, ())
        self.assertEqual([item.question_id for item in latest_answers], ["dg_1"])
        self.assertTrue(capture.evidence_id)

    def test_planner_state_drives_unresolved_feedback_from_missing_items(self) -> None:
        with self.session_factory() as session:
            answer = session.get(DecisionAnswer, "answer-1")
            assert answer is not None
            answer.status = "open"
            answer.normalized_answer = None
            answer.metadata_json = {}
            answer.accepted_at = None
            answer.updated_at = datetime.now(timezone.utc)
            session.commit()

            tenant = session.get(Tenant, "tenant-reply")
            project = session.get(Project, "project-reply")
            case = session.get(DecisionCase, "case-1")
            cycle = session.get(DecisionCycle, "cycle-1")
            assert (
                tenant is not None
                and project is not None
                and case is not None
                and cycle is not None
            )

            with (
                unittest.mock.patch(
                    "orchestrator.core.decision.reply_service.build_codex_runtime",
                    return_value=object(),
                ),
                unittest.mock.patch(
                    "orchestrator.core.decision.reply_service.invoke_runtime_json",
                    return_value={
                        "message": "Captured.",
                        "actions": [
                            {
                                "type": "capture_decision_answer",
                                "payload": {
                                    "question_id": "dg_1",
                                    "status": "answered",
                                    "answer": "Use the production bundle id.",
                                    "notes": "Entitlement confirmation is still missing.",
                                },
                            }
                        ],
                    },
                ),
            ):
                capture = capture_decision_reply(
                    session=session,
                    settings=self.settings,
                    tenant=tenant,
                    project=project,
                    issue_key="MAB-173",
                    reply_text="Follow-up reply",
                    source_transport="discord",
                    source_ref="msg-2",
                )
            accepted_ids, answered_ids, effect_ids = sync_cycle_answers_from_planner(
                session=session,
                tenant=tenant,
                project=project,
                case=case,
                cycle=cycle,
                planner_question_states=[
                    {
                        "question_id": "dg_1",
                        "kind": "decision_gate",
                        "question": "What config is approved?",
                        "status": "answered",
                        "detail": "Config values were captured, but entitlement confirmation is still missing.",
                    }
                ],
                now=datetime.now(timezone.utc),
            )
            session.commit()

            question_feedback = list(
                unresolved_question_feedback_for_cycle(
                    session=session, cycle_id=cycle.cycle_id
                )
            )
            comment = build_cycle_comment(session=session, case=case, cycle=cycle)

        self.assertEqual(capture.answered_question_ids, ("dg_1",))
        self.assertEqual(accepted_ids, ())
        self.assertEqual(answered_ids, ("dg_1",))
        self.assertEqual(effect_ids, ())
        self.assertEqual(len(question_feedback), 1)
        self.assertEqual(question_feedback[0]["question_id"], "dg_1")
        self.assertIn(
            "Config values were captured, but entitlement confirmation is still missing.",
            question_feedback[0]["note"],
        )
        self.assertIn(
            "Missing detail: Config values were captured, but entitlement confirmation is still missing.",
            comment,
        )

    def test_all_accepted_cycle_shows_no_outstanding_questions(self) -> None:
        with self.session_factory() as session:
            case = session.get(DecisionCase, "case-1")
            cycle = session.get(DecisionCycle, "cycle-1")
            answer = session.get(DecisionAnswer, "answer-1")
            assert case is not None and cycle is not None and answer is not None

            cycle.unresolved_question_ids_json = []
            answer.status = "accepted"
            answer.normalized_answer = "Accepted config answer."
            answer.updated_at = datetime.now(timezone.utc)
            session.commit()

            comment = build_cycle_comment(session=session, case=case, cycle=cycle)
            feedback = unresolved_question_feedback_for_cycle(
                session=session, cycle_id=cycle.cycle_id
            )

        self.assertIn("Outstanding questions:", comment)
        self.assertNotIn(
            "[dg_1] What config is approved?",
            comment.split("Outstanding questions:")[-1],
        )
        self.assertEqual(feedback, ())

    def test_capture_decision_reply_keeps_answered_status_until_planner_accepts(
        self,
    ) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-reply")
            project = session.get(Project, "project-reply")
            assert tenant is not None and project is not None
            self._create_gtd_dependencies_cycle(
                session=session,
                issue_key="MAB-174",
                question_text='For MAB-174, what should be recorded in Dependencies / Risks? Reply "none" if there are no additional dependencies or risks.',
            )
            session.commit()

            with (
                unittest.mock.patch(
                    "orchestrator.core.decision.reply_service.build_codex_runtime",
                    return_value=object(),
                ),
                unittest.mock.patch(
                    "orchestrator.core.decision.reply_service.invoke_runtime_json",
                    return_value={
                        "message": "Captured.",
                        "actions": [
                            {
                                "type": "capture_decision_answer",
                                "payload": {
                                    "question_id": "gtd_dependencies_risks",
                                    "status": "answered",
                                    "answer": "Dependencies / Risks: CI signing depends on a valid provisioning profile; risk is TestFlight build failure until signing is configured.",
                                    "notes": "Concrete final dependencies/risks entry.",
                                },
                            }
                        ],
                    },
                ),
            ):
                capture = capture_decision_reply(
                    session=session,
                    settings=self.settings,
                    tenant=tenant,
                    project=project,
                    issue_key="MAB-174",
                    reply_text="Dependencies / Risks: CI signing depends on a valid provisioning profile; risk is TestFlight build failure until signing is configured.",
                    source_transport="discord",
                    source_ref="msg-gtd-1",
                )
            session.commit()

            answer = session.execute(
                select(DecisionAnswer).where(
                    DecisionAnswer.issue_key == "MAB-174",
                    DecisionAnswer.question_id == "gtd_dependencies_risks",
                )
            ).scalar_one()

        self.assertEqual(capture.accepted_question_ids, ())
        self.assertEqual(capture.answered_question_ids, ("gtd_dependencies_risks",))
        self.assertEqual(answer.status, "answered")

    def test_sync_cycle_answers_from_planner_keeps_planner_answered_status(
        self,
    ) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-reply")
            project = session.get(Project, "project-reply")
            assert tenant is not None and project is not None
            case, cycle = self._create_gtd_dependencies_cycle(
                session=session,
                issue_key="MAB-175",
                question_text='For MAB-175, what should be recorded in Dependencies / Risks? Reply "none" if there are no additional dependencies or risks.',
            )
            answer = DecisionAnswer(
                answer_id="answer-gtd-175",
                case_id=case.case_id,
                cycle_id=cycle.cycle_id,
                tenant_id="tenant-reply",
                project_id="project-reply",
                issue_key="MAB-175",
                question_id="gtd_dependencies_risks",
                question_kind="gtd",
                question_text=cycle.question_set_json[0]["text"],
                status="answered",
                normalized_answer="Dependencies / Risks: CI signing depends on a valid provisioning profile; risk is TestFlight build failure until signing is configured.",
                source_transport="discord",
                source_ref="msg-gtd-175",
                evidence_ids_json=[],
                metadata_json={},
                answered_at=datetime.now(timezone.utc),
                accepted_at=None,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            session.add(answer)
            session.commit()

            accepted_ids, answered_ids, effect_ids = sync_cycle_answers_from_planner(
                session=session,
                tenant=tenant,
                project=project,
                case=case,
                cycle=cycle,
                planner_question_states=[
                    {
                        "question_id": "gtd_dependencies_risks",
                        "kind": "gtd",
                        "question": cycle.question_set_json[0]["text"],
                        "status": "answered",
                        "detail": "Concrete risk was captured and should become the final Dependencies / Risks entry.",
                    }
                ],
                now=datetime.now(timezone.utc),
            )
            session.commit()
            session.refresh(answer)

        self.assertEqual(accepted_ids, ())
        self.assertEqual(answered_ids, ("gtd_dependencies_risks",))
        self.assertEqual(answer.status, "answered")
        self.assertEqual(
            answer.normalized_answer,
            "Dependencies / Risks: CI signing depends on a valid provisioning profile; risk is TestFlight build failure until signing is configured.",
        )
        self.assertEqual(effect_ids, ())

    def test_sync_cycle_answers_from_planner_keeps_meta_dependencies_and_risks_answer_open(
        self,
    ) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-reply")
            project = session.get(Project, "project-reply")
            assert tenant is not None and project is not None
            case, cycle = self._create_gtd_dependencies_cycle(
                session=session,
                issue_key="MAB-176",
                question_text='For MAB-176, what should be recorded in Dependencies / Risks? Reply "none" if there are no additional dependencies or risks.',
            )
            answer = DecisionAnswer(
                answer_id="answer-gtd-176",
                case_id=case.case_id,
                cycle_id=cycle.cycle_id,
                tenant_id="tenant-reply",
                project_id="project-reply",
                issue_key="MAB-176",
                question_id="gtd_dependencies_risks",
                question_kind="gtd",
                question_text=cycle.question_set_json[0]["text"],
                status="answered",
                normalized_answer="The only remaining gap is an explicit Dependencies / Risks entry for the workflow.",
                source_transport="discord",
                source_ref="msg-gtd-176",
                evidence_ids_json=[],
                metadata_json={},
                answered_at=datetime.now(timezone.utc),
                accepted_at=None,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            session.add(answer)
            session.commit()

            accepted_ids, answered_ids, effect_ids = sync_cycle_answers_from_planner(
                session=session,
                tenant=tenant,
                project=project,
                case=case,
                cycle=cycle,
                planner_question_states=[
                    {
                        "question_id": "gtd_dependencies_risks",
                        "kind": "gtd",
                        "question": cycle.question_set_json[0]["text"],
                        "status": "answered",
                        "detail": "Still waiting for the final Dependencies / Risks wording.",
                    }
                ],
                now=datetime.now(timezone.utc),
            )
            session.commit()
            session.refresh(answer)

        self.assertEqual(accepted_ids, ())
        self.assertEqual(answered_ids, ("gtd_dependencies_risks",))
        self.assertEqual(answer.status, "answered")
        self.assertEqual(effect_ids, ())

    def test_apply_frozen_cycle_to_precheck_hides_questions_when_none_unresolved(
        self,
    ) -> None:
        with self.session_factory() as session:
            cycle = session.get(DecisionCycle, "cycle-1")
            assert cycle is not None
            cycle.unresolved_question_ids_json = []
            session.commit()

            pre_check = PreRunCheckResult(
                outcome="decision_gate_required",
                ready_label="agent:ready",
                ready_label_present=False,
                required_worker_capability="linux",
                required_worker_label="worker:linux",
                required_worker_label_present=False,
                decision_gate=DecisionGateResult(
                    triggered=True,
                    reason="Need config",
                    missing_sections=(),
                    questions=("What config is approved?",),
                    recommendation="Block",
                    tags=(),
                ),
                gtd=GoodToDoValidationResult(
                    valid=True,
                    missing_criteria=(),
                    clarification_questions=(),
                ),
            )

            resolved = apply_frozen_cycle_to_precheck(
                pre_check=pre_check, cycle=cycle, classification="decision_gate"
            )

        self.assertEqual(resolved.decision_gate.questions, ())

    def test_question_set_resolved_status_is_not_unresolved_without_answer_row(
        self,
    ) -> None:
        with self.session_factory() as session:
            cycle = session.get(DecisionCycle, "cycle-1")
            assert cycle is not None
            session.delete(session.get(DecisionAnswer, "answer-1"))
            cycle.question_set_json = [
                {
                    "id": "objective",
                    "kind": "decision_gate",
                    "text": "What is the objective?",
                    "status": "accepted",
                    "detail": "Already present in Jira.",
                },
                {
                    "id": "scope",
                    "kind": "decision_gate",
                    "text": "What is in scope?",
                    "status": "answered",
                    "detail": "Already present in Jira.",
                    "unresolved": False,
                },
                {
                    "id": "owner",
                    "kind": "decision_gate",
                    "text": "Who owns approval?",
                    "status": "open",
                    "detail": "Owner is missing.",
                    "unresolved": True,
                },
            ]
            cycle.unresolved_question_ids_json = ["objective", "scope", "owner"]
            session.commit()

            unresolved_ids = unresolved_question_ids_for_cycle(
                session=session, cycle=cycle
            )
            cycle.unresolved_question_ids_json = list(unresolved_ids)
            feedback = unresolved_question_feedback_for_cycle(
                session=session, cycle_id=cycle.cycle_id
            )

        self.assertEqual(unresolved_ids, ("owner",))
        self.assertEqual([item["question_id"] for item in feedback], ["owner"])
