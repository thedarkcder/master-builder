from __future__ import annotations

from types import SimpleNamespace

from orchestrator.core.deployment_previews import (
    _delivery_metadata_with_demo_proof_lease,
)


def test_demo_proof_lease_metadata_includes_durable_lease_id() -> None:
    metadata = _delivery_metadata_with_demo_proof_lease(
        delivery_metadata={},
        proof_scope_id="run:run-1:" + "a" * 40,
        commit_sha="a" * 40,
        settings=SimpleNamespace(run_preview_release_ttl_seconds=86400),
    )

    lease = metadata["demo_proof_lease"]
    assert (
        lease["lease_id"] == "demo-proof-lease:run:run-1:" + "a" * 40 + ":" + "a" * 40
    )
