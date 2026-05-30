"""Normalize project install approval labels.

Revision ID: 20260526_0111
Revises: 20260526_0110
Create Date: 2026-05-26 16:20:00.000000
"""

from __future__ import annotations

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "20260526_0111"
down_revision = "20260526_0110"
branch_labels = None
depends_on = None

KNOWN_INTEGRATION_LABELS = (
    "hubspot",
    "stripe",
    "slack",
    "jira",
    "github",
    "atlassian",
    "supabase",
    "railway",
    "fastlane",
)
GENERIC_INSTALL_LABEL_TOKENS = {
    "config",
    "configuration",
    "configure",
    "connect",
    "connector",
    "dependency",
    "dependencies",
    "install",
    "integration",
    "setup",
}


def _canonical_project_install_request_label(*, kind: str, label: str) -> str:
    normalized_label = str(label or "").strip()
    if str(kind or "").strip().lower() != "integration":
        return normalized_label
    tokens = [
        token
        for token in "".join(character.lower() if character.isalnum() else " " for character in normalized_label).split()
        if token
    ]
    for known_label in KNOWN_INTEGRATION_LABELS:
        if known_label in tokens:
            return known_label
    meaningful_tokens = [
        token
        for token in tokens
        if token not in GENERIC_INSTALL_LABEL_TOKENS and not any(character.isdigit() for character in token)
    ]
    if meaningful_tokens:
        return "-".join(meaningful_tokens)
    return normalized_label


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("project_install_requests"):
        return

    timestamp = datetime.now(timezone.utc)
    rows = list(
        bind.execute(
            sa.text(
                """
                SELECT request_id, tenant_id, project_id, kind, label, status
                FROM project_install_requests
                """
            )
        ).mappings()
    )
    approved_keys: set[tuple[str, str, str, str]] = set()
    canonical_by_request: dict[str, str] = {}
    for row in rows:
        canonical_label = _canonical_project_install_request_label(kind=str(row["kind"]), label=str(row["label"]))
        canonical_by_request[str(row["request_id"])] = canonical_label
        key = (str(row["tenant_id"]), str(row["project_id"]), str(row["kind"]), canonical_label)
        if str(row["status"]).strip().lower() == "approved":
            approved_keys.add(key)

    for row in rows:
        request_id = str(row["request_id"])
        canonical_label = canonical_by_request[request_id]
        current_status = str(row["status"]).strip().lower()
        key = (str(row["tenant_id"]), str(row["project_id"]), str(row["kind"]), canonical_label)
        next_status = "approved" if current_status == "pending" and key in approved_keys else current_status
        if canonical_label == row["label"] and next_status == current_status:
            continue
        bind.execute(
            sa.text(
                """
                UPDATE project_install_requests
                SET label = :label,
                    status = :status,
                    updated_at = :updated_at
                WHERE request_id = :request_id
                """
            ),
            {
                "request_id": request_id,
                "label": canonical_label,
                "status": next_status,
                "updated_at": timestamp,
            },
        )


def downgrade() -> None:
    raise RuntimeError("Project install request label normalization cannot be downgraded")
