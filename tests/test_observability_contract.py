import json
import logging
import unittest

from orchestrator.core.observability import (
    ObservabilityJsonFormatter,
    REQUIRED_LOG_FIELDS,
    current_log_context,
    reset_log_context,
    scoped_log_context,
    set_log_context,
    validate_log_payload,
)


class ObservabilityContractTests(unittest.TestCase):
    def test_formatter_emits_required_contract_fields(self) -> None:
        formatter = ObservabilityJsonFormatter(environment="test", platform_version="v-test")
        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname=__file__,
            lineno=42,
            msg="hello world",
            args=(),
            exc_info=None,
        )
        record.event_type = "contract_check"
        record.metadata = {"k": "v"}

        payload = json.loads(formatter.format(record))
        missing = validate_log_payload(payload)

        self.assertEqual(missing, [])
        for field in REQUIRED_LOG_FIELDS:
            self.assertIn(field, payload)
        self.assertEqual(payload["event_type"], "contract_check")
        self.assertEqual(payload["environment"], "test")
        self.assertEqual(payload["platform_version"], "v-test")
        self.assertIn("trace_id", payload)
        self.assertIn("span_id", payload)

    def test_context_is_applied_and_reset(self) -> None:
        tokens = set_log_context(correlation_id="cid-1", tenant_id="tenant-1", project_id="project-1", agent_id="agent-1")
        self.assertEqual(
            current_log_context(),
            {
                "correlation_id": "cid-1",
                "tenant_id": "tenant-1",
                "project_id": "project-1",
                "agent_id": "agent-1",
            },
        )
        reset_log_context(tokens)
        self.assertEqual(
            current_log_context(),
            {
                "correlation_id": None,
                "tenant_id": None,
                "project_id": None,
                "agent_id": None,
            },
        )

    def test_validate_log_payload_reports_missing_fields(self) -> None:
        missing = validate_log_payload({"timestamp": "x", "level": "INFO"})
        self.assertIn("environment", missing)
        self.assertIn("metadata", missing)

    def test_formatter_normalizes_empty_scope_fields_and_uses_default_agent_id(self) -> None:
        formatter = ObservabilityJsonFormatter(
            environment="test",
            platform_version="v-test",
            default_agent_id="api",
        )
        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname=__file__,
            lineno=7,
            msg="hello",
            args=(),
            exc_info=None,
        )

        payload = json.loads(formatter.format(record))

        self.assertEqual(payload["tenant_id"], "")
        self.assertEqual(payload["project_id"], "")
        self.assertEqual(payload["correlation_id"], "")
        self.assertEqual(payload["trace_id"], "")
        self.assertEqual(payload["span_id"], "")
        self.assertEqual(payload["agent_id"], "api")

    def test_scoped_log_context_applies_and_resets_context(self) -> None:
        with scoped_log_context(correlation_id="cid-2", tenant_id="tenant-2", project_id="project-2", agent_id="agent-2"):
            self.assertEqual(
                current_log_context(),
                {
                    "correlation_id": "cid-2",
                    "tenant_id": "tenant-2",
                    "project_id": "project-2",
                    "agent_id": "agent-2",
                },
            )

        self.assertEqual(
            current_log_context(),
            {
                "correlation_id": None,
                "tenant_id": None,
                "project_id": None,
                "agent_id": None,
            },
        )
