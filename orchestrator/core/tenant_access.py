from __future__ import annotations

from dataclasses import dataclass


ROLE_TENANT_ADMIN = "tenant_admin"
ROLE_TECHNICAL_MEMBER = "technical_member"
ROLE_BUSINESS_MEMBER = "business_member"

MODE_TECHNICAL = "technical"
MODE_NON_TECHNICAL = "non_technical"

PERMISSION_TENANT_MANAGE = "tenant.manage"
PERMISSION_MEMBERS_MANAGE = "members.manage"
PERMISSION_TEAMS_MANAGE = "teams.manage"
PERMISSION_ANALYTICS_BUSINESS_VIEW = "analytics.business.view"
PERMISSION_ANALYTICS_TECHNICAL_VIEW = "analytics.technical.view"
PERMISSION_RUNS_BUSINESS_VIEW = "runs.business.view"
PERMISSION_RUNS_TECHNICAL_VIEW = "runs.technical.view"
PERMISSION_SETTINGS_BUSINESS_VIEW = "settings.business.view"
PERMISSION_SETTINGS_TECHNICAL_VIEW = "settings.technical.view"

ALL_PERMISSION_KEYS = frozenset(
    {
        PERMISSION_TENANT_MANAGE,
        PERMISSION_MEMBERS_MANAGE,
        PERMISSION_TEAMS_MANAGE,
        PERMISSION_ANALYTICS_BUSINESS_VIEW,
        PERMISSION_ANALYTICS_TECHNICAL_VIEW,
        PERMISSION_RUNS_BUSINESS_VIEW,
        PERMISSION_RUNS_TECHNICAL_VIEW,
        PERMISSION_SETTINGS_BUSINESS_VIEW,
        PERMISSION_SETTINGS_TECHNICAL_VIEW,
    }
)

ROLE_PERMISSION_KEYS: dict[str, frozenset[str]] = {
    ROLE_TENANT_ADMIN: ALL_PERMISSION_KEYS,
    ROLE_TECHNICAL_MEMBER: frozenset(
        {
            PERMISSION_ANALYTICS_BUSINESS_VIEW,
            PERMISSION_ANALYTICS_TECHNICAL_VIEW,
            PERMISSION_RUNS_BUSINESS_VIEW,
            PERMISSION_RUNS_TECHNICAL_VIEW,
            PERMISSION_SETTINGS_BUSINESS_VIEW,
            PERMISSION_SETTINGS_TECHNICAL_VIEW,
        }
    ),
    ROLE_BUSINESS_MEMBER: frozenset(
        {
            PERMISSION_ANALYTICS_BUSINESS_VIEW,
            PERMISSION_RUNS_BUSINESS_VIEW,
            PERMISSION_SETTINGS_BUSINESS_VIEW,
        }
    ),
}

VALID_ROLE_KEYS = frozenset(ROLE_PERMISSION_KEYS.keys())
VALID_MODE_KEYS = frozenset({MODE_TECHNICAL, MODE_NON_TECHNICAL})
VALID_ONBOARDING_KINDS = frozenset({"tenant_admin_setup", "member_join"})


@dataclass(frozen=True)
class TenantPermissionSnapshot:
    permission_keys: tuple[str, ...]
    effective_mode: str


def get_role_permission_keys(role: str) -> frozenset[str]:
    return ROLE_PERMISSION_KEYS.get(role, frozenset())


def compute_permission_snapshot(
    *,
    tenant_default_mode: str,
    membership_role: str,
    membership_mode_override: str | None,
    team_permission_keys: list[str],
) -> TenantPermissionSnapshot:
    merged = set(get_role_permission_keys(membership_role))
    merged.update(permission for permission in team_permission_keys if permission in ALL_PERMISSION_KEYS)
    requested_mode = membership_mode_override or tenant_default_mode or MODE_TECHNICAL
    if PERMISSION_ANALYTICS_TECHNICAL_VIEW not in merged and PERMISSION_RUNS_TECHNICAL_VIEW not in merged:
        effective_mode = MODE_NON_TECHNICAL
    else:
        effective_mode = requested_mode if requested_mode in VALID_MODE_KEYS else MODE_TECHNICAL
    return TenantPermissionSnapshot(permission_keys=tuple(sorted(merged)), effective_mode=effective_mode)
