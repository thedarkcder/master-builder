import unittest

from orchestrator.core.communications.enqueue_reason_contract import (
    enqueue_reason_guidance,
)


class EnqueueReasonContractTests(unittest.TestCase):
    def test_known_reason_returns_guidance(self) -> None:
        guidance = enqueue_reason_guidance("tenant_concurrency_limit_reached")
        self.assertIn("concurrency limit", guidance.lower())

    def test_unknown_reason_returns_default_guidance(self) -> None:
        guidance = enqueue_reason_guidance("unexpected_reason")
        self.assertIn("execution policy", guidance.lower())
