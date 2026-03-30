from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable

from orchestrator.core.runtime_invocation import AgentInvocationContext
from orchestrator.core.knowledge_base import SlotResolution, parse_source_timestamp
from orchestrator.core.project_policy import resolve_effective_policy

logger = logging.getLogger(__name__)


def resolve_slots_before_block(
    *,
    session,
    tenant,
    project,
    issue_key: str,
    issue_summary: str | None,
    issue_description: str | None,
    missing_slots: list[str],
    settings,  # noqa: ANN001
    persisted_slot_answers: dict[str, SlotResolution],
    resolve_missing_slots_from_knowledge_fn: Callable[..., dict[str, SlotResolution]],
    resolve_slots_with_codex_fn: Callable[..., dict[str, SlotResolution]],
) -> dict[str, SlotResolution]:
    effective_policy = resolve_effective_policy(
        tenant_policy=tenant.policy_config or {},
        project_overrides=project.policy_overrides or {},
    )
    knowledge_enabled = bool(effective_policy.get("knowledge_base_enabled", True))
    mode = str(effective_policy.get("knowledge_auto_answer_mode") or "").strip().lower()
    if mode not in {"safe", "balanced", "aggressive"}:
        mode = "aggressive"

    resolved: dict[str, SlotResolution] = {
        slot_name: answer for slot_name, answer in persisted_slot_answers.items() if slot_name in missing_slots
    }
    remaining = [slot for slot in missing_slots if slot not in resolved]
    if knowledge_enabled and remaining:
        kb_answers = resolve_missing_slots_from_knowledge_fn(
            session=session,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
            missing_slots=remaining,
            mode=mode,
        )
        resolved.update(kb_answers)
    remaining = [slot for slot in missing_slots if slot not in resolved]
    if not remaining:
        return resolved

    codex_answers = resolve_slots_with_codex_fn(
        session=session,
        settings=settings,
        tenant=tenant,
        project=project,
        issue_key=issue_key,
        issue_summary=issue_summary,
        issue_description=issue_description,
        missing_slots=remaining,
    )
    merged = dict(resolved)
    for slot_name, answer in codex_answers.items():
        if slot_name in merged:
            continue
        merged[slot_name] = answer
    return merged


def resolve_slots_with_codex(
    *,
    session,
    settings,  # noqa: ANN001
    tenant,
    project,
    issue_key: str,
    issue_summary: str | None,
    issue_description: str | None,
    missing_slots: list[str],
    build_codex_runtime_fn,
    invoke_runtime_json_fn,
    project_repo_dir_fn,
    codex_runtime_error_type,
) -> dict[str, SlotResolution]:
    runtime = build_codex_runtime_fn(session=session, settings=settings)
    repo_evidence = collect_repo_evidence(
        base_dir=str(getattr(settings, "project_repo_checkout_base_dir", "") or ""),
        tenant_id=tenant.tenant_id,
        project=project,
        missing_slots=missing_slots,
        project_repo_dir_fn=project_repo_dir_fn,
    )
    issue_context = "\n".join(
        part
        for part in (
            str(issue_summary or "").strip(),
            str(issue_description or "").strip(),
        )
        if part
    ).strip()
    if not repo_evidence.strip() and not issue_context:
        return {}
    try:
        payload = invoke_runtime_json_fn(
            runtime=runtime,
            context=AgentInvocationContext(
                channel="system",
                tenant_id=tenant.tenant_id,
                project_id=project.project_id,
                command="policy",
                stage="decision_resolution",
                working_dir=".",
                issue_key=issue_key,
                reasoning_effort="low",
                issue_description_chars=len(str(issue_description or "")),
            ),
            system_prompt=(
                "You resolve missing decision slots from issue/Jira context and repository evidence. "
                "Return strict JSON object only: {\"answers\": {\"<slot_name>\": \"<value>\"}}. "
                "Only include slots with explicit evidence."
            ),
            user_prompt="\n".join(
                [
                    f"Issue key: {issue_key}",
                    f"Issue summary: {str(issue_summary or '').strip()}",
                    "Issue description:",
                    str(issue_description or "").strip(),
                    "",
                    f"Missing slots: {json.dumps(missing_slots)}",
                    "",
                    "Repository evidence:",
                    repo_evidence or "(none)",
                ]
            ),
        )
    except codex_runtime_error_type as exc:
        logger.warning(
            "decision_resolution_codex_failed tenant_id=%s project_id=%s issue_key=%s error=%s",
            tenant.tenant_id,
            project.project_id,
            issue_key,
            exc,
        )
        return {}

    answers_raw = payload.get("answers") if isinstance(payload, dict) else None
    if not isinstance(answers_raw, dict):
        return {}

    resolved: dict[str, SlotResolution] = {}
    for slot_name in missing_slots:
        value = str(answers_raw.get(slot_name) or "").strip()
        if not value:
            continue
        resolved[slot_name] = SlotResolution(
            slot_name=slot_name,
            slot_value=value,
            source_timestamp=None,
            confidence=0.66,
            citation={
                "source_type": "codex_inference",
                "title": "Decision resolution inference",
                "source_timestamp": None,
            },
            inferred=True,
        )
    return resolved


def collect_repo_evidence(
    *,
    base_dir: str,
    tenant_id: str,
    project,
    missing_slots: list[str],
    project_repo_dir_fn,
) -> str:
    if not base_dir.strip():
        return ""
    repo_dir = project_repo_dir_fn(base_dir=base_dir, tenant_id=tenant_id, project_id=project.project_id)
    if not (repo_dir / ".git").exists():
        return ""
    slot_tokens: dict[str, tuple[str, ...]] = {
        "objective": ("objective", "goal"),
        "scope": ("scope", "in scope"),
        "acceptance_criteria": ("acceptance", "done when"),
        "how_to_test": ("how to test", "test"),
        "nfr_intent": ("mvp", "scale-ready", "nfr"),
        "reliability_security_constraints": ("security", "reliability", "token", "session"),
        "out_of_scope": ("out of scope", "non-goal"),
        "rollout_constraints": ("rollout", "migration"),
        "decision_owner": ("owner", "approver"),
        "dependencies_and_risks": ("risk", "dependency"),
    }
    candidate_files: list[Path] = []
    for pattern in ("README*", "docs/**/*.md", "**/*architecture*.md", "**/*decision*.md"):
        candidate_files.extend(repo_dir.glob(pattern))
    lines: list[str] = []
    seen_paths: set[str] = set()
    for path in sorted(candidate_files):
        if len(lines) >= 20:
            break
        if not path.is_file():
            continue
        path_key = str(path)
        if path_key in seen_paths:
            continue
        seen_paths.add(path_key)
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        content_lower = content.lower()
        for slot_name in missing_slots:
            terms = slot_tokens.get(slot_name, ())
            if not terms:
                continue
            if not any(term in content_lower for term in terms):
                continue
            snippet = extract_first_matching_line(content=content, terms=terms)
            if snippet:
                rel_path = str(path.relative_to(repo_dir))
                lines.append(f"- {slot_name} [{rel_path}]: {snippet}")
                break
    return "\n".join(lines)


def extract_first_matching_line(*, content: str, terms: tuple[str, ...]) -> str | None:
    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            continue
        lower = line.lower()
        if not any(term in lower for term in terms):
            continue
        if len(line) > 240:
            return f"{line[:237].rstrip()}..."
        return line
    return None


def append_auto_resolved_block(*, issue_description: str | None, slot_answers: dict[str, SlotResolution]) -> str:
    lines = ["", "Auto-resolved context for precheck:"]
    for slot_name in sorted(slot_answers.keys()):
        answer = slot_answers[slot_name]
        citation_title = str(answer.citation.get("title") or answer.citation.get("source_type") or "").strip()
        if citation_title:
            lines.append(f"- {slot_name}: {answer.slot_value} (source: {citation_title})")
        else:
            lines.append(f"- {slot_name}: {answer.slot_value}")
    base = str(issue_description or "").strip()
    extra = "\n".join(lines).strip()
    if not base:
        return extra
    return f"{base}\n\n{extra}"


def deserialize_slot_resolution(*, slot_name: str, value: object) -> SlotResolution | None:
    if not isinstance(value, dict):
        return None
    slot_value = str(value.get("slot_value") or "").strip()
    if not slot_value:
        return None
    citation = value.get("citation")
    return SlotResolution(
        slot_name=slot_name,
        slot_value=slot_value,
        source_timestamp=parse_source_timestamp(value.get("source_timestamp")),
        confidence=float(value.get("confidence") or 0.0),
        citation=dict(citation) if isinstance(citation, dict) else {},
        inferred=bool(value.get("inferred", False)),
    )


def serialize_slot_resolution(answer: SlotResolution) -> dict[str, object]:
    return {
        "slot_value": answer.slot_value,
        "source_timestamp": answer.source_timestamp.isoformat() if answer.source_timestamp else None,
        "confidence": answer.confidence,
        "citation": dict(answer.citation),
        "inferred": answer.inferred,
    }


def slot_resolutions_from_case(*, case) -> dict[str, SlotResolution]:
    if case is None or not isinstance(case.metadata_json, dict):
        return {}
    answers = case.metadata_json.get("auto_resolved_answers")
    if not isinstance(answers, dict):
        return {}
    resolved: dict[str, SlotResolution] = {}
    for slot_name, raw in answers.items():
        resolution = deserialize_slot_resolution(slot_name=str(slot_name), value=raw)
        if resolution is None:
            continue
        resolved[str(slot_name)] = resolution
    return resolved
