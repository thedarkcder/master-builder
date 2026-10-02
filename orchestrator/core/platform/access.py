from __future__ import annotations

from dataclasses import dataclass


ROLE_TENANT_ADMIN = "tenant_admin"
ROLE_TECHNICAL_MEMBER = "technical_member"
ROLE_BUSINESS_MEMBER = "business_member"

MODE_TECHNICAL = "technical"
MODE_NON_TECHNICAL = "non_technical"

PERMISSION_WORKSPACE_MANAGE = "workspace.manage"
PERMISSION_PEOPLE_MANAGE = "people.manage"
PERMISSION_PROJECTS_MANAGE = "projects.manage"
PERMISSION_TECHNICAL_ACCESS = "technical.access"

ALL_PERMISSION_KEYS = frozenset(
    {
        PERMISSION_WORKSPACE_MANAGE,
        PERMISSION_PEOPLE_MANAGE,
        PERMISSION_PROJECTS_MANAGE,
        PERMISSION_TECHNICAL_ACCESS,
    }
)

KNOWN_PERMISSION_KEYS = ALL_PERMISSION_KEYS

ROLE_PERMISSION_KEYS: dict[str, frozenset[str]] = {
    ROLE_TENANT_ADMIN: ALL_PERMISSION_KEYS,
    ROLE_TECHNICAL_MEMBER: frozenset({PERMISSION_TECHNICAL_ACCESS}),
    ROLE_BUSINESS_MEMBER: frozenset(),
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


def normalize_permission_key(permission_key: str) -> str | None:
    normalized = str(permission_key).strip()
    if normalized in ALL_PERMISSION_KEYS:
        return normalized
    return None


def normalize_permission_keys(permission_keys: list[str]) -> tuple[str, ...]:
    normalized: set[str] = set()
    for permission_key in permission_keys:
        mapped = normalize_permission_key(permission_key)
        if mapped is not None:
            normalized.add(mapped)
    return tuple(sorted(normalized))


def compute_permission_snapshot(
    *,
    tenant_default_mode: str,
    membership_role: str,
    membership_mode_override: str | None,
    team_permission_keys: list[str],
) -> TenantPermissionSnapshot:
    merged = set(get_role_permission_keys(membership_role))
    merged.update(normalize_permission_keys(team_permission_keys))
    requested_mode = membership_mode_override or tenant_default_mode or MODE_TECHNICAL
    if PERMISSION_TECHNICAL_ACCESS not in merged:
        effective_mode = MODE_NON_TECHNICAL
    else:
        effective_mode = (
            requested_mode if requested_mode in VALID_MODE_KEYS else MODE_TECHNICAL
        )
    return TenantPermissionSnapshot(
        permission_keys=tuple(sorted(merged)), effective_mode=effective_mode
    )
