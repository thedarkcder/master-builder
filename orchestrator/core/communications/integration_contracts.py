from __future__ import annotations

from typing import Protocol

from orchestrator.core.communications.contracts import (
    CommunicationEvent,
    InboundMessage,
    TransportAction,
)


class InboundAdapter(Protocol):
    def verify(self, *, headers: dict[str, str], body: bytes) -> None: ...

    def parse(self, *, headers: dict[str, str], body: bytes) -> InboundMessage: ...


class OutboundAdapter(Protocol):
    def send(self, *, event: CommunicationEvent) -> None: ...


class CapabilityProvider(Protocol):
    pass


class TransportActionExecutor(Protocol):
    def execute(self, *, action: TransportAction) -> object | None: ...
