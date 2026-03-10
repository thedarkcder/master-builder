from __future__ import annotations

import unittest

from orchestrator.core.decision_reply_service import serialize_recorded_answers_for_policy
from orchestrator.storage.models import DecisionAnswer


class DecisionReplyServiceTests(unittest.TestCase):
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
