from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


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
