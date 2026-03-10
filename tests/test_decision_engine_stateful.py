from __future__ import annotations

from datetime import datetime, timezone
import tempfile
import unittest
from unittest.mock import patch

from orchestrator.core.config import get_settings
from orchestrator.core.decision_engine import DecisionEventInput, evaluate_decision_event
from orchestrator.core.decision_gate import DecisionGateResult
from orchestrator.core.gtd import GoodToDoValidationResult
from orchestrator.core.pre_run_check import PreRunCheckResult
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import DecisionCase, DecisionEffectOutbox, DecisionEvent, Project, Tenant


def _precheck_result(
    *,
    outcome: str,
    decision_gate_triggered: bool = False,
    decision_gate_reason: str = "Decision Gate not required",
    decision_gate_questions: tuple[str, ...] = (),
    decision_gate_missing_sections: tuple[str, ...] = (),
    gtd_valid: bool = True,
    gtd_missing_criteria: tuple[str, ...] = (),
    gtd_questions: tuple[str, ...] = (),
) -> PreRunCheckResult:
    return PreRunCheckResult(
        outcome=outcome,
        ready_label="agent:ready",
        ready_label_present=True,
        required_worker_capability="linux",
        required_worker_label="worker:linux",
        required_worker_label_present=True,
        decision_gate=DecisionGateResult(
            triggered=decision_gate_triggered,
            reason=decision_gate_reason,
            missing_sections=decision_gate_missing_sections,
            questions=decision_gate_questions,
            recommendation="Proceed" if not decision_gate_triggered else "Clarification required",
            tags=(),
        ),
        gtd=GoodToDoValidationResult(
            valid=gtd_valid,
            missing_criteria=gtd_missing_criteria,
            clarification_questions=gtd_questions,
        ),
    )


class DecisionEngineStatefulTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.database_url = f"sqlite:///{self._tmp.name}/decision_stateful.db"
        reset_db_engine_cache()
        run_migrations(database_url=self.database_url)
        self.session_factory = create_session_factory(database_url=self.database_url)
        self.settings = get_settings()

        with self.session_factory() as session:
            tenant = Tenant(
                tenant_id="tenant-stateful",
                name="Tenant",
                is_enabled=True,
                jira_config={},
                github_config={},
                repos_config={},
                policy_config={"knowledge_base_enabled": True, "knowledge_auto_answer_mode": "aggressive"},
                discord_config={},
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            project = Project(
                project_id="project-stateful",
                tenant_id="tenant-stateful",
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
            session.add(tenant)
            session.add(project)
            session.commit()

    def tearDown(self) -> None:
        self._tmp.cleanup()
        reset_db_engine_cache()

    def _tenant_and_project(self):
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-stateful")
            project = session.get(Project, "project-stateful")
            return tenant, project

    def test_freezes_cycle_questions_while_open(self) -> None:
        prechecks = [
            _precheck_result(
                outcome="decision_gate_required",
                decision_gate_triggered=True,
                decision_gate_reason="Need owner decision",
                decision_gate_questions=("Who owns this decision?",),
                decision_gate_missing_sections=("decision owner",),
            ),
            _precheck_result(
                outcome="decision_gate_required",
                decision_gate_triggered=True,
                decision_gate_reason="Need owner decision",
                decision_gate_questions=("Different regenerated question?",),
                decision_gate_missing_sections=("decision owner",),
            ),
        ]

        def _evaluate_pre_run_check_stub(**_: object) -> PreRunCheckResult:
            return prechecks.pop(0)

        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-stateful")
            project = session.get(Project, "project-stateful")
            assert tenant is not None and project is not None

            first = evaluate_decision_event(
                session=session,
                tenant=tenant,
                project=project,
                event=DecisionEventInput(
                    source="jira_webhook",
                    event_type="issue_updated",
                    idempotency_key="freeze-1",
                    issue_key="MAB-161",
                    issue_summary="Summary",
                    issue_description="Description",
                    issue_labels=[],
                ),
                settings=self.settings,
                tenant_jira_oauth_context_fn=lambda **__: None,
                evaluate_pre_run_check_fn=_evaluate_pre_run_check_stub,
            )
            second = evaluate_decision_event(
                session=session,
                tenant=tenant,
                project=project,
                event=DecisionEventInput(
                    source="jira_webhook",
                    event_type="issue_updated",
                    idempotency_key="freeze-2",
                    issue_key="MAB-161",
                    issue_summary="Summary",
                    issue_description="Description",
                    issue_labels=[],
                ),
                settings=self.settings,
                tenant_jira_oauth_context_fn=lambda **__: None,
                evaluate_pre_run_check_fn=_evaluate_pre_run_check_stub,
            )

        self.assertEqual(first.cycle_id, second.cycle_id)
        self.assertEqual(
            tuple(second.decision.pre_check.decision_gate.questions),
            ("Who owns this decision?",),
        )

    def test_idempotency_key_deduplicates_event_rows(self) -> None:
        precheck = _precheck_result(outcome="ready_for_agent")

        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-stateful")
            project = session.get(Project, "project-stateful")
            assert tenant is not None and project is not None

            first = evaluate_decision_event(
                session=session,
                tenant=tenant,
                project=project,
                event=DecisionEventInput(
                    source="jira_webhook",
                    event_type="issue_updated",
                    idempotency_key="dup-key",
                    issue_key="MAB-162",
                    issue_summary="Summary",
                    issue_description="Description",
                    issue_labels=[],
                ),
                settings=self.settings,
                tenant_jira_oauth_context_fn=lambda **__: None,
                evaluate_pre_run_check_fn=lambda **__: precheck,
            )
            second = evaluate_decision_event(
                session=session,
                tenant=tenant,
                project=project,
                event=DecisionEventInput(
                    source="jira_webhook",
                    event_type="issue_updated",
                    idempotency_key="dup-key",
                    issue_key="MAB-162",
                    issue_summary="Summary",
                    issue_description="Description",
                    issue_labels=[],
                ),
                settings=self.settings,
                tenant_jira_oauth_context_fn=lambda **__: None,
                evaluate_pre_run_check_fn=lambda **__: precheck,
            )
            event_count = len(session.query(DecisionEvent).all())

        self.assertFalse(first.duplicate_event)
        self.assertTrue(second.duplicate_event)
        self.assertEqual(event_count, 1)

    def test_duplicate_event_does_not_mutate_existing_case_state(self) -> None:
        prechecks = [
            _precheck_result(
                outcome="decision_gate_required",
                decision_gate_triggered=True,
                decision_gate_reason="Need owner",
                decision_gate_questions=("Who owns this?",),
                decision_gate_missing_sections=("decision owner",),
            ),
            _precheck_result(outcome="ready_for_agent"),
        ]

        def _evaluate_pre_run_check_stub(**_: object) -> PreRunCheckResult:
            return prechecks.pop(0)

        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-stateful")
            project = session.get(Project, "project-stateful")
            assert tenant is not None and project is not None

            first = evaluate_decision_event(
                session=session,
                tenant=tenant,
                project=project,
                event=DecisionEventInput(
                    source="jira_webhook",
                    event_type="issue_updated",
                    idempotency_key="dup-state",
                    issue_key="MAB-164",
                    issue_summary="Summary",
                    issue_description="Description",
                    issue_labels=[],
                ),
                settings=self.settings,
                tenant_jira_oauth_context_fn=lambda **__: None,
                evaluate_pre_run_check_fn=_evaluate_pre_run_check_stub,
            )
            case_before = session.query(DecisionCase).filter_by(issue_key="MAB-164").one()
            updated_at_before = case_before.updated_at

            second = evaluate_decision_event(
                session=session,
                tenant=tenant,
                project=project,
                event=DecisionEventInput(
                    source="jira_webhook",
                    event_type="issue_updated",
                    idempotency_key="dup-state",
                    issue_key="MAB-164",
                    issue_summary="Summary changed",
                    issue_description="Changed Description",
                    issue_labels=["agent:ready"],
                ),
                settings=self.settings,
                tenant_jira_oauth_context_fn=lambda **__: None,
                evaluate_pre_run_check_fn=_evaluate_pre_run_check_stub,
            )
            case_after = session.query(DecisionCase).filter_by(issue_key="MAB-164").one()

        self.assertEqual(first.case_state, "blocked_decision_gate")
        self.assertTrue(second.duplicate_event)
        self.assertEqual(second.case_state, "blocked_decision_gate")
        self.assertEqual(tuple(second.decision.pre_check.decision_gate.questions), ("Who owns this?",))
        self.assertEqual(case_after.updated_at, updated_at_before)

    def test_reevaluation_includes_recorded_answers(self) -> None:
        prechecks = [
            _precheck_result(
                outcome="decision_gate_required",
                decision_gate_triggered=True,
                decision_gate_reason="Need config",
                decision_gate_questions=("What config is approved?",),
                decision_gate_missing_sections=("config",),
            ),
            _precheck_result(outcome="ready_for_agent"),
        ]
        recorded_answers_seen: list[list[dict[str, str]] | None] = []

        def _evaluate_pre_run_check_stub(**kwargs: object) -> PreRunCheckResult:
            recorded_answers_seen.append(kwargs.get("recorded_answers"))  # type: ignore[arg-type]
            return prechecks.pop(0)

        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-stateful")
            project = session.get(Project, "project-stateful")
            assert tenant is not None and project is not None

            first = evaluate_decision_event(
                session=session,
                tenant=tenant,
                project=project,
                event=DecisionEventInput(
                    source="jira_webhook",
                    event_type="issue_updated",
                    idempotency_key="answer-context-1",
                    issue_key="MAB-166",
                    issue_summary="Summary",
                    issue_description="Description",
                    issue_labels=[],
                ),
                settings=self.settings,
                tenant_jira_oauth_context_fn=lambda **__: None,
                evaluate_pre_run_check_fn=_evaluate_pre_run_check_stub,
            )
            self.assertEqual(first.classification, "decision_gate")

            from orchestrator.storage.models import DecisionAnswer

            session.add(
                DecisionAnswer(
                    answer_id="ans-1",
                    case_id=first.case_id,
                    cycle_id=str(first.cycle_id),
                    tenant_id=tenant.tenant_id,
                    project_id=project.project_id,
                    issue_key="MAB-166",
                    question_id="dg_config",
                    question_kind="decision_gate",
                    question_text="What config is approved?",
                    status="answered",
                    normalized_answer="Production bundle ID is com.example.app.",
                    source_transport="discord",
                    source_ref=None,
                    evidence_ids_json=[],
                    metadata_json={},
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
            )
            session.commit()

            second = evaluate_decision_event(
                session=session,
                tenant=tenant,
                project=project,
                event=DecisionEventInput(
                    source="jira_webhook",
                    event_type="issue_updated",
                    idempotency_key="answer-context-2",
                    issue_key="MAB-166",
                    issue_summary="Summary",
                    issue_description="Description",
                    issue_labels=[],
                ),
                settings=self.settings,
                tenant_jira_oauth_context_fn=lambda **__: None,
                evaluate_pre_run_check_fn=_evaluate_pre_run_check_stub,
            )

        self.assertEqual(second.classification, "clear")
        self.assertGreaterEqual(len(recorded_answers_seen), 2)
        self.assertEqual(
            recorded_answers_seen[-1],
            [
                {
                    "question_id": "dg_config",
                    "question_kind": "decision_gate",
                    "question_text": "What config is approved?",
                    "status": "answered",
                    "answer": "Production bundle ID is com.example.app.",
                }
            ],
        )

    def test_jira_comment_effect_is_published_after_state_commit(self) -> None:
        precheck = _precheck_result(
            outcome="decision_gate_required",
            decision_gate_triggered=True,
            decision_gate_reason="Need owner decision",
            decision_gate_questions=("Who owns this decision?",),
            decision_gate_missing_sections=("decision owner",),
        )
        observed_state: dict[str, object] = {}

        def _publish(comment: str) -> tuple[bool, str | None]:
            with self.session_factory() as verify_session:
                case = verify_session.query(DecisionCase).filter_by(issue_key="MAB-165").one()
                effect = verify_session.query(DecisionEffectOutbox).filter_by(issue_key="MAB-165").one()
                observed_state["case_state"] = case.state
                observed_state["effect_status"] = effect.status
            observed_state["comment"] = comment
            return True, None

        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-stateful")
            project = session.get(Project, "project-stateful")
            assert tenant is not None and project is not None

            result = evaluate_decision_event(
                session=session,
                tenant=tenant,
                project=project,
                event=DecisionEventInput(
                    source="jira_webhook",
                    event_type="issue_updated",
                    idempotency_key="effect-1",
                    issue_key="MAB-165",
                    issue_summary="Summary",
                    issue_description="Description",
                    issue_labels=[],
                ),
                settings=self.settings,
                tenant_jira_oauth_context_fn=lambda **__: None,
                publish_jira_comment_fn=_publish,
                evaluate_pre_run_check_fn=lambda **__: precheck,
            )

            effect = session.query(DecisionEffectOutbox).filter_by(issue_key="MAB-165").one()

        self.assertEqual(result.case_state, "blocked_decision_gate")
        self.assertEqual(observed_state["case_state"], "blocked_decision_gate")
        self.assertEqual(observed_state["effect_status"], "pending")
        self.assertIn("Who owns this decision?", str(observed_state["comment"]))
        self.assertEqual(effect.status, "sent")

    def test_resolves_before_block_when_knowledge_answers_missing_slot(self) -> None:
        def _stub_precheck(**kwargs: object) -> PreRunCheckResult:
            description = str(kwargs.get("issue_description") or "")
            if "Auto-resolved context for precheck:" in description:
                return _precheck_result(outcome="ready_for_agent")
            return _precheck_result(
                outcome="decision_gate_required",
                decision_gate_triggered=True,
                decision_gate_reason="Missing objective",
                decision_gate_questions=("What is the objective?",),
                decision_gate_missing_sections=("objective",),
            )

        from orchestrator.core.knowledge_base import SlotResolution

        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-stateful")
            project = session.get(Project, "project-stateful")
            assert tenant is not None and project is not None
            with patch(
                "orchestrator.core.decision_engine.resolve_missing_slots_from_knowledge",
                return_value={
                    "objective": SlotResolution(
                        slot_name="objective",
                        slot_value="Implement centralized decision engine.",
                        source_timestamp=None,
                        confidence=0.95,
                        citation={"title": "KB fact", "source_type": "manual"},
                        inferred=False,
                    )
                },
            ):
                result = evaluate_decision_event(
                    session=session,
                    tenant=tenant,
                    project=project,
                    event=DecisionEventInput(
                        source="jira_webhook",
                        event_type="issue_updated",
                        idempotency_key="resolve-1",
                        issue_key="MAB-163",
                        issue_summary="Summary",
                        issue_description="Description",
                        issue_labels=[],
                    ),
                    settings=self.settings,
                    tenant_jira_oauth_context_fn=lambda **__: None,
                    evaluate_pre_run_check_fn=_stub_precheck,
                )

        self.assertEqual(result.decision.block_reason, None)
        self.assertIn("objective", result.auto_resolved_slots)
