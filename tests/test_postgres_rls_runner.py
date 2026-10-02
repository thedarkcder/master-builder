from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts import verify_postgres_rls


def test_runner_rejects_remote_docker_before_creating_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def docker_response(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(command)
        return SimpleNamespace(
            returncode=0, stdout='"tcp://docker.example.invalid:2376"', stderr=""
        )

    monkeypatch.setattr(verify_postgres_rls.subprocess, "run", docker_response)
    with pytest.raises(RuntimeError, match="local Unix"):
        verify_postgres_rls.main()
    assert len(calls) == 1
    assert calls[0][1:3] == ["context", "inspect"]
