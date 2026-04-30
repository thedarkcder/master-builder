from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from orchestrator.core.clarification_questions import ClarificationQuestion, ClarificationQuestionSet


@dataclass(frozen=True)
class ClarificationPublishEffects:
    state_recorded: bool
    jira_comment_created: bool = False
    discord_followup_created: bool = False
    jira_comment_id: str | None = None


class ClarificationPublisher(Protocol):
    def active_clarification_effects(
        self,
        *,
        issue_key: str,
        questions: tuple[ClarificationQuestion, ...],
    ) -> ClarificationPublishEffects | None: ...

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
    jira_comment_id: str | None = None

    @property
    def question_payload(self) -> list[dict[str, str]]:
        return ClarificationQuestionSet(questions=self.questions).to_payload()


@dataclass(frozen=True)
class ClarificationWaitingState:
    publication: ClarificationPublication
    message: str


class ParentPlanningClarificationService:
    def normalize_questions(self, values: tuple[object, ...] | list[object] | None) -> ClarificationQuestionSet:
        return ClarificationQuestionSet.from_values(values)

    def require_questions_for_waiting_state(
        self,
        *,
        questions: tuple[object, ...] | list[object] | None,
        context: str,
    ) -> tuple[ClarificationQuestion, ...]:
        question_set = self.normalize_questions(questions)
        if not question_set:
            normalized_context = str(context or "").strip() or "clarification"
            raise ValueError(f"{normalized_context} requires at least one clarification question")
        return question_set.questions

    def ensure_waiting_clarification(
        self,
        *,
        issue_key: str,
        questions: tuple[object, ...] | list[object] | None,
        publisher: ClarificationPublisher,
        context: str,
    ) -> ClarificationWaitingState:
        required_questions = self.require_questions_for_waiting_state(
            questions=questions,
            context=context,
        )
        publication = self.ensure_active_clarification(
            issue_key=issue_key,
            questions=required_questions,
            publisher=publisher,
        )
        return ClarificationWaitingState(
            publication=publication,
            message=self.build_missing_input_message(
                issue_key=publication.issue_key,
                questions=publication.questions,
            ),
        )

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

        active_effects = publisher.active_clarification_effects(
            issue_key=normalized_issue_key,
            questions=question_set.questions,
        )
        if active_effects is not None:
            return ClarificationPublication(
                issue_key=normalized_issue_key,
                questions=question_set.questions,
                already_active=True,
                state_recorded=active_effects.state_recorded,
                jira_comment_created=active_effects.jira_comment_created,
                discord_followup_created=active_effects.discord_followup_created,
                jira_comment_id=active_effects.jira_comment_id,
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
            jira_comment_id=effects.jira_comment_id,
        )

    def build_missing_input_message(
        self,
        *,
        issue_key: str,
        questions: tuple[object, ...] | list[object] | None,
    ) -> str:
        normalized_issue_key = str(issue_key or "").strip().upper()
        question_set = self.normalize_questions(questions)
        if not question_set:
            raise ValueError("Clarification waiting message requires at least one question")
        header = (
            f"Answer the product clarification on Jira issue {normalized_issue_key}, "
            "then retry engineering child fanout."
        )
        prompt = "\n".join(question_set.render_lines())
        return f"{header}\n\nQuestions to answer:\n{prompt}"
