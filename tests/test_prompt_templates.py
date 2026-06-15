from __future__ import annotations

import unittest
from unittest.mock import patch
from pathlib import Path

from orchestrator.core.prompt_templates import render_prompt


class PromptTemplateTests(unittest.TestCase):
    def test_render_prompt_fails_fast_when_jinja_missing(self) -> None:
        with patch("orchestrator.core.prompt_templates._jinja_environment", side_effect=RuntimeError("jinja2 missing")):
            with self.assertRaisesRegex(RuntimeError, "jinja2 missing"):
                render_prompt("workflow/pm_user.j2")

    def test_render_prompt_uses_template_environment(self) -> None:
        class _Template:
            def render(self, **context):  # noqa: ANN003
                return f"Tenant={context['tenant_id']} Skills={context['skills']}"

        class _Env:
            def get_template(self, template_name: str):  # noqa: ANN001
                self.template_name = template_name
                return _Template()

        fake_env = _Env()
        with patch("orchestrator.core.prompt_templates._jinja_environment", return_value=fake_env):
            rendered = render_prompt("workflow/pm_user.j2", tenant_id="tenant-1")
        self.assertEqual(fake_env.template_name, "workflow/pm_user.j2")
        self.assertEqual(rendered, "Tenant=tenant-1 Skills={}")

    def test_render_prompt_injects_required_skills_for_review_prompt(self) -> None:
        rendered = render_prompt("workflow/review_system.j2")

        self.assertIn('<skill name="staff-engineer-review">', rendered)
        self.assertIn("# Skill: Staff Engineering Review", rendered)
        self.assertIn("Autonomy Contract", rendered)
        self.assertIn("Workflow-stage adaptation:", rendered)

    def test_render_prompt_cannot_override_required_review_skills(self) -> None:
        rendered = render_prompt("workflow/review_system.j2", skills={})

        self.assertIn("# Skill: Staff Engineering Review", rendered)

    def test_pm_user_prompt_enforces_macos_signals_for_ios_work(self) -> None:
        prompt_path = (
            Path(__file__).resolve().parents[1]
            / "orchestrator"
            / "prompts"
            / "workflow"
            / "pm_user.j2"
        )
        prompt_text = prompt_path.read_text(encoding="utf-8")
        self.assertIn("mandatory macos signals", prompt_text)
        self.assertIn("xcodebuild", prompt_text)
        self.assertIn("XCUITest", prompt_text)
        self.assertIn('Never output "linux" when mandatory macos signals exist', prompt_text)
        self.assertIn("QA capture target constraints", prompt_text)
        self.assertIn("provider_available=false", prompt_text)
        self.assertIn("required_worker_platform", prompt_text)
        self.assertIn("Include at least two meaningful `variants` for every demo requirement", prompt_text)
        self.assertIn("QA records several walkthroughs", prompt_text)
        self.assertIn("implementation, test, or review capability", prompt_text)
        self.assertIn("do not requeue PM solely because a QA demo capture target", prompt_text)

    def test_qa_user_prompt_is_capture_target_oriented_not_browser_only(self) -> None:
        prompt_path = (
            Path(__file__).resolve().parents[1]
            / "orchestrator"
            / "prompts"
            / "workflow"
            / "qa_user.j2"
        )
        prompt_text = prompt_path.read_text(encoding="utf-8")
        self.assertIn("Browser capture reference", prompt_text)
        self.assertIn('For `capture_target="browser"`', prompt_text)
        self.assertIn('For `capture_target="ios"`, `capture_target="android"`, or `capture_target="desktop"`', prompt_text)
        self.assertIn("use `id=<accessibility_identifier>`", prompt_text)
        self.assertIn("use `text=<visible text>`", prompt_text)
        self.assertIn("never emit an unprefixed native selector", prompt_text)
        self.assertIn("prefer interaction patterns and accessibility identifiers already present in the local repository", prompt_text)
        self.assertIn("Native selector catalog", prompt_text)
        self.assertIn("authoritative", prompt_text)
        self.assertIn("do not use `fill` on buttons, labels, or static text", prompt_text)
        self.assertIn("Do not make required assertions against intentionally transient native surfaces", prompt_text)
        self.assertIn("never use `id=splash_screen` as a required assertion or click target", prompt_text)
        self.assertNotIn("Preview URL:", prompt_text)
        self.assertIn("does not have native `web.search`, `web.fetch`, `browser.open`, or `browser.snapshot` available", prompt_text)
        self.assertIn("Never issue an empty search/fetch request", prompt_text)

    def test_qa_system_prompt_keeps_scenario_planning_local_first(self) -> None:
        prompt_path = (
            Path(__file__).resolve().parents[1]
            / "orchestrator"
            / "prompts"
            / "workflow"
            / "qa_system.j2"
        )
        prompt_text = prompt_path.read_text(encoding="utf-8")
        self.assertIn("Plan from the supplied run context", prompt_text)
        self.assertIn("does not have native web search or browser inspection available", prompt_text)
        self.assertIn("Only use the governed tools that are actually listed", prompt_text)
        self.assertIn("never issue an empty search or fetch request", prompt_text)

    def test_pm_user_prompt_defines_decision_state_evidence_contract(self) -> None:
        prompt_path = (
            Path(__file__).resolve().parents[1]
            / "orchestrator"
            / "prompts"
            / "workflow"
            / "pm_user.j2"
        )
        prompt_text = prompt_path.read_text(encoding="utf-8")
        self.assertIn("`decision_state` means the persisted Decision Gate / clarification state", prompt_text)
        self.assertIn("The only authoritative way to determine `decision_state` is the `decision.read_state` tool", prompt_text)
        self.assertIn("If `decision.read_state` succeeds and reports that no prior decision state exists, that is a valid result", prompt_text)
        self.assertIn('If you can proceed, set outcome to `continue`', prompt_text)
        self.assertIn('If you cannot proceed because of a real blocker, set outcome to `blocked` or `waiting_for_input`', prompt_text)
        self.assertIn("Do not stop planning solely because a tool call failed", prompt_text)

    def test_pm_user_prompt_defines_run_request_human_input_contract(self) -> None:
        prompt_path = (
            Path(__file__).resolve().parents[1]
            / "orchestrator"
            / "prompts"
            / "workflow"
            / "pm_user.j2"
        )
        prompt_text = prompt_path.read_text(encoding="utf-8")
        self.assertIn("`run.request_human_input` requires this exact argument shape", prompt_text)
        self.assertIn('"request_type":"<stable snake_case type>"', prompt_text)
        self.assertIn("Never emit top-level `questions`", prompt_text)
        self.assertIn('use `request_type="decision_gate_clarification"`', prompt_text)

    def test_pm_prompts_require_plain_language_install_approval_contract(self) -> None:
        prompts_dir = Path(__file__).resolve().parents[1] / "orchestrator" / "prompts" / "workflow"
        pm_system = (prompts_dir / "pm_system.j2").read_text(encoding="utf-8")
        pm_user = (prompts_dir / "pm_user.j2").read_text(encoding="utf-8")

        for prompt_text in (pm_system, pm_user):
            self.assertIn("source project", prompt_text)
            self.assertIn("Master Builder", prompt_text)
            self.assertIn("secrets", prompt_text)
            self.assertIn("yes/no", prompt_text)

        self.assertIn("operator_decision", pm_user)
        self.assertIn("source_project_changes", pm_user)
        self.assertIn("master_builder_changes", pm_user)
        self.assertIn("secrets_or_bindings_needed", pm_user)
        self.assertIn("Do not ask the PM for API field names", pm_user)

    def test_pm_prompts_treat_repo_governance_as_preloaded_context(self) -> None:
        prompts_dir = Path(__file__).resolve().parents[1] / "orchestrator" / "prompts" / "workflow"
        pm_system = (prompts_dir / "pm_system.j2").read_text(encoding="utf-8")
        pm_user = (prompts_dir / "pm_user.j2").read_text(encoding="utf-8")

        self.assertIn("already available enforcement context", pm_system)
        self.assertIn("do not spend tool hops rereading `.codex/**`, `AGENTS.md`, or skill files", pm_system)
        self.assertIn("Preloaded repository governance context", pm_user)
        self.assertIn("runtime already loaded and enforced the repository policy/standards/operating context", pm_user)
        self.assertIn("Do not spend tool hops rereading `.codex/**`, `AGENTS.md`, or workflow skill files", pm_user)
        self.assertIn("ticket-relevant product files, tests, and build metadata", pm_user)

    def test_dev_prompts_treat_repo_governance_as_preloaded_context(self) -> None:
        prompts_dir = Path(__file__).resolve().parents[1] / "orchestrator" / "prompts" / "workflow"
        dev_system = (prompts_dir / "dev_system.j2").read_text(encoding="utf-8")
        dev_user = (prompts_dir / "dev_user.j2").read_text(encoding="utf-8")

        self.assertIn("already available enforcement context", dev_system)
        self.assertIn("do not spend shell commands or tool hops rereading `.codex/**`, `AGENTS.md`, or skill files", dev_system)
        self.assertIn("Preloaded repository governance context", dev_user)
        self.assertIn("runtime already loaded and enforced repository policy, engineering standards, operating rules, and required workflow skills", dev_user)
        self.assertIn("Do not spend shell commands or tool hops rereading `.codex/**`, `AGENTS.md`, or workflow skill files", dev_user)
        self.assertIn("ticket-relevant source files, tests, build targets, simulator/runtime evidence, and the current published diff", dev_user)

    def test_test_prompts_treat_repo_governance_as_preloaded_context(self) -> None:
        prompts_dir = Path(__file__).resolve().parents[1] / "orchestrator" / "prompts" / "workflow"
        test_system = (prompts_dir / "test_system.j2").read_text(encoding="utf-8")
        test_user = (prompts_dir / "test_user.j2").read_text(encoding="utf-8")

        self.assertIn("already available enforcement context", test_system)
        self.assertIn("do not spend shell commands or tool hops rereading `.codex/**`, `AGENTS.md`, `tasks/lessons.md`, or skill files", test_system)
        self.assertIn("Preloaded repository governance context", test_user)
        self.assertIn("runtime already loaded and enforced repository policy, engineering standards, operating rules, and required workflow skills", test_user)
        self.assertIn("Do not spend shell commands or tool hops rereading `.codex/**`, `AGENTS.md`, `tasks/lessons.md`, or workflow skill files", test_user)
        self.assertIn("current-head diff evidence, simulator/runtime discovery, targeted acceptance checks", test_user)

    def test_test_user_prompt_requires_changed_scope_before_full_suite(self) -> None:
        prompt_path = (
            Path(__file__).resolve().parents[1]
            / "orchestrator"
            / "prompts"
            / "workflow"
            / "test_user.j2"
        )
        prompt_text = prompt_path.read_text(encoding="utf-8")
        self.assertIn("execute targeted tests/checks only for the code you changed first", prompt_text)
        self.assertIn("Do not default to broad `xcodebuild test`", prompt_text)
        self.assertIn("Current published diff paths", prompt_text)
        self.assertIn("Requires current-head acceptance evidence", prompt_text)
        self.assertIn("Scope Step A to the currently published PR head", prompt_text)
        self.assertIn("Prior evidence from earlier attempts is supporting context only", prompt_text)
        self.assertIn("Never run multiple Apple-native validation commands concurrently", prompt_text)
        self.assertIn("validation_scope", prompt_text)
        self.assertIn("Never use `targeted_only`", prompt_text)
        self.assertIn("If you cannot identify a targeted test", prompt_text)
        self.assertIn("inspect the allowed tool list", prompt_text)
        self.assertIn("before concluding the run is blocked", prompt_text)
        self.assertIn("xcrun simctl list runtimes", prompt_text)
        self.assertIn("Do not hardcode an iPhone model name", prompt_text)
        self.assertIn("CODE_SIGNING_ALLOWED=NO", prompt_text)
        self.assertIn('If you return outcome="blocked" or outcome="waiting_for_input", `blocker_message` is required', prompt_text)

    def test_test_system_prompt_requires_blocker_message_for_blocked_or_waiting(self) -> None:
        prompt_path = (
            Path(__file__).resolve().parents[1]
            / "orchestrator"
            / "prompts"
            / "workflow"
            / "test_system.j2"
        )
        prompt_text = prompt_path.read_text(encoding="utf-8")
        self.assertIn('If you return outcome="blocked" or outcome="waiting_for_input", `blocker_message` is required', prompt_text)

    def test_dev_and_review_prompts_require_pr_head_to_use_integration_branch(self) -> None:
        prompts_dir = Path(__file__).resolve().parents[1] / "orchestrator" / "prompts" / "workflow"
        dev_prompt_text = (prompts_dir / "dev_user.j2").read_text(encoding="utf-8")
        review_prompt_text = (prompts_dir / "review_user.j2").read_text(encoding="utf-8")

        self.assertIn("Current published PR URL", dev_prompt_text)
        self.assertIn("Current published diff paths", dev_prompt_text)
        self.assertIn("current published PR head as the remediation baseline", dev_prompt_text)
        self.assertIn("Do not revert already-published PR-head changes", dev_prompt_text)
        self.assertIn("Keep repo housekeeping and self-improvement files out of product publication candidates", dev_prompt_text)
        self.assertIn("Do not edit `tasks/lessons.md`", dev_prompt_text)
        self.assertIn("Validate in changed scope first", dev_prompt_text)
        self.assertIn("Prefer file-scoped or target-scoped lint/test commands", dev_prompt_text)
        self.assertIn("Do not let unrelated pre-existing repo-wide validation failures", dev_prompt_text)
        self.assertIn("background repo debt", dev_prompt_text)
        self.assertIn("Never run multiple Apple-native validation commands concurrently", dev_prompt_text)
        self.assertIn("Integration branch as the only valid PR head branch", dev_prompt_text)
        self.assertIn("Never open or update a PR from the Execution branch", dev_prompt_text)
        self.assertIn("github.push_branch", dev_prompt_text)
        self.assertIn("Do not use raw `git push`", dev_prompt_text)
        self.assertIn("Do not use raw `gh` CLI commands", dev_prompt_text)
        self.assertIn("treat it as executable through this tool bridge", dev_prompt_text)
        self.assertIn("Do not use native `tool_search`", dev_prompt_text)
        self.assertIn("The allowed governed list is authoritative for this bridge", dev_prompt_text)
        self.assertIn("An empty or irrelevant `tool_search` result is not evidence", dev_prompt_text)
        self.assertIn("unless you issued the corresponding `tool_request` and it failed", dev_prompt_text)
        self.assertIn("inspect the allowed tool list", dev_prompt_text)
        self.assertIn("Integration branch as the canonical PR head branch", review_prompt_text)
        self.assertIn("Execution branch (`run/...`)", review_prompt_text)
        self.assertIn("github.push_branch", review_prompt_text)
        self.assertIn("Do not use raw `git push`", review_prompt_text)
        self.assertIn("Do not use raw `gh` CLI commands", review_prompt_text)
        self.assertIn("treat it as executable through this tool bridge", review_prompt_text)
        self.assertIn("Do not use native `tool_search`", review_prompt_text)
        self.assertIn("The allowed governed list is authoritative for this bridge", review_prompt_text)
        self.assertIn("An empty or irrelevant `tool_search` result is not evidence", review_prompt_text)
        self.assertIn("unless you issued the corresponding `tool_request` and it failed", review_prompt_text)
        self.assertIn("inspect the allowed tool list", review_prompt_text)
        self.assertIn("Treat repo housekeeping and self-improvement files as out of scope for product acceptance", review_prompt_text)
        self.assertIn("If the published diff includes `tasks/lessons.md`", review_prompt_text)
        self.assertIn("Test validation scope", review_prompt_text)
        self.assertIn("Current published diff paths", review_prompt_text)
        self.assertIn("Treat the Test stage as the primary validation authority", review_prompt_text)
        self.assertIn("current_head_acceptance", review_prompt_text)
        self.assertIn("background repo risk", review_prompt_text)

    def test_dev_and_review_system_prompts_make_remote_publication_tool_owned(self) -> None:
        prompts_dir = Path(__file__).resolve().parents[1] / "orchestrator" / "prompts" / "workflow"
        dev_system_prompt = (prompts_dir / "dev_system.j2").read_text(encoding="utf-8")
        review_system_prompt = (prompts_dir / "review_system.j2").read_text(encoding="utf-8")

        self.assertIn("Remote branch publication and PR creation/update are governed actions", dev_system_prompt)
        self.assertIn("instead of raw `git push`", dev_system_prompt)
        self.assertIn("Remote branch publication and PR creation/update are governed actions", review_system_prompt)
        self.assertIn("instead of raw `git push`", review_system_prompt)

    def test_review_system_prompt_uses_staff_engineer_review_protocol(self) -> None:
        prompts_dir = Path(__file__).resolve().parents[1] / "orchestrator" / "prompts" / "workflow"
        review_system_prompt = (prompts_dir / "review_system.j2").read_text(encoding="utf-8")

        self.assertIn("Apply the `staff-engineer-review` skill below as your mandatory review method", review_system_prompt)
        self.assertIn('{{ skills["staff-engineer-review"] }}', review_system_prompt)
        self.assertIn("Workflow-stage adaptation:", review_system_prompt)
        self.assertIn("Preserve the ReviewResult JSON contract", review_system_prompt)

    def test_workflow_stage_prompts_treat_tool_base_as_part_of_diagnosis(self) -> None:
        prompts_dir = Path(__file__).resolve().parents[1] / "orchestrator" / "prompts" / "workflow"
        for prompt_name in ("pm_user.j2", "dev_system.j2", "test_system.j2", "review_system.j2"):
            prompt_text = (prompts_dir / prompt_name).read_text(encoding="utf-8")
            self.assertIn("inspect the allowed tool list", prompt_text)
            self.assertIn("before concluding the blocker is real", prompt_text)
        for prompt_name in ("dev_system.j2", "test_system.j2", "review_system.j2"):
            prompt_text = (prompts_dir / prompt_name).read_text(encoding="utf-8")
            self.assertIn("If a tool call fails or returns unavailable, treat that as advisory context", prompt_text)

    def test_execution_stage_system_prompts_frame_tools_as_diagnostic_catalog(self) -> None:
        prompts_dir = Path(__file__).resolve().parents[1] / "orchestrator" / "prompts" / "workflow"
        for prompt_name in ("dev_system.j2", "test_system.j2", "review_system.j2"):
            prompt_text = (prompts_dir / prompt_name).read_text(encoding="utf-8")
            self.assertIn("authoritative path for governed context and diagnosis", prompt_text)
            self.assertNotIn("governed side effects", prompt_text)

    def test_execution_stage_prompts_require_troubleshooting_section_for_blocking_outputs(self) -> None:
        prompts_dir = Path(__file__).resolve().parents[1] / "orchestrator" / "prompts" / "workflow"
        for prompt_name in ("dev_system.j2", "dev_user.j2", "test_system.j2", "test_user.j2", "review_system.j2", "review_user.j2"):
            prompt_text = (prompts_dir / prompt_name).read_text(encoding="utf-8")
            self.assertIn("Troubleshooting:", prompt_text)
            self.assertIn("concrete operator actions", prompt_text)

    def test_voice_room_engineer_prompt_enforces_spoken_style(self) -> None:
        system_prompt_path = (
            Path(__file__).resolve().parents[1]
            / "orchestrator"
            / "prompts"
            / "discord"
            / "voice_room_engineer_system.j2"
        )
        user_prompt_path = (
            Path(__file__).resolve().parents[1]
            / "orchestrator"
            / "prompts"
            / "discord"
            / "voice_room_engineer_user.j2"
        )
        system_prompt_text = system_prompt_path.read_text(encoding="utf-8")
        user_prompt_text = user_prompt_path.read_text(encoding="utf-8")

        self.assertIn("answering someone out loud", system_prompt_text)
        self.assertIn("Avoid phrases like \"Engineering-wise\"", system_prompt_text)
        self.assertIn("Use clean, grammatical sentences with one main idea per sentence.", system_prompt_text)
        self.assertIn("Avoid opening with filler like \"Today\", \"Right now\", or \"Currently\"", system_prompt_text)
        self.assertIn("Write for speech, not for a sprint update.", user_prompt_text)
        self.assertIn("Include ticket numbers when they materially anchor the answer.", user_prompt_text)
        self.assertIn("Mention them naturally in spoken language", user_prompt_text)
        self.assertIn("Use clean, grammatical sentences with one main idea per sentence.", user_prompt_text)

    def test_voice_room_persona_prompts_treat_transcript_as_direct_request(self) -> None:
        prompts_dir = Path(__file__).resolve().parents[1] / "orchestrator" / "prompts" / "discord"
        for persona in ("architect", "engineer", "pm", "qa", "security"):
            system_prompt_text = (prompts_dir / f"voice_room_{persona}_system.j2").read_text(encoding="utf-8")
            user_prompt_text = (prompts_dir / f"voice_room_{persona}_user.j2").read_text(encoding="utf-8")

            self.assertIn("Treat the routed transcript as the user's direct request to you.", system_prompt_text)
            self.assertIn("direct", system_prompt_text)
            self.assertIn("Treat the transcript as the user's direct request", user_prompt_text)
            self.assertIn("Prefer the best direct", user_prompt_text)

    def test_voice_room_pm_prompt_avoids_scope_triage_fallback(self) -> None:
        prompts_dir = Path(__file__).resolve().parents[1] / "orchestrator" / "prompts" / "discord"
        system_prompt_text = (prompts_dir / "voice_room_pm_system.j2").read_text(encoding="utf-8")
        user_prompt_text = (prompts_dir / "voice_room_pm_user.j2").read_text(encoding="utf-8")

        self.assertIn("Do not default to intake or scope-triage language", system_prompt_text)
        self.assertIn("Prefer the best direct recommendation, decision, or next step", user_prompt_text)
        self.assertIn("Do not fall back to intake or scope-triage language", user_prompt_text)
        self.assertNotIn("If the brief is incomplete", user_prompt_text)

    def test_pm_persona_prompts_encode_customer_vision_and_outcome_ownership(self) -> None:
        discord_prompts_dir = Path(__file__).resolve().parents[1] / "orchestrator" / "prompts" / "discord"
        workflow_prompts_dir = Path(__file__).resolve().parents[1] / "orchestrator" / "prompts" / "workflow"

        pm_answer_system = (discord_prompts_dir / "pm_answer_system.j2").read_text(encoding="utf-8")
        voice_room_pm_system = (discord_prompts_dir / "voice_room_pm_system.j2").read_text(encoding="utf-8")
        workflow_pm_system = (workflow_prompts_dir / "pm_system.j2").read_text(encoding="utf-8")

        for prompt_text in (pm_answer_system, voice_room_pm_system):
            self.assertIn("Listen deeply to customers and stakeholders", prompt_text)
            self.assertIn("Hold a clear product vision", prompt_text)
            self.assertIn("Be decisive but open to evidence and feedback", prompt_text)
            self.assertIn("Bridge engineering, design, and go-to-market teams", prompt_text)
            self.assertIn("Own outcomes and value delivery", prompt_text)

        self.assertIn("Ground planning decisions in user value and business outcomes", workflow_pm_system)

    def test_decision_planner_prompts_forbid_direct_db_inspection(self) -> None:
        prompts_dir = Path(__file__).resolve().parents[1] / "orchestrator" / "prompts" / "policy"
        system_prompt_text = (prompts_dir / "decision_planner_system.j2").read_text(encoding="utf-8")
        user_prompt_text = (prompts_dir / "decision_planner_user.j2").read_text(encoding="utf-8")

        self.assertIn("Never improvise direct database inspection", system_prompt_text)
        self.assertIn("Persisted decision state must be read via `decision.read_state`", user_prompt_text)
        self.assertIn("If `decision.read_state` or another allowed tool fails", user_prompt_text)
        self.assertIn('"type":"tool_request"', user_prompt_text)
        self.assertIn('"type":"final_response"', user_prompt_text)
        self.assertNotIn("Agent tool command:", user_prompt_text)

    def test_decision_gate_prompts_require_pm_facing_business_language(self) -> None:
        prompts_dir = Path(__file__).resolve().parents[1] / "orchestrator" / "prompts" / "policy"
        planner_prompt = (prompts_dir / "decision_planner_system.j2").read_text(encoding="utf-8")
        reply_prompt = (prompts_dir / "decision_reply_system.j2").read_text(encoding="utf-8")

        for prompt_text in (planner_prompt, reply_prompt):
            self.assertIn("business mapping", prompt_text)
            self.assertIn("Do not ask", prompt_text)
            self.assertIn("implementation identifiers", prompt_text)
            self.assertIn("Engineering can map", prompt_text)
            self.assertIn("Translate", prompt_text)

        self.assertIn("a paid invoice activates access", reply_prompt)
        self.assertIn("what should happen to customer access", reply_prompt)
        self.assertIn("I need the product rule for how HubSpot billing controls access", reply_prompt)
        self.assertIn("Do not ask for \"record types\"", reply_prompt)
        self.assertIn("Do not call web search or any native runtime tool", reply_prompt)
        self.assertIn("Do not call web search or any native runtime tool", planner_prompt)
        self.assertIn("allowed `tool_request` contract only", planner_prompt)
        self.assertIn("Do not copy", reply_prompt)
        self.assertNotIn("hs_invoice_id", reply_prompt)
        self.assertNotIn("dealstage", reply_prompt)
        self.assertNotIn("term_start", reply_prompt)

    def test_workflow_stage_prompts_forbid_direct_db_inspection(self) -> None:
        prompts_dir = Path(__file__).resolve().parents[1] / "orchestrator" / "prompts" / "workflow"
        expected_text = (
            "Do not inspect application or planner state directly with Python, shell, SQL, or raw database clients;"
        )
        for prompt_name in ("pm_system.j2", "dev_system.j2", "test_system.j2", "review_system.j2"):
            prompt_text = (prompts_dir / prompt_name).read_text(encoding="utf-8")
            self.assertIn(expected_text, prompt_text)

        for prompt_name in ("pm_user.j2", "dev_user.j2", "test_user.j2", "review_user.j2"):
            prompt_text = (prompts_dir / prompt_name).read_text(encoding="utf-8")
            self.assertIn('"type":"tool_request"', prompt_text)
            self.assertIn('"type":"final_response"', prompt_text)
            self.assertNotIn("Agent tool command:", prompt_text)


if __name__ == "__main__":
    unittest.main()
