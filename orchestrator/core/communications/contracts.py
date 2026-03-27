from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, ClassVar


@dataclass(frozen=True)
class ActorIdentity:
    actor_id: str
    display_name: str | None = None
    roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProjectScope:
    tenant_id: str
    project_id: str
    jira_project_key: str
    channel_id: str | None = None


@dataclass(frozen=True)
class InboundMessage:
    source: str
    source_event_type: str
    tenant_id: str
    project_scope: ProjectScope | None
    actor: ActorIdentity
    message_text: str
    channel_or_thread_ref: str | None = None
    correlation_id: str | None = None
    idempotency_key: str | None = None
    metadata: dict = field(default_factory=dict)
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class CommandRequest:
    command_name: str
    arguments: tuple[str, ...]
    inbound: InboundMessage


@dataclass(frozen=True)
class CommunicationLink:
    label: str
    url: str


@dataclass(frozen=True)
class CommunicationAction:
    action_id: str
    label: str
    kind: str


@dataclass(frozen=True)
class CommunicationEvent:
    event_type: str
    tenant_id: str
    project_id: str | None
    run_id: str | None
    issue_key: str | None
    title: str
    summary_markdown: str
    links: tuple[CommunicationLink, ...] = ()
    actions: tuple[CommunicationAction, ...] = ()
    correlation_id: str | None = None
    emitted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class TransportEnvelope:
    transport: str
    event_type: str
    request_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    tenant_id_hint: str | None = None
    delivery_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class TransportAction:
    kind: ClassVar[str] = "transport_action"


@dataclass(frozen=True)
class HttpJsonResponseAction(TransportAction):
    kind: ClassVar[str] = "http_json_response"
    status_code: int
    content: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class HttpJsonResponseBytesAction(TransportAction):
    kind: ClassVar[str] = "http_json_response_bytes"
    status_code: int
    body: bytes | str
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DiscordInteractionResponseAction(TransportAction):
    kind: ClassVar[str] = "discord_interaction_response"
    interaction_id: str
    interaction_token: str
    status_code: int
    body: bytes | str


@dataclass(frozen=True)
class DiscordChannelMessageAction(TransportAction):
    kind: ClassVar[str] = "discord_channel_message"
    channel_id: str
    content: str
    components: list[dict[str, Any]] | None = None


@dataclass(frozen=True)
class DiscordChannelMessageWithAttachmentAction(TransportAction):
    kind: ClassVar[str] = "discord_channel_message_with_attachment"
    channel_id: str
    content: str
    filename: str
    file_bytes: bytes
    content_type: str
    components: list[dict[str, Any]] | None = None
    fallback_content_on_failure: str | None = None
    fallback_components_on_failure: list[dict[str, Any]] | None = None
    failure_user_id: str | None = None


@dataclass(frozen=True)
class DiscordInteractionFollowupAction(TransportAction):
    kind: ClassVar[str] = "discord_interaction_followup"
    application_id: str
    interaction_token: str
    content: str
    ephemeral: bool = False
    components: list[dict[str, Any]] | None = None
    reply_to_message_id: str | None = None
    channel_id: str | None = None


@dataclass(frozen=True)
class DiscordThreadReplyAction(TransportAction):
    kind: ClassVar[str] = "discord_thread_reply"
    tenant_id: str
    channel_id: str
    reply_to_message_id: str
    content: str
    components: list[dict[str, Any]] | None = None


@dataclass(frozen=True)
class DiscordAskWithThreadAction(TransportAction):
    kind: ClassVar[str] = "discord_ask_with_thread"
    tenant_id: str
    channel_id: str
    user_id: str
    content: str
    components: list[dict[str, Any]] | None = None
    issue_key: str | None = None
    followup_context_type: str = "ask_thread"


@dataclass(frozen=True)
class DiscordSeedWithThreadAction(TransportAction):
    kind: ClassVar[str] = "discord_seed_with_thread"
    tenant_id: str
    channel_id: str
    user_id: str
    content: str
    request_id: str
    questions: list[str]


@dataclass(frozen=True)
class DiscordTenantNotificationAction(TransportAction):
    kind: ClassVar[str] = "discord_tenant_notification"
    tenant_id: str
    project_id: str | None
    message: str
    event: str | None = None
    open_thread: bool = False
    thread_name: str | None = None
    thread_intro: str | None = None
    thread_intro_components: list[dict[str, Any]] | None = None


@dataclass(frozen=True)
class GitHubIssueCommentReactionAction(TransportAction):
    kind: ClassVar[str] = "github_issue_comment_reaction"
    repo_full_name: str
    comment_id: int
    content: str


@dataclass(frozen=True)
class GitHubPullRequestReviewCommentReactionAction(TransportAction):
    kind: ClassVar[str] = "github_pull_request_review_comment_reaction"
    repo_full_name: str
    comment_id: int
    content: str


@dataclass(frozen=True)
class GitHubStickyReviewCommentAction(TransportAction):
    kind: ClassVar[str] = "github_sticky_review_comment"
    request_id: str
    repo_full_name: str
    pr_number: int
    tenant_id: str
    project_id: str
    head_sha: str
    signal: Any
    findings_result: Any
    event: str
    action_name: str | None


@dataclass(frozen=True)
class GitHubInlineReviewBatchAction(TransportAction):
    kind: ClassVar[str] = "github_inline_review_batch"
    request_id: str
    repo_full_name: str
    pr_number: int
    head_sha: str
    tenant_id: str
    project_id: str
    findings: tuple[Any, ...]
    changed_paths: set[str]


@dataclass(frozen=True)
class GitHubManualFixReviewThreadReplyAction(TransportAction):
    kind: ClassVar[str] = "github_manual_fix_review_thread_reply"
    repo_full_name: str
    pr_number: int
    tenant_id: str
    project_id: str
    triggering_comment_id: int
    requested_by: str | None
    triggering_comment_url: str | None
    instruction_text: str | None
    issue_key: str | None
    issue_url: str | None
    enqueued: bool
    run_id: str | None
    reason: str | None
    status_label: str | None = None
    pr_url: str | None = None
    change_summary: tuple[str, ...] = ()


@dataclass(frozen=True)
class GitHubManualFixIssueCommentReplyAction(TransportAction):
    kind: ClassVar[str] = "github_manual_fix_issue_comment_reply"
    repo_full_name: str
    pr_number: int
    tenant_id: str
    project_id: str
    triggering_comment_id: int
    requested_by: str | None
    triggering_comment_url: str | None
    instruction_text: str | None
    issue_key: str | None
    issue_url: str | None
    enqueued: bool
    run_id: str | None
    reason: str | None
    status_label: str | None = None
    pr_url: str | None = None
    change_summary: tuple[str, ...] = ()


@dataclass(frozen=True)
class GitHubPullRequestMergeAction(TransportAction):
    kind: ClassVar[str] = "github_pull_request_merge"
    repo_full_name: str
    pr_number: int
    head_sha: str


@dataclass
class DeferredTransportWork:
    kind: str
    runner: Callable[[], Awaitable[None]]
    metadata: dict[str, Any] = field(default_factory=dict)
    on_scheduled: Callable[[Any], None] | None = None


@dataclass
class IngressResult:
    actions: tuple[TransportAction, ...] = ()
    deferred_work: tuple[DeferredTransportWork, ...] = ()
