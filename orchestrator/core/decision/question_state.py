from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def normalized_question_id(item: Mapping[str, Any]) -> str:
    return str(item.get("id") or item.get("question_id") or "").strip()


def question_requires_followup(item: Mapping[str, Any]) -> bool:
    """Return whether this persisted question should still be shown to the user.

    `status` describes evidence quality; `unresolved` describes askability.
    An answered question can still require follow-up when the answer is partial.
    """
    if "unresolved" in item:
        return bool(item.get("unresolved"))
    status = str(item.get("status") or "").strip().lower()
    return status != "accepted"


def unresolved_question_ids_for_question_set(
    *,
    question_set: Iterable[Mapping[str, Any]],
    accepted_question_ids: Iterable[str] = (),
) -> list[str]:
    accepted_ids = {
        str(question_id).strip()
        for question_id in accepted_question_ids
        if str(question_id).strip()
    }
    ids: list[str] = []
    for item in question_set:
        question_id = normalized_question_id(item)
        if not question_id or question_id in accepted_ids:
            continue
        if question_requires_followup(item):
            ids.append(question_id)
    return ids
