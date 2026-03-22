import hikari
from typing_extensions import Annotated

from sqlalchemy import select
from orchestrator.core.discord.live_voice_transport_client import GoJsonLinesLiveVoiceTransportClient


def test_runtime_dependency_imports_are_available() -> None:
    assert Annotated is not None
    assert hikari is not None
    assert GoJsonLinesLiveVoiceTransportClient is not None
    assert select(1) is not None
