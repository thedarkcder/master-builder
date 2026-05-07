from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator.core.knowledge.base import KnowledgeEmbeddingAccessMode
from orchestrator.core.runtime.invocation import (
    AgentInvocationContext,
    _AsyncRuntimeLogWriter,
    RuntimeJsonContractError,
    ToolBridgeExhaustedError,
    ToolBridgeProtocolError,
    ToolExecutionError,
    invoke_runtime_json,
    invoke_runtime_json_with_tools,
)
from orchestrator.core.runtime.runtime import CodexRuntime, CodexRuntimeError


class CodexInvocationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._emit_invocation_event_patcher = patch("orchestrator.core.runtime.invocation._emit_invocation_event")
        self._emit_invocation_event_patcher.start()
        self.addCleanup(self._emit_invocation_event_patcher.stop)

    class _Writer:
        def enqueue(self, *, context, stream: str, message: str) -> bool:  # noqa: ANN001
            _ = context
            _ = stream
            _ = message
            return True

        def flush_invocation(self, *, invocation_id: str, timeout_seconds: float = 3.0) -> None:  # noqa: ARG002
            _ = invocation_id
            return None

    def test_turn_completed_log_line_is_persisted_even_when_sampled(self) -> None:
        def _request(  # noqa: ANN001
            _system_prompt,
            _user_prompt,
            _working_dir,
            on_log_line,
            _reasoning_effort,
            _resume_session_id,
            _on_session_id,
            _on_usage,
        ) -> str:
            if on_log_line is not None:
                for idx in range(1, 41):
                    on_log_line("stdout", f"noise-{idx}")
                on_log_line(
                    "stdout",
                    '{"type":"turn.completed","usage":{"input_tokens":77,"cached_input_tokens":55,"output_tokens":9}}',
                )
            return '{"ok": true}'

        runtime = CodexRuntime(
            model="m",
            max_output_tokens=10,
            command="override",
            _request=_request,
        )
        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="policy",
            stage="pm",
            working_dir=".",
            run_id="run-1",
        )

        persisted_messages: list[str] = []

        def _capture_persist(*, context, stream: str, message: str) -> None:  # noqa: ANN001
            _ = context
            _ = stream
            persisted_messages.append(message)

        with (
            patch("orchestrator.core.runtime.invocation._get_log_writer", return_value=self._Writer()),
            patch("orchestrator.core.runtime.invocation._enqueue_runtime_log_line", side_effect=_capture_persist),
            patch("orchestrator.core.runtime.invocation._append_raw_log_line"),
            patch("orchestrator.core.runtime.invocation._emit_invocation_event"),
            patch("orchestrator.core.runtime.invocation.get_settings") as settings_mock,
        ):
            settings_mock.return_value = type(
                "_Settings",
                (),
                {
                    "codex_db_log_sampling_interval": 1000,
                },
            )()
            payload = invoke_runtime_json(
                runtime=runtime,
                context=context,
                system_prompt="system",
                user_prompt="user",
            )

        self.assertEqual(payload, {"ok": True})
        self.assertTrue(
            any('"type":"turn.completed"' in message for message in persisted_messages),
            "turn.completed usage lines must bypass DB sampling",
        )

    def test_async_runtime_log_writer_isolates_persistence_failures_by_invocation(self) -> None:
        writer = _AsyncRuntimeLogWriter()
        writer._batch_size = 100
        writer._batch_flush_ms = 50
        contexts = {
            invocation_id: AgentInvocationContext(
                channel="worker",
                tenant_id="tenant-1",
                project_id="proj-1",
                command="policy",
                stage="pm",
                working_dir=".",
                run_id=f"run-{invocation_id}",
                invocation_id=invocation_id,
            )
            for invocation_id in ("bad", "good")
        }
        persisted_groups: list[list[str]] = []

        def _persist_group(*, items):  # noqa: ANN001
            invocation_ids = [str(item.context.invocation_id) for item in items]
            persisted_groups.append(invocation_ids)
            if invocation_ids == ["bad"]:
                raise RuntimeError("bad invocation")

        with patch("orchestrator.core.runtime.invocation._persist_runtime_log_lines", side_effect=_persist_group):
            self.assertTrue(writer.enqueue(context=contexts["bad"], stream="stdout", message="bad line"))
            self.assertTrue(writer.enqueue(context=contexts["good"], stream="stdout", message="good line"))

            with self.assertRaisesRegex(RuntimeError, "bad invocation"):
                writer.flush_invocation(invocation_id="bad")
            writer.flush_invocation(invocation_id="good")

        self.assertIn(["bad"], persisted_groups)
        self.assertIn(["good"], persisted_groups)

    def test_invoke_runtime_json_captures_usage_from_turn_completed_log_line(self) -> None:
        def _request(  # noqa: ANN001
            _system_prompt,
            _user_prompt,
            _working_dir,
            on_log_line,
            _reasoning_effort,
            _resume_session_id,
            _on_session_id,
            _on_usage,
        ) -> str:
            if on_log_line is not None:
                on_log_line(
                    "stdout",
                    '{"type":"turn.completed","usage":{"input_tokens":51,"output_tokens":13,"total_tokens":64}}',
                )
            return '{"ok": true}'

        runtime = CodexRuntime(
            model="m",
            max_output_tokens=10,
            command="override",
            _request=_request,
        )
        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="test",
            working_dir=".",
            run_id="run-1",
        )

        events: list[tuple[str, dict[str, object]]] = []

        def _capture_event(*, context, event_kind: str, payload: dict[str, object]) -> None:  # noqa: ANN001
            _ = context
            events.append((event_kind, payload))

        with (
            patch("orchestrator.core.runtime.invocation._get_log_writer", return_value=self._Writer()),
            patch("orchestrator.core.runtime.invocation._emit_invocation_event", side_effect=_capture_event),
        ):
            payload = invoke_runtime_json(
                runtime=runtime,
                context=context,
                system_prompt="system",
                user_prompt="user",
            )

        self.assertEqual(payload, {"ok": True})
        finished_payload = next(
            payload
            for event_kind, payload in events
            if event_kind == "stage_invocation_finished"
        )
        self.assertEqual(finished_payload["actual_prompt_tokens"], 51)
        self.assertEqual(finished_payload["actual_completion_tokens"], 13)
        self.assertEqual(finished_payload["actual_total_tokens"], 64)
        self.assertEqual(finished_payload["actual_usage_observed"], True)

    def test_runtime_log_sink_redacts_sensitive_content_before_persistence(self) -> None:
        captured_db: list[str] = []

        def _request(  # noqa: ANN001
            _system_prompt,
            _user_prompt,
            _working_dir,
            on_log_line,
            _reasoning_effort,
            _resume_session_id,
            _on_session_id,
            _on_usage,
        ) -> str:
            if on_log_line is not None:
                on_log_line(
                    "stderr",
                    (
                        "run_id=123e4567-e89b-12d3-a456-426614174000 "
                        "APP_STORE_CONNECT_API_KEY_BASE64=super-secret-value "
                        "email=user@example.com"
                    ),
                )
            return '{"ok": true}'

        runtime = CodexRuntime(
            model="m",
            max_output_tokens=10,
            command="override",
            _request=_request,
        )
        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="test",
            working_dir=".",
            run_id="run-1",
        )

        def _capture_db(*, context, stream: str, message: str) -> None:  # noqa: ANN001
            _ = context
            _ = stream
            captured_db.append(message)

        with (
            patch("orchestrator.core.runtime.invocation._get_log_writer", return_value=self._Writer()),
            patch("orchestrator.core.runtime.invocation._append_raw_log_line"),
            patch("orchestrator.core.runtime.invocation._enqueue_runtime_log_line", side_effect=_capture_db),
            patch("orchestrator.core.runtime.invocation._emit_invocation_event"),
        ):
            payload = invoke_runtime_json(
                runtime=runtime,
                context=context,
                system_prompt="system",
                user_prompt="user",
            )

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(len(captured_db), 1)
        for message in captured_db:
            self.assertNotIn("super-secret-value", message)
            self.assertNotIn("user@example.com", message)
            self.assertIn("123e4567-e89b-12d3-a456-426614174000", message)
            self.assertIn("[REDACTED]", message)

    def test_invoke_runtime_json_flushes_invocation_logs(self) -> None:
        runtime = CodexRuntime(
            model="m",
            max_output_tokens=10,
            command="override",
            _request=lambda _s, _u, _w, _l=None: '{"ok": true}',
        )
        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="pm",
            working_dir=".",
            run_id="run-1",
        )

        class _CaptureWriter:
            def __init__(self) -> None:
                self.flushed_invocations: list[str] = []

            def flush_invocation(self, *, invocation_id: str, timeout_seconds: float = 3.0) -> None:
                _ = timeout_seconds
                self.flushed_invocations.append(invocation_id)

        writer = _CaptureWriter()
        with patch("orchestrator.core.runtime.invocation._get_log_writer", return_value=writer):
            payload = invoke_runtime_json(
                runtime=runtime,
                context=context,
                system_prompt="system",
                user_prompt="user",
            )

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(len(writer.flushed_invocations), 1)
        self.assertTrue(writer.flushed_invocations[0].strip())

    def test_invoke_runtime_json_uses_explicit_codex_session_and_persists_it(self) -> None:
        runtime_call: dict[str, str | None] = {}

        def _request(  # noqa: ANN001
            _system_prompt,
            _user_prompt,
            _working_dir,
            _on_log_line,
            _reasoning_effort,
            resume_session_id,
            on_session_id,
        ) -> str:
            runtime_call["resume_session_id"] = resume_session_id
            if on_session_id is not None:
                on_session_id("bbbbbbbb-cccc-dddd-eeee-ffffffffffff")
            return '{"ok": true}'

        runtime = CodexRuntime(
            model="m",
            max_output_tokens=10,
            command="override",
            _request=_request,
        )
        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="pm",
            working_dir=".",
            workflow_id="workflow-1",
            run_id="run-1",
            codex_session_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        )

        with (
            patch("orchestrator.core.runtime.invocation._get_log_writer", return_value=self._Writer()),
            patch("orchestrator.core.runtime.invocation._persist_checkpoint_session_id") as persist_mock,
        ):
            payload = invoke_runtime_json(
                runtime=runtime,
                context=context,
                system_prompt="system",
                user_prompt="user",
            )

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(runtime_call["resume_session_id"], "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        persist_mock.assert_called_with(
            workflow_id="workflow-1",
            run_id="run-1",
            session_id="bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
            checkpoint_kind="pm",
            stage="pm",
        )

    def test_invoke_runtime_json_uses_execution_session_bucket_for_dev(self) -> None:
        runtime_call: dict[str, str | None] = {}

        def _request(  # noqa: ANN001
            _system_prompt,
            _user_prompt,
            _working_dir,
            _on_log_line,
            _reasoning_effort,
            resume_session_id,
            on_session_id,
        ) -> str:
            runtime_call["resume_session_id"] = resume_session_id
            if on_session_id is not None:
                on_session_id("bbbbbbbb-cccc-dddd-eeee-ffffffffffff")
            return '{"ok": true}'

        runtime = CodexRuntime(
            model="m",
            max_output_tokens=10,
            command="override",
            _request=_request,
        )
        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="dev",
            working_dir=".",
            workflow_id="workflow-1",
            run_id="run-1",
        )

        with (
            patch("orchestrator.core.runtime.invocation._get_log_writer", return_value=self._Writer()),
            patch("orchestrator.core.runtime.invocation._persist_checkpoint_session_id") as persist_mock,
        ):
            payload = invoke_runtime_json(
                runtime=runtime,
                context=context,
                system_prompt="system",
                user_prompt="user",
            )

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(runtime_call["resume_session_id"], None)
        persist_mock.assert_called_with(
            workflow_id="workflow-1",
            run_id="run-1",
            session_id="bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
            checkpoint_kind="execution",
            stage="dev",
        )

    def test_invoke_runtime_json_uses_orchestrated_session_bucket(self) -> None:
        runtime_call: dict[str, str | None] = {}

        def _request(  # noqa: ANN001
            _system_prompt,
            _user_prompt,
            _working_dir,
            _on_log_line,
            _reasoning_effort,
            resume_session_id,
            on_session_id,
        ) -> str:
            runtime_call["resume_session_id"] = resume_session_id
            if on_session_id is not None:
                on_session_id("bbbbbbbb-cccc-dddd-eeee-ffffffffffff")
            return '{"ok": true}'

        runtime = CodexRuntime(
            model="m",
            max_output_tokens=10,
            command="override",
            _request=_request,
        )
        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="orchestrated_run",
            working_dir=".",
            workflow_id="workflow-1",
            run_id="run-1",
        )

        with (
            patch("orchestrator.core.runtime.invocation._get_log_writer", return_value=self._Writer()),
            patch("orchestrator.core.runtime.invocation._persist_checkpoint_session_id") as persist_mock,
        ):
            payload = invoke_runtime_json(
                runtime=runtime,
                context=context,
                system_prompt="system",
                user_prompt="user",
            )

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(runtime_call["resume_session_id"], None)
        persist_mock.assert_called_with(
            workflow_id="workflow-1",
            run_id="run-1",
            session_id="bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
            checkpoint_kind="orchestrated",
            stage="orchestrated_run",
        )

    def test_invoke_runtime_json_does_not_resume_fresh_attempt_from_checkpoint_storage(self) -> None:
        runtime_call: dict[str, str | None] = {}

        def _request(  # noqa: ANN001
            _system_prompt,
            _user_prompt,
            _working_dir,
            _on_log_line,
            _reasoning_effort,
            resume_session_id,
            on_session_id,
        ) -> str:
            runtime_call["resume_session_id"] = resume_session_id
            if on_session_id is not None:
                on_session_id("fresh-session-id")
            return '{"ok": true}'

        runtime = CodexRuntime(
            model="m",
            max_output_tokens=10,
            command="override",
            _request=_request,
        )
        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="pm",
            working_dir=".",
            workflow_id="workflow-1",
            run_id="run-1",
        )

        with (
            patch("orchestrator.core.runtime.invocation._get_log_writer", return_value=self._Writer()),
            patch("orchestrator.core.runtime.invocation._persist_checkpoint_session_id"),
        ):
            payload = invoke_runtime_json(
                runtime=runtime,
                context=context,
                system_prompt="system",
                user_prompt="user",
            )

        self.assertEqual(payload, {"ok": True})
        self.assertIsNone(runtime_call["resume_session_id"])

    def test_invoke_runtime_json_emits_actual_usage_metrics(self) -> None:
        def _request(  # noqa: ANN001
            _system_prompt,
            _user_prompt,
            _working_dir,
            _on_log_line,
            _reasoning_effort,
            _resume_session_id,
            _on_session_id,
            on_usage,
        ) -> str:
            if on_usage is not None:
                on_usage(
                    {
                        "prompt_tokens": 41,
                        "completion_tokens": 9,
                        "total_tokens": 50,
                    }
                )
            return '{"ok": true}'

        runtime = CodexRuntime(
            model="m",
            max_output_tokens=10,
            command="override",
            _request=_request,
        )
        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="pm",
            working_dir=".",
            run_id="run-1",
        )

        events: list[tuple[str, dict[str, object]]] = []

        def _capture_event(*, context, event_kind: str, payload: dict[str, object]) -> None:  # noqa: ANN001
            _ = context
            events.append((event_kind, payload))

        with (
            patch("orchestrator.core.runtime.invocation._get_log_writer", return_value=self._Writer()),
            patch("orchestrator.core.runtime.invocation._emit_invocation_event", side_effect=_capture_event),
        ):
            payload = invoke_runtime_json(
                runtime=runtime,
                context=context,
                system_prompt="system",
                user_prompt="user",
            )

        self.assertEqual(payload, {"ok": True})
        finished_payload = next(
            payload
            for event_kind, payload in events
            if event_kind == "stage_invocation_finished"
        )
        self.assertEqual(finished_payload["actual_prompt_tokens"], 41)
        self.assertEqual(finished_payload["actual_completion_tokens"], 9)
        self.assertEqual(finished_payload["actual_total_tokens"], 50)
        self.assertEqual(finished_payload["actual_usage_observed"], True)

    def test_invoke_runtime_json_normalizes_provider_usage_metrics(self) -> None:
        def _request(  # noqa: ANN001
            _system_prompt,
            _user_prompt,
            _working_dir,
            _on_log_line,
            _reasoning_effort,
            _resume_session_id,
            _on_session_id,
            on_usage,
        ) -> str:
            if on_usage is not None:
                on_usage(
                    {
                        "input_tokens": 41,
                        "cached_input_tokens": 17,
                        "output_tokens": 9,
                    }
                )
            return '{"ok": true}'

        runtime = CodexRuntime(
            model="m",
            max_output_tokens=10,
            command="override",
            _request=_request,
        )
        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="pm",
            working_dir=".",
            run_id="run-1",
        )

        events: list[tuple[str, dict[str, object]]] = []

        def _capture_event(*, context, event_kind: str, payload: dict[str, object]) -> None:  # noqa: ANN001
            _ = context
            events.append((event_kind, payload))

        with (
            patch("orchestrator.core.runtime.invocation._get_log_writer", return_value=self._Writer()),
            patch("orchestrator.core.runtime.invocation._emit_invocation_event", side_effect=_capture_event),
        ):
            payload = invoke_runtime_json(
                runtime=runtime,
                context=context,
                system_prompt="system",
                user_prompt="user",
            )

        self.assertEqual(payload, {"ok": True})
        finished_payload = next(
            payload
            for event_kind, payload in events
            if event_kind == "stage_invocation_finished"
        )
        self.assertEqual(finished_payload["actual_prompt_tokens"], 41)
        self.assertEqual(finished_payload["actual_completion_tokens"], 9)
        self.assertEqual(finished_payload["actual_total_tokens"], 50)
        self.assertEqual(finished_payload["actual_cached_input_tokens"], 17)
        self.assertEqual(finished_payload["actual_usage_observed"], True)

    def test_invoke_runtime_json_passes_effective_policy_model_to_runtime(self) -> None:
        captured: dict[str, object] = {}

        class _Runtime:
            def run_json(self, **kwargs):  # noqa: ANN003
                captured.update(kwargs)
                return {"ok": True}

        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="pm",
            working_dir=".",
            run_id="run-1",
        )

        with (
            patch("orchestrator.core.runtime.invocation._get_log_writer", return_value=self._Writer()),
            patch(
                "orchestrator.core.runtime.invocation._resolve_knowledge_policy_for_context",
                return_value=("proj-1", True, "aggressive", "gpt-5.4-mini", "high", True),
            ),
            patch("orchestrator.core.runtime.invocation.create_session_factory"),
            patch("orchestrator.core.runtime.invocation.build_knowledge_prompt_context", return_value=SimpleNamespace(text="", citations=[])),
        ):
            payload = invoke_runtime_json(
                runtime=_Runtime(),  # type: ignore[arg-type]
                context=context,
                system_prompt="system",
                user_prompt="user",
        )

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(captured["model_override"], "gpt-5.4-mini")
        self.assertEqual(captured["reasoning_effort"], "high")

    def test_invoke_runtime_json_uses_best_effort_knowledge_lookup_for_worker_execution(self) -> None:
        class _Runtime:
            def run_json(self, **_kwargs):  # noqa: ANN003
                return {"ok": True}

        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="dev",
            working_dir=".",
            run_id="run-1",
        )

        with (
            patch("orchestrator.core.runtime.invocation._get_log_writer", return_value=self._Writer()),
            patch(
                "orchestrator.core.runtime.invocation._resolve_knowledge_policy_for_context",
                return_value=("proj-1", True, "aggressive", "gpt-5.4", "medium", False),
            ),
            patch("orchestrator.core.runtime.invocation.create_session_factory"),
            patch(
                "orchestrator.core.runtime.invocation.build_knowledge_prompt_context",
                return_value=SimpleNamespace(text="", citations=[]),
            ) as knowledge_mock,
        ):
            payload = invoke_runtime_json(
                runtime=_Runtime(),  # type: ignore[arg-type]
                context=context,
                system_prompt="system",
                user_prompt="user",
            )

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(
            knowledge_mock.call_args.kwargs["embedding_access_mode"],
            KnowledgeEmbeddingAccessMode.BEST_EFFORT,
        )

    def test_invoke_runtime_json_prefers_scoped_reasoning_override_over_context_default(self) -> None:
        captured: dict[str, object] = {}

        class _Runtime:
            def run_json(self, **kwargs):  # noqa: ANN003
                captured.update(kwargs)
                return {"ok": True}

        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="pm",
            working_dir=".",
            run_id="run-1",
            reasoning_effort="medium",
        )

        with (
            patch("orchestrator.core.runtime.invocation._get_log_writer", return_value=self._Writer()),
            patch(
                "orchestrator.core.runtime.invocation._resolve_knowledge_policy_for_context",
                return_value=("proj-1", True, "aggressive", "gpt-5.4-mini", "high", True),
            ),
            patch("orchestrator.core.runtime.invocation.create_session_factory"),
            patch(
                "orchestrator.core.runtime.invocation.build_knowledge_prompt_context",
                return_value=SimpleNamespace(text="", citations=[]),
            ),
        ):
            payload = invoke_runtime_json(
                runtime=_Runtime(),  # type: ignore[arg-type]
                context=context,
                system_prompt="system",
                user_prompt="user",
            )

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(captured["reasoning_effort"], "high")

    def test_invoke_runtime_json_uses_scoped_model_when_kb_injection_disabled(self) -> None:
        captured: dict[str, object] = {}

        class _Runtime:
            def run_json(self, **kwargs):  # noqa: ANN003
                captured.update(kwargs)
                return {"ok": True}

        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="pm",
            working_dir=".",
            run_id="run-1",
        )

        with (
            patch("orchestrator.core.runtime.invocation._get_log_writer", return_value=self._Writer()),
            patch(
                "orchestrator.core.runtime.invocation._resolve_knowledge_policy_for_context",
                return_value=("proj-1", False, "aggressive", "gpt-5.4-mini", "high", True),
            ) as resolve_policy_mock,
            patch("orchestrator.core.runtime.invocation.get_settings") as settings_mock,
        ):
            settings_mock.return_value = type(
                "_Settings",
                (),
                {
                    "knowledge_injection_enabled": False,
                    "database_url": "sqlite:///ignored.db",
                    "codex_model": "gpt-5.4",
                    "codex_reasoning_effort": "medium",
                    "codex_db_log_sampling_interval": 1,
                },
            )()
            payload = invoke_runtime_json(
                runtime=_Runtime(),  # type: ignore[arg-type]
                context=context,
                system_prompt="system",
                user_prompt="user",
            )

        self.assertEqual(payload, {"ok": True})
        resolve_policy_mock.assert_called_once_with(context=context)
        self.assertEqual(captured["model_override"], "gpt-5.4-mini")
        self.assertEqual(captured["reasoning_effort"], "high")

    def test_invoke_runtime_json_prefers_http_runtime_profile_model_over_policy_model(self) -> None:
        captured: dict[str, object] = {}

        class _Runtime:
            command = "http:lm_studio"
            model = "openai/gpt-oss-20b"

            def run_json(self, **kwargs):  # noqa: ANN003
                captured.update(kwargs)
                return {"ok": True}

        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="project_automation",
            stage="standup_voice_brief",
            working_dir=".",
            run_id="run-1",
        )

        with (
            patch("orchestrator.core.runtime.invocation._get_log_writer", return_value=self._Writer()),
            patch(
                "orchestrator.core.runtime.invocation._resolve_knowledge_policy_for_context",
                return_value=("proj-1", True, "aggressive", "gpt-5.4", "high", True),
            ),
            patch("orchestrator.core.runtime.invocation.create_session_factory"),
            patch("orchestrator.core.runtime.invocation.build_knowledge_prompt_context", return_value=SimpleNamespace(text="", citations=[])),
        ):
            payload = invoke_runtime_json(
                runtime=_Runtime(),  # type: ignore[arg-type]
                context=context,
                system_prompt="system",
                user_prompt="user",
            )

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(captured["model_override"], "openai/gpt-oss-20b")
        self.assertEqual(captured["reasoning_effort"], "high")

    def test_invoke_runtime_json_fails_on_non_json_payload(self) -> None:
        class _Runtime:
            def run_json(self, **_kwargs):  # noqa: ANN003
                raise CodexRuntimeError(
                    "Invalid JSON payload from Codex runtime: Expecting value",
                    payload_preview="not-json",
                )

        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="dev",
            working_dir=".",
            run_id="run-1",
        )

        with (
            patch("orchestrator.core.runtime.invocation._get_log_writer", return_value=self._Writer()),
            self.assertRaises(RuntimeJsonContractError) as raised,
        ):
            invoke_runtime_json(
                runtime=_Runtime(),  # type: ignore[arg-type]
                context=context,
                system_prompt="system",
                user_prompt="user",
            )

        self.assertIn("invalid json payload", str(raised.exception).lower())

    def test_invoke_runtime_json_does_not_reclassify_runtime_failures_as_json_contract_errors(self) -> None:
        class _Runtime:
            def run_json(self, **_kwargs):  # noqa: ANN003
                raise CodexRuntimeError("Codex CLI command failed with exit code 2: unknown option --json")

        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="dev",
            working_dir=".",
            run_id="run-1",
        )

        with (
            patch("orchestrator.core.runtime.invocation._get_log_writer", return_value=self._Writer()),
            self.assertRaises(CodexRuntimeError),
        ):
            invoke_runtime_json(
                runtime=_Runtime(),  # type: ignore[arg-type]
                context=context,
                system_prompt="system",
                user_prompt="user",
            )

    def test_invoke_runtime_json_with_tools_executes_request_and_resumes_same_session(self) -> None:
        runtime_calls: list[dict[str, object]] = []
        tool_calls: list[tuple[str, dict[str, object]]] = []

        class _Runtime:
            def run_json(self, **kwargs):  # noqa: ANN003
                runtime_calls.append(kwargs)
                if len(runtime_calls) == 1:
                    on_session_id = kwargs.get("on_session_id")
                    if callable(on_session_id):
                        on_session_id("tool-session-1")
                    return {
                        "type": "tool_request",
                        "tool_name": "decision.read_state",
                        "tool_args": {"issue_key": "GP-124"},
                    }
                return {
                    "type": "final_response",
                    "result": {"gate_status": "clear", "reason": "", "questions": []},
                }

        context = AgentInvocationContext(
            channel="system",
            tenant_id="tenant-1",
            project_id=None,
            command="policy",
            stage="decision_planner",
            working_dir=".",
        )

        with patch("orchestrator.core.runtime.invocation._get_log_writer", return_value=self._Writer()):
            payload = invoke_runtime_json_with_tools(
                runtime=_Runtime(),  # type: ignore[arg-type]
                context=context,
                system_prompt="system",
                user_prompt="user",
                allowed_tools={"decision.read_state"},
                execute_tool=lambda tool_name, tool_args: tool_calls.append((tool_name, tool_args)) or {"case": "ok"},
            )

        self.assertEqual(payload, {"gate_status": "clear", "reason": "", "questions": []})
        self.assertEqual(tool_calls, [("decision.read_state", {"issue_key": "GP-124"})])
        self.assertIsNone(runtime_calls[0]["resume_session_id"])
        self.assertEqual(runtime_calls[1]["resume_session_id"], "tool-session-1")
        self.assertIn('"tool_name": "decision.read_state"', str(runtime_calls[1]["user_prompt"]))
        self.assertIn('"ok": true', str(runtime_calls[1]["user_prompt"]).lower())

    def test_invoke_runtime_json_preserves_operation_id_in_runtime_log_sink(self) -> None:
        captured: dict[str, object] = {}
        live_lines: list[tuple[str | None, str | None, str]] = []

        class _Runtime:
            def run_json(self, **kwargs):  # noqa: ANN003
                on_log_line = kwargs.get("on_log_line")
                if callable(on_log_line):
                    on_log_line("stdout", "planning line")
                return {"ok": True}

        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="engineering_planning",
            working_dir=".",
            workflow_id="parent_planning:MAB-215",
            operation_id="operation-jira-child-fanout",
            attempt_id="attempt-1",
            run_id="run-1",
        )

        def _fake_sink(**kwargs):  # noqa: ANN003
            captured.update(kwargs)
            return lambda _stream, _message: None

        def _capture_live_line(*, context, stream: str, message: str) -> None:  # noqa: ANN001
            _ = stream
            live_lines.append((context.operation_id, context.attempt_id, message))

        with (
            patch("orchestrator.core.runtime.invocation._get_log_writer", return_value=self._Writer()),
            patch("orchestrator.core.runtime.invocation.build_runtime_log_sink", side_effect=_fake_sink),
            patch("orchestrator.core.runtime.invocation._emit_invocation_event"),
            patch("orchestrator.core.runtime.invocation._append_raw_log_line"),
            patch("orchestrator.core.runtime.invocation._enqueue_runtime_log_line", side_effect=_capture_live_line),
        ):
            payload = invoke_runtime_json(
                runtime=_Runtime(),  # type: ignore[arg-type]
                context=context,
                system_prompt="system",
                user_prompt="user",
            )

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(captured["workflow_id"], "parent_planning:MAB-215")
        self.assertEqual(captured["operation_id"], "operation-jira-child-fanout")
        self.assertEqual(captured["attempt_id"], "attempt-1")
        self.assertEqual(live_lines, [("operation-jira-child-fanout", "attempt-1", "planning line")])

    def test_invoke_runtime_json_with_tools_preserves_operation_attempt_context_and_emits_tool_events(self) -> None:
        sink_contexts: list[tuple[str | None, str | None]] = []
        invocation_events: list[tuple[str, str | None, str | None, dict[str, object]]] = []

        class _Runtime:
            def __init__(self) -> None:
                self._calls = 0

            def run_json(self, **kwargs):  # noqa: ANN003
                self._calls += 1
                if self._calls == 1:
                    on_session_id = kwargs.get("on_session_id")
                    if callable(on_session_id):
                        on_session_id("tool-session-1")
                    return {
                        "type": "tool_request",
                        "tool_name": "decision.read_state",
                        "tool_args": {"issue_key": "GP-124"},
                    }
                return {
                    "type": "final_response",
                    "result": {"gate_status": "clear"},
                }

        context = AgentInvocationContext(
            channel="system",
            tenant_id="tenant-1",
            project_id=None,
            command="policy",
            stage="decision_planner",
            working_dir=".",
            workflow_id="parent_planning:MAB-215",
            operation_id="operation-jira-child-fanout",
            attempt_id="attempt-7",
        )

        def _fake_sink(**kwargs):  # noqa: ANN003
            sink_contexts.append((kwargs.get("operation_id"), kwargs.get("attempt_id")))
            return lambda _stream, _message: None

        def _capture_event(*, context, event_kind: str, payload: dict[str, object]) -> None:  # noqa: ANN001
            invocation_events.append((event_kind, context.operation_id, context.attempt_id, payload))

        with (
            patch("orchestrator.core.runtime.invocation._get_log_writer", return_value=self._Writer()),
            patch("orchestrator.core.runtime.invocation.build_runtime_log_sink", side_effect=_fake_sink),
            patch("orchestrator.core.runtime.invocation._emit_invocation_event", side_effect=_capture_event),
            patch("orchestrator.core.runtime.invocation._append_raw_log_line"),
            patch("orchestrator.core.runtime.invocation._enqueue_runtime_log_line"),
        ):
            payload = invoke_runtime_json_with_tools(
                runtime=_Runtime(),  # type: ignore[arg-type]
                context=context,
                system_prompt="system",
                user_prompt="user",
                allowed_tools={"decision.read_state"},
                execute_tool=lambda _tool_name, _tool_args: {"case": "ok"},
            )

        self.assertEqual(payload, {"gate_status": "clear"})
        self.assertEqual(
            sink_contexts,
            [
                ("operation-jira-child-fanout", "attempt-7"),
                ("operation-jira-child-fanout", "attempt-7"),
            ],
        )
        self.assertIn(
            (
                "tool_request",
                "operation-jira-child-fanout",
                "attempt-7",
                {"message": "Requested tool decision.read_state.", "tool_name": "decision.read_state", "tool_hop": 1, "tool_args": {"issue_key": "GP-124"}},
            ),
            invocation_events,
        )
        self.assertIn(
            (
                "tool_result",
                "operation-jira-child-fanout",
                "attempt-7",
                {"message": "Completed tool decision.read_state.", "tool_name": "decision.read_state", "tool_hop": 1, "ok": True, "tool_result": {"case": "ok"}},
            ),
            invocation_events,
        )

    def test_invoke_runtime_json_with_tools_fails_on_disallowed_tool(self) -> None:
        runtime_calls: list[dict[str, object]] = []

        class _Runtime:
            def run_json(self, **kwargs):  # noqa: ANN003
                runtime_calls.append(kwargs)
                if len(runtime_calls) == 1:
                    return {
                        "type": "tool_request",
                        "tool_name": "decision.read_state",
                        "tool_args": {},
                    }
                return {
                    "type": "final_response",
                    "result": {"message": "continued without disallowed tool"},
                }

        context = AgentInvocationContext(
            channel="system",
            tenant_id="tenant-1",
            project_id=None,
            command="policy",
            stage="decision_planner",
            working_dir=".",
        )

        with (
            patch("orchestrator.core.runtime.invocation._get_log_writer", return_value=self._Writer()),
            self.assertRaises(ToolBridgeProtocolError) as raised,
        ):
            invoke_runtime_json_with_tools(
                runtime=_Runtime(),  # type: ignore[arg-type]
                context=context,
                system_prompt="system",
                user_prompt="user",
                allowed_tools=set(),
                execute_tool=lambda _tool_name, _tool_args: {"ok": True},
            )

        self.assertIn("disallowed tool", str(raised.exception).lower())
        self.assertEqual(len(runtime_calls), 1)

    def test_invoke_runtime_json_with_tools_fails_after_tool_hop_limit(self) -> None:
        runtime_calls: list[dict[str, object]] = []

        class _Runtime:
            def run_json(self, **kwargs):  # noqa: ANN003
                runtime_calls.append(kwargs)
                if len(runtime_calls) <= 2:
                    return {
                        "type": "tool_request",
                        "tool_name": "decision.read_state",
                        "tool_args": {},
                    }
                return {
                    "type": "final_response",
                    "result": {"message": "continued after hop limit"},
                }

        context = AgentInvocationContext(
            channel="system",
            tenant_id="tenant-1",
            project_id=None,
            command="policy",
            stage="decision_planner",
            working_dir=".",
        )

        with (
            patch("orchestrator.core.runtime.invocation._get_log_writer", return_value=self._Writer()),
            self.assertRaises(ToolBridgeExhaustedError) as raised,
        ):
            invoke_runtime_json_with_tools(
                runtime=_Runtime(),  # type: ignore[arg-type]
                context=context,
                system_prompt="system",
                user_prompt="user",
                allowed_tools={"decision.read_state"},
                execute_tool=lambda _tool_name, _tool_args: {"ok": True},
                max_tool_hops=1,
            )

        self.assertIn("tool hop limit exceeded", str(raised.exception).lower())
        self.assertEqual(len(runtime_calls), 2)

    def test_invoke_runtime_json_with_tools_fails_when_bridge_never_finalizes(self) -> None:
        class _Runtime:
            def run_json(self, **_kwargs):  # noqa: ANN003
                return {
                    "type": "tool_request",
                    "tool_name": "decision.read_state",
                    "tool_args": {},
                }

        context = AgentInvocationContext(
            channel="system",
            tenant_id="tenant-1",
            project_id=None,
            command="policy",
            stage="decision_planner",
            working_dir=".",
        )

        with (
            patch("orchestrator.core.runtime.invocation._get_log_writer", return_value=self._Writer()),
            self.assertRaises(ToolBridgeProtocolError) as raised,
        ):
            invoke_runtime_json_with_tools(
                runtime=_Runtime(),  # type: ignore[arg-type]
                context=context,
                system_prompt="system",
                user_prompt="user",
                allowed_tools=set(),
                execute_tool=lambda _tool_name, _tool_args: {"ok": True},
            )

        self.assertIn("disallowed tool", str(raised.exception).lower())

    def test_invoke_runtime_json_with_tools_fails_when_tool_executor_fails(self) -> None:
        class _Runtime:
            def run_json(self, **_kwargs):  # noqa: ANN003
                return {
                    "type": "tool_request",
                    "tool_name": "decision.read_state",
                    "tool_args": {},
                }

        context = AgentInvocationContext(
            channel="system",
            tenant_id="tenant-1",
            project_id=None,
            command="policy",
            stage="decision_planner",
            working_dir=".",
        )

        def _raise_tool_failure(_tool_name: str, _tool_args: dict[str, object]) -> dict[str, object]:
            raise RuntimeError("tool database unavailable")

        with (
            patch("orchestrator.core.runtime.invocation._get_log_writer", return_value=self._Writer()),
            self.assertRaises(ToolExecutionError) as raised,
        ):
            invoke_runtime_json_with_tools(
                runtime=_Runtime(),  # type: ignore[arg-type]
                context=context,
                system_prompt="system",
                user_prompt="user",
                allowed_tools={"decision.read_state"},
                execute_tool=_raise_tool_failure,
            )

        self.assertIn("tool database unavailable", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
