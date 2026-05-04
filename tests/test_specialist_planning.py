from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator.core.planning.specialist import (
    ArchitectStageOutput,
    ChildTicketSpec,
    PLANNING_STATE_BLOCKED,
    PLANNING_STATE_COMPLETED,
    PLANNING_STATE_ENGINEERING,
    PLANNING_STATE_SECURITY,
    PLANNING_STATE_TEST,
    RetryableSpecialistPlanningContractError,
    SecurityStageOutput,
    SpecialistPlanningRequest,
    TestingStageOutput,
    build_runtime_seed_planning_package,
    run_specialist_planning_fanout,
)
from orchestrator.core.planning.specialist.models import PLANNING_STAGES
from orchestrator.core.prompt_domain_models import prompt_domain_model_for_template
from orchestrator.core.prompt_templates import render_prompt


def _technical_decision(decision_id: str = "decision-1") -> dict[str, object]:
    return {
        "decision_id": decision_id,
        "area": "architecture",
        "question": "How should invite links be generated and verified?",
        "options": [
            {
                "option_id": "signed-token",
                "title": "Signed invite token",
                "description": "Generate a signed token with expiry and verify it server-side.",
                "benefits": ["Tamper-resistant", "Works without exposing raw user IDs"],
                "risks": ["Requires key rotation discipline"],
                "rejected_reason": "",
            },
            {
                "option_id": "plain-id",
                "title": "Plain invite identifier",
                "description": "Use a database identifier directly in the invite link.",
                "benefits": ["Simple to implement"],
                "risks": ["Enumeration risk", "Leaks implementation details"],
                "rejected_reason": "The security risk is not justified for invite links.",
            },
        ],
        "selected_option_id": "signed-token",
        "rationale": "Signed expiring tokens satisfy the brief without exposing raw identifiers.",
        "evidence": ["Parent brief requires share links", "Security stage requires spoofing protection"],
        "confidence": "high",
        "product_impact": "none",
    }


def _stage_contract(**extra: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "technical_decisions": [_technical_decision()],
        "pm_decision_requests": [],
    }
    payload.update(extra)
    return payload


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
                return _stage_contract(**{
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
                    "acceptance_impacts": ["Invite flow works from Profile"],
                    "mermaid_diagram": "flowchart TD\n  Share[Share entry] --> InviteService[Invite service]",
                })
            if context.stage == PLANNING_STATE_SECURITY:
                return _stage_contract(**{
                    "findings": ["Invite links should not reveal raw user IDs"],
                    "recommendations": ["Sign links and verify expiry"],
                    "required_tasks": ["Add signed invite tokens"],
                    "acceptance_impacts": ["Unauthorized reuse is blocked"],
                })
            return _stage_contract(**{
                "findings": ["Need coverage for expired and malformed links"],
                "recommendations": ["Add regression tests for both cases"],
                "required_tasks": ["Add expired-link test", "Add malformed-link test"],
                "acceptance_impacts": ["Acceptance criteria remain testable"],
            })

        with (
            patch("orchestrator.core.planning.specialist.stage_runner.render_prompt", side_effect=_render_prompt),
            patch("orchestrator.core.runtime.stage_session.invoke_runtime_json", side_effect=_invoke_runtime_json),
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
        self.assertIsInstance(result.stages[0], ArchitectStageOutput)
        self.assertIsInstance(result.stages[1], SecurityStageOutput)
        self.assertIsInstance(result.stages[2], TestingStageOutput)
        self.assertFalse(any(stage.blocked for stage in result.stages))
        self.assertIn("Architecture should split invite creation from delivery", result.findings)
        self.assertIn("Use a dedicated invite service", result.recommendations)
        self.assertIn("Add signed invite tokens", result.required_tasks)
        self.assertIn("Invite flow works from Profile", result.acceptance_impacts)
        self.assertEqual(result.pm_decision_requests, ())
        self.assertTrue(result.technical_decisions)
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
        self.assertIsInstance(result.stages[0].child_ticket_specs[0], ChildTicketSpec)
        self.assertTrue(prompts)
        first_prompt_name, first_prompt_context = prompts[0]
        self.assertEqual(first_prompt_name, "workflow/pm_planning_architect_system.j2")
        self.assertIn("domain_model", first_prompt_context)
        self.assertEqual(first_prompt_context["domain_model"]["name"], "ArchitectStageOutput")
        user_prompt_name, user_prompt_context = prompts[1]
        self.assertEqual(user_prompt_name, "workflow/pm_planning_architect_user.j2")
        self.assertEqual(user_prompt_context["parent_issue_key"], "PM-42")
        self.assertEqual(user_prompt_context["domain_model"], first_prompt_context["domain_model"])
        self.assertIn("Let users share the app with friends", user_prompt_context["product_brief_json"])

    def test_pm_decision_requests_block_planning_but_still_run_all_stages(self) -> None:
        request = self._request()

        def _invoke_runtime_json(*, context, system_prompt, user_prompt, runtime):  # noqa: ANN001
            _ = (system_prompt, user_prompt, runtime)
            if context.stage == PLANNING_STATE_ENGINEERING:
                return _stage_contract(**{
                    "findings": ["Architecture is straightforward"],
                    "recommendations": ["Proceed with a service boundary"],
                    "required_tasks": ["Add invite service"],
                    "child_ticket_specs": [],
                    "acceptance_impacts": ["Share entry point exists"],
                })
            if context.stage == PLANNING_STATE_SECURITY:
                return _stage_contract(**{
                    "findings": ["Share target is unclear"],
                    "recommendations": ["Clarify whether this is invite, referral, or social share"],
                    "required_tasks": [],
                    "pm_decision_requests": [
                        {
                            "request_id": "pm-share-type",
                            "question": "Is this a simple invite link or a referral system? Examples: invite-only link, reward-based referral.",
                            "why_it_matters": "The answer changes product behavior and abuse controls.",
                            "related_decision_ids": ["decision-1"],
                        }
                    ],
                    "acceptance_impacts": ["Security model depends on the share type"],
                })
            return _stage_contract(**{
                "findings": ["Testing depends on the share type"],
                "recommendations": ["Hold test automation until the share behavior is clarified"],
                "required_tasks": ["Draft negative-path test matrix"],
                "acceptance_impacts": ["Validation scope depends on the share type"],
            })

        with (
            patch("orchestrator.core.planning.specialist.stage_runner.render_prompt", return_value="prompt"),
            patch("orchestrator.core.runtime.stage_session.invoke_runtime_json", side_effect=_invoke_runtime_json),
        ):
            result = run_specialist_planning_fanout(runtime=SimpleNamespace(), request=request)

        self.assertEqual(result.planning_state, PLANNING_STATE_BLOCKED)
        self.assertEqual(result.blocked_stage_states, (PLANNING_STATE_SECURITY,))
        self.assertEqual(len(result.stages), 3)
        self.assertTrue(result.stages[1].blocked)
        self.assertIn(
            "Is this a simple invite link or a referral system?",
            result.pm_decision_requests[0].question,
        )
        self.assertIn("security_planning", result.block_reason or "")
        self.assertIn("Share entry point exists", result.acceptance_impacts)

    def test_prompt_templates_render_structured_contracts(self) -> None:
        from pathlib import Path

        prompts_dir = Path(__file__).resolve().parents[1] / "orchestrator" / "prompts" / "workflow"
        user_context = {
            "parent_issue_key": "PM-42",
            "parent_summary": "Share the app with friends",
            "parent_description": "Let users share invite links.",
            "product_brief_json": "{}",
            "project_keys_json": "[]",
            "related_issues_json": "[]",
            "status_counts_json": "{}",
            "github_context_json": "{}",
            "conversation_history_json": "[]",
            "governed_tools_json": "[]",
            "native_tools_json": "[]",
        }
        for stage in PLANNING_STAGES:
            domain_model = prompt_domain_model_for_template(stage.system_prompt_template)
            self.assertIsNotNone(domain_model)
            system_prompt = render_prompt(stage.system_prompt_template, domain_model=domain_model)
            user_prompt = render_prompt(
                stage.user_prompt_template,
                domain_model=domain_model,
                **user_context,
            )
            raw_system = (prompts_dir / stage.system_prompt_template.removeprefix("workflow/")).read_text(
                encoding="utf-8"
            )
            raw_user = (prompts_dir / stage.user_prompt_template.removeprefix("workflow/")).read_text(
                encoding="utf-8"
            )
            self.assertIn("{{ domain_model", raw_system)
            self.assertIn("{{ domain_model", raw_user)
            for prompt_text in (system_prompt, user_prompt):
                self.assertIn(str(domain_model["name"]), prompt_text)
                self.assertIn("Return JSON matching this domain model", prompt_text)
                self.assertIn("findings", prompt_text)
                self.assertIn("recommendations", prompt_text)
                self.assertIn("technical_decisions", prompt_text)
                self.assertIn("pm_decision_requests", prompt_text)
                self.assertNotIn("open_behavior_questions", prompt_text)
                self.assertNotIn("internal_decisions", prompt_text)
                self.assertNotIn("stakeholder_escalation_recommendations", prompt_text)
                self.assertIn("acceptance_impacts", prompt_text)
                self.assertIn("required_tasks", prompt_text)
            if stage.persona_id == "architect":
                prompt_text = "\n".join((system_prompt, user_prompt))
                self.assertIn("mermaid_diagram", prompt_text)
                self.assertIn("child_ticket_specs", prompt_text)
                self.assertIn("done_means", prompt_text)
            self.assertIn('"type":"tool_request"', user_prompt)
            self.assertIn('"type":"final_response"', user_prompt)
            self.assertIn("Allowed governed tools for this stage", user_prompt)
            self.assertIn("Native Codex tools available directly in this runtime", user_prompt)
            self.assertIn('When the user message includes "Allowed governed tools" and "Native Codex tools" sections', system_prompt)

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
            return _stage_contract(**{
                "findings": [],
                "recommendations": [],
                "required_tasks": [],
                "child_ticket_specs": [] if context.stage == PLANNING_STATE_ENGINEERING else None,
                "acceptance_impacts": [],
                "mermaid_diagram": "",
            })

        with (
            patch("orchestrator.core.planning.specialist.stage_runner.render_prompt", side_effect=_render_prompt),
            patch("orchestrator.core.runtime.stage_session.invoke_runtime_json_with_tools", side_effect=_invoke_runtime_json_with_tools),
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
                return _stage_contract(**{
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
                    "acceptance_impacts": ["Invite flow works from Profile"],
                    "mermaid_diagram": "flowchart TD\n  Share[Share entry] --> InviteService[Invite service]",
                })
            if context.stage == PLANNING_STATE_SECURITY:
                return _stage_contract(**{
                    "findings": ["Signed links prevent spoofing"],
                    "recommendations": ["Verify expiry and signature server-side"],
                    "required_tasks": ["Add signed invite tokens"],
                    "acceptance_impacts": ["Unauthorized reuse is blocked"],
                })
            return _stage_contract(**{
                "findings": ["Need malformed-link coverage"],
                "recommendations": ["Add regression tests"],
                "required_tasks": ["Add malformed-link test"],
                "acceptance_impacts": ["Acceptance criteria remain testable"],
            })

        with (
            patch("orchestrator.core.planning.specialist.stage_runner.render_prompt", return_value="prompt"),
            patch("orchestrator.core.runtime.stage_session.invoke_runtime_json", side_effect=_invoke_runtime_json),
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

    def test_architect_stage_retries_contract_violation_and_uses_corrected_payload(self) -> None:
        prompts: list[str] = []

        def _invoke_runtime_json(*, context, system_prompt, user_prompt, runtime):  # noqa: ANN001
            _ = (system_prompt, runtime)
            prompts.append(user_prompt)
            if context.stage == PLANNING_STATE_ENGINEERING and len(prompts) == 1:
                return _stage_contract(**{
                    "findings": ["Architecture should split invite creation from delivery"],
                    "recommendations": ["Use a dedicated invite service"],
                    "required_tasks": ["Build invite service"],
                    "child_ticket_specs": [
                        {
                            "summary": "Create invite service",
                            "capability": "Invite creation and delivery",
                            "delivery": "Build the invite flow entry point and service so users can create and send app invites from Profile.",
                            "expected_outcome": "Users can create and send invites without leaving Profile.",
                            "acceptance_criteria": ["Invite flow is available from Profile"],
                            "how_to_test": ["Run invite flow integration tests"],
                            "done_means": [],
                            "dependencies": [],
                            "risks": [],
                            "labels": ["engineering"],
                        }
                    ],
                    "acceptance_impacts": ["Invite flow works from Profile"],
                    "mermaid_diagram": "flowchart TD\n  Share[Share entry] --> InviteService[Invite service]",
                })
            if context.stage == PLANNING_STATE_ENGINEERING:
                return _stage_contract(**{
                    "findings": ["Architecture should split invite creation from delivery"],
                    "recommendations": ["Use a dedicated invite service"],
                    "required_tasks": ["Build invite service"],
                    "child_ticket_specs": [
                        {
                            "summary": "Create invite service",
                            "capability": "Invite creation and delivery",
                            "delivery": "Build the invite flow entry point and service so users can create and send app invites from Profile.",
                            "expected_outcome": "Users can create and send invites without leaving Profile.",
                            "acceptance_criteria": ["Invite flow is available from Profile"],
                            "how_to_test": ["Run invite flow integration tests"],
                            "done_means": ["Invite service is implemented with end-to-end verification"],
                            "dependencies": [],
                            "risks": [],
                            "labels": ["engineering"],
                        }
                    ],
                    "acceptance_impacts": ["Invite flow works from Profile"],
                    "mermaid_diagram": "flowchart TD\n  Share[Share entry] --> InviteService[Invite service]",
                })
            return _stage_contract(**{
                "findings": [],
                "recommendations": [],
                "required_tasks": [],
                "acceptance_impacts": [],
            })

        with (
            patch("orchestrator.core.planning.specialist.stage_runner.render_prompt", return_value="prompt"),
            patch("orchestrator.core.runtime.stage_session.invoke_runtime_json", side_effect=_invoke_runtime_json),
        ):
            result = run_specialist_planning_fanout(
                runtime=SimpleNamespace(),
                request=self._request(),
                runtime_for_selector=lambda _selector: SimpleNamespace(),
            )

        self.assertEqual(result.planning_state, PLANNING_STATE_COMPLETED)
        self.assertEqual(
            result.stages[0].child_ticket_specs[0].done_means,
            ("Invite service is implemented with end-to-end verification",),
        )
        self.assertIn("CONTRACT REPAIR REQUIRED", prompts[1])
        self.assertIn(
            "engineering_planning child_ticket_specs[1] with empty done_means; expected a non-empty array of strings",
            prompts[1],
        )
        self.assertIn("Domain model JSON", prompts[1])
        self.assertIn("ArchitectStageOutput", prompts[1])
        self.assertIn("done_means", prompts[1])

    def test_stage_preserves_multiple_pm_decision_requests_for_same_decision_set(self) -> None:
        prompts: list[str] = []

        def _invoke_runtime_json(*, context, system_prompt, user_prompt, runtime):  # noqa: ANN001
            _ = (system_prompt, runtime)
            prompts.append(user_prompt)
            if context.stage == PLANNING_STATE_SECURITY:
                return _stage_contract(**{
                    "findings": ["Share behavior may change abuse controls"],
                    "recommendations": ["Ask product-owned questions"],
                    "required_tasks": [],
                    "pm_decision_requests": [
                        {
                            "request_id": "pm-share-type-1",
                            "question": "Is this a simple invite link or a referral system?",
                            "why_it_matters": "The answer changes abuse controls.",
                            "related_decision_ids": ["decision-1"],
                        },
                        {
                            "request_id": "pm-share-type-2",
                            "question": "Should sharing be invite-only or reward referral?",
                            "why_it_matters": "The answer changes product behavior.",
                            "related_decision_ids": ["decision-1"],
                        },
                    ],
                    "acceptance_impacts": ["Security model depends on share type"],
                })
            return _stage_contract(**{
                "findings": ["No blocking ambiguity"],
                "recommendations": ["Proceed"],
                "required_tasks": [],
                "acceptance_impacts": [],
                **(
                    {
                        "child_ticket_specs": [],
                        "mermaid_diagram": "flowchart TD\nA[Start]",
                    }
                    if context.stage == PLANNING_STATE_ENGINEERING
                    else {}
                ),
            })

        with (
            patch("orchestrator.core.planning.specialist.stage_runner.render_prompt", return_value="prompt"),
            patch("orchestrator.core.runtime.stage_session.invoke_runtime_json", side_effect=_invoke_runtime_json),
        ):
            result = run_specialist_planning_fanout(runtime=SimpleNamespace(), request=self._request())

        self.assertEqual(len(result.pm_decision_requests), 2)
        self.assertNotIn("CONTRACT REPAIR REQUIRED", "\n".join(prompts))

    def test_security_stage_repairs_object_findings_into_string_findings(self) -> None:
        prompts: list[str] = []
        security_calls = 0

        def _invoke_runtime_json(*, context, system_prompt, user_prompt, runtime):  # noqa: ANN001
            nonlocal security_calls
            _ = (system_prompt, runtime)
            prompts.append(user_prompt)
            if context.stage == PLANNING_STATE_SECURITY:
                security_calls += 1
                if security_calls == 1:
                    return _stage_contract(**{
                        "findings": [
                            {
                                "title": "Email verification boundary",
                                "severity": "high",
                                "evidence": ["Local auth email binding affects account takeover risk"],
                            }
                        ],
                        "recommendations": ["Use verified-email evidence before sensitive binding"],
                        "required_tasks": ["Add security tests for verified email binding"],
                        "acceptance_impacts": [],
                    })
                return _stage_contract(**{
                    "findings": ["Email verification boundary affects account takeover risk."],
                    "recommendations": ["Use verified-email evidence before sensitive binding"],
                    "required_tasks": ["Add security tests for verified email binding"],
                    "acceptance_impacts": [],
                })
            return _stage_contract(**{
                "findings": ["No blocking ambiguity"],
                "recommendations": ["Proceed"],
                "required_tasks": [],
                "acceptance_impacts": [],
                **(
                    {
                        "child_ticket_specs": [],
                        "mermaid_diagram": "flowchart TD\nA[Start]",
                    }
                    if context.stage == PLANNING_STATE_ENGINEERING
                    else {}
                ),
            })

        with (
            patch("orchestrator.core.planning.specialist.stage_runner.render_prompt", return_value="prompt"),
            patch("orchestrator.core.runtime.stage_session.invoke_runtime_json", side_effect=_invoke_runtime_json),
        ):
            result = run_specialist_planning_fanout(runtime=SimpleNamespace(), request=self._request())

        self.assertIn("Email verification boundary affects account takeover risk.", result.findings)
        self.assertEqual(security_calls, 2)
        self.assertIn("CONTRACT REPAIR REQUIRED", prompts[2])
        self.assertIn("Domain model JSON", prompts[2])
        self.assertIn("SpecialistStageOutput", prompts[2])
        self.assertIn("findings", prompts[2])

    def test_architect_stage_fails_hard_when_child_ticket_spec_repair_is_still_missing_done_means(self) -> None:
        calls: list[str] = []

        def _invoke_runtime_json(*, context, system_prompt, user_prompt, runtime):  # noqa: ANN001
            _ = (system_prompt, user_prompt, runtime)
            calls.append(context.stage)
            if context.stage == PLANNING_STATE_ENGINEERING:
                return _stage_contract(**{
                    "findings": ["Architecture should split invite creation from delivery"],
                    "recommendations": ["Use a dedicated invite service"],
                    "required_tasks": ["Build invite service"],
                    "child_ticket_specs": [
                        {
                            "summary": "Create invite service",
                            "capability": "Invite creation and delivery",
                            "delivery": "Build the invite flow entry point and service so users can create and send app invites from Profile.",
                            "expected_outcome": "Users can create and send invites without leaving Profile.",
                            "acceptance_criteria": ["Invite flow is available from Profile"],
                            "how_to_test": ["Run invite flow integration tests"],
                            "done_means": [],
                            "dependencies": [],
                            "risks": [],
                            "labels": ["engineering"],
                        }
                    ],
                    "acceptance_impacts": ["Invite flow works from Profile"],
                    "mermaid_diagram": "flowchart TD\n  Share[Share entry] --> InviteService[Invite service]",
                })
            return _stage_contract(**{
                "findings": [],
                "recommendations": [],
                "required_tasks": [],
                "acceptance_impacts": [],
            })

        with (
            patch("orchestrator.core.planning.specialist.stage_runner.render_prompt", return_value="prompt"),
            patch("orchestrator.core.runtime.stage_session.invoke_runtime_json", side_effect=_invoke_runtime_json),
        ):
            with self.assertRaisesRegex(
                RetryableSpecialistPlanningContractError,
                "engineering_planning child_ticket_specs\\[1\\] with empty done_means",
            ):
                run_specialist_planning_fanout(
                    runtime=SimpleNamespace(),
                    request=self._request(),
                    runtime_for_selector=lambda _selector: SimpleNamespace(),
                )
        self.assertEqual(calls, [PLANNING_STATE_ENGINEERING, PLANNING_STATE_ENGINEERING])


if __name__ == "__main__":
    unittest.main()
