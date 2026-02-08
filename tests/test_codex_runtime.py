import unittest
from unittest.mock import patch

from orchestrator.core.codex_runtime import CodexRuntime, CodexRuntimeError, build_codex_runtime
from orchestrator.core.config import Settings


class CodexRuntimeTests(unittest.TestCase):
    def test_run_json_accepts_raw_json(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            timeout_seconds=30,
            max_output_tokens=400,
            command="override",
            _request=lambda _system, _user: '{"message":"ok","value":1}',
        )

        payload = runtime.run_json(system_prompt="sys", user_prompt="user")

        self.assertEqual(payload["message"], "ok")
        self.assertEqual(payload["value"], 1)

    def test_run_json_accepts_code_fence_json(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            timeout_seconds=30,
            max_output_tokens=400,
            command="override",
            _request=lambda _system, _user: """```json\n{\"message\":\"ok\"}\n```""",
        )

        payload = runtime.run_json(system_prompt="sys", user_prompt="user")

        self.assertEqual(payload, {"message": "ok"})

    def test_run_json_rejects_non_json(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            timeout_seconds=30,
            max_output_tokens=400,
            command="override",
            _request=lambda _system, _user: "not json",
        )

        with self.assertRaises(CodexRuntimeError):
            runtime.run_json(system_prompt="sys", user_prompt="user")

    def test_build_runtime_fails_when_codex_cli_missing(self) -> None:
        settings = Settings(
            codex_cli_command="missing-codex",
            codex_model="gpt-5-codex",
            codex_timeout_seconds=30,
            codex_max_output_tokens=400,
        )
        with patch("orchestrator.core.codex_runtime.shutil.which", return_value=None):
            with self.assertRaises(CodexRuntimeError):
                build_codex_runtime(settings=settings)

    def test_build_runtime_uses_request_override(self) -> None:
        settings = Settings(codex_cli_command="codex")
        captured: dict[str, str] = {}

        def _request(system_prompt: str, _user_prompt: str) -> str:
            captured["system_prompt"] = system_prompt
            return '{"ok":true}'

        runtime = build_codex_runtime(
            settings=settings,
            request_override=_request,
        )
        payload = runtime.run_json(system_prompt="sys", user_prompt="user")
        self.assertEqual(payload, {"ok": True})
        self.assertIn("Run enforcement context (must apply):", captured["system_prompt"])
        self.assertIn("Agent role instructions:", captured["system_prompt"])
        self.assertIn("sys", captured["system_prompt"])


if __name__ == "__main__":
    unittest.main()
