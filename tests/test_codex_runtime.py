import unittest

from orchestrator.core.codex_runtime import CodexRuntime, CodexRuntimeError


class CodexRuntimeTests(unittest.TestCase):
    def test_run_json_accepts_raw_json(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            timeout_seconds=30,
            max_output_tokens=400,
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
            _request=lambda _system, _user: """```json\n{\"message\":\"ok\"}\n```""",
        )

        payload = runtime.run_json(system_prompt="sys", user_prompt="user")

        self.assertEqual(payload, {"message": "ok"})

    def test_run_json_rejects_non_json(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            timeout_seconds=30,
            max_output_tokens=400,
            _request=lambda _system, _user: "not json",
        )

        with self.assertRaises(CodexRuntimeError):
            runtime.run_json(system_prompt="sys", user_prompt="user")


if __name__ == "__main__":
    unittest.main()
