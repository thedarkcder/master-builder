from __future__ import annotations

import json
from pathlib import Path

from orchestrator.core.config import get_settings
from orchestrator.core.runtime.invocation import (
    AgentInvocationContext,
    invoke_runtime_json,
)
from orchestrator.core.runtime.runtime import CodexRuntimeError, build_codex_runtime
from orchestrator.core.project_app_planner import (
    ProjectAppAnalysisResult,
    ProjectAppAnalysisRunMetadata,
    normalize_project_app_planner_output,
    scan_repo_for_project_apps,
)
from orchestrator.storage.models import Project, Tenant

_PROJECT_APP_ANALYSIS_SYSTEM_PROMPT = """\
You are the deploy planner for Master Builder.
Return JSON only.
Analyze the repository checkout and identify deployable app candidates.
Use the deterministic pre-scan as a baseline and do not invent paths outside the repository.
For each app, emit only the contract keys required by the runtime:
name, source_path, build_strategy, port, healthcheck, resources, env, secrets, needs_generated_files.
Prefer repo-relative source paths and deterministic output ordering.
"""


def _candidate_prompt_json(candidate) -> dict[str, object]:  # noqa: ANN001
    return {
        "name": candidate.name,
        "source_path": candidate.source_path,
        "build_strategy": candidate.build_strategy,
        "detected_runtime": candidate.detected_runtime,
        "detected_language": candidate.detected_language,
        "detection_confidence": candidate.detection_confidence,
        "exposed_port": candidate.exposed_port,
        "healthcheck": candidate.healthcheck,
        "start_command": candidate.start_command,
        "env_schema_json": candidate.env_schema_json,
        "secret_schema_json": candidate.secret_schema_json,
        "analysis_source": candidate.analysis_source,
        "needs_generated_files": candidate.needs_generated_files,
        "services": [
            dict(service) for service in getattr(candidate, "services_json", ())
        ],
        "resources": [dict(resource) for resource in candidate.resources_json],
        "volumes": [dict(volume) for volume in getattr(candidate, "volumes_json", ())],
    }


def _render_user_prompt(
    *,
    tenant: Tenant,
    project: Project,
    checkout_path: str,
    analysis_source: str | None,
    pre_scan_candidates_json: str,
) -> str:
    payload = {
        "tenant_id": tenant.tenant_id,
        "project_id": project.project_id,
        "project_name": project.name,
        "github_repository": project.github_repository,
        "checkout_path": str(Path(checkout_path).resolve()),
        "analysis_source": analysis_source,
        "pre_scan_candidates": json.loads(pre_scan_candidates_json),
    }
    return json.dumps(payload, sort_keys=True, indent=2)


def run_project_app_analysis(
    *,
    tenant: Tenant,
    project: Project,
    checkout_path: str,
    analysis_source: str | None = None,
    planner_version: str | None = None,
    session=None,  # noqa: ANN001
    settings=None,  # noqa: ANN001
) -> ProjectAppAnalysisResult:
    normalized_checkout_path = str(Path(checkout_path or "").resolve())
    pre_scan_candidates = scan_repo_for_project_apps(
        checkout_path=normalized_checkout_path,
        analysis_source=analysis_source,
    )
    pre_scan_candidates_json = json.dumps(
        [_candidate_prompt_json(candidate) for candidate in pre_scan_candidates],
        sort_keys=True,
    )
    active_settings = settings or get_settings()
    runtime = build_codex_runtime(session=session, settings=active_settings)
    try:
        runtime_payload = invoke_runtime_json(
            runtime=runtime,
            context=AgentInvocationContext(
                channel="system",
                tenant_id=tenant.tenant_id,
                project_id=project.project_id,
                command="project_app_analysis",
                stage="project_app_analysis",
                working_dir=normalized_checkout_path,
                reasoning_effort="low",
            ),
            system_prompt=_PROJECT_APP_ANALYSIS_SYSTEM_PROMPT,
            user_prompt=_render_user_prompt(
                tenant=tenant,
                project=project,
                checkout_path=normalized_checkout_path,
                analysis_source=analysis_source,
                pre_scan_candidates_json=pre_scan_candidates_json,
            ),
        )
    except CodexRuntimeError as exc:
        raise RuntimeError(f"Project app analysis failed: {exc}") from exc

    normalized_apps = normalize_project_app_planner_output(
        pre_scan_candidates=pre_scan_candidates,
        runtime_payload=runtime_payload,
        analysis_source=analysis_source,
    )
    metadata = ProjectAppAnalysisRunMetadata(
        tenant_id=tenant.tenant_id,
        project_id=project.project_id,
        checkout_path=normalized_checkout_path,
        analysis_source=analysis_source,
        planner_version=planner_version,
        pre_scan_count=len(pre_scan_candidates),
        runtime_count=len(runtime_payload.get("apps", []))
        if isinstance(runtime_payload.get("apps"), list)
        else 0,
        normalized_count=len(normalized_apps),
        raw_planner_result_json=dict(runtime_payload),
    )
    return ProjectAppAnalysisResult(apps=normalized_apps, metadata=metadata)
