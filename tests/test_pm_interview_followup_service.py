from __future__ import annotations

from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import select

from orchestrator.storage.models import PMInterviewCase
from orchestrator.core.pm_interview_followup_service import continue_pm_interview_from_followup
from orchestrator.core.pm_interview_service import (
    PM_INTERVIEW_STATUS_PM_COMPLETED,
    PM_INTERVIEW_STATUS_QUESTION_PENDING,
    upsert_pm_interview_case,
)
from orchestrator.core.runtime_invocation import AgentInvocationContext

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


class PMInterviewFollowupServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url, _ = configure_runtime_environment(
            temp_dir=self.temp_dir,
            database_name="pm_interview_followup_service.db",
        )
        self.session_factory = session_factory_for(self.database_url)
        seed_core_runtime_state(self.session_factory)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        clear_runtime_environment()

    def test_continue_pm_interview_from_followup_is_idempotent_for_same_source_ref(self) -> None:
        with self.session_factory() as session:
            upsert_pm_interview_case(
                session=session,
                tenant_id="route25",
                project_id="route25-default",
                request_id="pm-req-followup-1",
                source_kind="jira_parent",
                channel_id="jira-parent-sync",
                source_text="Identity redesign parent",
                status=PM_INTERVIEW_STATUS_QUESTION_PENDING,
                brief={
                    "objective": "Tenant identity redesign",
                    "user_value": "Admins can manage identity safely",
                    "acceptance_criteria": ["Invitations can be sent"],
                    "scope_in": ["Tenant identity"],
                    "scope_out": ["SSO overhaul"],
                    "constraints": [],
                    "risks": ["Audit export misuse"],
                    "success_outcomes": ["Admins can export audit logs within policy"],
                },
            )
            session.commit()

        pm_payload = {
            "message": "What audit retention window should v1 support?\nExamples:\n- 90 days\n- 12 months\n- 7 years",
            "brief": {
                "objective": "Tenant identity redesign",
                "user_value": "Admins can manage identity safely",
                "acceptance_criteria": ["Invitations can be sent"],
                "scope_in": ["Tenant identity"],
                "scope_out": ["SSO overhaul"],
                "constraints": ["12 month retention window"],
                "risks": ["Audit export misuse"],
                "success_outcomes": ["Admins can export audit logs within policy"],
                "recommendation": "",
                "open_questions": ["What audit retention window should v1 support?"],
                "next_steps": [],
            },
            "status": "question_pending",
            "ready_to_write": False,
            "next_question": {
                "slot_key": "constraints",
                "question": "What audit retention window should v1 support?",
                "examples": ["90 days", "12 months", "7 years"],
            },
        }

        with (
            self.session_factory() as session,
            patch(
                "orchestrator.core.pm_interview_followup_service.plan_pm_interview_with_runtime",
                return_value=pm_payload,
            ) as plan_mock,
        ):
            first = continue_pm_interview_from_followup(
                session=session,
                runtime=SimpleNamespace(),
                tenant_id="route25",
                project_id="route25-default",
                request_id="pm-req-followup-1",
                reply_text="Use a 12 month retention window.",
                source_transport="jira_comment",
                source_ref="comment-1",
                actor_ref="jira-user-1",
                invocation_context=AgentInvocationContext(
                    channel="jira",
                    tenant_id="route25",
                    project_id="route25-default",
                    command="pm",
                    stage="interview",
                    working_dir=".",
                    issue_key="MAB-231",
                ),
                project_keys=["MAB"],
                settings=SimpleNamespace(),
            )
            session.commit()

            second = continue_pm_interview_from_followup(
                session=session,
                runtime=SimpleNamespace(),
                tenant_id="route25",
                project_id="route25-default",
                request_id="pm-req-followup-1",
                reply_text="Use a 12 month retention window.",
                source_transport="jira_comment",
                source_ref="comment-1",
                actor_ref="jira-user-1",
                invocation_context=AgentInvocationContext(
                    channel="jira",
                    tenant_id="route25",
                    project_id="route25-default",
                    command="pm",
                    stage="interview",
                    working_dir=".",
                    issue_key="MAB-231",
                ),
                project_keys=["MAB"],
                settings=SimpleNamespace(),
            )

            self.assertEqual(plan_mock.call_count, 1)
            self.assertEqual(first.assessment.brief.constraints, ("12 month retention window",))
            self.assertEqual(second.assessment.brief.constraints, ("12 month retention window",))
            self.assertEqual(second.clarification_questions, first.clarification_questions)
            self.assertEqual(
                len(list(getattr(second.interview_case, "evidence_json", None) or [])),
                1,
            )
            self.assertEqual(second.interview_case.evidence_json[0]["source_ref"], "comment-1")

    def test_continue_pm_interview_from_followup_completes_and_clears_stale_question(self) -> None:
        with self.session_factory() as session:
            upsert_pm_interview_case(
                session=session,
                tenant_id="route25",
                project_id="route25-default",
                request_id="pm-req-followup-2",
                source_kind="jira_parent",
                channel_id="jira-parent-sync",
                source_text="Identity redesign parent",
                status=PM_INTERVIEW_STATUS_QUESTION_PENDING,
                brief={
                    "objective": "Tenant identity redesign",
                    "user_value": "Admins can manage identity safely",
                    "acceptance_criteria": ["Invitations can be sent"],
                    "scope_in": ["Tenant identity"],
                    "scope_out": ["SSO overhaul"],
                    "constraints": [],
                    "risks": ["Audit export misuse"],
                    "success_outcomes": ["Admins can export audit logs within policy"],
                },
                current_question={
                    "slot_key": "constraints",
                    "question": "What audit retention window should v1 support?",
                    "examples": ["90 days", "12 months", "7 years"],
                },
                next_question={
                    "slot_key": "constraints",
                    "question": "What audit retention window should v1 support?",
                    "examples": ["90 days", "12 months", "7 years"],
                },
            )
            session.commit()

        pm_payload = {
            "message": "The brief is complete.",
            "brief": {
                "objective": "Tenant identity redesign",
                "user_value": "Admins can manage identity safely",
                "acceptance_criteria": ["Invitations can be sent"],
                "scope_in": ["Tenant identity"],
                "scope_out": ["SSO overhaul"],
                "constraints": ["12 month retention window"],
                "risks": ["Audit export misuse"],
                "success_outcomes": ["Admins can export audit logs within policy"],
                "recommendation": "Proceed to planning.",
                "open_questions": [],
                "next_steps": ["Plan child tickets"],
            },
            "status": PM_INTERVIEW_STATUS_PM_COMPLETED,
            "ready_to_write": True,
            "next_question": None,
        }

        with (
            self.session_factory() as session,
            patch(
                "orchestrator.core.pm_interview_followup_service.plan_pm_interview_with_runtime",
                return_value=pm_payload,
            ),
        ):
            result = continue_pm_interview_from_followup(
                session=session,
                runtime=SimpleNamespace(),
                tenant_id="route25",
                project_id="route25-default",
                request_id="pm-req-followup-2",
                reply_text="Use a 12 month retention window.",
                source_transport="jira_comment",
                source_ref="comment-2",
                actor_ref="jira-user-1",
                invocation_context=AgentInvocationContext(
                    channel="jira",
                    tenant_id="route25",
                    project_id="route25-default",
                    command="pm",
                    stage="interview",
                    working_dir=".",
                    issue_key="MAB-231",
                ),
                project_keys=["MAB"],
                settings=SimpleNamespace(),
            )
            session.commit()

            case = session.execute(
                select(PMInterviewCase).where(PMInterviewCase.request_id == "pm-req-followup-2")
            ).scalars().one()

            self.assertTrue(result.assessment.ready_to_write)
            self.assertEqual(result.clarification_questions, ())
            self.assertEqual(case.current_question_json, {})
            self.assertEqual(case.next_question_json, {})
