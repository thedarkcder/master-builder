from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from orchestrator.core.clarification_questions import ClarificationQuestion, ClarificationQuestionSet


@dataclass(frozen=True)
class ClarificationPublishEffects:
    state_recorded: bool
    jira_comment_created: bool = False
    discord_followup_created: bool = False


class ClarificationPublisher(Protocol):
    def has_active_clarification(
        self,
        *,
        issue_key: str,
        questions: tuple[ClarificationQuestion, ...],
    ) -> bool: ...

    def publish_clarification(
        self,
        *,
        issue_key: str,
        questions: tuple[ClarificationQuestion, ...],
    ) -> ClarificationPublishEffects: ...


@dataclass(frozen=True)
class ClarificationPublication:
    issue_key: str
    questions: tuple[ClarificationQuestion, ...]
    already_active: bool
    state_recorded: bool
    jira_comment_created: bool = False
    discord_followup_created: bool = False

    @property
    def question_payload(self) -> list[dict[str, str]]:
        return ClarificationQuestionSet(questions=self.questions).to_payload()


class ParentPlanningClarificationService:
    def normalize_questions(self, values: tuple[object, ...] | list[object] | None) -> ClarificationQuestionSet:
        return ClarificationQuestionSet.from_values(values)

    def ensure_active_clarification(
        self,
        *,
        issue_key: str,
        questions: tuple[object, ...] | list[object] | None,
        publisher: ClarificationPublisher,
    ) -> ClarificationPublication:
        normalized_issue_key = str(issue_key or "").strip().upper()
        question_set = self.normalize_questions(questions)
        if not normalized_issue_key or not question_set:
            return ClarificationPublication(
                issue_key=normalized_issue_key,
                questions=(),
                already_active=False,
                state_recorded=False,
            )

        if publisher.has_active_clarification(issue_key=normalized_issue_key, questions=question_set.questions):
            return ClarificationPublication(
                issue_key=normalized_issue_key,
                questions=question_set.questions,
                already_active=True,
                state_recorded=True,
            )

        effects = publisher.publish_clarification(
            issue_key=normalized_issue_key,
            questions=question_set.questions,
        )
        return ClarificationPublication(
            issue_key=normalized_issue_key,
            questions=question_set.questions,
            already_active=False,
            state_recorded=effects.state_recorded,
            jira_comment_created=effects.jira_comment_created,
            discord_followup_created=effects.discord_followup_created,
        )

    def build_missing_input_message(
        self,
        *,
        issue_key: str,
        questions: tuple[object, ...] | list[object] | None,
    ) -> str:
        normalized_issue_key = str(issue_key or "").strip().upper()
        header = (
            f"Answer the product clarification on Jira issue {normalized_issue_key}, "
            "then retry engineering child fanout."
        )
        question_set = self.normalize_questions(questions)
        if not question_set:
            return header
        prompt = "\n".join(question_set.render_lines())
        return f"{header}\n\nQuestions to answer:\n{prompt}"
