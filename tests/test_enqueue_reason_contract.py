import unittest
from types import SimpleNamespace

from orchestrator.core.communications.enqueue_reason_contract import (
    enqueue_reason_guidance,
    format_enqueue_conflict_detail,
)


class EnqueueReasonContractTests(unittest.TestCase):
    def test_known_reason_returns_guidance(self) -> None:
        guidance = enqueue_reason_guidance("tenant_concurrency_limit_reached")
        self.assertIn("concurrency limit", guidance.lower())

    def test_unknown_reason_returns_default_guidance(self) -> None:
        guidance = enqueue_reason_guidance("unexpected_reason")
        self.assertIn("execution policy", guidance.lower())

    def test_conflict_detail_includes_active_run_context(self) -> None:
        detail = format_enqueue_conflict_detail(
            prefix="Run could not be queued",
            enqueue_reason="run_already_active",
            enqueue_run_obj=SimpleNamespace(run_id="run-1", status="queued"),
        )
        self.assertIn("run_already_active", detail)
        self.assertIn("run-1", detail)
        self.assertIn("already active", detail.lower())

