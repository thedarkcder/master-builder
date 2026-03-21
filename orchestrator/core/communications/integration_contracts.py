from __future__ import annotations

from typing import Protocol

from orchestrator.core.communications.contracts import (
    CommunicationEvent,
    InboundMessage,
    TransportAction,
)


class InboundAdapter(Protocol):
    def verify(self, *, headers: dict[str, str], body: bytes) -> None:
        ...

    def parse(self, *, headers: dict[str, str], body: bytes) -> InboundMessage:
        ...


class OutboundAdapter(Protocol):
    def send(self, *, event: CommunicationEvent) -> None:
        ...


class CapabilityProvider(Protocol):
    pass


class TransportActionExecutor(Protocol):
    def execute(self, *, action: TransportAction) -> None:
        ...


class InteractiveReplyTransport(Protocol):
    def send_interaction_followup(
        self,
        *,
        application_id: str,
        interaction_token: str,
        content: str,
        ephemeral: bool = False,
        components: list[dict] | None = None,
        reply_to_message_id: str | None = None,
        channel_id: str | None = None,
    ) -> None:
        ...

    def send_thread_reply(
        self,
        *,
        session,
        settings,
        tenant,
        channel_id: str,
        reply_to_message_id: str,
        content: str,
        components: list[dict] | None = None,
    ) -> None:
        ...

    def send_ask_with_thread(
        self,
        *,
        session,
        settings,
        tenant,
        channel_id: str,
        user_id: str,
        content: str,
    ) -> None:
        ...

    def send_seed_with_thread(
        self,
        *,
        session,
        settings,
        tenant,
        channel_id: str,
        user_id: str,
        content: str,
        request_id: str,
        questions: list[str],
    ) -> None:
        ...
