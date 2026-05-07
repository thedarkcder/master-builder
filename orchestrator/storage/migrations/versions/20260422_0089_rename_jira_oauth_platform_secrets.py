"""Rename Jira OAuth platform secret refs to Atlassian OAuth.

Revision ID: 20260422_0089
Revises: 20260422_0088
Create Date: 2026-04-22 15:30:00.000000
"""

from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision = "20260422_0089"
down_revision = "20260422_0088"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE managed_secrets
        SET secret_ref = 'platform/ATLASSIAN_OAUTH_CLIENT_ID'
        WHERE secret_ref = 'platform/JIRA_OAUTH_CLIENT_ID'
        """
    )
    op.execute(
        """
        UPDATE managed_secrets
        SET secret_ref = 'platform/ATLASSIAN_OAUTH_CLIENT_SECRET'
        WHERE secret_ref = 'platform/JIRA_OAUTH_CLIENT_SECRET'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE managed_secrets
        SET secret_ref = 'platform/JIRA_OAUTH_CLIENT_ID'
        WHERE secret_ref = 'platform/ATLASSIAN_OAUTH_CLIENT_ID'
        """
    )
    op.execute(
        """
        UPDATE managed_secrets
        SET secret_ref = 'platform/JIRA_OAUTH_CLIENT_SECRET'
        WHERE secret_ref = 'platform/ATLASSIAN_OAUTH_CLIENT_SECRET'
        """
    )
