from __future__ import annotations

import unittest

from orchestrator.core.workflow.transitions import (
    ATTEMPT_STATUS_QUEUED,
    INPUT_STATUS_ANSWERED,
    INPUT_STATUS_PENDING,
    WORKFLOW_STATUS_QUEUED,
    WORKFLOW_STATUS_RUNNING,
    WorkflowTransitionError,
    transition_attempt_state,
    transition_input_request_state,
    transition_workflow_state,
)


class WorkflowTransitionTests(unittest.TestCase):
    def test_workflow_moves_to_running_when_attempt_starts(self) -> None:
        self.assertEqual(
            transition_workflow_state(
                current_state=WORKFLOW_STATUS_QUEUED,
                event="attempt_started",
            ),
            WORKFLOW_STATUS_RUNNING,
        )

    def test_workflow_rejects_invalid_transition(self) -> None:
        with self.assertRaises(WorkflowTransitionError):
            transition_workflow_state(
                current_state=WORKFLOW_STATUS_RUNNING,
                event="workflow_enqueued",
            )

    def test_attempt_moves_to_waiting_for_input(self) -> None:
        self.assertEqual(
            transition_attempt_state(
                current_state="running",
                event="human_input_requested",
            ),
            "waiting_for_input",
        )

    def test_attempt_rejects_invalid_transition(self) -> None:
        with self.assertRaises(WorkflowTransitionError):
            transition_attempt_state(
                current_state=ATTEMPT_STATUS_QUEUED,
                event="attempt_succeeded",
            )

    def test_input_request_moves_from_answered_to_consumed(self) -> None:
        self.assertEqual(
            transition_input_request_state(
                current_state=INPUT_STATUS_ANSWERED,
                event="resume_attempt_created",
            ),
            "consumed",
        )

    def test_input_request_rejects_invalid_transition(self) -> None:
        with self.assertRaises(WorkflowTransitionError):
            transition_input_request_state(
                current_state=INPUT_STATUS_PENDING,
                event="resume_attempt_created",
            )


if __name__ == "__main__":
    unittest.main()
