from __future__ import annotations

from collections.abc import Iterable


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

    lines = [f"✅ PR Ready: {pr_url}"]
    if jira_url or run_id:
        jira_text = jira_url or "n/a"
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
        lines.extend(f"- {item}" for item in followups)

    return _clip_message_lines(lines, max_lines=80, max_chars=5000)
