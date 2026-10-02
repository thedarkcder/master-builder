"""Replace persisted QA delivery URLs from proved object scope; invalidate ambiguous proof."""

from copy import deepcopy
from urllib.parse import urlsplit

from alembic import op
import sqlalchemy as sa

from orchestrator.core.config import get_settings
from orchestrator.core.qa.artifact_storage import validate_object_key
from orchestrator.storage.tenant_rls import set_platform_system_rls_context

revision = "20261002_0136"
down_revision = "20261002_0135"
branch_labels = None
depends_on = None


def _delivery_base(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.rstrip("/").endswith("/api/qa-artifacts")
        or (
            parsed.scheme == "http"
            and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        )
    ):
        raise RuntimeError(
            "Migration requires explicit authenticated QA delivery base URL; configure ORCHESTRATOR_QA_DEMO_ARTIFACT_PUBLIC_BASE_URL and retry."
        )
    return value.rstrip("/")


def migrate_snapshot(
    payload: object,
    *,
    tenant_id: str,
    project_id: str | None,
    run_id: str,
    delivery_base_url: str,
) -> tuple[object, bool]:
    if not isinstance(payload, dict) or not isinstance(payload.get("stages"), dict):
        return payload, False
    stage = payload["stages"].get("qa")
    if not isinstance(stage, dict) or not isinstance(stage.get("artifact"), dict):
        return payload, False
    artifact = stage["artifact"]
    if not artifact.get("recordings") and not artifact.get("failure_evidence"):
        return payload, False
    base = _delivery_base(delivery_base_url)
    updated = deepcopy(payload)
    artifact = updated["stages"]["qa"]["artifact"]
    prefix = f"{tenant_id}/{project_id}/{run_id}/"
    ambiguous = project_id is None
    for field in ("recordings", "failure_evidence"):
        records = artifact.get(field, [])
        if not isinstance(records, list):
            ambiguous = True
            break
        for recording in records:
            if not isinstance(recording, dict) or not isinstance(
                recording.get("object_key"), str
            ):
                ambiguous = True
                break
            key = recording["object_key"]
            try:
                validate_object_key(key)
            except ValueError:
                ambiguous = True
                break
            if not key.startswith(prefix):
                ambiguous = True
                break
            recording["artifact_url"] = f"{base}/{key}"
    if ambiguous:
        artifact.update(
            recordings=[],
            failure_evidence=[],
            outcome="blocked",
            blocker_message="QA artifact scope could not be verified during private-delivery migration; recapture QA evidence.",
        )
        updated["stages"]["qa"]["status"] = "blocked"
    return updated, updated != payload


def upgrade() -> None:
    bind = op.get_bind()
    set_platform_system_rls_context(
        bind, system_purpose="private_qa_delivery_migration"
    )
    metadata = sa.MetaData()
    runs = sa.Table("runs", metadata, autoload_with=bind)
    checkpoints = sa.Table("workflow_checkpoints", metadata, autoload_with=bind)
    base = get_settings().qa_demo_artifact_public_base_url
    # Keyset batches bound memory for existing large execution stores.
    for table, key_column, payload_column in (
        (runs, runs.c.run_id, runs.c.plan),
        (checkpoints, checkpoints.c.checkpoint_id, checkpoints.c.payload_json),
    ):
        upper_key = bind.execute(sa.select(sa.func.max(key_column))).scalar_one()
        if upper_key is None:
            continue
        last_key = ""
        while last_key < upper_key:
            source = (
                table
                if table is runs
                else table.join(runs, table.c.run_id == runs.c.run_id)
            )
            rows = (
                bind.execute(
                    sa.select(
                        key_column.label("row_key"),
                        payload_column.label("payload"),
                        runs.c.tenant_id,
                        runs.c.project_id,
                        runs.c.run_id,
                    )
                    .select_from(source)
                    .where(
                        key_column > last_key,
                        key_column <= upper_key,
                        payload_column.is_not(None),
                    )
                    .order_by(key_column)
                    .limit(200)
                )
                .mappings()
                .all()
            )
            if not rows:
                break
            for row in rows:
                payload, changed = migrate_snapshot(
                    row["payload"],
                    tenant_id=row["tenant_id"],
                    project_id=row["project_id"],
                    run_id=row["run_id"],
                    delivery_base_url=base,
                )
                if changed:
                    bind.execute(
                        table.update()
                        .where(key_column == row["row_key"])
                        .values({payload_column.name: payload})
                    )
            last_key = rows[-1]["row_key"]


def downgrade() -> None:
    raise RuntimeError(
        "Private QA delivery cannot be downgraded to anonymous URLs; use a reviewed forward migration."
    )
