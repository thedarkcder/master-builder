from __future__ import annotations

from sqlalchemy import select

from orchestrator.storage.models import DecisionAnswer, DecisionCase, DecisionCycle


def build_cycle_comment(*, session, case: DecisionCase, cycle: DecisionCycle) -> str:
    unresolved_ids = {
        str(question_id).strip()
        for question_id in cycle.unresolved_question_ids_json
        if str(question_id).strip()
    }
    answers = session.execute(
        select(DecisionAnswer)
        .where(
            DecisionAnswer.cycle_id == cycle.cycle_id,
            DecisionAnswer.status.in_(("answered", "accepted")),
        )
        .order_by(DecisionAnswer.created_at.asc())
    ).scalars().all()
    lines = [
        f"<!-- decision-cycle:{cycle.cycle_id} -->",
        f"Decision state: `{case.state}`",
    ]
    if cycle.reason:
        lines.append(f"Reason: {cycle.reason}")
    if answers:
        lines.append("Recorded answers:")
        for answer in answers:
            answer_text = str(answer.normalized_answer or "").strip()
            if not answer_text:
                continue
            status = str(answer.status or "").strip() or "answered"
            lines.append(f"- [{answer.question_id}] {answer.question_text}")
            lines.append(f"  Status: {status}")
            lines.append(f"  Answer: {answer_text}")
    if cycle.question_set_json:
        lines.append("Outstanding questions:")
        for item in cycle.question_set_json:
            question_id = str(item.get("id") or "").strip()
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            if unresolved_ids and question_id and question_id not in unresolved_ids:
                continue
            if question_id:
                lines.append(f"- [{question_id}] {text}")
            else:
                lines.append(f"- {text}")
    return "\n".join(lines)
