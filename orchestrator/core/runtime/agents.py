from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from orchestrator.core.runtime.invocation import (
    AgentInvocationContext,
    invoke_runtime_json,
)
from orchestrator.core.runtime.runtime import CodexRuntime, CodexRuntimeError
from orchestrator.core.discord.personas import get_voice_room_persona_definition
from orchestrator.core.prompt_templates import render_prompt
from orchestrator.core.runtime.payload_models import (
    AskIntent,
    EngineeringClarification,
    EngineeringSeedPlan,
    PMMessageBrief,
    PmParentSeedPlan,
    RuntimeMessage,
    VoiceEntryRoute,
)
from orchestrator.core.runtime.stage_session import (
    RuntimeStageSession,
    build_governed_tool_executor,
    build_runtime_stage_tooling,
)
from orchestrator.core.worker.capability_normalization import parse_worker_capability
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.core.workflow.runner import (
    DemoRequirement,
    DevResult,
    PmPlan,
    QaRecording,
    QaResult,
    QaScenario,
    QaStep,
    ReviewResult,
    StageOutcome,
    TestResult,
    WorkflowRequest,
)


def _stage_prompt_tool_context(
    *,
    tool_stage: str,
    runtime_command: str | None,
    worker_platform: str | None = None,
) -> dict[str, str]:
    tooling = build_runtime_stage_tooling(
        policy_stage=tool_stage,
        runtime_command=runtime_command,
        worker_platform=worker_platform,
    )
    return tooling.governed_native_prompt_context()


def _stage_has_governed_tools(
    *,
    tool_stage: str,
    runtime_command: str | None,
    worker_platform: str | None = None,
) -> bool:
    tooling = build_runtime_stage_tooling(
        policy_stage=tool_stage,
        runtime_command=runtime_command,
        worker_platform=worker_platform,
    )
    return bool(tooling.governed_tools)


_ACCESSIBILITY_IDENTIFIER_RE = re.compile(r'accessibilityIdentifier\("([^"]+)"\)')
_TEXT_LITERAL_RE = re.compile(r'Text\("([^"]+)"\)')
_UI_TEST_SELECTOR_RE = re.compile(r'selector:\s*"((?:id|text)=[^"]+)"')
_XCUITEST_TEXT_QUERY_RE = re.compile(r'app\.(?:buttons|staticTexts|otherElements|navigationBars)\["([^"]+)"\]')
_ANDROID_RESOURCE_ID_RE = re.compile(r'android:id="@\+id/([^"]+)"')
_ANDROID_TEXT_RE = re.compile(r'android:text="([^"@][^"]*)"')
_ANDROID_COMPOSE_TEST_TAG_RE = re.compile(r'\.testTag\("([^"]+)"\)')
_ANDROID_COMPOSE_TEXT_RE = re.compile(r'Text\(\s*"([^"]+)"')
_NATIVE_INPUT_ID_HINTS = ("field", "input", "email", "password", "search", "username", "code", "otp")
_TEST_VALIDATION_SCOPES = frozenset({"targeted_only", "current_head_acceptance", "full_suite"})
_QA_CAPTURE_TARGETS = frozenset({"browser", "ios", "android", "desktop"})
_ISSUE_REQUIRED_CAPTURE_TARGET_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "browser": (
        re.compile(r"\bweb(?:site| app)?\b", re.IGNORECASE),
        re.compile(r"\bbrowser\b", re.IGNORECASE),
        re.compile(r"\bfrontend\b", re.IGNORECASE),
    ),
    "ios": (
        re.compile(r"\bios\b", re.IGNORECASE),
        re.compile(r"\biphone\b", re.IGNORECASE),
        re.compile(r"\bipad\b", re.IGNORECASE),
    ),
    "android": (re.compile(r"\bandroid\b", re.IGNORECASE),),
}


def _capture_target_constraints(value: str | None) -> dict[str, dict[str, object]]:
    raw = str(value or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, list):
        return {}
    constraints: dict[str, dict[str, object]] = {}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        capture_target = _optional_string(item.get("capture_target"))
        if capture_target not in _QA_CAPTURE_TARGETS:
            continue
        constraints[capture_target] = {
            "provider_available": bool(item.get("provider_available")),
            "required_worker_platform": _optional_string(item.get("required_worker_platform")),
            "availability_reason": _optional_string(item.get("availability_reason")),
        }
    return constraints


def _required_demo_capture_targets_from_issue_text(*values: str | None) -> set[str]:
    text = "\n".join(str(value or "") for value in values)
    required_targets: set[str] = set()
    for capture_target, patterns in _ISSUE_REQUIRED_CAPTURE_TARGET_PATTERNS.items():
        if any(pattern.search(text) for pattern in patterns):
            required_targets.add(capture_target)
    return required_targets


def _native_selector_catalog(repo_dir: str | None, source_paths: Iterable[str] | None = None) -> dict[str, list[str]]:
    resolved_repo_dir = Path(str(repo_dir or "").strip())
    if not resolved_repo_dir.exists():
        return {"accessibility_ids": [], "text_anchors": [], "ui_test_selectors": []}
    roots = _native_selector_catalog_roots(repo_dir=resolved_repo_dir, source_paths=source_paths)

    accessibility_ids: set[str] = set()
    text_anchors: set[str] = set()
    ui_test_selectors: set[str] = set()
    for swift_file in _catalog_files(roots=roots, patterns=("*.swift",)):
        try:
            source = swift_file.read_text(encoding="utf-8")
        except OSError:
            continue
        accessibility_ids.update(match.group(1) for match in _ACCESSIBILITY_IDENTIFIER_RE.finditer(source))
        text_anchors.update(match.group(1) for match in _TEXT_LITERAL_RE.finditer(source))
        ui_test_selectors.update(match.group(1) for match in _UI_TEST_SELECTOR_RE.finditer(source))
        text_anchors.update(match.group(1) for match in _XCUITEST_TEXT_QUERY_RE.finditer(source))
        for selector in list(ui_test_selectors):
            if selector.startswith("text="):
                text_anchors.add(selector[5:])
    for native_file in _catalog_files(roots=roots, patterns=("*.kt", "*.java", "*.xml")):
        try:
            source = native_file.read_text(encoding="utf-8")
        except OSError:
            continue
        accessibility_ids.update(match.group(1) for match in _ANDROID_RESOURCE_ID_RE.finditer(source))
        accessibility_ids.update(match.group(1) for match in _ANDROID_COMPOSE_TEST_TAG_RE.finditer(source))
        text_anchors.update(match.group(1) for match in _ANDROID_TEXT_RE.finditer(source))
        text_anchors.update(match.group(1) for match in _ANDROID_COMPOSE_TEXT_RE.finditer(source))
    return {
        "accessibility_ids": sorted(accessibility_ids),
        "text_anchors": sorted(text_anchors),
        "ui_test_selectors": sorted(ui_test_selectors),
    }


def _native_selector_catalog_roots(*, repo_dir: Path, source_paths: Iterable[str] | None) -> tuple[Path, ...]:
    raw_source_paths = [str(path or "").strip() for path in source_paths or () if str(path or "").strip()]
    if not raw_source_paths:
        return (repo_dir,)
    repo_root = repo_dir.resolve()
    roots: list[Path] = []
    for raw_path in raw_source_paths:
        if Path(raw_path).is_absolute():
            raise CodexRuntimeError(f"QA native selector source path must be repository-relative: {raw_path}")
        candidate = (repo_root / raw_path).resolve()
        if candidate != repo_root and repo_root not in candidate.parents:
            raise CodexRuntimeError(f"QA native selector source path escapes repository: {raw_path}")
        if not candidate.exists() or not candidate.is_dir():
            raise CodexRuntimeError(f"QA native selector source path is missing: {raw_path}")
        roots.append(candidate)
    return tuple(dict.fromkeys(roots))


def _catalog_files(*, roots: Iterable[Path], patterns: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        for pattern in patterns:
            files.extend(root.rglob(pattern))
    return files


def _capture_target_source_paths_from_available_targets(value: str | None) -> dict[str, tuple[str, ...]]:
    raw = str(value or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, list):
        return {}
    source_paths_by_target: dict[str, tuple[str, ...]] = {}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        capture_target = _optional_string(item.get("capture_target"))
        if capture_target not in _QA_CAPTURE_TARGETS:
            continue
        raw_source_paths = item.get("source_paths")
        if not isinstance(raw_source_paths, list):
            continue
        source_paths = tuple(
            str(source_path).strip()
            for source_path in raw_source_paths
            if isinstance(source_path, str) and str(source_path).strip()
        )
        if source_paths:
            source_paths_by_target[capture_target] = source_paths
    return source_paths_by_target


def _merged_source_paths(source_paths_by_target: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    ordered: list[str] = []
    for source_paths in source_paths_by_target.values():
        for source_path in source_paths:
            if source_path not in ordered:
                ordered.append(source_path)
    return tuple(ordered)


def _current_head_diff_paths(repo_dir: str | None, base_branch: str | None) -> list[str]:
    resolved_repo_dir = Path(str(repo_dir or "").strip())
    resolved_base_branch = str(base_branch or "").strip()
    if not resolved_base_branch or not resolved_repo_dir.exists():
        return []
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{resolved_base_branch}...HEAD"],
            cwd=resolved_repo_dir,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    paths = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return sorted(dict.fromkeys(paths))


def _path_requires_current_head_acceptance(path: str) -> bool:
    normalized = path.strip().lower()
    if not normalized:
        return False
    if normalized.endswith((".md", ".txt", ".json", ".yaml", ".yml", ".lock")):
        return False
    for marker in ("/tests/", "\\tests\\", "tests/", "test/"):
        if marker in normalized:
            return False
    return True


def _requires_current_head_acceptance_evidence(
    *,
    plan: PmPlan,
    repo_dir: str | None,
    base_branch: str | None,
) -> tuple[bool, list[str]]:
    current_head_diff_paths = _current_head_diff_paths(repo_dir, base_branch)
    if not plan.demo_requirements:
        return False, current_head_diff_paths
    return any(_path_requires_current_head_acceptance(path) for path in current_head_diff_paths), current_head_diff_paths


def _validate_native_qa_scenarios(
    *,
    scenarios: list[QaScenario],
    repo_dir: str | None,
    source_paths_by_target: dict[str, tuple[str, ...]] | None = None,
) -> list[QaScenario]:
    repo_catalog = _native_selector_catalog(repo_dir)
    repo_valid_ids = set(repo_catalog["accessibility_ids"])
    repo_valid_texts = set(repo_catalog["text_anchors"])
    target_catalogs = {
        target: _native_selector_catalog(repo_dir, source_paths=source_paths)
        for target, source_paths in (source_paths_by_target or {}).items()
    }
    for scenario in scenarios:
        if scenario.capture_target == "browser":
            continue
        catalog = target_catalogs.get(scenario.capture_target)
        scoped_catalog = catalog is not None
        valid_ids = set((catalog or {}).get("accessibility_ids", ())) if scoped_catalog else repo_valid_ids
        valid_texts = set((catalog or {}).get("text_anchors", ())) if scoped_catalog else repo_valid_texts
        for step in scenario.steps:
            selector = str(step.selector or "").strip()
            if selector.startswith("id="):
                native_id = selector[3:]
                if (scoped_catalog or valid_ids) and native_id not in valid_ids:
                    raise CodexRuntimeError(
                        f"Codex qa response invented native accessibility identifier: {native_id}"
                    )
            elif selector.startswith("text="):
                text_value = selector[5:]
                if (scoped_catalog or valid_texts) and text_value not in valid_texts:
                    raise CodexRuntimeError(f"Codex qa response invented native text anchor: {text_value}")
            if step.action == "fill":
                if not selector.startswith("id="):
                    raise CodexRuntimeError(
                        "Codex qa response used fill on a native selector that is not an accessibility identifier"
                    )
                if not any(hint in selector[3:].lower() for hint in _NATIVE_INPUT_ID_HINTS):
                    raise CodexRuntimeError(
                        f"Codex qa response used fill on non-input native selector: {selector}"
                    )
    return scenarios


def _discord_tool_bridge_suffix(
    *,
    tool_stage: str,
    runtime_command: str | None = None,
    worker_platform: str | None = None,
) -> str:
    tooling = build_runtime_stage_tooling(
        policy_stage=tool_stage,
        runtime_command=runtime_command,
        worker_platform=worker_platform,
    )
    if not tooling.governed_tools:
        return ""
    return "\n\n" + render_prompt(
        "discord/codex_tool_bridge_suffix.j2",
        allowed_tools_json=json.dumps(sorted(tooling.governed_tools)),
    )


def _codex_discord_execute_tool(
    *,
    session: Session,
    settings: Any,
    invocation_context: AgentInvocationContext,
    tool_stage: str,
) -> Callable[[str, dict[str, object]], dict[str, object]]:
    return build_governed_tool_executor(
        session=session,
        settings=settings,
        context=invocation_context,
        policy_stage=tool_stage,
    )


def _invoke_discord_json_maybe_tools(
    *,
    runtime: CodexRuntime,
    context: AgentInvocationContext,
    system_prompt: str,
    user_prompt: str,
    tool_stage: str,
    sqlalchemy_session: Session | None,
    settings: Any | None,
    max_tool_hops: int = 8,
) -> dict:
    runtime_command = str(getattr(runtime, "command", "") or "").strip()
    stage_session = RuntimeStageSession.create(
        runtime=runtime,
        context=context,
        policy_stage=tool_stage,
        execute_tool=(
            _codex_discord_execute_tool(
                session=sqlalchemy_session,
                settings=settings,
                invocation_context=context,
                tool_stage=tool_stage,
            )
            if sqlalchemy_session is not None and settings is not None and str(context.tenant_id or "").strip()
            else None
        ),
    )
    bridged_user = user_prompt
    if stage_session.tooling.governed_tools:
        bridged_user += _discord_tool_bridge_suffix(
            tool_stage=tool_stage,
            runtime_command=runtime_command,
            worker_platform=context.worker_platform,
        )
    return stage_session.invoke_json(
        system_prompt=system_prompt,
        user_prompt=bridged_user,
        max_tool_hops=max_tool_hops,
    )


class CodexWorkflowAgents:
    def __init__(
        self,
        *,
        runtime: CodexRuntime,
        runtime_resolver: Callable[[str, WorkflowRequest], CodexRuntime] | None = None,
        log_sink: Callable[[dict], None] | None = None,
        execute_tool: Callable[[AgentInvocationContext, str, dict[str, object]], dict[str, object]] | None = None,
    ):
        self._runtime = runtime
        self._runtime_resolver = runtime_resolver
        self._log_sink = log_sink
        self._execute_tool = execute_tool

    def _runtime_for_stage(self, *, stage: str, request: WorkflowRequest) -> CodexRuntime:
        if self._runtime_resolver is None:
            return self._runtime
        return self._runtime_resolver(stage, request)

    def _stage_log_sink(
        self,
        *,
        request: WorkflowRequest,
        stage: str,
        attempt: int | None,
    ) -> Callable[[str, str], None] | None:
        if self._log_sink is None:
            return None

        def _emit(stream: str, message: str) -> None:
            self._log_sink(
                {
                    "tenant_id": request.tenant_id,
                    "project_id": request.project_id,
                    "run_id": request.run_id,
                    "issue_key": request.issue_key,
                    "stage": stage,
                    "attempt": attempt,
                    "stream": stream,
                    "message": message,
                }
            )

        return _emit

    def _resume_session_id_for_stage(self, *, request: WorkflowRequest, stage: str) -> str | None:
        if str(request.entry_mode or "").strip().lower() != "resume":
            return None
        checkpoint_kind = str(request.checkpoint_kind or "").strip().lower()
        session_id = str(request.checkpoint_session_id or "").strip() or None
        if not session_id:
            return None
        if checkpoint_kind == "orchestrated" and stage == "pm":
            return session_id
        if checkpoint_kind == "pm" and stage == "pm":
            return session_id
        if checkpoint_kind == "execution" and stage in {"dev", "test", "review"}:
            return session_id
        return None

    def _resume_source_state(self, *, request: WorkflowRequest) -> dict[str, Any]:
        payload = request.checkpoint_payload
        snapshot = ExecutionSnapshot.load(payload)
        if snapshot is None:
            return {}
        review_result = snapshot.review_result()
        if review_result is None:
            return {}
        return {
            "review_summary": list(review_result.summary),
            "review_feedback": review_result.feedback,
            "review_pr_url": review_result.pr_url,
        }

    def _invoke_stage_payload(
        self,
        *,
        request: WorkflowRequest,
        stage: str,
        attempt: int,
        system_prompt: str,
        user_prompt: str,
        reasoning_effort: str = "medium",
        runtime_override: CodexRuntime | None = None,
    ) -> dict[str, Any]:
        runtime = runtime_override or self._runtime_for_stage(stage=stage, request=request)
        context = AgentInvocationContext(
            channel="worker",
            tenant_id=request.tenant_id,
            project_id=request.project_id,
            command="workflow",
            stage=stage,
            working_dir=request.execution_repo_dir or ".",
            workflow_id=request.workflow_id,
            issue_key=request.issue_key,
            run_id=request.run_id,
            attempt=attempt,
            reasoning_effort=reasoning_effort,
            issue_description_chars=len(request.issue_description or ""),
            codex_session_id=self._resume_session_id_for_stage(request=request, stage=stage),
            worker_platform=request.current_worker_capability.value,
        )
        stage_session = RuntimeStageSession.create(
            runtime=runtime,
            context=context,
            policy_stage=stage,
            execute_tool=lambda tool_name, tool_args: self._execute_stage_tool(
                context=context,
                tool_name=tool_name,
                tool_args=tool_args,
            ),
        )
        return stage_session.invoke_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            extra_on_log_line=self._stage_log_sink(request=request, stage=stage, attempt=attempt),
        )

    def _execute_stage_tool(
        self,
        *,
        context: AgentInvocationContext,
        tool_name: str,
        tool_args: dict[str, object],
    ) -> dict[str, object]:
        if self._execute_tool is None:
            raise RuntimeError("Codex workflow stage requested a tool but no tool executor is configured")
        return self._execute_tool(context, tool_name, tool_args)

    def pm(
        self,
        request: WorkflowRequest,
        attempt: int,
        feedback: str | None,
        history: list[dict[str, str]],
        last_dev_result: DevResult | None,
        last_test_result: TestResult | None,
        last_review_result: ReviewResult | None,
        capture_target_constraints_json: str = "[]",
    ) -> PmPlan:
        runtime = self._runtime_for_stage(stage="pm", request=request)
        runtime_command = str(getattr(runtime, "command", "") or "")
        payload = self._invoke_stage_payload(
            request=request,
            stage="pm",
            attempt=attempt,
            system_prompt=render_prompt("workflow/pm_system.j2"),
            user_prompt=render_prompt(
                "workflow/pm_user.j2",
                tenant_id=request.tenant_id,
                project_id=request.project_id or "",
                project_name=request.project_name or "",
                github_repository=request.github_repository or "",
                jira_project_key=request.jira_project_key or "",
                run_id=request.run_id,
                issue_key=request.issue_key,
                execution_repo_dir=request.execution_repo_dir or "",
                execution_branch=request.execution_branch or "",
                base_branch=request.base_branch or "",
                integration_branch=request.integration_branch or "",
                pr_target_branch=request.pr_target_branch or "",
                allow_pr_creation="true" if request.allow_pr_creation else "false",
                issue_summary=request.issue_summary,
                issue_description=request.issue_description,
                attempt=attempt,
                feedback=feedback or "none",
                history_json=json.dumps(history[-25:]),
                last_dev_summary_json=json.dumps(last_dev_result.change_summary if last_dev_result else []),
                last_dev_pr_url=last_dev_result.pr_url if last_dev_result and last_dev_result.pr_url else "none",
                last_test_outcome=(
                    "none"
                    if last_test_result is None
                    else last_test_result.outcome
                ),
                last_test_feedback=last_test_result.feedback if last_test_result and last_test_result.feedback else "none",
                last_test_guidance_json=json.dumps(last_test_result.guidance if last_test_result else []),
                last_review_outcome=(
                    "none"
                    if last_review_result is None
                    else last_review_result.outcome
                ),
                last_review_feedback=(
                    last_review_result.feedback
                    if last_review_result and last_review_result.feedback
                    else "none"
                ),
                last_review_summary_json=json.dumps(last_review_result.summary if last_review_result else []),
                current_worker_capability=request.current_worker_capability.value,
                available_worker_capabilities_json=json.dumps(
                    [capability.value for capability in request.available_worker_capabilities]
                ),
                project_demo_capture_targets_json=json.dumps(list(request.project_demo_capture_targets)),
                project_demo_capture_target_sources_json=json.dumps(
                    {
                        target: list(source_paths)
                        for target, source_paths in request.project_demo_capture_target_sources.items()
                    },
                    sort_keys=True,
                ),
                qa_capture_target_constraints_json=capture_target_constraints_json,
                human_inputs_json=json.dumps(request.human_inputs),
                **_stage_prompt_tool_context(
                    tool_stage="pm",
                    runtime_command=runtime_command,
                    worker_platform=request.current_worker_capability.value,
                ),
            ),
            runtime_override=runtime,
        )
        outcome = _require_stage_outcome(payload=payload, stage="pm")
        next_stage_raw = payload.get("next_stage")
        next_stage = next_stage_raw if isinstance(next_stage_raw, str) else None
        if next_stage not in {"dev", "test"}:
            raise CodexRuntimeError("Codex pm response missing valid required next_stage")
        execution_worker_capability = parse_worker_capability(payload.get("execution_worker_capability"))
        if execution_worker_capability is None:
            raise CodexRuntimeError("Codex pm response missing valid required execution_worker_capability")
        blocker_message = _optional_string(payload.get("blocker_message"))
        requeue_target_raw = payload.get("requeue_target")
        requeue_target = (
            parse_worker_capability(requeue_target_raw)
            if requeue_target_raw is not None
            else None
        )
        requeue_reason = _optional_string(payload.get("requeue_reason"))
        if outcome == "blocked" and blocker_message is None:
            raise CodexRuntimeError("Codex pm response missing blocker_message for blocked outcome")
        if outcome == "requeue" and requeue_target is None:
            raise CodexRuntimeError("Codex pm response missing requeue_target for requeue outcome")
        if outcome == "requeue" and requeue_reason is None:
            raise CodexRuntimeError("Codex pm response missing requeue_reason for requeue outcome")
        demo_requirements = _required_demo_requirements(payload.get("demo_requirements"), stage="pm")
        target_constraints = _capture_target_constraints(capture_target_constraints_json)
        if outcome == "continue":
            required_issue_targets = _required_demo_capture_targets_from_issue_text(
                request.issue_summary,
                request.issue_description,
            )
            required_project_targets = {
                target for target in request.project_demo_capture_targets if target in _QA_CAPTURE_TARGETS
            }
            selected_targets = {requirement.capture_target for requirement in demo_requirements}
            missing_required_targets = sorted(
                (required_issue_targets | required_project_targets) - selected_targets
            )
            if missing_required_targets:
                raise CodexRuntimeError(
                    "Codex pm response missing required demo capture target(s): "
                    + ", ".join(missing_required_targets)
                )
        for requirement in demo_requirements:
            constraint = target_constraints.get(requirement.capture_target)
            if constraint is None:
                continue
            if not bool(constraint.get("provider_available")):
                reason = _optional_string(constraint.get("availability_reason"))
                raise CodexRuntimeError(
                    f"Codex pm response selected unavailable capture target '{requirement.capture_target}'"
                    + (f": {reason}" if reason else "")
                )
        return PmPlan(
            plan_steps=_required_string_list(payload.get("plan_steps"), stage="pm", field="plan_steps"),
            acceptance_criteria=_required_string_list(
                payload.get("acceptance_criteria"),
                stage="pm",
                field="acceptance_criteria",
            ),
            risks=_string_list(payload.get("risks")),
            demo_requirements=demo_requirements,
            outcome=outcome,
            next_stage=next_stage,
            execution_worker_capability=execution_worker_capability.value,
            blocker_message=blocker_message,
            requeue_target=(requeue_target.value if requeue_target is not None else None),
            requeue_reason=requeue_reason,
            resolved_prerequisites=_string_list(payload.get("resolved_prerequisites")),
            unresolved_prerequisites=_string_list(payload.get("unresolved_prerequisites")),
        )

    def dev(
        self,
        request: WorkflowRequest,
        plan: PmPlan,
        attempt: int,
        feedback: str | None,
    ) -> DevResult:
        resume_source_state = self._resume_source_state(request=request)
        runtime = self._runtime_for_stage(stage="dev", request=request)
        runtime_command = str(getattr(runtime, "command", "") or "")
        current_head_diff_paths = _current_head_diff_paths(
            request.execution_repo_dir,
            request.base_branch,
        )
        payload = self._invoke_stage_payload(
            request=request,
            stage="dev",
            attempt=attempt,
            system_prompt=render_prompt("workflow/dev_system.j2"),
            user_prompt=render_prompt(
                "workflow/dev_user.j2",
                tenant_id=request.tenant_id,
                project_id=request.project_id or "",
                project_name=request.project_name or "",
                github_repository=request.github_repository or "",
                jira_project_key=request.jira_project_key or "",
                run_id=request.run_id,
                issue_key=request.issue_key,
                execution_repo_dir=request.execution_repo_dir or "",
                execution_branch=request.execution_branch or "",
                base_branch=request.base_branch or "",
                integration_branch=request.integration_branch or "",
                pr_target_branch=request.pr_target_branch or "",
                allow_pr_creation="true" if request.allow_pr_creation else "false",
                attempt=attempt,
                feedback=feedback or "none",
                plan_json=json.dumps(plan.plan_steps),
                acceptance_criteria_json=json.dumps(plan.acceptance_criteria),
                current_head_diff_paths_json=json.dumps(current_head_diff_paths),
                current_pr_url=(
                    str(resume_source_state.get("review_pr_url") or "").strip()
                    or "none"
                ),
                resolved_prerequisites_json=json.dumps(plan.resolved_prerequisites),
                unresolved_prerequisites_json=json.dumps(plan.unresolved_prerequisites),
                pm_outcome=plan.outcome,
                human_inputs_json=json.dumps(request.human_inputs),
                **_stage_prompt_tool_context(
                    tool_stage="dev",
                    runtime_command=runtime_command,
                    worker_platform=request.current_worker_capability.value,
                ),
            ),
            runtime_override=runtime,
        )
        pr_url_raw = payload.get("pr_url")
        pr_url = str(pr_url_raw).strip() if isinstance(pr_url_raw, str) and str(pr_url_raw).strip() else None
        outcome = _require_stage_outcome(payload=payload, stage="dev")
        blocker_message = _optional_string(payload.get("blocker_message"))
        if outcome == "blocked" and blocker_message is None:
            raise CodexRuntimeError("Codex dev response missing blocker_message for blocked outcome")
        return DevResult(
            change_summary=_required_string_list(payload.get("change_summary"), stage="dev", field="change_summary"),
            pr_url=pr_url,
            outcome=outcome,
            blocker_message=blocker_message,
        )

    def test(
        self,
        request: WorkflowRequest,
        plan: PmPlan,
        dev_result: DevResult,
        attempt: int,
    ) -> TestResult:
        runtime = self._runtime_for_stage(stage="test", request=request)
        runtime_command = str(getattr(runtime, "command", "") or "")
        (
            requires_current_head_acceptance_evidence,
            current_head_diff_paths,
        ) = _requires_current_head_acceptance_evidence(
            plan=plan,
            repo_dir=request.execution_repo_dir,
            base_branch=request.base_branch,
        )
        payload = self._invoke_stage_payload(
            request=request,
            stage="test",
            attempt=attempt,
            system_prompt=render_prompt("workflow/test_system.j2"),
            user_prompt=render_prompt(
                "workflow/test_user.j2",
                tenant_id=request.tenant_id,
                project_id=request.project_id or "",
                project_name=request.project_name or "",
                github_repository=request.github_repository or "",
                jira_project_key=request.jira_project_key or "",
                run_id=request.run_id,
                issue_key=request.issue_key,
                execution_repo_dir=request.execution_repo_dir or "",
                execution_branch=request.execution_branch or "",
                base_branch=request.base_branch or "",
                integration_branch=request.integration_branch or "",
                pr_target_branch=request.pr_target_branch or "",
                allow_pr_creation="true" if request.allow_pr_creation else "false",
                attempt=attempt,
                acceptance_criteria_json=json.dumps(plan.acceptance_criteria),
                demo_requirements_json=json.dumps([item.__dict__ for item in plan.demo_requirements]),
                dev_summary_json=json.dumps(dev_result.change_summary),
                pr_url=dev_result.pr_url or "none",
                current_head_diff_paths_json=json.dumps(current_head_diff_paths),
                requires_current_head_acceptance_evidence=(
                    "true" if requires_current_head_acceptance_evidence else "false"
                ),
                suggested_test_commands_json=json.dumps(request.suggested_test_commands),
                resolved_prerequisites_json=json.dumps(plan.resolved_prerequisites),
                unresolved_prerequisites_json=json.dumps(plan.unresolved_prerequisites),
                dev_outcome=dev_result.outcome,
                human_inputs_json=json.dumps(request.human_inputs),
                **_stage_prompt_tool_context(
                    tool_stage="test",
                    runtime_command=runtime_command,
                    worker_platform=request.current_worker_capability.value,
                ),
            ),
            runtime_override=runtime,
        )
        outcome = _require_stage_outcome(payload=payload, stage="test")
        feedback_raw = payload.get("feedback")
        feedback = str(feedback_raw).strip() if isinstance(feedback_raw, str) and str(feedback_raw).strip() else None
        blocker_message = _optional_string(payload.get("blocker_message"))
        if outcome == "blocked" and blocker_message is None:
            raise CodexRuntimeError("Codex test response missing blocker_message for blocked outcome")
        guidance = _required_string_list(payload.get("guidance"), stage="test", field="guidance")
        validation_scope = _required_test_validation_scope(payload.get("validation_scope"))
        if outcome == "continue" and requires_current_head_acceptance_evidence and validation_scope == "targeted_only":
            raise CodexRuntimeError(
                "Codex test response cannot use targeted_only validation_scope when current-head acceptance evidence is required"
            )
        return TestResult(
            guidance=guidance,
            validation_scope=validation_scope,
            outcome=outcome,
            feedback=feedback,
            blocker_message=blocker_message,
        )

    def review(
        self,
        request: WorkflowRequest,
        plan: PmPlan,
        dev_result: DevResult,
        test_result: TestResult,
        attempt: int,
    ) -> ReviewResult:
        resume_source_state = self._resume_source_state(request=request)
        runtime = self._runtime_for_stage(stage="review", request=request)
        runtime_command = str(getattr(runtime, "command", "") or "")
        current_head_diff_paths = _current_head_diff_paths(
            request.execution_repo_dir,
            request.base_branch,
        )
        payload = self._invoke_stage_payload(
            request=request,
            stage="review",
            attempt=attempt,
            system_prompt=render_prompt("workflow/review_system.j2"),
            user_prompt=render_prompt(
                "workflow/review_user.j2",
                tenant_id=request.tenant_id,
                project_id=request.project_id or "",
                project_name=request.project_name or "",
                github_repository=request.github_repository or "",
                jira_project_key=request.jira_project_key or "",
                run_id=request.run_id,
                issue_key=request.issue_key,
                execution_repo_dir=request.execution_repo_dir or "",
                execution_branch=request.execution_branch or "",
                base_branch=request.base_branch or "",
                integration_branch=request.integration_branch or "",
                pr_target_branch=request.pr_target_branch or "",
                allow_pr_creation="true" if request.allow_pr_creation else "false",
                attempt=attempt,
                plan_steps_json=json.dumps(plan.plan_steps),
                acceptance_criteria_json=json.dumps(plan.acceptance_criteria),
                dev_summary_json=json.dumps(dev_result.change_summary),
                test_outcome=test_result.outcome,
                test_validation_scope=test_result.validation_scope,
                test_guidance_json=json.dumps(test_result.guidance),
                test_feedback=test_result.feedback or "none",
                current_head_diff_paths_json=json.dumps(current_head_diff_paths),
                pr_url=(
                    str(resume_source_state.get("review_pr_url") or "").strip()
                    or dev_result.pr_url
                    or "none"
                ),
                resolved_prerequisites_json=json.dumps(plan.resolved_prerequisites),
                unresolved_prerequisites_json=json.dumps(plan.unresolved_prerequisites),
                human_inputs_json=json.dumps(request.human_inputs),
                previous_review_summary_json=json.dumps(resume_source_state.get("review_summary") or []),
                previous_review_feedback=str(resume_source_state.get("review_feedback") or "").strip() or "none",
                **_stage_prompt_tool_context(
                    tool_stage="review",
                    runtime_command=runtime_command,
                    worker_platform=request.current_worker_capability.value,
                ),
            ),
            runtime_override=runtime,
        )
        feedback_raw = payload.get("feedback")
        feedback = str(feedback_raw).strip() if isinstance(feedback_raw, str) and str(feedback_raw).strip() else None
        pr_url_raw = payload.get("pr_url")
        pr_url = (
            str(pr_url_raw).strip()
            if isinstance(pr_url_raw, str) and str(pr_url_raw).strip()
            else str(resume_source_state.get("review_pr_url") or "").strip() or dev_result.pr_url
        )
        outcome = _require_stage_outcome(payload=payload, stage="review")
        blocker_message = _optional_string(payload.get("blocker_message"))
        if outcome == "blocked" and blocker_message is None:
            raise CodexRuntimeError("Codex review response missing blocker_message for blocked outcome")
        return ReviewResult(
            summary=_required_string_list(payload.get("summary"), stage="review", field="summary"),
            outcome=outcome,
            feedback=feedback,
            pr_url=pr_url,
            blocker_message=blocker_message,
        )

    def qa(
        self,
        request: WorkflowRequest,
        plan: PmPlan,
        dev_result: DevResult,
        test_result: TestResult,
        review_result: ReviewResult,
        browser_capture_reference: str,
        available_capture_targets_json: str,
        attempt: int,
    ) -> QaResult:
        runtime = self._runtime_for_stage(stage="qa", request=request)
        runtime_command = str(getattr(runtime, "command", "") or "")
        source_paths_by_target = _capture_target_source_paths_from_available_targets(available_capture_targets_json)
        native_selector_catalog_json = json.dumps(
            _native_selector_catalog(
                request.execution_repo_dir,
                source_paths=_merged_source_paths(source_paths_by_target),
            )
        )
        payload = self._invoke_stage_payload(
            request=request,
            stage="qa",
            attempt=attempt,
            system_prompt=render_prompt("workflow/qa_system.j2"),
            user_prompt=render_prompt(
                "workflow/qa_user.j2",
                tenant_id=request.tenant_id,
                project_id=request.project_id or "",
                project_name=request.project_name or "",
                github_repository=request.github_repository or "",
                jira_project_key=request.jira_project_key or "",
                run_id=request.run_id,
                issue_key=request.issue_key,
                execution_repo_dir=request.execution_repo_dir or "",
                execution_branch=request.execution_branch or "",
                base_branch=request.base_branch or "",
                integration_branch=request.integration_branch or "",
                pr_target_branch=request.pr_target_branch or "",
                attempt=attempt,
                browser_capture_reference=browser_capture_reference,
                available_capture_targets_json=available_capture_targets_json,
                pr_url=review_result.pr_url or dev_result.pr_url or "none",
                acceptance_criteria_json=json.dumps(plan.acceptance_criteria),
                demo_requirements_json=json.dumps([item.__dict__ for item in plan.demo_requirements]),
                native_selector_catalog_json=native_selector_catalog_json,
                dev_summary_json=json.dumps(dev_result.change_summary),
                test_guidance_json=json.dumps(test_result.guidance),
                review_summary_json=json.dumps(review_result.summary),
                human_inputs_json=json.dumps(request.human_inputs),
                **_stage_prompt_tool_context(
                    tool_stage="qa",
                    runtime_command=runtime_command,
                    worker_platform=request.current_worker_capability.value,
                ),
            ),
            runtime_override=runtime,
        )
        outcome = _require_stage_outcome(payload=payload, stage="qa")
        blocker_message = _optional_string(payload.get("blocker_message"))
        if outcome == "blocked" and blocker_message is None:
            raise CodexRuntimeError("Codex qa response missing blocker_message for blocked outcome")
        feedback = _optional_string(payload.get("feedback"))
        return QaResult(
            summary=_required_string_list(payload.get("summary"), stage="qa", field="summary"),
            scenarios=_validate_native_qa_scenarios(
                scenarios=_required_qa_scenarios(payload.get("scenarios")),
                repo_dir=request.execution_repo_dir,
                source_paths_by_target=source_paths_by_target,
            ),
            recordings=_qa_recordings(payload.get("recordings")),
            outcome=outcome,
            feedback=feedback,
            blocker_message=blocker_message,
        )



def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    parsed: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return []
        if not item.strip():
            return []
        parsed.append(item)
    return parsed


def _required_string_list(value: object, *, stage: str, field: str) -> list[str]:
    normalized = _string_list(value)
    if normalized:
        return normalized
    raise CodexRuntimeError(f"Codex {stage} response missing required non-empty {field}")


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    if not value.strip():
        return None
    return value


def _required_test_validation_scope(value: object) -> str:
    if isinstance(value, str) and value in _TEST_VALIDATION_SCOPES:
        return value
    raise CodexRuntimeError("Codex test response missing required validation_scope")


def _demo_requirements(value: object) -> list[DemoRequirement]:
    if not isinstance(value, list):
        return []
    parsed: list[DemoRequirement] = []
    for item in value:
        if not isinstance(item, dict):
            return []
        title = _optional_string(item.get("title"))
        acceptance_criterion = _optional_string(item.get("acceptance_criterion"))
        capture_target = _optional_string(item.get("capture_target"))
        variants = _string_list(item.get("variants"))
        if title is None or acceptance_criterion is None or capture_target is None or not variants:
            return []
        if capture_target not in _QA_CAPTURE_TARGETS:
            return []
        parsed.append(
            DemoRequirement(
                title=title,
                acceptance_criterion=acceptance_criterion,
                capture_target=capture_target,  # type: ignore[arg-type]
                variants=variants,
            )
        )
    return parsed


def _required_demo_requirements(value: object, *, stage: str) -> list[DemoRequirement]:
    if not isinstance(value, list) or not value:
        raise CodexRuntimeError(f"Codex {stage} response missing required non-empty demo_requirements")
    parsed = _demo_requirements(value)
    if parsed:
        return parsed
    has_missing_required_fields = any(
        not isinstance(item, dict)
        or _optional_string(item.get("title")) is None
        or _optional_string(item.get("acceptance_criterion")) is None
        or _optional_string(item.get("capture_target")) is None
        for item in value
    )
    if has_missing_required_fields:
        raise CodexRuntimeError(f"Codex {stage} response missing required non-empty demo_requirements")
    if any(isinstance(item, dict) and not _string_list(item.get("variants")) for item in value):
        raise CodexRuntimeError(
            f"Codex {stage} response demo_requirements must include non-empty variants for QA walkthrough coverage"
        )
    raise CodexRuntimeError(f"Codex {stage} response missing required non-empty demo_requirements")


def _qa_steps(value: object) -> list[QaStep]:
    if not isinstance(value, list):
        return []
    parsed: list[QaStep] = []
    for item in value:
        if not isinstance(item, dict):
            return []
        action = _optional_string(item.get("action"))
        if action not in {
            "goto",
            "relaunch_app",
            "click",
            "fill",
            "press",
            "select_option",
            "wait_for_text",
            "wait_for_url",
            "assert_text",
            "assert_visible",
        }:
            return []
        parsed.append(
            QaStep(
                action=action,  # type: ignore[arg-type]
                selector=_optional_string(item.get("selector")),
                value=_optional_string(item.get("value")),
            )
        )
    return parsed


def _required_qa_scenarios(value: object) -> list[QaScenario]:
    if not isinstance(value, list) or not value:
        raise CodexRuntimeError("Codex qa response missing required non-empty scenarios")
    parsed: list[QaScenario] = []
    for item in value:
        if not isinstance(item, dict):
            raise CodexRuntimeError("Codex qa response contained invalid scenario")
        name = _optional_string(item.get("name"))
        objective = _optional_string(item.get("objective"))
        capture_target = _optional_string(item.get("capture_target"))
        start_path = _optional_string(item.get("start_path")) or "/"
        expected_outcomes = _string_list(item.get("expected_outcomes"))
        steps = _qa_steps(item.get("steps"))
        if name is None or objective is None or capture_target is None or not steps:
            raise CodexRuntimeError("Codex qa response missing required scenario fields")
        if capture_target not in _QA_CAPTURE_TARGETS:
            raise CodexRuntimeError("Codex qa response contains invalid capture_target")
        parsed.append(
            QaScenario(
                name=name,
                objective=objective,
                capture_target=capture_target,  # type: ignore[arg-type]
                start_path=start_path,
                expected_outcomes=expected_outcomes,
                steps=steps,
            )
        )
    return parsed


def _qa_recordings(value: object) -> list[QaRecording]:
    if value is None:
        return []
    if not isinstance(value, list):
        return []
    parsed: list[QaRecording] = []
    for item in value:
        if not isinstance(item, dict):
            return []
        name = _optional_string(item.get("name"))
        artifact_url = _optional_string(item.get("artifact_url"))
        object_key = _optional_string(item.get("object_key"))
        capture_target = _optional_string(item.get("capture_target"))
        capture_reference = _optional_string(item.get("capture_reference"))
        content_sha256 = _content_sha256(item.get("content_sha256"))
        if (
            name is None
            or artifact_url is None
            or object_key is None
            or capture_target is None
            or capture_reference is None
            or content_sha256 is None
        ):
            return []
        if capture_target not in _QA_CAPTURE_TARGETS:
            return []
        parsed.append(
            QaRecording(
                name=name,
                artifact_url=artifact_url,
                object_key=object_key,
                capture_target=capture_target,  # type: ignore[arg-type]
                capture_reference=capture_reference,
                content_sha256=content_sha256,
            )
        )
    return parsed


def _content_sha256(value: object) -> str | None:
    parsed = _optional_string(value)
    if parsed is None:
        return None
    normalized = parsed.lower()
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        return None
    return normalized


def _normalize_stage_outcome(value: object) -> StageOutcome | None:
    if isinstance(value, str) and value in {"continue", "requeue", "waiting_for_input", "blocked", "failed"}:
        return value  # type: ignore[return-value]
    return None


def _require_stage_outcome(*, payload: dict[str, Any], stage: str) -> StageOutcome:
    outcome = _normalize_stage_outcome(payload.get("outcome"))
    if outcome is None:
        raise CodexRuntimeError(f"Codex {stage} response missing valid required outcome")
    return outcome



def answer_board_question_with_runtime(
    *,
    runtime: CodexRuntime,
    question: str,
    project_keys: list[str],
    issues: list[dict],
    status_counts: dict[str, int],
    invocation_context: AgentInvocationContext,
    history: list[dict] | None = None,
    github_context: dict | None = None,
    sqlalchemy_session: Session | None = None,
    settings: Any | None = None,
    answer_persona_id: str | None = None,
) -> str:
    normalized_history = history if isinstance(history, list) else []
    normalized_github_context = github_context or {}
    history_slice = normalized_history[-25:] if normalized_history else []
    persona = str(answer_persona_id or "").strip().lower() or "pm"
    user_prompt = render_prompt(
        "discord/ask_answer_user.j2",
        question=question,
        persona_id=persona,
        project_keys_json=json.dumps(project_keys),
        status_counts_json=json.dumps(status_counts),
        github_context_json=json.dumps(normalized_github_context),
        history_json=json.dumps(history_slice),
        issues_json=json.dumps(issues[:40]),
    )
    payload = _invoke_discord_json_maybe_tools(
        runtime=runtime,
        context=invocation_context,
        system_prompt=render_prompt("discord/ask_answer_system.j2", persona_id=persona),
        user_prompt=user_prompt,
        tool_stage="discord_ask_answer",
        sqlalchemy_session=sqlalchemy_session,
        settings=settings,
        max_tool_hops=8,
    )
    try:
        return RuntimeMessage.from_payload(payload, context="Ask answer payload").message
    except RuntimeError as exc:
        raise CodexRuntimeError(str(exc)) from exc


def answer_pm_question_with_runtime(
    *,
    runtime: CodexRuntime,
    question: str,
    action: str | None,
    project_keys: list[str],
    issues: list[dict],
    status_counts: dict[str, int],
    invocation_context: AgentInvocationContext,
    history: list[dict] | None = None,
    github_context: dict | None = None,
) -> dict:
    normalized_action = str(action or "ask").strip().lower()
    if normalized_action not in {"ask", "approve"}:
        normalized_action = "ask"
    normalized_history = history if isinstance(history, list) else []
    normalized_github_context = github_context or {}
    payload = invoke_runtime_json(
        runtime=runtime,
        context=invocation_context,
        system_prompt=render_prompt("discord/pm_answer_system.j2"),
        user_prompt=render_prompt(
            "discord/pm_answer_user.j2",
            action=normalized_action,
            question=question,
            project_keys_json=json.dumps(project_keys),
            status_counts_json=json.dumps(status_counts),
            github_context_json=json.dumps(normalized_github_context),
            history_json=json.dumps(normalized_history[-25:]),
            issues_json=json.dumps(issues[:40]),
        ),
    )
    try:
        return PMMessageBrief.from_payload(payload, context="PM answer payload").to_payload()
    except RuntimeError as exc:
        raise CodexRuntimeError(str(exc)) from exc


def classify_engineering_clarification_with_runtime(
    *,
    runtime: CodexRuntime,
    parent_issue_key: str,
    parent_summary: str,
    parent_description: str,
    child_issue_key: str,
    child_summary: str,
    child_description: str,
    question: str,
    invocation_context: AgentInvocationContext,
) -> EngineeringClarification:
    payload = invoke_runtime_json(
        runtime=runtime,
        context=invocation_context,
        system_prompt=render_prompt("jira/engineering_clarification_system.j2"),
        user_prompt=render_prompt(
            "jira/engineering_clarification_user.j2",
            parent_issue_key=parent_issue_key,
            parent_summary=parent_summary,
            parent_description=parent_description,
            child_issue_key=child_issue_key,
            child_summary=child_summary,
            child_description=child_description,
            question=question,
        ),
    )
    try:
        return EngineeringClarification.from_payload(payload)
    except RuntimeError as exc:
        raise CodexRuntimeError(str(exc)) from exc


def route_voice_entry_with_runtime(
    *,
    runtime: CodexRuntime,
    transcript: str,
    invocation_context: AgentInvocationContext,
    entry_source: str,
    history: list[dict] | None = None,
    room_context: dict | None = None,
    sqlalchemy_session: Session | None = None,
    settings: Any | None = None,
) -> VoiceEntryRoute:
    """Route voice transcript to ask vs interview (strict JSON from agent runtime)."""
    normalized_history = history if isinstance(history, list) else []
    user_prompt = render_prompt(
        "discord/voice_entry_router_user.j2",
        transcript=transcript,
        entry_source=entry_source,
        history_json=json.dumps(normalized_history[-25:]),
        room_context_json=json.dumps(room_context or {}),
    )
    payload = _invoke_discord_json_maybe_tools(
        runtime=runtime,
        context=invocation_context,
        system_prompt=render_prompt("discord/voice_entry_router_system.j2"),
        user_prompt=user_prompt,
        tool_stage="voice_entry_router",
        sqlalchemy_session=sqlalchemy_session,
        settings=settings,
        max_tool_hops=6,
    )
    try:
        return VoiceEntryRoute.from_payload(payload)
    except RuntimeError as exc:
        raise CodexRuntimeError(str(exc)) from exc


def answer_voice_room_persona_with_runtime(
    *,
    runtime: CodexRuntime,
    persona_id: str,
    transcript: str,
    project_keys: list[str],
    issues: list[dict],
    status_counts: dict[str, int],
    invocation_context: AgentInvocationContext,
    history: list[dict] | None = None,
    github_context: dict | None = None,
    room_context: dict | None = None,
    sqlalchemy_session: Session | None = None,
    settings: Any | None = None,
) -> dict:
    persona = get_voice_room_persona_definition(persona_id)
    normalized_history = history if isinstance(history, list) else []
    system_prompt = render_prompt(persona.system_prompt_template)
    if sqlalchemy_session is not None and settings is not None and _stage_has_governed_tools(
        "discord_voice_room_persona",
        runtime_command=str(getattr(runtime, "command", "") or ""),
    ):
        system_prompt += (
            "\n\nWhen the user message includes an Allowed tools section, use tool_request then final_response. "
            "final_response.result must match the same JSON shape required above (same keys as without tools)."
        )
    user_prompt = render_prompt(
        persona.user_prompt_template,
        transcript=transcript,
        history_json=json.dumps(normalized_history[-25:]),
        room_context_json=json.dumps(
            room_context
            or {
                "project_keys": project_keys,
                "status_counts": status_counts,
                "github_context": github_context or {},
                "issues": issues[:40],
            }
        ),
    )
    payload = _invoke_discord_json_maybe_tools(
        runtime=runtime,
        context=invocation_context,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        tool_stage="discord_voice_room_persona",
        sqlalchemy_session=sqlalchemy_session,
        settings=settings,
        max_tool_hops=6,
    )
    try:
        return PMMessageBrief.from_payload(payload, context="Voice room persona payload").to_payload()
    except RuntimeError as exc:
        raise CodexRuntimeError(str(exc)) from exc


def plan_discord_ask_intent_with_runtime(
    *,
    runtime: CodexRuntime,
    question: str,
    project_keys: list[str],
    issues: list[dict],
    status_counts: dict[str, int],
    invocation_context: AgentInvocationContext,
    history: list[dict] | None = None,
    github_context: dict | None = None,
) -> AskIntent:
    normalized_history: list[dict] = []
    normalized_github_context = github_context or {}
    payload = invoke_runtime_json(
        runtime=runtime,
        context=invocation_context,
        system_prompt=render_prompt("discord/ask_intent_system.j2"),
        user_prompt=render_prompt(
            "discord/ask_intent_user.j2",
            question=question,
            project_keys_json=json.dumps(project_keys),
            status_counts_json=json.dumps(status_counts),
            github_context_json=json.dumps(normalized_github_context),
            history_json=json.dumps(normalized_history),
            issues_json=json.dumps(issues[:40]),
        ),
    )
    try:
        return AskIntent.from_payload(payload)
    except RuntimeError as exc:
        raise CodexRuntimeError(str(exc)) from exc


def plan_seed_issues_with_runtime(
    *,
    runtime: CodexRuntime,
    prompt_markdown: str,
    allowed_project_keys: list[str],
    project_issue_types_by_key: dict[str, list[str]],
    invocation_context: AgentInvocationContext,
) -> EngineeringSeedPlan:
    payload = invoke_runtime_json(
        runtime=runtime,
        context=invocation_context,
        system_prompt=render_prompt("discord/issues_seed_system.j2"),
        user_prompt=render_prompt(
            "discord/issues_seed_user.j2",
            allowed_project_keys_json=json.dumps(allowed_project_keys),
            project_issue_types_json=json.dumps(project_issue_types_by_key, sort_keys=True),
            prompt_markdown=prompt_markdown,
        ),
    )
    try:
        return EngineeringSeedPlan.from_payload(payload)
    except RuntimeError as exc:
        raise CodexRuntimeError(str(exc)) from exc


def plan_pm_parent_issues_with_runtime(
    *,
    runtime: CodexRuntime,
    prompt_markdown: str,
    allowed_project_keys: list[str],
    project_issue_types_by_key: dict[str, list[str]],
    invocation_context: AgentInvocationContext,
) -> PmParentSeedPlan:
    payload = invoke_runtime_json(
        runtime=runtime,
        context=invocation_context,
        system_prompt=render_prompt("discord/pm_seed_batch_system.j2"),
        user_prompt=render_prompt(
            "discord/pm_seed_batch_user.j2",
            allowed_project_keys_json=json.dumps(allowed_project_keys),
            project_issue_types_json=json.dumps(project_issue_types_by_key, sort_keys=True),
            prompt_markdown=prompt_markdown,
        ),
    )
    try:
        return PmParentSeedPlan.from_payload(payload)
    except RuntimeError as exc:
        raise CodexRuntimeError(str(exc)) from exc
