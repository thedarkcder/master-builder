from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, status

from orchestrator.core.clarification_questions import ClarificationQuestionSet
from orchestrator.api.discord.seed.description import (
    build_engineering_child_description,
    build_parent_feature_description,
)
from orchestrator.tools.atlassian_oauth import JiraIssueCreateInput

_MAX_ENGINEERING_CHILDREN = 12
_PM_COMPLETE_STATUSES = {"ready_to_write", "pm_completed"}
_PLANNING_COMPLETE_STATUSES = {"planning_completed"}
_SYNC_CURRENT_LABEL = "sync-current"
_SYNC_STALE_LABEL = "sync-stale"
_SYNC_BLOCKED_LABEL = "sync-blocked"
_PM_COMPLETE_LABEL = "pm-complete"
_PLANNING_COMPLETE_LABEL = "planning-complete"
_PM_PARENT_LABEL = "pm-parent"
_ENGINEERING_CHILD_LABEL = "engineering-child"


def _string_list_field(*, issue_index: int, field_name: str, raw_value: object) -> list[str]:
    if raw_value is None:
        return []
    if not isinstance(raw_value, list):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Codex issue draft {issue_index} has invalid '{field_name}' (expected list of strings)",
        )
    values: list[str] = []
    for entry in raw_value:
        if not isinstance(entry, str):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Codex issue draft {issue_index} has invalid '{field_name}' entry type",
            )
        text = entry.strip()
        if text:
            values.append(text)
    return values


def _optional_string(*, issue_index: int, field_name: str, raw_value: object) -> str:
    if raw_value is None:
        return ""
    if not isinstance(raw_value, str):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Codex issue draft {issue_index} has invalid '{field_name}' (expected string)",
        )
    return raw_value.strip()


def _normalize_issue_key(raw_value: object, *, field_name: str, issue_index: int, issue_key_pattern) -> str | None:  # noqa: ANN001
    if raw_value is None:
        return None
    if not isinstance(raw_value, str):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Codex issue draft {issue_index} has invalid '{field_name}' (expected string)",
        )
    issue_key = raw_value.strip().upper()
    if not issue_key:
        return None
    if issue_key_pattern.match(issue_key) is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Codex issue draft {issue_index} has invalid {field_name} '{issue_key}'",
        )
    return issue_key


def _normalized_status(value: object) -> str:
    return str(value or "").strip().lower()


def _is_pm_complete(pm_status: object) -> bool:
    return _normalized_status(pm_status) in _PM_COMPLETE_STATUSES


def _is_planning_complete(planning_state: object) -> bool:
    return _normalized_status(planning_state) in _PLANNING_COMPLETE_STATUSES


def _normalize_label(raw_value: str, *, prefix: str = "") -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", str(raw_value or "").strip().lower()).strip("-")
    if not normalized:
        return ""
    if prefix:
        normalized = f"{prefix}{normalized}"
    return normalized[:64]


def _dedupe_labels(*label_groups: list[str]) -> list[str]:
    seen: set[str] = set()
    labels: list[str] = []
    for group in label_groups:
        for raw_label in group:
            label = str(raw_label or "").strip()
            if not label:
                continue
            key = label.casefold()
            if key in seen:
                continue
            seen.add(key)
            labels.append(label)
    return labels


def _sync_label(sync_status: str) -> str:
    normalized = str(sync_status or "").strip().lower()
    if normalized == "children_current":
        return _SYNC_CURRENT_LABEL
    if normalized in {"sync_blocked", "planning_blocked"}:
        return _SYNC_BLOCKED_LABEL
    return _SYNC_STALE_LABEL


def _parse_questions(raw_questions: object) -> list[str]:
    questions: list[str] = []
    seen: set[str] = set()
    if not isinstance(raw_questions, list):
        return questions
    for raw_question in raw_questions:
        question = str(raw_question or "").strip()
        if not question or question in seen:
            continue
        seen.add(question)
        questions.append(question)
    return questions


@dataclass(frozen=True)
class PlanningPackageDraft:
    planning_state: str
    specialist_summary: list[str]
    architecture_summary: list[str]
    architecture_diagram: str | None
    child_issues: list[dict[str, Any]]

    @property
    def blocked(self) -> bool:
        return bool(self.planning_state) and not _is_planning_complete(self.planning_state)

    @property
    def planning_state_for_description(self) -> str | None:
        if self.planning_state:
            return self.planning_state
        if self.blocked:
            return "planning_blocked"
        return None


def _string_list_from_stage(*, stage_name: str, field_name: str, raw_value: object) -> list[str]:
    if raw_value is None:
        return []
    if not isinstance(raw_value, list):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Planning package stage '{stage_name}' has invalid '{field_name}' (expected list of strings)",
        )
    values: list[str] = []
    for entry in raw_value:
        if not isinstance(entry, str):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Planning package stage '{stage_name}' has invalid item type",
            )
        text = entry.strip()
        if text:
            values.append(text)
    return values


def _question_list_from_stage(*, stage_name: str, raw_value: object) -> list[str]:
    if raw_value is None:
        return []
    if not isinstance(raw_value, list):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Planning package stage '{stage_name}' has invalid 'open_behavior_questions' "
                "(expected list of clarification questions)"
            ),
        )
    return [question.question for question in ClarificationQuestionSet.from_values(raw_value).questions]


def _planning_stage_summary_lines(*, stage_name: str, raw_stage: object) -> list[str]:
    if raw_stage is None:
        return []
    if not isinstance(raw_stage, dict):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Planning package stage '{stage_name}' must be an object",
        )
    lines: list[str] = []
    for field_name, label in (
        ("findings", "Findings"),
        ("recommendations", "Recommendations"),
        ("required_tasks", "Required tasks"),
        ("acceptance_impacts", "Acceptance impacts"),
    ):
        values = _string_list_from_stage(stage_name=stage_name, field_name=field_name, raw_value=raw_stage.get(field_name))
        if values:
            lines.append(f"{stage_name.title()} {label}: {'; '.join(values)}")
    question_values = _question_list_from_stage(
        stage_name=stage_name,
        raw_value=raw_stage.get("open_behavior_questions"),
    )
    if question_values:
        lines.append(f"{stage_name.title()} Open behavior questions: {'; '.join(question_values)}")
    return lines


def normalize_planning_package(raw_planning_package: object) -> PlanningPackageDraft:
    if raw_planning_package is None:
        return PlanningPackageDraft("", [], [], None, [])
    if not isinstance(raw_planning_package, dict):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Planning package must be an object",
        )
    specialist_outputs = raw_planning_package.get("specialist_outputs")
    if specialist_outputs is not None and not isinstance(specialist_outputs, dict):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Planning package specialist_outputs must be an object",
        )
    child_issues = raw_planning_package.get("child_issues")
    if child_issues is None:
        child_issues = raw_planning_package.get("recommended_child_tickets")
    if child_issues is None:
        child_issues = raw_planning_package.get("engineering_children")
    if not isinstance(child_issues, list):
        child_issues = []
    planning_state = _normalized_status(raw_planning_package.get("planning_state") or raw_planning_package.get("state"))
    summary_lines: list[str] = []
    architecture_summary_raw = raw_planning_package.get("architecture_summary")
    if architecture_summary_raw is not None and not isinstance(architecture_summary_raw, list):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Planning package architecture_summary must be a list of strings",
        )
    architecture_diagram_raw = raw_planning_package.get("architecture_diagram")
    if architecture_diagram_raw is not None and not isinstance(architecture_diagram_raw, str):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Planning package architecture_diagram must be a string",
        )
    architecture_summary: list[str] = [
        str(value).strip() for value in (architecture_summary_raw or []) if str(value).strip()
    ]
    architecture_diagram = (
        architecture_diagram_raw.strip()
        if isinstance(architecture_diagram_raw, str) and architecture_diagram_raw.strip()
        else None
    )
    if isinstance(specialist_outputs, dict):
        for stage_name in ("architecture", "engineering", "security", "testing"):
            summary_lines.extend(
                _planning_stage_summary_lines(
                    stage_name=stage_name,
                    raw_stage=specialist_outputs.get(stage_name),
                )
            )
        if not architecture_summary:
            architecture_stage = specialist_outputs.get("architecture")
            if isinstance(architecture_stage, dict):
                for field_name in ("findings", "recommendations", "acceptance_impacts"):
                    architecture_summary.extend(
                        _string_list_from_stage(
                            stage_name="architecture",
                            field_name=field_name,
                            raw_value=architecture_stage.get(field_name),
                        )
                    )
                if architecture_diagram is None and isinstance(architecture_stage.get("mermaid_diagram"), str):
                    architecture_diagram = architecture_stage.get("mermaid_diagram", "").strip() or None
    return PlanningPackageDraft(
        planning_state=planning_state,
        specialist_summary=summary_lines,
        architecture_summary=architecture_summary,
        architecture_diagram=architecture_diagram,
        child_issues=child_issues,
    )


@dataclass(frozen=True)
class ParentIssueDraft:
    summary: str
    issue_type: str
    objective: str
    user_value: str
    recommendation: str
    scope_in: list[str]
    scope_out: list[str]
    acceptance_criteria: list[str]
    ui_references: list[str]
    success_outcomes: list[str]
    dependencies: list[str]
    risks: list[str]
    open_questions: list[str]
    labels: list[str]
    requested_issue_key: str | None

    @property
    def revision(self) -> str:
        payload = json.dumps(
            {
                "summary": self.summary,
                "issue_type": self.issue_type,
                "objective": self.objective,
                "user_value": self.user_value,
                "recommendation": self.recommendation,
                "scope_in": self.scope_in,
                "scope_out": self.scope_out,
                "acceptance_criteria": self.acceptance_criteria,
                "ui_references": self.ui_references,
                "success_outcomes": self.success_outcomes,
                "dependencies": self.dependencies,
                "risks": self.risks,
                "open_questions": self.open_questions,
                "labels": self.labels,
                "requested_issue_key": self.requested_issue_key,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]

    def normalized_issue_type(self, *, engineering_children: list[EngineeringChildDraft], available_issue_types: list[str]) -> str:
        del engineering_children
        by_lower = {name.casefold(): name for name in available_issue_types if str(name).strip()}
        if not by_lower:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="No supported parent Jira issue type is available for this project",
            )
        if not self.issue_type:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Parent issue '{self.summary}' is missing issue_type",
            )
        requested_match = by_lower.get(self.issue_type.casefold())
        if requested_match:
            return requested_match
        available = ", ".join(available_issue_types)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Parent issue '{self.summary}' requested unavailable Jira issue_type '{self.issue_type}'; available issue types: {available}",
        )

    def with_issue_type(self, issue_type: str) -> ParentIssueDraft:
        return ParentIssueDraft(
            summary=self.summary,
            issue_type=issue_type,
            objective=self.objective,
            user_value=self.user_value,
            recommendation=self.recommendation,
            scope_in=list(self.scope_in),
            scope_out=list(self.scope_out),
            acceptance_criteria=list(self.acceptance_criteria),
            ui_references=list(self.ui_references),
            success_outcomes=list(self.success_outcomes),
            dependencies=list(self.dependencies),
            risks=list(self.risks),
            open_questions=list(self.open_questions),
            labels=list(self.labels),
            requested_issue_key=self.requested_issue_key,
        )

    def to_jira_input(
        self,
        *,
        sync_status: str,
        pm_status: str | None = None,
        planning_state: str | None = None,
    ) -> JiraIssueCreateInput:
        return JiraIssueCreateInput(
            summary=self.summary,
            description=build_parent_feature_description(
                objective=self.objective,
                user_value=self.user_value,
                recommendation=self.recommendation,
                scope_in=self.scope_in,
                scope_out=self.scope_out,
                acceptance_criteria=self.acceptance_criteria,
                ui_references=self.ui_references,
                success_outcomes=self.success_outcomes,
                dependencies_and_risks=[*self.dependencies, *self.risks],
                open_questions=self.open_questions,
                parent_revision=self.revision,
                sync_status=sync_status,
                pm_status=pm_status,
                planning_state=planning_state,
            ),
            labels=_dedupe_labels(
                self.labels,
                [_PM_PARENT_LABEL, _sync_label(sync_status)],
                [_PM_COMPLETE_LABEL] if _is_pm_complete(pm_status) else [],
                [_PLANNING_COMPLETE_LABEL] if _is_planning_complete(planning_state) else [],
            ),
            issue_type=self.issue_type,
        )


@dataclass(frozen=True)
class EngineeringChildDraft:
    summary: str
    issue_type: str
    capability: str
    delivery: str
    expected_outcome: str
    acceptance_criteria: list[str]
    dependencies: list[str]
    risks: list[str]
    how_to_test: list[str]
    done_means: list[str]
    labels: list[str]
    requested_issue_key: str | None

    def with_issue_type(self, issue_type: str) -> EngineeringChildDraft:
        return EngineeringChildDraft(
            summary=self.summary,
            issue_type=issue_type,
            capability=self.capability,
            delivery=self.delivery,
            expected_outcome=self.expected_outcome,
            acceptance_criteria=list(self.acceptance_criteria),
            dependencies=list(self.dependencies),
            risks=list(self.risks),
            how_to_test=list(self.how_to_test),
            done_means=list(self.done_means),
            labels=list(self.labels),
            requested_issue_key=self.requested_issue_key,
        )

    def to_jira_input(
        self,
        *,
        parent_issue_key: str,
        parent_summary: str,
        parent_revision: str,
        sync_status: str,
        specialist_summary: list[str] | None = None,
        planning_state: str | None = None,
        pm_status: str | None = None,
    ) -> JiraIssueCreateInput:
        parent_label = _normalize_label(parent_issue_key, prefix="parent-")
        return JiraIssueCreateInput(
            summary=self.summary,
            description=build_engineering_child_description(
                parent_issue_key=parent_issue_key,
                parent_summary=parent_summary,
                parent_revision=parent_revision,
                capability=self.capability,
                delivery=self.delivery,
                expected_outcome=self.expected_outcome,
                acceptance_criteria=self.acceptance_criteria,
                how_to_test=self.how_to_test,
                done_means=self.done_means,
                dependencies_and_risks=[*self.dependencies, *self.risks],
                specialist_summary=specialist_summary,
                planning_state=planning_state,
            ),
            labels=_dedupe_labels(
                self.labels,
                [_ENGINEERING_CHILD_LABEL, parent_label, _sync_label(sync_status)],
                [_PM_COMPLETE_LABEL] if _is_pm_complete(pm_status) else [],
                [_PLANNING_COMPLETE_LABEL] if _is_planning_complete(planning_state) else [],
            ),
            issue_type=self.issue_type,
            parent_issue_key=parent_issue_key,
            linked_parent_issue_key=parent_issue_key,
        )


@dataclass(frozen=True)
class EngineeringSeedDraftSet:
    parent_issue: ParentIssueDraft
    engineering_children: list[EngineeringChildDraft]
    clarification_questions: list[str]
    pm_status: str | None
    planning_package: PlanningPackageDraft


@dataclass(frozen=True)
class ParentSeedDraftSet:
    parent_issues: list[ParentIssueDraft]
    clarification_questions: list[str]
    pm_status: str | None


def parse_engineering_seed_drafts(
    *,
    plan_payload: dict[str, Any],
    force_issue_keys: list[str],
    issue_key_pattern,
    allow_empty_children: bool = False,
    pm_status: str | None = None,
    planning_package: dict[str, Any] | None = None,
    required_child_issue_type: str | None = None,
) -> EngineeringSeedDraftSet:
    clarification_questions = _parse_questions(plan_payload.get("questions"))
    effective_pm_status = _normalized_status(pm_status or plan_payload.get("pm_status") or plan_payload.get("interview_status"))
    if pm_status is not None and not _is_pm_complete(effective_pm_status):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="PM interview is not ready to write Jira issues yet",
        )
    effective_planning_package = normalize_planning_package(
        planning_package
        if planning_package is not None
        else plan_payload.get("planning_package") or plan_payload.get("specialist_planning")
    )
    planning_package_supplied = planning_package is not None or "planning_package" in plan_payload or "specialist_planning" in plan_payload
    child_issue_drafts = effective_planning_package.child_issues or plan_payload.get("engineering_children")
    allow_empty_child_drafts = allow_empty_children or planning_package_supplied
    parent_issue = _parse_parent_issue(
        raw_parent=plan_payload.get("parent_issue"),
        force_issue_keys=force_issue_keys,
        issue_key_pattern=issue_key_pattern,
    )
    engineering_children = _parse_engineering_children(
        raw_children=child_issue_drafts,
        force_issue_keys=force_issue_keys,
        issue_key_pattern=issue_key_pattern,
        allow_empty_children=allow_empty_child_drafts,
        required_child_issue_type=required_child_issue_type,
    )
    return EngineeringSeedDraftSet(
        parent_issue=parent_issue,
        engineering_children=engineering_children,
        clarification_questions=clarification_questions,
        pm_status=effective_pm_status or None,
        planning_package=effective_planning_package,
    )


def parse_parent_seed_drafts(
    *,
    plan_payload: dict[str, Any],
    force_issue_keys: list[str],
    issue_key_pattern,
    pm_status: str | None = None,
) -> ParentSeedDraftSet:
    clarification_questions = _parse_questions(plan_payload.get("questions"))
    effective_pm_status = _normalized_status(pm_status or plan_payload.get("pm_status") or plan_payload.get("interview_status"))
    if pm_status is not None and not _is_pm_complete(effective_pm_status):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="PM interview is not ready to write Jira parent issues yet",
        )
    parent_issues = _parse_parent_issue_drafts(
        raw_issues=plan_payload.get("issues"),
        force_issue_keys=force_issue_keys,
        issue_key_pattern=issue_key_pattern,
    )
    return ParentSeedDraftSet(
        parent_issues=parent_issues,
        clarification_questions=clarification_questions,
        pm_status=effective_pm_status or None,
    )


def _parse_parent_issue(
    *,
    raw_parent: object,
    force_issue_keys: list[str],
    issue_key_pattern,
) -> ParentIssueDraft:  # noqa: ANN001
    if not isinstance(raw_parent, dict):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Codex did not return parent_issue")
    issue_index = 1
    summary = _optional_string(issue_index=issue_index, field_name="summary", raw_value=raw_parent.get("summary"))
    if not summary:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Codex parent_issue is missing summary")
    issue_type = _optional_string(
        issue_index=issue_index,
        field_name="issue_type",
        raw_value=raw_parent.get("issue_type"),
    )
    requested_issue_key = _normalize_issue_key(
        raw_parent.get("issue_key"),
        field_name="issue_key",
        issue_index=issue_index,
        issue_key_pattern=issue_key_pattern,
    )
    if requested_issue_key is None and force_issue_keys:
        requested_issue_key = force_issue_keys[0]
    labels = _string_list_field(issue_index=issue_index, field_name="labels", raw_value=raw_parent.get("labels"))
    return ParentIssueDraft(
        summary=summary[:90],
        issue_type=issue_type,
        objective=_optional_string(issue_index=issue_index, field_name="objective", raw_value=raw_parent.get("objective")),
        user_value=_optional_string(issue_index=issue_index, field_name="user_value", raw_value=raw_parent.get("user_value")),
        recommendation=_optional_string(issue_index=issue_index, field_name="recommendation", raw_value=raw_parent.get("recommendation")),
        scope_in=_string_list_field(issue_index=issue_index, field_name="scope_in", raw_value=raw_parent.get("scope_in")),
        scope_out=_string_list_field(issue_index=issue_index, field_name="scope_out", raw_value=raw_parent.get("scope_out")),
        acceptance_criteria=_string_list_field(issue_index=issue_index, field_name="acceptance_criteria", raw_value=raw_parent.get("acceptance_criteria")),
        ui_references=_string_list_field(issue_index=issue_index, field_name="ui_references", raw_value=raw_parent.get("ui_references")),
        success_outcomes=_string_list_field(issue_index=issue_index, field_name="success_outcomes", raw_value=raw_parent.get("success_outcomes")),
        dependencies=_string_list_field(issue_index=issue_index, field_name="dependencies", raw_value=raw_parent.get("dependencies")),
        risks=_string_list_field(issue_index=issue_index, field_name="risks", raw_value=raw_parent.get("risks")),
        open_questions=_string_list_field(issue_index=issue_index, field_name="open_questions", raw_value=raw_parent.get("open_questions")),
        labels=labels,
        requested_issue_key=requested_issue_key,
    )


def _parse_parent_issue_drafts(
    *,
    raw_issues: object,
    force_issue_keys: list[str],
    issue_key_pattern,
) -> list[ParentIssueDraft]:  # noqa: ANN001
    if not isinstance(raw_issues, list):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Codex did not return issue drafts")
    issues: list[ParentIssueDraft] = []
    for issue_index, item in enumerate(raw_issues[:12], start=1):
        if not isinstance(item, dict):
            continue
        summary = _optional_string(issue_index=issue_index, field_name="summary", raw_value=item.get("summary"))
        if not summary:
            continue
        requested_issue_key = _normalize_issue_key(
            item.get("issue_key"),
            field_name="issue_key",
            issue_index=issue_index,
            issue_key_pattern=issue_key_pattern,
        )
        if requested_issue_key is None and len(force_issue_keys) >= issue_index:
            requested_issue_key = force_issue_keys[issue_index - 1]
        labels = _string_list_field(issue_index=issue_index, field_name="labels", raw_value=item.get("labels"))
        issues.append(
            ParentIssueDraft(
                summary=summary[:90],
                issue_type=_optional_string(issue_index=issue_index, field_name="issue_type", raw_value=item.get("issue_type")),
                objective=_optional_string(issue_index=issue_index, field_name="objective", raw_value=item.get("objective")),
                user_value=_optional_string(issue_index=issue_index, field_name="user_value", raw_value=item.get("user_value")),
                recommendation=_optional_string(issue_index=issue_index, field_name="recommendation", raw_value=item.get("recommendation")),
                scope_in=_string_list_field(issue_index=issue_index, field_name="scope_in", raw_value=item.get("scope_in")),
                scope_out=_string_list_field(issue_index=issue_index, field_name="scope_out", raw_value=item.get("scope_out")),
                acceptance_criteria=_string_list_field(issue_index=issue_index, field_name="acceptance_criteria", raw_value=item.get("acceptance_criteria")),
                ui_references=_string_list_field(issue_index=issue_index, field_name="ui_references", raw_value=item.get("ui_references")),
                dependencies=_string_list_field(issue_index=issue_index, field_name="dependencies", raw_value=item.get("dependencies")),
                risks=_string_list_field(issue_index=issue_index, field_name="risks", raw_value=item.get("risks")),
                open_questions=_string_list_field(issue_index=issue_index, field_name="open_questions", raw_value=item.get("open_questions")),
                success_outcomes=_string_list_field(issue_index=issue_index, field_name="success_outcomes", raw_value=item.get("success_outcomes")),
                labels=labels,
                requested_issue_key=requested_issue_key,
            )
        )
    if not issues:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Codex returned no valid parent issue drafts")
    return issues


def _parse_engineering_children(
    *,
    raw_children: object,
    force_issue_keys: list[str],
    issue_key_pattern,
    allow_empty_children: bool = False,
    required_child_issue_type: str | None = None,
) -> list[EngineeringChildDraft]:  # noqa: ANN001
    if not isinstance(raw_children, list):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Codex did not return engineering_children")
    children: list[EngineeringChildDraft] = []
    remaining_force_keys = force_issue_keys[1:] if force_issue_keys else []
    for issue_index, item in enumerate(raw_children[:_MAX_ENGINEERING_CHILDREN], start=2):
        if not isinstance(item, dict):
            continue
        summary = _optional_string(issue_index=issue_index, field_name="summary", raw_value=item.get("summary"))
        if not summary:
            continue
        delivery = _optional_string(issue_index=issue_index, field_name="delivery", raw_value=item.get("delivery"))
        if not delivery:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Engineering child {issue_index} is missing delivery",
            )
        acceptance_criteria = _string_list_field(
            issue_index=issue_index,
            field_name="acceptance_criteria",
            raw_value=item.get("acceptance_criteria"),
        )
        if not acceptance_criteria:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Engineering child {issue_index} is missing acceptance_criteria",
            )
        how_to_test = _string_list_field(
            issue_index=issue_index,
            field_name="how_to_test",
            raw_value=item.get("how_to_test"),
        )
        if not how_to_test:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Engineering child {issue_index} is missing how_to_test",
            )
        done_means = _string_list_field(
            issue_index=issue_index,
            field_name="done_means",
            raw_value=item.get("done_means"),
        )
        if not done_means:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Engineering child {issue_index} is missing done_means",
            )
        raw_issue_type = _optional_string(issue_index=issue_index, field_name="issue_type", raw_value=item.get("issue_type"))
        if not raw_issue_type and required_child_issue_type:
            raw_issue_type = required_child_issue_type
        if not raw_issue_type:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Engineering child {issue_index} is missing Jira issue_type",
            )
        if raw_issue_type.strip().casefold() not in {"sub-task", "subtask"}:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Engineering child {issue_index} must use a Jira subtask issue type",
            )
        requested_issue_key = _normalize_issue_key(
            item.get("issue_key"),
            field_name="issue_key",
            issue_index=issue_index,
            issue_key_pattern=issue_key_pattern,
        )
        if requested_issue_key is None and len(remaining_force_keys) >= len(children) + 1:
            requested_issue_key = remaining_force_keys[len(children)]
        children.append(
            EngineeringChildDraft(
                summary=summary[:90],
                issue_type=raw_issue_type.strip(),
                capability=_optional_string(issue_index=issue_index, field_name="capability", raw_value=item.get("capability")),
                delivery=delivery,
                expected_outcome=_optional_string(issue_index=issue_index, field_name="expected_outcome", raw_value=item.get("expected_outcome")),
                acceptance_criteria=acceptance_criteria,
                dependencies=_string_list_field(issue_index=issue_index, field_name="dependencies", raw_value=item.get("dependencies")),
                risks=_string_list_field(issue_index=issue_index, field_name="risks", raw_value=item.get("risks")),
                how_to_test=how_to_test,
                done_means=done_means,
                labels=_string_list_field(issue_index=issue_index, field_name="labels", raw_value=item.get("labels")),
                requested_issue_key=requested_issue_key,
            )
        )
    if not children and not allow_empty_children:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Issue seeding returned no valid engineering_children")
    return children
