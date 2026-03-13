from orchestrator.core.precheck_question_lock import (
    PRECHECK_QUESTIONS_BLOCK_END,
    PRECHECK_QUESTIONS_BLOCK_START,
    parse_locked_precheck_questions,
    remove_precheck_questions_block,
    upsert_precheck_questions_block,
)


def _sample_block(*, reason: str = "Locked reason") -> str:
    return "\n".join(
        [
            PRECHECK_QUESTIONS_BLOCK_START,
            "## Precheck Clarification Questions",
            f"Decision Gate reason: {reason}",
            "- [decision_gate] Locked question one?",
            "- [gtd] Locked GTD question?",
            PRECHECK_QUESTIONS_BLOCK_END,
        ]
    )


def test_parse_locked_precheck_questions_ignores_earlier_unrelated_end_marker() -> None:
    description = "\n".join(
        [
            "Release notes mention literal marker here:",
            PRECHECK_QUESTIONS_BLOCK_END,
            "Some context before the real block.",
            _sample_block(),
        ]
    )

    result = parse_locked_precheck_questions(issue_description=description)

    assert result.decision_gate_reason == "Locked reason"
    assert result.decision_gate_questions == ("Locked question one?",)
    assert result.gtd_questions == ("Locked GTD question?",)


def test_remove_precheck_questions_block_ignores_earlier_unrelated_end_marker() -> None:
    description = "\n".join(
        [
            "Prefix line.",
            PRECHECK_QUESTIONS_BLOCK_END,
            "Other text.",
            _sample_block(),
            "Suffix line.",
        ]
    )

    updated = remove_precheck_questions_block(current_description=description)

    assert PRECHECK_QUESTIONS_BLOCK_START not in updated
    assert PRECHECK_QUESTIONS_BLOCK_END in updated
    assert "Prefix line." in updated
    assert "Suffix line." in updated
    assert "Locked question one?" not in updated


def test_upsert_precheck_questions_block_replaces_real_block_after_unrelated_end_marker() -> None:
    description = "\n".join(
        [
            "Prefix line.",
            PRECHECK_QUESTIONS_BLOCK_END,
            "Other text.",
            _sample_block(reason="Old reason"),
            "Suffix line.",
        ]
    )
    replacement = _sample_block(reason="New reason")

    updated = upsert_precheck_questions_block(current_description=description, block=replacement)

    assert updated.count(PRECHECK_QUESTIONS_BLOCK_START) == 1
    assert "Decision Gate reason: New reason" in updated
    assert "Decision Gate reason: Old reason" not in updated
    assert PRECHECK_QUESTIONS_BLOCK_END in updated
    assert "Prefix line." in updated
    assert "Suffix line." in updated
