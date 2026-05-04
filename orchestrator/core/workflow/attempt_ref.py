from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkflowAttemptRef:
    workflow_id: str | None = None
    operation_id: str | None = None
    attempt_id: str | None = None
    number: int | None = None

    def require_attempt_id(self) -> str:
        normalized = str(self.attempt_id or "").strip()
        if not normalized:
            raise ValueError("WorkflowAttemptRef requires attempt_id")
        return normalized

    def require_operation_id(self) -> str:
        normalized = str(self.operation_id or "").strip()
        if not normalized:
            raise ValueError("WorkflowAttemptRef requires operation_id")
        return normalized

    def require_workflow_id(self) -> str:
        normalized = str(self.workflow_id or "").strip()
        if not normalized:
            raise ValueError("WorkflowAttemptRef requires workflow_id")
        return normalized

    def assert_matches_operation(self, operation_id: str) -> None:
        expected = self.require_operation_id()
        actual = str(operation_id or "").strip()
        if actual != expected:
            raise ValueError("WorkflowAttemptRef operation_id does not match operation")
