from __future__ import annotations

from dataclasses import dataclass

from orchestrator.core.workflow_advance import WorkflowAdvanceHandler, WorkflowOperationRetryHandler


@dataclass(frozen=True)
class WorkflowHandlerRegistry:
    advance_handlers: dict[str, WorkflowAdvanceHandler]
    operation_retry_handlers: dict[str, WorkflowOperationRetryHandler]

    def resolve_advance_handler(self, handler_key: str) -> WorkflowAdvanceHandler:
        normalized = str(handler_key or "").strip()
        handler = self.advance_handlers.get(normalized)
        if handler is None:
            raise LookupError(f"No workflow advance handler is registered for {handler_key}")
        return handler

    def resolve_operation_retry_handler(self, handler_key: str) -> WorkflowOperationRetryHandler:
        normalized = str(handler_key or "").strip()
        handler = self.operation_retry_handlers.get(normalized)
        if handler is None:
            raise LookupError(f"No workflow operation retry handler is registered for {handler_key}")
        return handler


def build_workflow_handler_registry(
    *,
    advance_handlers: dict[str, WorkflowAdvanceHandler],
    operation_retry_handlers: dict[str, WorkflowOperationRetryHandler],
) -> WorkflowHandlerRegistry:
    return WorkflowHandlerRegistry(
        advance_handlers=dict(advance_handlers),
        operation_retry_handlers=dict(operation_retry_handlers),
    )
