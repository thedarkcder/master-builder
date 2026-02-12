from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from orchestrator.core.codex_runtime import (
    CodexRuntime,
    CodexRuntimeError,
    _extract_json_payload,
    build_codex_runtime,
)


class ExtractJsonPayloadTests(unittest.TestCase):
    def test_extract_json_payload_variants(self) -> None:
        self.assertEqual(_extract_json_payload('{"ok": true}'), {"ok": True})
        self.assertEqual(_extract_json_payload('```json\n{"x":1}\n```'), {"x": 1})
        self.assertEqual(_extract_json_payload('prefix {"k":"v"} suffix'), {"k": "v"})

    def test_extract_json_payload_errors(self) -> None:
        with self.assertRaises(CodexRuntimeError):
            _extract_json_payload("```json\\n{bad}\\n```")
        with self.assertRaises(CodexRuntimeError):
            _extract_json_payload("no json here")


class CodexRuntimeTests(unittest.TestCase):
    def test_run_text_and_json(self) -> None:
        runtime = CodexRuntime(
            model="m",
            timeout_seconds=10,
            max_output_tokens=1000,
            command="override",
            _request=lambda _s, _u: " {\"ok\": true} ",
            enforcement_context="ctx",
        )
        text = runtime.run_text(system_prompt="sys", user_prompt="usr")
        self.assertEqual(text, '{"ok": true}')
        payload = runtime.run_json(system_prompt="sys", user_prompt="usr")
        self.assertEqual(payload, {"ok": True})

        empty_runtime = CodexRuntime(
            model="m",
            timeout_seconds=10,
            max_output_tokens=1000,
            command="override",
            _request=lambda _s, _u: "   ",
        )
        with self.assertRaises(CodexRuntimeError):
            empty_runtime.run_text(system_prompt="s", user_prompt="u")

        non_dict_runtime = CodexRuntime(
            model="m",
            timeout_seconds=10,
            max_output_tokens=1000,
            command="override",
            _request=lambda _s, _u: "[1,2]",
        )
        with self.assertRaises(CodexRuntimeError):
            non_dict_runtime.run_json(system_prompt="s", user_prompt="u")


class BuildCodexRuntimeTests(unittest.TestCase):
    def _settings(self) -> SimpleNamespace:
        return SimpleNamespace(
            codex_model="gpt-5-codex",
            codex_timeout_seconds=30,
            codex_max_output_tokens=4096,
            codex_cli_command="codex",
        )

    def test_build_with_request_override(self) -> None:
        settings = self._settings()
        with patch("orchestrator.core.codex_runtime.build_agent_enforcement_context", return_value="ctx"):
            runtime = build_codex_runtime(
                settings=settings,
                request_override=lambda _s, _u: "ok",
            )
        self.assertEqual(runtime.command, "override")
        self.assertEqual(runtime.enforcement_context, "ctx")

    def test_build_enforcement_failure(self) -> None:
        settings = self._settings()
        with patch("orchestrator.core.codex_runtime.build_agent_enforcement_context", side_effect=ValueError("bad")):
            with self.assertRaises(CodexRuntimeError):
                build_codex_runtime(settings=settings)

    def test_build_cli_command_validation(self) -> None:
        settings = self._settings()
        settings.codex_cli_command = ""
        with patch("orchestrator.core.codex_runtime.build_agent_enforcement_context", return_value="ctx"):
            with self.assertRaises(CodexRuntimeError):
                build_codex_runtime(settings=settings)

        settings = self._settings()
        with (
            patch("orchestrator.core.codex_runtime.build_agent_enforcement_context", return_value="ctx"),
            patch("orchestrator.core.codex_runtime.shutil.which", return_value=None),
        ):
            with self.assertRaises(CodexRuntimeError):
                build_codex_runtime(settings=settings)

    def test_cli_request_success_and_fallbacks(self) -> None:
        settings = self._settings()

        def fake_run_write_output(args, **kwargs):  # noqa: ANN001
            output_idx = args.index("--output-last-message") + 1
            output_path = Path(args[output_idx])
            output_path.write_text("json-output", encoding="utf-8")
            return SimpleNamespace(returncode=0, stderr="", stdout="")

        with (
            patch("orchestrator.core.codex_runtime.build_agent_enforcement_context", return_value="ctx"),
            patch("orchestrator.core.codex_runtime.shutil.which", return_value="/usr/bin/codex"),
            patch("orchestrator.core.codex_runtime.subprocess.run", side_effect=fake_run_write_output),
        ):
            runtime = build_codex_runtime(settings=settings)
            self.assertEqual(runtime.run_text(system_prompt="s", user_prompt="u"), "json-output")

        def fake_run_stdout(args, **kwargs):  # noqa: ANN001
            output_idx = args.index("--output-last-message") + 1
            output_path = Path(args[output_idx])
            output_path.write_text("", encoding="utf-8")
            return SimpleNamespace(returncode=0, stderr="", stdout="stdout-output")

        with (
            patch("orchestrator.core.codex_runtime.build_agent_enforcement_context", return_value="ctx"),
            patch("orchestrator.core.codex_runtime.shutil.which", return_value="/usr/bin/codex"),
            patch("orchestrator.core.codex_runtime.subprocess.run", side_effect=fake_run_stdout),
        ):
            runtime = build_codex_runtime(settings=settings)
            self.assertEqual(runtime.run_text(system_prompt="s", user_prompt="u"), "stdout-output")

    def test_cli_request_failures(self) -> None:
        settings = self._settings()

        with (
            patch("orchestrator.core.codex_runtime.build_agent_enforcement_context", return_value="ctx"),
            patch("orchestrator.core.codex_runtime.shutil.which", return_value="/usr/bin/codex"),
            patch(
                "orchestrator.core.codex_runtime.subprocess.run",
                return_value=SimpleNamespace(returncode=1, stderr="auth required", stdout=""),
            ),
        ):
            runtime = build_codex_runtime(settings=settings)
            with self.assertRaises(CodexRuntimeError):
                runtime.run_text(system_prompt="s", user_prompt="u")

        with (
            patch("orchestrator.core.codex_runtime.build_agent_enforcement_context", return_value="ctx"),
            patch("orchestrator.core.codex_runtime.shutil.which", return_value="/usr/bin/codex"),
            patch(
                "orchestrator.core.codex_runtime.subprocess.run",
                return_value=SimpleNamespace(returncode=2, stderr="boom", stdout=""),
            ),
        ):
            runtime = build_codex_runtime(settings=settings)
            with self.assertRaises(CodexRuntimeError):
                runtime.run_text(system_prompt="s", user_prompt="u")

        def fake_run_empty(args, **kwargs):  # noqa: ANN001
            output_idx = args.index("--output-last-message") + 1
            output_path = Path(args[output_idx])
            output_path.write_text("", encoding="utf-8")
            return SimpleNamespace(returncode=0, stderr="", stdout="")

        with (
            patch("orchestrator.core.codex_runtime.build_agent_enforcement_context", return_value="ctx"),
            patch("orchestrator.core.codex_runtime.shutil.which", return_value="/usr/bin/codex"),
            patch("orchestrator.core.codex_runtime.subprocess.run", side_effect=fake_run_empty),
        ):
            runtime = build_codex_runtime(settings=settings)
            with self.assertRaises(CodexRuntimeError):
                runtime.run_text(system_prompt="s", user_prompt="u")


if __name__ == "__main__":
    unittest.main()
