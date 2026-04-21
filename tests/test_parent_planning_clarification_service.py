from __future__ import annotations

from orchestrator.core.parent_planning_clarification_service import (
    ClarificationPublishEffects,
    ParentPlanningClarificationService,
)


class _FakePublisher:
    def __init__(self, *, already_active: bool = False) -> None:
        self.already_active = already_active
        self.has_active_calls: list[tuple[str, tuple[object, ...]]] = []
        self.publish_calls: list[tuple[str, tuple[object, ...]]] = []

    def has_active_clarification(self, *, issue_key: str, questions):  # noqa: ANN001
        self.has_active_calls.append((issue_key, tuple(questions)))
        return self.already_active

    def publish_clarification(self, *, issue_key: str, questions):  # noqa: ANN001
        self.publish_calls.append((issue_key, tuple(questions)))
        return ClarificationPublishEffects(
            state_recorded=True,
            jira_comment_created=True,
            discord_followup_created=False,
        )


def test_service_normalizes_questions_and_publishes_once() -> None:
    service = ParentPlanningClarificationService()
    publisher = _FakePublisher()

    result = service.ensure_active_clarification(
        issue_key="mab-215",
        questions=[
            {
                "question": "What invitation TTL should v1 enforce for automatic expiry?",
                "why_it_matters": "This changes account recovery behavior.",
            },
            "What audit retention window must exports support in v1?",
        ],
        publisher=publisher,
    )

    assert result.issue_key == "MAB-215"
    assert result.already_active is False
    assert result.state_recorded is True
    assert result.jira_comment_created is True
    assert [question.to_payload() for question in result.questions] == [
        {
            "question": "What invitation TTL should v1 enforce for automatic expiry?",
            "why_it_matters": "This changes account recovery behavior.",
        },
        {"question": "What audit retention window must exports support in v1?"},
    ]
    assert len(publisher.has_active_calls) == 1
    assert len(publisher.publish_calls) == 1


def test_service_short_circuits_when_matching_clarification_is_already_active() -> None:
    service = ParentPlanningClarificationService()
    publisher = _FakePublisher(already_active=True)

    result = service.ensure_active_clarification(
        issue_key="MAB-215",
        questions=["What exact step-up freshness window should v1 use?"],
        publisher=publisher,
    )

    assert result.already_active is True
    assert result.state_recorded is True
    assert publisher.publish_calls == []


def test_service_builds_actionable_missing_input_message() -> None:
    service = ParentPlanningClarificationService()

    message = service.build_missing_input_message(
        issue_key="mab-215",
        questions=[
            {"question": "What invitation TTL should v1 enforce for automatic expiry?"},
            "What exact step-up freshness window should v1 use?",
        ],
    )

    assert "Answer the product clarification on Jira issue MAB-215" in message
    assert "- What invitation TTL should v1 enforce for automatic expiry?" in message
    assert "- What exact step-up freshness window should v1 use?" in message
