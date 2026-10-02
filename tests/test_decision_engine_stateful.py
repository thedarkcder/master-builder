from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from orchestrator.core.config import get_settings
from orchestrator.core.decision.planner import (
    DecisionPlannerQuestion,
    DecisionPlannerResult,
)
from orchestrator.core.decision.engine import (
    DecisionEventInput,
    evaluate_decision_event,
)
from orchestrator.core.decision.state_repository import existing_case_for_issue
from orchestrator.core.decision.gate import DecisionGateResult
from orchestrator.core.gtd import GoodToDoValidationResult
from orchestrator.core.precheck.pre_run_check import PreRunCheckResult
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.models import (
    DecisionCase,
    DecisionCycle,
    DecisionEffectOutbox,
    DecisionEvent,
    Project,
    Tenant,
    TenantMembership,
    TenantUser,
)
from tests.test_support.db_harness import SqliteTemplateDbTestCase


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
            recommendation="Proceed"
            if not decision_gate_triggered
            else "Clarification required",
            tags=(),
        ),
        gtd=GoodToDoValidationResult(
            valid=gtd_valid,
            missing_criteria=gtd_missing_criteria,
            clarification_questions=gtd_questions,
        ),
    )


def _planner_result(
    *,
    gate_status: str,
    reason: str,
    questions: tuple[tuple[str, str], ...],
    statuses: dict[str, str] | None = None,
    details: dict[str, str] | None = None,
    question_states: tuple[tuple[str, str], ...] | None = None,
) -> DecisionPlannerResult:
    normalized_statuses = dict(statuses or {})
    normalized_details = dict(details or {})
    state_questions = question_states or questions
    planner_question_states = tuple(
        DecisionPlannerQuestion(
            question_id=question_id,
            kind="decision_gate",
            question=question_text,
            status=normalized_statuses.get(question_id, "open"),
            detail=normalized_details.get(question_id),
        )
        for question_id, question_text in state_questions
    )
    planner_questions = tuple(
        DecisionPlannerQuestion(
            question_id=question_id,
            kind="decision_gate",
            question=question_text,
            status=normalized_statuses.get(question_id, "open"),
            detail=normalized_details.get(question_id),
        )
        for question_id, question_text in questions
    )
    return DecisionPlannerResult(
        gate_status=gate_status,
        reason=reason,
        questions=tuple(
            question
            for question in planner_questions
            if question.status in {"open", "answered"}
        ),
        question_states=planner_question_states,
        resolved_items=(),
        missing_items=(),
        captured_answer_summary=None,
    )


def _add_active_tenant_member(
    *, session, tenant_id: str, user_id: str, email: str
) -> None:  # noqa: ANN001
    now = datetime.now(timezone.utc)
    session.add(
        TenantUser(
            user_id=user_id,
            email=email,
            full_name=user_id,
            is_active=True,
            created_at=now,
            updated_at=now,
        )
    )
    session.add(
        TenantMembership(
            membership_id=f"membership-{user_id}",
            tenant_id=tenant_id,
            user_id=user_id,
            role="tenant_admin",
            mode_override=None,
            onboarding_kind="member_join",
            first_signed_in_at=now,
            onboarding_completed_at=now,
            onboarding_version=None,
            discord_state={},
            created_at=now,
            updated_at=now,
        )
    )
    session.flush()


class DecisionEngineStatefulTests(SqliteTemplateDbTestCase):
    @classmethod
    def bootstrap_template_database(cls) -> None:
        session_factory = create_session_factory(
            database_url=cls._template_database_url
        )
        with session_factory() as session:
            tenant = Tenant(
                tenant_id="tenant-stateful",
                name="Tenant",
                is_enabled=True,
                jira_config={},
                github_config={},
                repos_config={},
                policy_config={
                    "knowledge_base_enabled": True,
                    "knowledge_auto_answer_mode": "aggressive",
                },
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

    def setUp(self) -> None:
        self.database_url = self._prepare_test_database(name_prefix="decision-stateful")
        reset_db_engine_cache()
        self.session_factory = create_session_factory(database_url=self.database_url)
        self.settings = get_settings()
        self._codex_resolution_patcher = patch(
            "orchestrator.core.decision.engine.resolve_slots_with_runtime_resolution",
            return_value={},
        )
        self._codex_resolution_patcher.start()

    def tearDown(self) -> None:
        self._codex_resolution_patcher.stop()
        self._cleanup_test_database()
        reset_db_engine_cache()

    def _tenant_and_project(self):
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-stateful")
            project = session.get(Project, "project-stateful")
            return tenant, project

    def test_freezes_cycle_questions_while_open(self) -> None:
        precheck_calls = 0
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
            nonlocal precheck_calls
            precheck_calls += 1
            return prechecks.pop(0)

        with (
            self.session_factory() as session,
            patch(
                "orchestrator.core.decision.engine.plan_decision_questions",
                side_effect=[
                    _planner_result(
                        gate_status="blocked_decision_gate",
                        reason="Need owner decision",
                        questions=(("dg_owner", "Who owns this decision?"),),
                    ),
                    _planner_result(
                        gate_status="blocked_decision_gate",
                        reason="Need owner decision",
                        questions=(("dg_owner", "Who owns this decision?"),),
                    ),
                ],
            ),
        ):
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
                tenant_atlassian_oauth_context_fn=lambda **__: None,
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
                tenant_atlassian_oauth_context_fn=lambda **__: None,
                evaluate_pre_run_check_fn=_evaluate_pre_run_check_stub,
            )

        self.assertEqual(first.cycle_id, second.cycle_id)
        self.assertEqual(
            tuple(second.decision.pre_check.decision_gate.questions),
            ("Who owns this decision?",),
        )
        self.assertEqual(precheck_calls, 1)

    def test_open_cycle_uses_planner_narrowed_question_wording(self) -> None:
        precheck = _precheck_result(
            outcome="decision_gate_required",
            decision_gate_triggered=True,
            decision_gate_reason="Need auth decisions",
            decision_gate_questions=("Confirm auth configuration.",),
            decision_gate_missing_sections=("auth configuration",),
        )

        with (
            self.session_factory() as session,
            patch(
                "orchestrator.core.decision.engine.plan_decision_questions",
                side_effect=[
                    _planner_result(
                        gate_status="blocked_decision_gate",
                        reason="Need auth decisions",
                        questions=(
                            (
                                "dg_auth",
                                "Confirm Apple Sign In and Supabase auth configuration.",
                            ),
                        ),
                    ),
                    _planner_result(
                        gate_status="blocked_decision_gate",
                        reason="Need auth decisions",
                        questions=(
                            (
                                "dg_auth",
                                "What entitlement/capability values are required for production and staging?",
                            ),
                        ),
                        question_states=(
                            (
                                "dg_auth",
                                "Confirm Apple Sign In and Supabase auth configuration.",
                            ),
                        ),
                        statuses={"dg_auth": "answered"},
                        details={
                            "dg_auth": "Bundle IDs and redirect URI were captured; entitlement values are still missing."
                        },
                    ),
                ],
            ),
        ):
            tenant = session.get(Tenant, "tenant-stateful")
            project = session.get(Project, "project-stateful")
            assert tenant is not None and project is not None

            evaluate_decision_event(
                session=session,
                tenant=tenant,
                project=project,
                event=DecisionEventInput(
                    source="jira_webhook",
                    event_type="issue_updated",
                    idempotency_key="narrow-1",
                    issue_key="MAB-171",
                    issue_summary="Summary",
                    issue_description="Description",
                    issue_labels=[],
                ),
                settings=self.settings,
                tenant_atlassian_oauth_context_fn=lambda **__: None,
                evaluate_pre_run_check_fn=lambda **__: precheck,
            )
            second = evaluate_decision_event(
                session=session,
                tenant=tenant,
                project=project,
                event=DecisionEventInput(
                    source="discord_reply",
                    event_type="reply_added",
                    idempotency_key="narrow-2",
                    issue_key="MAB-171",
                    issue_summary="Summary",
                    issue_description="Description",
                    issue_labels=[],
                ),
                settings=self.settings,
                tenant_atlassian_oauth_context_fn=lambda **__: None,
                evaluate_pre_run_check_fn=lambda **__: precheck,
            )
            cycle = session.get(DecisionCycle, second.cycle_id)

        assert cycle is not None
        self.assertEqual(
            tuple(second.decision.pre_check.decision_gate.questions),
            (
                "What entitlement/capability values are required for production and staging?",
            ),
        )
        self.assertEqual(
            cycle.question_set_json,
            [
                {
                    "id": "dg_auth",
                    "kind": "decision_gate",
                    "text": "What entitlement/capability values are required for production and staging?",
                    "status": "answered",
                    "detail": "Bundle IDs and redirect URI were captured; entitlement values are still missing.",
                    "unresolved": True,
                }
            ],
        )

    def test_decision_owner_question_is_suppressed_for_single_member_account(
        self,
    ) -> None:
        precheck = _precheck_result(
            outcome="decision_gate_required",
            decision_gate_triggered=True,
            decision_gate_reason="Need business owner and rollout decision",
            decision_gate_questions=(
                "Who owns this decision?",
                "What is the rollout policy?",
            ),
            decision_gate_missing_sections=("decision_owner", "rollout_constraints"),
        )

        with (
            self.session_factory() as session,
            patch(
                "orchestrator.core.decision.engine.plan_decision_questions",
                return_value=_planner_result(
                    gate_status="blocked_decision_gate",
                    reason="Need business owner and rollout decision",
                    questions=(
                        (
                            "decision_owner",
                            "Who is the accountable decision owner approving MAB-248 (name and role)?",
                        ),
                        ("rollout_constraints", "What rollout constraints apply?"),
                    ),
                    question_states=(
                        (
                            "decision_owner",
                            "Who is the accountable decision owner approving MAB-248 (name and role)?",
                        ),
                        ("rollout_constraints", "What rollout constraints apply?"),
                    ),
                ),
            ),
        ):
            tenant = session.get(Tenant, "tenant-stateful")
            project = session.get(Project, "project-stateful")
            assert tenant is not None and project is not None
            _add_active_tenant_member(
                session=session,
                tenant_id=tenant.tenant_id,
                user_id="single-member",
                email="single@example.com",
            )
            result = evaluate_decision_event(
                session=session,
                tenant=tenant,
                project=project,
                event=DecisionEventInput(
                    source="discord_run",
                    event_type="discord_discord_run",
                    idempotency_key="single-owner-policy-1",
                    issue_key="MAB-248",
                    issue_summary="Summary",
                    issue_description="Description",
                    issue_labels=[],
                ),
                settings=self.settings,
                tenant_atlassian_oauth_context_fn=lambda **__: None,
                evaluate_pre_run_check_fn=lambda **__: precheck,
            )
            cycle = session.get(DecisionCycle, result.cycle_id)

        assert cycle is not None
        self.assertEqual(
            tuple(result.decision.pre_check.decision_gate.questions),
            ("What rollout constraints apply?",),
        )
        self.assertEqual(cycle.unresolved_question_ids_json, ["rollout_constraints"])
        owner_state = next(
            item for item in cycle.question_set_json if item["id"] == "decision_owner"
        )
        self.assertEqual(owner_state["status"], "accepted")
        self.assertFalse(owner_state["unresolved"])

    def test_decision_owner_question_is_required_for_multi_member_account(self) -> None:
        precheck = _precheck_result(
            outcome="decision_gate_required",
            decision_gate_triggered=True,
            decision_gate_reason="Need business owner",
            decision_gate_questions=("Who owns this decision?",),
            decision_gate_missing_sections=("decision_owner",),
        )

        with (
            self.session_factory() as session,
            patch(
                "orchestrator.core.decision.engine.plan_decision_questions",
                return_value=_planner_result(
                    gate_status="blocked_decision_gate",
                    reason="Need business owner",
                    questions=(
                        (
                            "decision_owner",
                            "Who is the accountable decision owner approving MAB-248 (name and role)?",
                        ),
                    ),
                    question_states=(
                        (
                            "decision_owner",
                            "Who is the accountable decision owner approving MAB-248 (name and role)?",
                        ),
                    ),
                ),
            ),
        ):
            tenant = session.get(Tenant, "tenant-stateful")
            project = session.get(Project, "project-stateful")
            assert tenant is not None and project is not None
            _add_active_tenant_member(
                session=session,
                tenant_id=tenant.tenant_id,
                user_id="first-member",
                email="first@example.com",
            )
            _add_active_tenant_member(
                session=session,
                tenant_id=tenant.tenant_id,
                user_id="second-member",
                email="second@example.com",
            )
            result = evaluate_decision_event(
                session=session,
                tenant=tenant,
                project=project,
                event=DecisionEventInput(
                    source="discord_run",
                    event_type="discord_discord_run",
                    idempotency_key="multi-owner-policy-1",
                    issue_key="MAB-248",
                    issue_summary="Summary",
                    issue_description="Description",
                    issue_labels=[],
                ),
                settings=self.settings,
                tenant_atlassian_oauth_context_fn=lambda **__: None,
                evaluate_pre_run_check_fn=lambda **__: precheck,
            )
            cycle = session.get(DecisionCycle, result.cycle_id)

        assert cycle is not None
        self.assertEqual(
            tuple(result.decision.pre_check.decision_gate.questions),
            (
                "Who is the accountable decision owner approving MAB-248 (name and role)?",
            ),
        )
        self.assertEqual(cycle.unresolved_question_ids_json, ["decision_owner"])

    def test_cycle_transitions_from_open_to_answered_unresolved_to_accepted_clear(
        self,
    ) -> None:
        precheck = _precheck_result(
            outcome="decision_gate_required",
            decision_gate_triggered=True,
            decision_gate_reason="Need subscription decisions",
            decision_gate_questions=(
                "What is in scope?",
                "What rollout constraints apply?",
            ),
            decision_gate_missing_sections=("scope", "rollout_constraints"),
        )
        precheck_calls = 0

        def _evaluate_pre_run_check_stub(**_: object) -> PreRunCheckResult:
            nonlocal precheck_calls
            precheck_calls += 1
            return precheck

        with (
            self.session_factory() as session,
            patch(
                "orchestrator.core.decision.engine.plan_decision_questions",
                side_effect=[
                    _planner_result(
                        gate_status="blocked_decision_gate",
                        reason="Need subscription decisions",
                        questions=(
                            ("scope", "What is in scope?"),
                            ("rollout_constraints", "What rollout constraints apply?"),
                        ),
                    ),
                    _planner_result(
                        gate_status="blocked_decision_gate",
                        reason="Need remaining rollout decision",
                        questions=(
                            (
                                "scope",
                                "Which HubSpot information should drive subscription access?",
                            ),
                            ("rollout_constraints", "What rollout constraints apply?"),
                        ),
                        question_states=(
                            ("scope", "What is in scope?"),
                            ("rollout_constraints", "What rollout constraints apply?"),
                        ),
                        statuses={"scope": "answered", "rollout_constraints": "open"},
                        details={
                            "scope": "HubSpot invoices are in scope; subscription access rules are still missing."
                        },
                    ),
                    _planner_result(
                        gate_status="clear",
                        reason="All decisions accepted",
                        questions=(
                            (
                                "scope",
                                "Which HubSpot information should drive subscription access?",
                            ),
                            ("rollout_constraints", "What rollout constraints apply?"),
                        ),
                        question_states=(
                            ("scope", "What is in scope?"),
                            ("rollout_constraints", "What rollout constraints apply?"),
                        ),
                        statuses={
                            "scope": "accepted",
                            "rollout_constraints": "accepted",
                        },
                        details={
                            "scope": "Required HubSpot subscription rules accepted.",
                            "rollout_constraints": "No rollout constraints.",
                        },
                    ),
                ],
            ),
        ):
            tenant = session.get(Tenant, "tenant-stateful")
            project = session.get(Project, "project-stateful")
            assert tenant is not None and project is not None

            first = evaluate_decision_event(
                session=session,
                tenant=tenant,
                project=project,
                event=DecisionEventInput(
                    source="discord_run",
                    event_type="discord_discord_run",
                    idempotency_key="full-cycle-1",
                    issue_key="MAB-249",
                    issue_summary="Summary",
                    issue_description="Description",
                    issue_labels=[],
                ),
                settings=self.settings,
                tenant_atlassian_oauth_context_fn=lambda **__: None,
                evaluate_pre_run_check_fn=_evaluate_pre_run_check_stub,
            )
            cycle = session.get(DecisionCycle, str(first.cycle_id))
            assert cycle is not None

            from orchestrator.storage.models import DecisionAnswer

            session.add(
                DecisionAnswer(
                    answer_id="ans-full-cycle-scope-partial",
                    case_id=first.case_id,
                    cycle_id=str(first.cycle_id),
                    tenant_id=tenant.tenant_id,
                    project_id=project.project_id,
                    issue_key="MAB-249",
                    question_id="scope",
                    question_kind="decision_gate",
                    question_text="What is in scope?",
                    status="answered",
                    normalized_answer="HubSpot invoices are in scope.",
                    source_transport="discord",
                    source_ref=None,
                    evidence_ids_json=[],
                    metadata_json={},
                    answered_at=datetime.now(timezone.utc),
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
                    source="discord_reply",
                    event_type="reply_added",
                    idempotency_key="full-cycle-2",
                    issue_key="MAB-249",
                    issue_summary="Summary",
                    issue_description="Description",
                    issue_labels=[],
                ),
                settings=self.settings,
                tenant_atlassian_oauth_context_fn=lambda **__: None,
                evaluate_pre_run_check_fn=_evaluate_pre_run_check_stub,
            )
            cycle_after_partial = session.get(DecisionCycle, str(first.cycle_id))
            assert cycle_after_partial is not None
            partial_unresolved_question_ids = list(
                cycle_after_partial.unresolved_question_ids_json
            )
            partial_question_set = list(cycle_after_partial.question_set_json)

            scope_answer = (
                session.query(DecisionAnswer)
                .filter_by(cycle_id=str(first.cycle_id), question_id="scope")
                .one()
            )
            scope_answer.status = "accepted"
            scope_answer.normalized_answer = (
                "Required HubSpot subscription rules accepted."
            )
            scope_answer.accepted_at = datetime.now(timezone.utc)
            scope_answer.updated_at = datetime.now(timezone.utc)
            rollout_answer = (
                session.query(DecisionAnswer)
                .filter_by(
                    cycle_id=str(first.cycle_id), question_id="rollout_constraints"
                )
                .one()
            )
            rollout_answer.status = "accepted"
            rollout_answer.normalized_answer = "No rollout constraints."
            rollout_answer.accepted_at = datetime.now(timezone.utc)
            rollout_answer.updated_at = datetime.now(timezone.utc)
            session.commit()

            third = evaluate_decision_event(
                session=session,
                tenant=tenant,
                project=project,
                event=DecisionEventInput(
                    source="discord_reply",
                    event_type="reply_added",
                    idempotency_key="full-cycle-3",
                    issue_key="MAB-249",
                    issue_summary="Summary",
                    issue_description="Description",
                    issue_labels=[],
                ),
                settings=self.settings,
                tenant_atlassian_oauth_context_fn=lambda **__: None,
                evaluate_pre_run_check_fn=_evaluate_pre_run_check_stub,
            )
            case = session.query(DecisionCase).filter_by(issue_key="MAB-249").one()
            closed_cycle = session.get(DecisionCycle, str(first.cycle_id))

        self.assertEqual(first.classification, "decision_gate")
        self.assertEqual(second.classification, "decision_gate")
        self.assertEqual(
            partial_unresolved_question_ids, ["scope", "rollout_constraints"]
        )
        by_id = {item["id"]: item for item in partial_question_set}
        self.assertEqual(by_id["scope"]["status"], "answered")
        self.assertTrue(by_id["scope"]["unresolved"])
        self.assertEqual(third.classification, "clear")
        self.assertEqual(third.case_state, "ready_for_execution")
        self.assertEqual(closed_cycle.status, "resolved")
        self.assertIsNone(case.active_cycle_id)
        self.assertEqual(precheck_calls, 1)

    def test_idempotency_key_deduplicates_event_rows(self) -> None:
        precheck = _precheck_result(outcome="ready_for_agent")

        with (
            self.session_factory() as session,
            patch(
                "orchestrator.core.decision.engine.plan_decision_questions",
                return_value=_planner_result(
                    gate_status="blocked_decision_gate",
                    reason="Need config",
                    questions=(("dg_config", "What config is approved?"),),
                ),
            ),
        ):
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
                tenant_atlassian_oauth_context_fn=lambda **__: None,
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
                tenant_atlassian_oauth_context_fn=lambda **__: None,
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

        with (
            self.session_factory() as session,
            patch(
                "orchestrator.core.decision.engine.plan_decision_questions",
                return_value=_planner_result(
                    gate_status="blocked_decision_gate",
                    reason="Need owner decision",
                    questions=(("dg_owner", "Who owns this decision?"),),
                ),
            ),
        ):
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
                tenant_atlassian_oauth_context_fn=lambda **__: None,
                evaluate_pre_run_check_fn=_evaluate_pre_run_check_stub,
            )
            case_before = (
                session.query(DecisionCase).filter_by(issue_key="MAB-164").one()
            )
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
                tenant_atlassian_oauth_context_fn=lambda **__: None,
                evaluate_pre_run_check_fn=_evaluate_pre_run_check_stub,
            )
            case_after = (
                session.query(DecisionCase).filter_by(issue_key="MAB-164").one()
            )

        self.assertEqual(first.case_state, "blocked_decision_gate")
        self.assertTrue(second.duplicate_event)
        self.assertEqual(second.case_state, "blocked_decision_gate")
        self.assertEqual(
            tuple(second.decision.pre_check.decision_gate.questions),
            ("Who owns this decision?",),
        )
        self.assertEqual(case_after.updated_at, updated_at_before)

    def test_open_cycle_with_answered_reply_does_not_rerun_precheck(self) -> None:
        prechecks = [
            _precheck_result(
                outcome="decision_gate_required",
                decision_gate_triggered=True,
                decision_gate_reason="Need config",
                decision_gate_questions=("What config is approved?",),
                decision_gate_missing_sections=("config",),
            )
        ]
        precheck_calls = 0
        recorded_answers_seen: list[list[dict[str, str]] | None] = []

        def _evaluate_pre_run_check_stub(**kwargs: object) -> PreRunCheckResult:
            nonlocal precheck_calls
            precheck_calls += 1
            recorded_answers_seen.append(kwargs.get("recorded_answers"))  # type: ignore[arg-type]
            return prechecks.pop(0)

        with (
            self.session_factory() as session,
            patch(
                "orchestrator.core.decision.engine.plan_decision_questions",
                side_effect=[
                    _planner_result(
                        gate_status="blocked_decision_gate",
                        reason="Need config",
                        questions=(("dg_config", "What config is approved?"),),
                    ),
                    _planner_result(
                        gate_status="blocked_decision_gate",
                        reason="Need config",
                        questions=(("dg_config", "What config is approved?"),),
                        statuses={"dg_config": "answered"},
                        details={
                            "dg_config": "Production bundle ID is com.example.app."
                        },
                    ),
                ],
            ),
        ):
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
                tenant_atlassian_oauth_context_fn=lambda **__: None,
                evaluate_pre_run_check_fn=_evaluate_pre_run_check_stub,
            )
            self.assertEqual(first.classification, "decision_gate")
            cycle = session.get(DecisionCycle, str(first.cycle_id))
            assert cycle is not None
            question_id = str(cycle.question_set_json[0]["id"])

            from orchestrator.storage.models import DecisionAnswer

            session.add(
                DecisionAnswer(
                    answer_id="ans-1",
                    case_id=first.case_id,
                    cycle_id=str(first.cycle_id),
                    tenant_id=tenant.tenant_id,
                    project_id=project.project_id,
                    issue_key="MAB-166",
                    question_id=question_id,
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
                tenant_atlassian_oauth_context_fn=lambda **__: None,
                evaluate_pre_run_check_fn=_evaluate_pre_run_check_stub,
            )

        self.assertEqual(second.classification, "decision_gate")
        self.assertEqual(
            tuple(second.decision.pre_check.decision_gate.questions),
            ("What config is approved?",),
        )
        self.assertEqual(precheck_calls, 1)
        self.assertEqual(recorded_answers_seen, [[]])

    def test_resolved_cycle_persists_clear_state_without_rerunning_precheck(
        self,
    ) -> None:
        prechecks = [
            _precheck_result(
                outcome="decision_gate_required",
                decision_gate_triggered=True,
                decision_gate_reason="Need config",
                decision_gate_questions=("What config is approved?",),
                decision_gate_missing_sections=("config",),
            ),
        ]
        precheck_calls = 0
        recorded_answers_seen: list[list[dict[str, str]] | None] = []

        def _evaluate_pre_run_check_stub(**kwargs: object) -> PreRunCheckResult:
            nonlocal precheck_calls
            precheck_calls += 1
            recorded_answers_seen.append(kwargs.get("recorded_answers"))  # type: ignore[arg-type]
            return prechecks.pop(0)

        with (
            self.session_factory() as session,
            patch(
                "orchestrator.core.decision.engine.plan_decision_questions",
                side_effect=[
                    _planner_result(
                        gate_status="blocked_decision_gate",
                        reason="Need config",
                        questions=(("dg_config", "What config is approved?"),),
                    ),
                    _planner_result(
                        gate_status="clear",
                        reason="Clarification complete",
                        questions=(("dg_config", "What config is approved?"),),
                        statuses={"dg_config": "accepted"},
                        details={
                            "dg_config": "Production bundle ID is com.example.app."
                        },
                    ),
                ],
            ),
        ):
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
                    idempotency_key="answer-context-clear-1",
                    issue_key="MAB-167",
                    issue_summary="Summary",
                    issue_description="Description",
                    issue_labels=[],
                ),
                settings=self.settings,
                tenant_atlassian_oauth_context_fn=lambda **__: None,
                evaluate_pre_run_check_fn=_evaluate_pre_run_check_stub,
            )
            self.assertEqual(first.classification, "decision_gate")
            cycle = session.get(DecisionCycle, str(first.cycle_id))
            assert cycle is not None
            question_id = str(cycle.question_set_json[0]["id"])

            from orchestrator.storage.models import DecisionAnswer

            session.add(
                DecisionAnswer(
                    answer_id="ans-2",
                    case_id=first.case_id,
                    cycle_id=str(first.cycle_id),
                    tenant_id=tenant.tenant_id,
                    project_id=project.project_id,
                    issue_key="MAB-167",
                    question_id=question_id,
                    question_kind="decision_gate",
                    question_text="What config is approved?",
                    status="accepted",
                    normalized_answer="Production bundle ID is com.example.app.",
                    source_transport="discord",
                    source_ref=None,
                    evidence_ids_json=[],
                    metadata_json={},
                    accepted_at=datetime.now(timezone.utc),
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
                    idempotency_key="answer-context-clear-2",
                    issue_key="MAB-167",
                    issue_summary="Summary",
                    issue_description="Description",
                    issue_labels=[],
                ),
                settings=self.settings,
                tenant_atlassian_oauth_context_fn=lambda **__: None,
                evaluate_pre_run_check_fn=_evaluate_pre_run_check_stub,
            )
            case = session.query(DecisionCase).filter_by(issue_key="MAB-167").one()

        self.assertEqual(second.classification, "clear")
        self.assertEqual(second.case_state, "ready_for_execution")
        self.assertEqual(precheck_calls, 1)
        self.assertEqual(recorded_answers_seen, [[]])
        self.assertIsNone(case.active_cycle_id)
        self.assertIsNone(case.blocked_reason)

    def test_clear_case_with_same_fingerprint_reuses_persisted_clear_snapshot(
        self,
    ) -> None:
        prechecks = [
            _precheck_result(
                outcome="decision_gate_required",
                decision_gate_triggered=True,
                decision_gate_reason="Need config",
                decision_gate_questions=("What config is approved?",),
                decision_gate_missing_sections=("config",),
            ),
        ]
        precheck_calls = 0

        def _evaluate_pre_run_check_stub(**_: object) -> PreRunCheckResult:
            nonlocal precheck_calls
            precheck_calls += 1
            return prechecks.pop(0)

        with (
            self.session_factory() as session,
            patch(
                "orchestrator.core.decision.engine.plan_decision_questions",
                side_effect=[
                    _planner_result(
                        gate_status="blocked_decision_gate",
                        reason="Need config",
                        questions=(("dg_config", "What config is approved?"),),
                    ),
                    _planner_result(
                        gate_status="clear",
                        reason="Clarification complete",
                        questions=(("dg_config", "What config is approved?"),),
                        statuses={"dg_config": "accepted"},
                        details={
                            "dg_config": "Production bundle ID is com.example.app."
                        },
                    ),
                ],
            ),
        ):
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
                    idempotency_key="clear-snapshot-1",
                    issue_key="MAB-168",
                    issue_summary="Summary",
                    issue_description="Description",
                    issue_labels=[],
                ),
                settings=self.settings,
                tenant_atlassian_oauth_context_fn=lambda **__: None,
                evaluate_pre_run_check_fn=_evaluate_pre_run_check_stub,
            )
            cycle = session.get(DecisionCycle, str(first.cycle_id))
            assert cycle is not None
            question_id = str(cycle.question_set_json[0]["id"])

            from orchestrator.storage.models import DecisionAnswer

            session.add(
                DecisionAnswer(
                    answer_id="ans-3",
                    case_id=first.case_id,
                    cycle_id=str(first.cycle_id),
                    tenant_id=tenant.tenant_id,
                    project_id=project.project_id,
                    issue_key="MAB-168",
                    question_id=question_id,
                    question_kind="decision_gate",
                    question_text="What config is approved?",
                    status="accepted",
                    normalized_answer="Production bundle ID is com.example.app.",
                    source_transport="discord",
                    source_ref=None,
                    evidence_ids_json=[],
                    metadata_json={},
                    accepted_at=datetime.now(timezone.utc),
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
            )
            session.commit()

            cleared = evaluate_decision_event(
                session=session,
                tenant=tenant,
                project=project,
                event=DecisionEventInput(
                    source="discord_reply",
                    event_type="reply_added",
                    idempotency_key="clear-snapshot-2",
                    issue_key="MAB-168",
                    issue_summary="Summary",
                    issue_description="Description",
                    issue_labels=[],
                ),
                settings=self.settings,
                tenant_atlassian_oauth_context_fn=lambda **__: None,
                evaluate_pre_run_check_fn=_evaluate_pre_run_check_stub,
            )

            reused = evaluate_decision_event(
                session=session,
                tenant=tenant,
                project=project,
                event=DecisionEventInput(
                    source="discord_run",
                    event_type="discord_discord_run",
                    idempotency_key="clear-snapshot-3",
                    issue_key="MAB-168",
                    issue_summary="Summary",
                    issue_description="Description",
                    issue_labels=[],
                ),
                settings=self.settings,
                tenant_atlassian_oauth_context_fn=lambda **__: None,
                evaluate_pre_run_check_fn=_evaluate_pre_run_check_stub,
            )
            case = session.query(DecisionCase).filter_by(issue_key="MAB-168").one()

        self.assertEqual(cleared.classification, "clear")
        self.assertEqual(reused.classification, "clear")
        self.assertEqual(reused.case_state, "ready_for_execution")
        self.assertEqual(precheck_calls, 1)
        self.assertIsNone(case.active_cycle_id)
        self.assertIsNone(case.blocked_reason)

    def test_closed_cycle_does_not_reopen_when_issue_summary_description_and_labels_change(
        self,
    ) -> None:
        precheck_calls = 0

        def _evaluate_pre_run_check_stub(**kwargs: object) -> PreRunCheckResult:
            nonlocal precheck_calls
            precheck_calls += 1
            recorded_answers = kwargs.get("recorded_answers")
            has_relink_policy = any(
                isinstance(item, dict)
                and str(item.get("question_id") or "").strip() == "dg_relink"
                and str(item.get("status") or "").strip() == "accepted"
                and "Reject relink" in str(item.get("answer") or "")
                for item in (recorded_answers or [])
            )
            if has_relink_policy:
                return _precheck_result(
                    outcome="ready_for_agent",
                    decision_gate_triggered=False,
                    decision_gate_reason="Decision Gate not required",
                    decision_gate_questions=(),
                    decision_gate_missing_sections=(),
                )
            return _precheck_result(
                outcome="decision_gate_required",
                decision_gate_triggered=True,
                decision_gate_reason="Need relink policy",
                decision_gate_questions=("What is the cross-account relink policy?",),
                decision_gate_missing_sections=("policy",),
            )

        with (
            self.session_factory() as session,
            patch(
                "orchestrator.core.decision.engine.plan_decision_questions",
                side_effect=[
                    _planner_result(
                        gate_status="blocked_decision_gate",
                        reason="Need relink policy",
                        questions=(
                            ("dg_relink", "What is the cross-account relink policy?"),
                        ),
                    ),
                    _planner_result(
                        gate_status="clear",
                        reason="Clarification complete",
                        questions=(
                            ("dg_relink", "What is the cross-account relink policy?"),
                        ),
                        statuses={"dg_relink": "accepted"},
                        details={
                            "dg_relink": "Reject relink; device_id stays bound to one user only."
                        },
                    ),
                ],
            ),
        ):
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
                    idempotency_key="answer-context-fingerprint-1",
                    issue_key="MAB-170",
                    issue_summary="Summary",
                    issue_description="Description",
                    issue_labels=[],
                ),
                settings=self.settings,
                tenant_atlassian_oauth_context_fn=lambda **__: None,
                evaluate_pre_run_check_fn=_evaluate_pre_run_check_stub,
            )
            self.assertEqual(first.classification, "decision_gate")
            cycle = session.get(DecisionCycle, str(first.cycle_id))
            assert cycle is not None

            from orchestrator.storage.models import DecisionAnswer

            session.add(
                DecisionAnswer(
                    answer_id="ans-170",
                    case_id=first.case_id,
                    cycle_id=str(first.cycle_id),
                    tenant_id=tenant.tenant_id,
                    project_id=project.project_id,
                    issue_key="MAB-170",
                    question_id=str(cycle.question_set_json[0]["id"]),
                    question_kind="decision_gate",
                    question_text="What is the cross-account relink policy?",
                    status="accepted",
                    normalized_answer="Reject relink; device_id stays bound to one user only.",
                    source_transport="discord",
                    source_ref=None,
                    evidence_ids_json=[],
                    metadata_json={},
                    accepted_at=datetime.now(timezone.utc),
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
            )
            session.commit()

            cleared = evaluate_decision_event(
                session=session,
                tenant=tenant,
                project=project,
                event=DecisionEventInput(
                    source="discord_reply",
                    event_type="reply_added",
                    idempotency_key="answer-context-fingerprint-2",
                    issue_key="MAB-170",
                    issue_summary="Summary",
                    issue_description="Description",
                    issue_labels=[],
                ),
                settings=self.settings,
                tenant_atlassian_oauth_context_fn=lambda **__: None,
                evaluate_pre_run_check_fn=_evaluate_pre_run_check_stub,
            )

            reused = evaluate_decision_event(
                session=session,
                tenant=tenant,
                project=project,
                event=DecisionEventInput(
                    source="discord_run",
                    event_type="discord_discord_run",
                    idempotency_key="answer-context-fingerprint-3",
                    issue_key="MAB-170",
                    issue_summary="Summary changed after clarification closed",
                    issue_description="Description changed after clarification closed.\n\nRecorded answer block changed.",
                    issue_labels=["agent:ready", "ios", "payments"],
                ),
                settings=self.settings,
                tenant_atlassian_oauth_context_fn=lambda **__: None,
                evaluate_pre_run_check_fn=_evaluate_pre_run_check_stub,
            )
            case = session.query(DecisionCase).filter_by(issue_key="MAB-170").one()
            cycle_count = (
                session.query(DecisionCycle).filter_by(issue_key="MAB-170").count()
            )

        self.assertEqual(cleared.classification, "clear")
        self.assertEqual(reused.classification, "clear")
        self.assertEqual(reused.case_state, "ready_for_execution")
        self.assertFalse(reused.decision.pre_check.decision_gate.triggered)
        self.assertEqual(precheck_calls, 1)
        self.assertIsNone(case.active_cycle_id)
        self.assertEqual(cycle_count, 1)

    def test_closed_cycle_applies_missing_ready_label_without_reopening_gate(
        self,
    ) -> None:
        prechecks = [
            _precheck_result(
                outcome="decision_gate_required",
                decision_gate_triggered=True,
                decision_gate_reason="Need relink policy",
                decision_gate_questions=("What is the cross-account relink policy?",),
                decision_gate_missing_sections=("policy",),
            ),
        ]
        precheck_calls = 0

        def _evaluate_pre_run_check_stub(**_: object) -> PreRunCheckResult:
            nonlocal precheck_calls
            precheck_calls += 1
            return prechecks.pop(0)

        oauth_client = SimpleNamespace(add_issue_labels=MagicMock())
        oauth_context = {
            "connection": SimpleNamespace(cloud_id="cloud-1"),
            "access_token": "token-1",
            "client": oauth_client,
        }
        with (
            self.session_factory() as session,
            patch(
                "orchestrator.core.decision.engine.plan_decision_questions",
                side_effect=[
                    _planner_result(
                        gate_status="blocked_decision_gate",
                        reason="Need relink policy",
                        questions=(
                            ("dg_relink", "What is the cross-account relink policy?"),
                        ),
                    ),
                    _planner_result(
                        gate_status="clear",
                        reason="Clarification complete",
                        questions=(
                            ("dg_relink", "What is the cross-account relink policy?"),
                        ),
                        statuses={"dg_relink": "accepted"},
                        details={
                            "dg_relink": "Reject relink; device_id stays bound to one user only."
                        },
                    ),
                ],
            ),
        ):
            tenant = session.get(Tenant, "tenant-stateful")
            project = session.get(Project, "project-stateful")
            assert tenant is not None and project is not None
            tenant.jira_config = {"ready_label": "agent:ready"}
            tenant.policy_config = {
                **tenant.policy_config,
                "allow_label_mutations": True,
            }

            first = evaluate_decision_event(
                session=session,
                tenant=tenant,
                project=project,
                event=DecisionEventInput(
                    source="jira_webhook",
                    event_type="issue_updated",
                    idempotency_key="missing-ready-after-close-1",
                    issue_key="MAB-171",
                    issue_summary="Summary",
                    issue_description="Description",
                    issue_labels=["agent:ready", "worker:linux"],
                ),
                settings=self.settings,
                tenant_atlassian_oauth_context_fn=lambda **__: oauth_context,
                evaluate_pre_run_check_fn=_evaluate_pre_run_check_stub,
            )
            self.assertEqual(first.classification, "decision_gate")
            cycle = session.get(DecisionCycle, str(first.cycle_id))
            assert cycle is not None

            from orchestrator.storage.models import DecisionAnswer

            session.add(
                DecisionAnswer(
                    answer_id="ans-171",
                    case_id=first.case_id,
                    cycle_id=str(first.cycle_id),
                    tenant_id=tenant.tenant_id,
                    project_id=project.project_id,
                    issue_key="MAB-171",
                    question_id=str(cycle.question_set_json[0]["id"]),
                    question_kind="decision_gate",
                    question_text="What is the cross-account relink policy?",
                    status="accepted",
                    normalized_answer="Reject relink; device_id stays bound to one user only.",
                    source_transport="discord",
                    source_ref=None,
                    evidence_ids_json=[],
                    metadata_json={},
                    accepted_at=datetime.now(timezone.utc),
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
            )
            session.commit()

            cleared = evaluate_decision_event(
                session=session,
                tenant=tenant,
                project=project,
                event=DecisionEventInput(
                    source="discord_reply",
                    event_type="reply_added",
                    idempotency_key="missing-ready-after-close-2",
                    issue_key="MAB-171",
                    issue_summary="Summary",
                    issue_description="Description",
                    issue_labels=["worker:linux"],
                ),
                settings=self.settings,
                tenant_atlassian_oauth_context_fn=lambda **__: oauth_context,
                evaluate_pre_run_check_fn=_evaluate_pre_run_check_stub,
            )
            rerun = evaluate_decision_event(
                session=session,
                tenant=tenant,
                project=project,
                event=DecisionEventInput(
                    source="discord_run",
                    event_type="discord_discord_run",
                    idempotency_key="missing-ready-after-close-3",
                    issue_key="MAB-171",
                    issue_summary="Summary changed",
                    issue_description="Description changed",
                    issue_labels=cleared.issue_labels,
                ),
                settings=self.settings,
                tenant_atlassian_oauth_context_fn=lambda **__: oauth_context,
                evaluate_pre_run_check_fn=_evaluate_pre_run_check_stub,
            )
            case = session.query(DecisionCase).filter_by(issue_key="MAB-171").one()
            cycle_count = (
                session.query(DecisionCycle).filter_by(issue_key="MAB-171").count()
            )

        self.assertEqual(cleared.classification, "clear")
        self.assertIn("agent:ready", cleared.issue_labels)
        self.assertIsNone(cleared.decision.block_reason)
        self.assertTrue(cleared.decision.pre_check.ready_label_present)
        self.assertEqual(rerun.classification, "clear")
        self.assertIsNone(rerun.decision.block_reason)
        self.assertFalse(rerun.decision.pre_check.decision_gate.triggered)
        self.assertEqual(precheck_calls, 1)
        self.assertIsNone(case.active_cycle_id)
        self.assertEqual(cycle_count, 1)
        oauth_client.add_issue_labels.assert_called_once_with(
            access_token="token-1",
            cloud_id="cloud-1",
            issue_id_or_key="MAB-171",
            labels=["agent:ready"],
        )

    def test_existing_case_load_does_not_mutate_stale_clear_snapshot(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-stateful")
            project = session.get(Project, "project-stateful")
            assert tenant is not None and project is not None

            case = DecisionCase(
                case_id="case-stale-clear",
                tenant_id=tenant.tenant_id,
                project_id=project.project_id,
                issue_key="MAB-169",
                state="clear",
                blocked_reason="policy_eval_failed",
                classification="clear",
                issue_fingerprint="fp-169",
                active_cycle_id=None,
                last_source="worker_execution",
                last_event_type="run_started",
                last_event_at=datetime.now(timezone.utc),
                required_worker_capability="linux",
                required_worker_label="worker:linux",
                ready_label="agent:ready",
                ready_label_present=True,
                metadata_json={
                    "result_snapshot": {
                        "classification": "clear",
                        "issue_labels": [],
                        "missing_slots": [],
                        "auto_resolved_slots": [],
                        "block_reason": "policy_eval_failed",
                        "guidance": "Pre-run policy evaluation failed.",
                        "policy_error": "Codex precheck policy evaluation failed",
                        "pre_check": {
                            "outcome": "decision_gate_required",
                            "ready_label": "agent:ready",
                            "ready_label_present": True,
                            "required_worker_capability": "linux",
                            "required_worker_label": "worker:linux",
                            "required_worker_label_present": True,
                            "decision_gate": {
                                "triggered": True,
                                "reason": "Need config",
                                "missing_sections": [],
                                "questions": ["What config is approved?"],
                                "recommendation": "Clarification required",
                                "tags": [],
                            },
                            "gtd": {
                                "valid": True,
                                "missing_criteria": [],
                                "clarification_questions": [],
                            },
                        },
                    }
                },
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            session.add(case)
            session.commit()

            loaded = existing_case_for_issue(
                session=session,
                tenant_id=tenant.tenant_id,
                issue_key="MAB-169",
            )
            session.commit()
            session.refresh(case)

        assert loaded is not None
        self.assertEqual(case.blocked_reason, "policy_eval_failed")
        self.assertEqual(case.classification, "clear")
        snapshot = dict(case.metadata_json.get("result_snapshot") or {})
        self.assertEqual(snapshot.get("classification"), "clear")
        self.assertEqual(snapshot.get("block_reason"), "policy_eval_failed")
        self.assertEqual(
            snapshot.get("policy_error"), "Codex precheck policy evaluation failed"
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
                case = (
                    verify_session.query(DecisionCase)
                    .filter_by(issue_key="MAB-165")
                    .one()
                )
                effect = (
                    verify_session.query(DecisionEffectOutbox)
                    .filter_by(issue_key="MAB-165")
                    .one()
                )
                observed_state["case_state"] = case.state
                observed_state["effect_status"] = effect.status
            observed_state["comment"] = comment
            return True, None

        with (
            self.session_factory() as session,
            patch(
                "orchestrator.core.decision.engine.plan_decision_questions",
                return_value=_planner_result(
                    gate_status="blocked_decision_gate",
                    reason="Need owner decision",
                    questions=(("dg_owner", "Who owns this decision?"),),
                ),
            ),
        ):
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
                tenant_atlassian_oauth_context_fn=lambda **__: None,
                publish_jira_comment_fn=_publish,
                evaluate_pre_run_check_fn=lambda **__: precheck,
            )

            effect = (
                session.query(DecisionEffectOutbox).filter_by(issue_key="MAB-165").one()
            )

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

        from orchestrator.core.knowledge.base import SlotResolution

        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-stateful")
            project = session.get(Project, "project-stateful")
            assert tenant is not None and project is not None
            with patch(
                "orchestrator.core.decision.engine.resolve_missing_slots_from_knowledge",
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
                    tenant_atlassian_oauth_context_fn=lambda **__: None,
                    evaluate_pre_run_check_fn=_stub_precheck,
                )

        self.assertEqual(result.decision.block_reason, None)
        self.assertIn("objective", result.auto_resolved_slots)

    def test_serializes_auto_resolved_slot_timestamps_for_decision_metadata(
        self,
    ) -> None:
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

        from orchestrator.core.knowledge.base import SlotResolution

        source_time = datetime(2026, 3, 23, 13, 30, tzinfo=timezone.utc)

        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-stateful")
            project = session.get(Project, "project-stateful")
            assert tenant is not None and project is not None
            with patch(
                "orchestrator.core.decision.engine.resolve_missing_slots_from_knowledge",
                return_value={
                    "objective": SlotResolution(
                        slot_name="objective",
                        slot_value="Implement centralized decision engine.",
                        source_timestamp=source_time,
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
                        idempotency_key="resolve-datetime-1",
                        issue_key="MAB-164",
                        issue_summary="Summary",
                        issue_description="Description",
                        issue_labels=[],
                    ),
                    settings=self.settings,
                    tenant_atlassian_oauth_context_fn=lambda **__: None,
                    evaluate_pre_run_check_fn=_stub_precheck,
                )

            case = existing_case_for_issue(
                session=session, tenant_id=tenant.tenant_id, issue_key="MAB-164"
            )
            assert case is not None

        self.assertEqual(result.decision.block_reason, None)
        stored_answers = dict(case.metadata_json.get("auto_resolved_answers") or {})
        objective = dict(stored_answers.get("objective") or {})
        self.assertEqual(objective.get("source_timestamp"), source_time.isoformat())

    def test_requires_resolved_project_for_decision_evaluation(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-stateful")
            assert tenant is not None

            with self.assertRaisesRegex(ValueError, "requires a resolved project"):
                evaluate_decision_event(
                    session=session,
                    tenant=tenant,
                    project=None,
                    event=DecisionEventInput(
                        source="jira_webhook",
                        event_type="issue_updated",
                        idempotency_key="missing-project-1",
                        issue_key="MAB-999",
                        issue_summary="Summary",
                        issue_description="Description",
                        issue_labels=[],
                    ),
                    settings=self.settings,
                    tenant_atlassian_oauth_context_fn=lambda **__: None,
                    evaluate_pre_run_check_fn=lambda **__: _precheck_result(
                        outcome="ready_for_agent"
                    ),
                )
