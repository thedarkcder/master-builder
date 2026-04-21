from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator.core.specialist_planning import (
    PLANNING_STATE_BLOCKED,
    PLANNING_STATE_COMPLETED,
    PLANNING_STATE_ENGINEERING,
    PLANNING_STATE_SECURITY,
    PLANNING_STATE_TEST,
    SpecialistPlanningRequest,
    build_runtime_seed_planning_package,
    run_specialist_planning_fanout,
)


class SpecialistPlanningTests(unittest.TestCase):
    def _request(self) -> SpecialistPlanningRequest:
        return SpecialistPlanningRequest(
            tenant_id="tenant-1",
            project_id="project-1",
            parent_issue_key="PM-42",
            parent_summary="Share the app with friends",
            parent_description="PM-complete product brief",
            product_brief={
                "objective": "Let users share the app with friends",
                "user_value": "Users invite friends and expand adoption",
                "acceptance_criteria": [
                    "Users can share from Profile",
                    "The link opens the store if the app is not installed",
                ],
                "scope_in": ["Share entry point", "Invite link"],
                "scope_out": ["Referral rewards"],
                "open_questions": [],
            },
            project_keys=("MAB",),
            related_issues=({"key": "MAB-19", "summary": "Auth session refresh"},),
            status_counts={"To Do": 2},
            github_context={"repos": ["acme/app"]},
            conversation_history=({"question": "What do friends receive?", "answer": "A store link"},),
            working_dir="/tmp/workspace",
        )

    def test_fanout_runs_all_specialists_and_aggregates_completed_state(self) -> None:
        request = self._request()
        selectors: list[str] = []
        prompts: list[tuple[str, dict[str, object]]] = []

        def _runtime_for_selector(selector: str):  # noqa: ANN001
            selectors.append(selector)
            return SimpleNamespace(selector=selector)

        def _render_prompt(template_name: str, **kwargs):  # noqa: ANN001
            prompts.append((template_name, kwargs))
            return template_name

        def _invoke_runtime_json(*, context, system_prompt, user_prompt, runtime):  # noqa: ANN001
            _ = (system_prompt, user_prompt, runtime)
            if context.stage == PLANNING_STATE_ENGINEERING:
                return {
                    "findings": ["Architecture should split invite creation from delivery"],
                    "recommendations": ["Use a dedicated invite service"],
                    "required_tasks": ["Build invite service", "Persist invite state"],
                    "child_ticket_specs": [
                        {
                            "summary": "Create invite service",
                            "capability": "Invite creation and delivery",
                            "delivery": "Build the invite flow entry point and service so users can create and send app invites from Profile.",
                            "expected_outcome": "Users can create an invite from Profile and the system delivers a signed invite link.",
                            "acceptance_criteria": ["Users can start invite flow from Profile", "Invite links are delivered through the chosen channel"],
                            "how_to_test": ["Run invite service integration tests", "Verify Profile invite flow in browser automation"],
                            "done_means": ["Invite flow is implemented and validated end to end"],
                            "dependencies": ["Invite delivery channel remains available"],
                            "risks": ["Invite creation and delivery can drift apart if boundaries are unclear"],
                            "labels": ["engineering"],
                        }
                    ],
                    "open_behavior_questions": [],
                    "acceptance_impacts": ["Invite flow works from Profile"],
                    "mermaid_diagram": "flowchart TD\n  Share[Share entry] --> InviteService[Invite service]",
                }
            if context.stage == PLANNING_STATE_SECURITY:
                return {
                    "findings": ["Invite links should not reveal raw user IDs"],
                    "recommendations": ["Sign links and verify expiry"],
                    "required_tasks": ["Add signed invite tokens"],
                    "open_behavior_questions": [],
                    "acceptance_impacts": ["Unauthorized reuse is blocked"],
                }
            return {
                "findings": ["Need coverage for expired and malformed links"],
                "recommendations": ["Add regression tests for both cases"],
                "required_tasks": ["Add expired-link test", "Add malformed-link test"],
                "open_behavior_questions": [],
                "acceptance_impacts": ["Acceptance criteria remain testable"],
            }

        with (
            patch("orchestrator.core.specialist_planning.render_prompt", side_effect=_render_prompt),
            patch("orchestrator.core.runtime_stage_session.invoke_runtime_json", side_effect=_invoke_runtime_json),
        ):
            result = run_specialist_planning_fanout(
                runtime=SimpleNamespace(),
                request=request,
                runtime_for_selector=_runtime_for_selector,
            )

        self.assertEqual(
            selectors,
            [
                "workflow.pm_planning_architect",
                "workflow.pm_planning_security",
                "workflow.pm_planning_tester",
            ],
        )
        self.assertEqual(result.planning_state, PLANNING_STATE_COMPLETED)
        self.assertEqual(
            [stage.planning_state for stage in result.stages],
            [PLANNING_STATE_ENGINEERING, PLANNING_STATE_SECURITY, PLANNING_STATE_TEST],
        )
        self.assertFalse(any(stage.blocked for stage in result.stages))
        self.assertIn("Architecture should split invite creation from delivery", result.findings)
        self.assertIn("Use a dedicated invite service", result.recommendations)
        self.assertIn("Add signed invite tokens", result.required_tasks)
        self.assertIn("Invite flow works from Profile", result.acceptance_impacts)
        self.assertEqual(result.open_behavior_questions, ())
        self.assertIsNone(result.block_reason)
        self.assertEqual(
            result.architecture_summary,
            (
                "Architecture should split invite creation from delivery",
                "Use a dedicated invite service",
                "Invite flow works from Profile",
            ),
        )
        self.assertIn("Invite service", result.architecture_diagram or "")
        self.assertTrue(prompts)
        first_prompt_name, first_prompt_context = prompts[0]
        self.assertEqual(first_prompt_name, "workflow/pm_planning_architect_system.j2")
        self.assertEqual(first_prompt_context, {})
        user_prompt_name, user_prompt_context = prompts[1]
        self.assertEqual(user_prompt_name, "workflow/pm_planning_architect_user.j2")
        self.assertEqual(user_prompt_context["parent_issue_key"], "PM-42")
        self.assertIn("Let users share the app with friends", user_prompt_context["product_brief_json"])

    def test_open_questions_block_planning_but_still_run_all_stages(self) -> None:
        request = self._request()

        def _invoke_runtime_json(*, context, system_prompt, user_prompt, runtime):  # noqa: ANN001
            _ = (system_prompt, user_prompt, runtime)
            if context.stage == PLANNING_STATE_ENGINEERING:
                return {
                    "findings": ["Architecture is straightforward"],
                    "recommendations": ["Proceed with a service boundary"],
                    "required_tasks": ["Add invite service"],
                    "open_behavior_questions": [],
                    "acceptance_impacts": ["Share entry point exists"],
                }
            if context.stage == PLANNING_STATE_SECURITY:
                return {
                    "findings": ["Share target is unclear"],
                    "recommendations": ["Clarify whether this is invite, referral, or social share"],
                    "required_tasks": [],
                    "open_behavior_questions": [
                        "Is this a simple invite link or a referral system? Examples: invite-only link, reward-based referral."
                    ],
                    "acceptance_impacts": ["Security model depends on the share type"],
                }
            return {
                "findings": ["Testing depends on the share type"],
                "recommendations": ["Hold test automation until the share behavior is clarified"],
                "required_tasks": ["Draft negative-path test matrix"],
                "open_behavior_questions": [],
                "acceptance_impacts": ["Validation scope depends on the share type"],
            }

        with (
            patch("orchestrator.core.specialist_planning.render_prompt", return_value="prompt"),
            patch("orchestrator.core.runtime_stage_session.invoke_runtime_json", side_effect=_invoke_runtime_json),
        ):
            result = run_specialist_planning_fanout(runtime=SimpleNamespace(), request=request)

        self.assertEqual(result.planning_state, PLANNING_STATE_BLOCKED)
        self.assertEqual(result.blocked_stage_states, (PLANNING_STATE_SECURITY,))
        self.assertEqual(len(result.stages), 3)
        self.assertTrue(result.stages[1].blocked)
        self.assertIn(
            "Is this a simple invite link or a referral system?",
            result.open_behavior_questions[0].question,
        )
        self.assertIn("security_planning", result.block_reason or "")
        self.assertIn("Share entry point exists", result.acceptance_impacts)

    def test_prompt_templates_render_structured_contracts(self) -> None:
        from pathlib import Path

        prompts_dir = Path(__file__).resolve().parents[1] / "orchestrator" / "prompts" / "workflow"
        system_prompts = (
            "pm_planning_architect_system.j2",
            "pm_planning_security_system.j2",
            "pm_planning_tester_system.j2",
        )
        user_prompts = (
            "pm_planning_architect_user.j2",
            "pm_planning_security_user.j2",
            "pm_planning_tester_user.j2",
        )
        for prompt_name in (*system_prompts, *user_prompts):
            prompt_text = (prompts_dir / prompt_name).read_text(encoding="utf-8")
            self.assertIn("Return strict JSON only with keys", prompt_text)
            self.assertIn("findings", prompt_text)
            self.assertIn("recommendations", prompt_text)
            self.assertIn("required_tasks", prompt_text)
            self.assertIn("open_behavior_questions", prompt_text)
            self.assertIn("acceptance_impacts", prompt_text)
            if "architect" in prompt_name:
                self.assertIn("mermaid_diagram", prompt_text)
                self.assertIn("child_ticket_specs", prompt_text)
            if prompt_name in user_prompts:
                self.assertIn('"type":"tool_request"', prompt_text)
                self.assertIn('"type":"final_response"', prompt_text)
                self.assertIn("Allowed governed tools for this stage", prompt_text)
                self.assertIn("Native Codex tools available directly in this runtime", prompt_text)
            else:
                self.assertIn('When the user message includes "Allowed governed tools" and "Native Codex tools" sections', prompt_text)

    def test_fanout_uses_tool_bridge_when_session_and_settings_present(self) -> None:
        request = self._request()
        captured: dict[str, object] = {"stages": []}

        def _render_prompt(template_name: str, **kwargs):  # noqa: ANN001
            if template_name == "workflow/pm_planning_architect_user.j2":
                captured["governed_tools_json"] = kwargs["governed_tools_json"]
                captured["native_tools_json"] = kwargs["native_tools_json"]
            return template_name

        def _invoke_runtime_json_with_tools(*, context, user_prompt, allowed_tools, **kwargs):  # noqa: ANN001
            _ = kwargs
            captured["stages"].append(context.stage)
            captured["user_prompt"] = user_prompt
            captured["allowed_tools"] = set(allowed_tools)
            return {
                "findings": [],
                "recommendations": [],
                "required_tasks": [],
                "open_behavior_questions": [],
                "acceptance_impacts": [],
                "mermaid_diagram": "",
            }

        with (
            patch("orchestrator.core.specialist_planning.render_prompt", side_effect=_render_prompt),
            patch("orchestrator.core.runtime_stage_session.invoke_runtime_json_with_tools", side_effect=_invoke_runtime_json_with_tools),
        ):
            run_specialist_planning_fanout(
                session=object(),
                settings=object(),
                runtime=SimpleNamespace(command="codex"),
                request=request,
                runtime_for_selector=lambda _selector: SimpleNamespace(command="codex"),
            )

        self.assertIn(PLANNING_STATE_ENGINEERING, captured["stages"])
        self.assertIn("knowledge.read", captured["allowed_tools"])
        self.assertIn("repo.read", captured["allowed_tools"])
        self.assertNotIn("web.search", captured["allowed_tools"])
        self.assertIn("tool_name", str(captured["governed_tools_json"]))
        self.assertIn("project.check_runtime_bindings", str(captured["governed_tools_json"]))
        self.assertIn("web.search", str(captured["native_tools_json"]))
        self.assertIn("browser.snapshot", str(captured["native_tools_json"]))

    def test_build_runtime_seed_planning_package_keeps_architecture_artifacts(self) -> None:
        def _invoke_runtime_json(*, context, system_prompt, user_prompt, runtime):  # noqa: ANN001
            _ = (system_prompt, user_prompt, runtime)
            if context.stage == PLANNING_STATE_ENGINEERING:
                return {
                    "findings": ["Architecture should split invite creation from delivery"],
                    "recommendations": ["Use a dedicated invite service"],
                    "required_tasks": ["Build invite service"],
                    "child_ticket_specs": [
                        {
                            "summary": "Create invite service",
                            "capability": "Invite creation and delivery",
                            "delivery": "Build the invite flow entry point and service so users can create and send app invites from Profile.",
                            "expected_outcome": "Users can create and send invites without leaving Profile.",
                            "acceptance_criteria": ["Invite flow is available from Profile", "Invite delivery succeeds with a signed link"],
                            "how_to_test": ["Run invite flow integration tests"],
                            "done_means": ["Invite service lands with automated verification"],
                            "dependencies": ["Invite delivery provider remains available"],
                            "risks": ["Invite state may split from delivery outcome if boundaries blur"],
                            "labels": ["engineering"],
                        }
                    ],
                    "open_behavior_questions": [],
                    "acceptance_impacts": ["Invite flow works from Profile"],
                    "mermaid_diagram": "flowchart TD\n  Share[Share entry] --> InviteService[Invite service]",
                }
            if context.stage == PLANNING_STATE_SECURITY:
                return {
                    "findings": ["Signed links prevent spoofing"],
                    "recommendations": ["Verify expiry and signature server-side"],
                    "required_tasks": ["Add signed invite tokens"],
                    "open_behavior_questions": [],
                    "acceptance_impacts": ["Unauthorized reuse is blocked"],
                }
            return {
                "findings": ["Need malformed-link coverage"],
                "recommendations": ["Add regression tests"],
                "required_tasks": ["Add malformed-link test"],
                "open_behavior_questions": [],
                "acceptance_impacts": ["Acceptance criteria remain testable"],
            }

        with (
            patch("orchestrator.core.specialist_planning.render_prompt", return_value="prompt"),
            patch("orchestrator.core.runtime_stage_session.invoke_runtime_json", side_effect=_invoke_runtime_json),
        ):
            result = run_specialist_planning_fanout(
                runtime=SimpleNamespace(),
                request=self._request(),
                runtime_for_selector=lambda _selector: SimpleNamespace(),
            )

        package = build_runtime_seed_planning_package(
            result=result,
        )

        self.assertEqual(package["planning_state"], PLANNING_STATE_COMPLETED)
        self.assertIn("architecture", package["specialist_outputs"])
        self.assertEqual(
            package["architecture_summary"],
            [
                "Architecture should split invite creation from delivery",
                "Use a dedicated invite service",
                "Invite flow works from Profile",
            ],
        )
        self.assertIn("Invite service", str(package["architecture_diagram"]))
        self.assertTrue(package["child_issues"])
        self.assertEqual(package["child_issues"][0]["summary"], "Create invite service")
        self.assertIn("Build the invite flow entry point", package["child_issues"][0]["delivery"])
        self.assertIn("Invite flow is available from Profile", package["child_issues"][0]["acceptance_criteria"][0])


if __name__ == "__main__":
    unittest.main()
