"""Remove framework-injected Maven application configuration using captured evidence.

Revision ID: 20261002_0134
Revises: 20260616_0133
"""

from __future__ import annotations

import copy
import re
from typing import Any

from alembic import op
import sqlalchemy as sa
import yaml

revision = "20261002_0134"
down_revision = "20260616_0133"
branch_labels = None
depends_on = None

_CONFIG_BUILD = "RUN mkdir -p /tmp/master-builder-config"
_CONFIG_COPY = "COPY --from=build /tmp/master-builder-config /app/master-builder-config"
_LOGGING_BUILD = "RUN cat > /app/master-builder-logback.xml <<'XML'"
_ROLE_SCRIPT = "/docker-entrypoint-initdb.d/20-master-builder-app-roles.sh"
_PROVENANCE_ERROR = (
    "Maven configuration cleanup requires unambiguous original planner evidence. "
    "Review and regenerate affected deployment plans before retrying; configuration values were not logged."
)


def _compose(raw: object) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise RuntimeError(_PROVENANCE_ERROR)
    try:
        payload = yaml.safe_load(raw)
    except yaml.YAMLError:
        raise RuntimeError(_PROVENANCE_ERROR) from None
    if not isinstance(payload, dict) or not isinstance(payload.get("services"), dict):
        raise RuntimeError(_PROVENANCE_ERROR)
    return payload


def _inline(service: object) -> str:
    if not isinstance(service, dict) or not isinstance(service.get("build"), dict):
        return ""
    value = service["build"].get("dockerfile_inline")
    return value if isinstance(value, str) else ""


def _marked_services(compose: dict[str, Any]) -> dict[str, str]:
    return {
        str(name): _inline(service)
        for name, service in compose["services"].items()
        if "master-builder-config" in _inline(service)
        or "master-builder-logback.xml" in _inline(service)
        or _ROLE_SCRIPT in _inline(service)
    }


def _without_generated_files(dockerfile: str) -> str:
    if not all(
        marker in dockerfile for marker in (_CONFIG_BUILD, _CONFIG_COPY, _LOGGING_BUILD)
    ):
        raise RuntimeError(_PROVENANCE_ERROR)
    retained: list[str] = []
    logging_block = False
    for line in dockerfile.splitlines():
        stripped = line.strip()
        if logging_block:
            if stripped == "XML":
                logging_block = False
            continue
        if stripped.startswith(_LOGGING_BUILD):
            logging_block = True
        elif stripped.startswith(_CONFIG_BUILD) or stripped == _CONFIG_COPY:
            continue
        else:
            retained.append(line)
    if logging_block:
        raise RuntimeError(_PROVENANCE_ERROR)
    cleaned = "\n".join(retained) + ("\n" if dockerfile.endswith("\n") else "")
    if "master-builder-config" in cleaned or "master-builder-logback.xml" in cleaned:
        raise RuntimeError(_PROVENANCE_ERROR)
    return cleaned


def _environment(value: object) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, list):
        result: dict[str, Any] = {}
        for item in value:
            if not isinstance(item, str) or "=" not in item:
                raise RuntimeError(_PROVENANCE_ERROR)
            key, entry = item.split("=", 1)
            result[key] = entry
        return result
    raise RuntimeError(_PROVENANCE_ERROR)


def _resource_alias_only(
    original_value: object, generated_value: object, original_compose: dict
) -> bool:
    if not isinstance(original_value, str) or not isinstance(generated_value, str):
        return str(original_value) == generated_value
    aliased = original_value
    for name in original_compose["services"]:
        if aliased == name:
            aliased = f"mb-{name}"
        else:
            aliased = re.sub(
                rf"(?<![\w-]){re.escape(str(name))}(?=:)", f"mb-{name}", aliased
            )
    return aliased == generated_value


def _restore_generated_field(
    current: dict, generated: dict, original: dict, field: str
) -> None:
    if field not in generated or current.get(field) != generated[field]:
        return
    if field in original:
        current[field] = copy.deepcopy(original[field])
    else:
        current.pop(field, None)


def _clean_compose(current: dict, generated: dict, original: dict) -> dict:
    cleaned = copy.deepcopy(current)
    for name, dockerfile in _marked_services(current).items():
        if _ROLE_SCRIPT in dockerfile and "master-builder-config" not in dockerfile:
            continue
        service = cleaned["services"][name]
        generated_service = generated["services"].get(name)
        original_service = original["services"].get(name)
        if not isinstance(generated_service, dict) or not isinstance(
            original_service, dict
        ):
            raise RuntimeError(_PROVENANCE_ERROR)
        if _marked_services({"services": {name: original_service}}):
            raise RuntimeError(_PROVENANCE_ERROR)
        if service.get("build") != generated_service.get("build"):
            raise RuntimeError(_PROVENANCE_ERROR)
        service["build"]["dockerfile_inline"] = _without_generated_files(
            _inline(service)
        )
        current_env = _environment(service.get("environment"))
        generated_env = _environment(generated_service.get("environment"))
        original_env = _environment(original_service.get("environment"))
        for key, value in generated_env.items():
            if (
                key not in current_env
                or current_env[key] != value
                or original_env.get(key) == value
            ):
                continue
            if key in original_env:
                if _resource_alias_only(original_env[key], value, original):
                    continue
                current_env[key] = original_env[key]
            else:
                current_env.pop(key)
        if current_env:
            service["environment"] = current_env
        else:
            service.pop("environment", None)
        _restore_generated_field(
            service, generated_service, original_service, "depends_on"
        )
        if "healthcheck" not in original_service:
            _restore_generated_field(
                service, generated_service, original_service, "healthcheck"
            )
    for name, generated_service in generated["services"].items():
        if _ROLE_SCRIPT not in _inline(generated_service):
            continue
        current_service = cleaned["services"].get(name)
        original_service = original["services"].get(name)
        if not isinstance(current_service, dict) or not isinstance(
            original_service, dict
        ):
            raise RuntimeError(_PROVENANCE_ERROR)
        if _ROLE_SCRIPT in _inline(original_service):
            raise RuntimeError(_PROVENANCE_ERROR)
        for field in ("build", "healthcheck"):
            _restore_generated_field(
                current_service, generated_service, original_service, field
            )
        if "build" not in current_service and "image" in original_service:
            current_service["image"] = original_service["image"]
    return cleaned


def _has_injection(raw: object) -> bool:
    return isinstance(raw, str) and any(
        marker in raw
        for marker in (
            "master-builder-config",
            "master-builder-logback.xml",
            _ROLE_SCRIPT,
        )
    )


def _clean_raw(raw: str, evidence: list[tuple[dict, object]]) -> str:
    current = _compose(raw)
    marked = _marked_services(current)
    if not marked:
        raise RuntimeError(_PROVENANCE_ERROR)
    candidates: dict[str, tuple[dict, dict]] = {}
    exact: dict[str, tuple[dict, dict]] = {}
    for baseline, original in evidence:
        baseline_raw = baseline.get("generated_compose_raw")
        if not _has_injection(baseline_raw):
            continue
        generated = _compose(baseline_raw)
        generated_marked = _marked_services(generated)
        if any(generated_marked.get(name) != value for name, value in marked.items()):
            continue
        original = _compose(original) if isinstance(original, str) else original
        if not isinstance(original, dict) or not isinstance(
            original.get("services"), dict
        ):
            raise RuntimeError(_PROVENANCE_ERROR)
        fingerprint = yaml.safe_dump([generated, original], sort_keys=True)
        candidates[fingerprint] = generated, original
        if baseline_raw == raw:
            exact[fingerprint] = generated, original
    selected = exact if exact else candidates
    if len(selected) != 1:
        raise RuntimeError(_PROVENANCE_ERROR)
    generated, original = next(iter(selected.values()))
    cleaned = yaml.safe_dump(
        _clean_compose(current, generated, original), sort_keys=False
    )
    if _has_injection(cleaned):
        raise RuntimeError(_PROVENANCE_ERROR)
    return cleaned


def clean_configuration(
    payload: dict, evidence: list[tuple[dict, object]]
) -> tuple[dict, bool]:
    raw = payload.get("generated_compose_raw")
    plan = payload.get("deployment_plan")
    plan_raw = plan.get("compose_raw") if isinstance(plan, dict) else None
    if not _has_injection(raw) and not _has_injection(plan_raw):
        return payload, False
    updated = copy.deepcopy(payload)
    if _has_injection(raw):
        updated["generated_compose_raw"] = _clean_raw(raw, evidence)
    if _has_injection(plan_raw):
        updated["deployment_plan"]["compose_raw"] = _clean_raw(plan_raw, evidence)
    return updated, updated != payload


def _walk(payload: Any, evidence: list[tuple[dict, object]]) -> Any:
    if isinstance(payload, dict):
        cleaned, _ = clean_configuration(payload, evidence)
        return {key: _walk(value, evidence) for key, value in cleaned.items()}
    if isinstance(payload, list):
        return [_walk(value, evidence) for value in payload]
    return payload


def upgrade() -> None:
    bind = op.get_bind()
    metadata = sa.MetaData()
    names = set(sa.inspect(bind).get_table_names())
    evidence_by_project: dict[tuple[str, str], list[tuple[dict, object]]] = {}
    analysis_rows = []
    if "project_app_analysis_runs" in names:
        analysis = sa.Table("project_app_analysis_runs", metadata, autoload_with=bind)
        analysis_rows = bind.execute(sa.select(analysis)).mappings().all()
        for row in analysis_rows:
            result = row["result_payload"]
            if not isinstance(result, dict):
                continue
            raw_result = result.get("raw_planner_result_json")
            original_plan = (
                raw_result.get("deployment") if isinstance(raw_result, dict) else None
            )
            if not isinstance(original_plan, dict) or not isinstance(
                original_plan.get("compose_raw"), str
            ):
                continue
            original = original_plan["compose_raw"]
            normalized_apps = result.get("normalized_apps")
            if not isinstance(normalized_apps, list):
                continue
            for candidate in normalized_apps:
                if isinstance(candidate, dict) and isinstance(
                    candidate.get("deployment_config"), dict
                ):
                    key = row["tenant_id"], row["project_id"]
                    evidence_by_project.setdefault(key, []).append(
                        (candidate["deployment_config"], original)
                    )
    for table_name, identifier, column in (
        ("projects", "project_id", "deployment_config"),
        ("project_apps", "app_id", "deployment_config"),
        ("project_deployment_releases", "release_id", "deployment_snapshot"),
        ("project_app_analysis_runs", "run_id", "result_payload"),
    ):
        if table_name not in names:
            continue
        table = sa.Table(table_name, metadata, autoload_with=bind, extend_existing=True)
        if column not in table.c:
            continue
        rows = (
            analysis_rows
            if table_name == "project_app_analysis_runs"
            else bind.execute(sa.select(table)).mappings().all()
        )
        for row in rows:
            key = row["tenant_id"], row["project_id"]
            updated = _walk(row[column], evidence_by_project.get(key, []))
            if updated != row[column]:
                bind.execute(
                    table.update()
                    .where(table.c[identifier] == row[identifier])
                    .values({column: updated})
                )


def downgrade() -> None:
    raise RuntimeError(
        "Private injected Maven configuration is intentionally not restored by downgrade."
    )
