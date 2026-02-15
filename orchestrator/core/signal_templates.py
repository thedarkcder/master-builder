from __future__ import annotations

from collections.abc import Iterable
import re

ISSUE_KEY_PATTERN = re.compile(r"\b[A-Z][A-Z0-9_]+-\d+\b")


def _normalize_lines(values: Iterable[str], *, max_items: int, max_line_chars: int) -> list[str]:
    normalized: list[str] = []
    for value in values:
        line = " ".join(str(value).strip().split())
        if not line:
            continue
        if len(line) > max_line_chars:
            line = f"{line[: max_line_chars - 1].rstrip()}…"
        normalized.append(line)
        if len(normalized) >= max_items:
            break
    return normalized


def _clip_message_lines(lines: list[str], *, max_lines: int = 40, max_chars: int = 1800) -> str:
    clipped = lines[:max_lines]
    rendered = "\n".join(clipped).strip()
    if len(rendered) <= max_chars:
        return rendered
    return f"{rendered[: max_chars - 1].rstrip()}…"


def _format_issue_reference(issue_key: str, jira_url: str | None) -> str:
    if jira_url:
        return f"[{issue_key}]({jira_url})"
    return issue_key


def format_discord_ready_gate_guidance(*, issue_key: str, issue_status: str, ready_statuses: Iterable[str]) -> str:
    statuses = _normalize_lines(ready_statuses, max_items=6, max_line_chars=80)
    formatted_statuses = ", ".join(statuses) if statuses else "Ready for Agent"
    lines = [
        f"Issue {issue_key} was not queued.",
        f"Current status: {issue_status}",
        f"Required ready status: {formatted_statuses}",
        "Move the issue to a ready status, then retry.",
    ]
    return _clip_message_lines(lines, max_lines=8, max_chars=500)


def format_stage_discord_update(
    *,
    tenant_id: str,
    issue_key: str,
    run_id: str,
    stage: str,
    jira_url: str | None = None,
    run_url: str | None = None,
    pr_url: str | None = None,
    error: str | None = None,
    next_steps: Iterable[str] = (),
) -> str:
    lines = [f"Stage update: {stage}", f"Tenant: {tenant_id} | Run: {run_id}"]
    lines.append(f"Issue: {_format_issue_reference(issue_key, jira_url)}")
    if run_url:
        lines.append(f"Run: [Open dashboard run]({run_url})")
    if pr_url:
        lines.append(f"PR: [Open PR]({pr_url})")
    if error:
        lines.append(f"Error: {' '.join(error.strip().split())[:200]}")

    steps = _normalize_lines(next_steps, max_items=3, max_line_chars=140)
    if steps:
        lines.append("Next steps:")
        lines.extend(f"- {step}" for step in steps)

    return _clip_message_lines(lines, max_lines=20, max_chars=900)


def format_stage_jira_update(
    *,
    tenant_id: str,
    issue_key: str,
    run_id: str,
    stage: str,
    jira_url: str | None = None,
    pr_url: str | None = None,
    error: str | None = None,
    next_steps: Iterable[str] = (),
) -> str:
    lines = [
        f"Stage: {stage}",
        f"- Tenant: `{tenant_id}`",
        f"- Issue: {_format_issue_reference(issue_key, jira_url)}",
        f"- Run: `{run_id}`",
    ]
    if jira_url:
        lines.append(f"- Jira: {jira_url}")
    if pr_url:
        lines.append(f"- PR: {pr_url}")
    if error:
        lines.append(f"- Error: {' '.join(error.strip().split())[:300]}")

    steps = _normalize_lines(next_steps, max_items=3, max_line_chars=200)
    if steps:
        lines.append("")
        lines.append("Next steps:")
        lines.extend(f"- {step}" for step in steps)

    return _clip_message_lines(lines, max_lines=30, max_chars=1800)


def format_discord_pr_ready_message(
    *,
    pr_url: str,
    jira_url: str | None,
    run_id: str | None,
    what_changed: Iterable[str],
    risk_impact: Iterable[str],
    how_to_test: Iterable[str],
    questions: Iterable[str] | None = None,
    next_action: str = "Please review + merge",
) -> str:
    changed = _normalize_lines(what_changed, max_items=3, max_line_chars=140)
    risks = _normalize_lines(risk_impact, max_items=2, max_line_chars=140)
    test_steps = _normalize_lines(how_to_test, max_items=3, max_line_chars=140)
    unresolved_questions = _normalize_lines(questions or (), max_items=2, max_line_chars=140)

    lines = [f"✅ PR Ready: [Open PR]({pr_url})"]
    if jira_url or run_id:
        jira_text = f"[Open issue]({jira_url})" if jira_url else "n/a"
        run_text = run_id or "n/a"
        lines.append(f"Jira: {jira_text} | Run: {run_text}")
    if changed:
        lines.append("")
        lines.append("What changed:")
        lines.extend(f"- {item}" for item in changed)
    if risks:
        lines.append("")
        lines.append("Risk/Impact:")
        lines.extend(f"- {item}" for item in risks)
    if test_steps:
        lines.append("")
        lines.append("How to test:")
        lines.extend(f"{idx}) {item}" for idx, item in enumerate(test_steps, start=1))
    if unresolved_questions:
        lines.append("")
        lines.append("Questions:")
        lines.extend(f"- {item}" for item in unresolved_questions)
    lines.append("")
    lines.append("Next action:")
    lines.append(f"- {next_action}")
    return _clip_message_lines(lines)


def format_jira_final_comment(
    *,
    pr_url: str,
    summary: Iterable[str],
    acceptance_criteria: Iterable[str],
    command_steps: Iterable[str],
    manual_steps: Iterable[str],
    notes: Iterable[str] = (),
    open_questions: Iterable[str] = (),
    follow_up_issues: Iterable[str] = (),
    jira_base_url: str | None = None,
) -> str:
    summary_lines = _normalize_lines(summary, max_items=4, max_line_chars=200)
    criteria_lines = _normalize_lines(acceptance_criteria, max_items=8, max_line_chars=200)
    command_lines = _normalize_lines(command_steps, max_items=6, max_line_chars=200)
    manual_lines = _normalize_lines(manual_steps, max_items=6, max_line_chars=200)
    note_lines = _normalize_lines(notes, max_items=5, max_line_chars=200)
    question_lines = _normalize_lines(open_questions, max_items=3, max_line_chars=200)
    followups = _normalize_lines(follow_up_issues, max_items=6, max_line_chars=200)

    lines = [f"PR: {pr_url}", "", "Summary:"]
    lines.extend(f"- {item}" for item in summary_lines)

    lines.append("")
    lines.append("Acceptance criteria:")
    lines.extend(f"- [✅] {item}" for item in criteria_lines)

    lines.append("")
    lines.append("How to test:")
    if command_lines:
        lines.append("- Commands:")
        lines.extend(f"  - `{item}`" for item in command_lines)
    if manual_lines:
        lines.append("- Steps:")
        lines.extend(f"  {idx}) {item}" for idx, item in enumerate(manual_lines, start=1))

    if note_lines:
        lines.append("")
        lines.append("Notes:")
        lines.extend(f"- {item}" for item in note_lines)

    if question_lines:
        lines.append("")
        lines.append("Open questions:")
        lines.extend(f"- {item}" for item in question_lines)

    if followups:
        lines.append("")
        lines.append("Follow-ups created (Backlog):")
        for item in followups:
            if "http://" in item or "https://" in item:
                lines.append(f"- {item}")
                continue
            issue_match = ISSUE_KEY_PATTERN.search(item)
            if issue_match and jira_base_url:
                issue_key = issue_match.group(0)
                lines.append(f"- {item} ({jira_base_url.rstrip('/')}/browse/{issue_key})")
            else:
                lines.append(f"- {item}")

    return _clip_message_lines(lines, max_lines=80, max_chars=5000)
