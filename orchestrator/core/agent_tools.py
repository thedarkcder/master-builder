from __future__ import annotations

import base64
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session
from sqlalchemy import select

from orchestrator.api.jira_oauth.service import jira_oauth_client, refresh_jira_connection_tokens
from orchestrator.core.binding_resolution_service import check_project_bindings
from orchestrator.core.decision_types import JiraConfigKey, tenant_jira_config_text
from orchestrator.core.install_registry_service import get_project_install, list_project_installs
from orchestrator.core.install_request_service import ProjectInstallRequestWrite, create_install_request, request_kind_for_install
from orchestrator.core.knowledge_exact_read import ExactReadRequest, exact_read_knowledge_source
from orchestrator.core.knowledge_base import KnowledgeEmbeddingAccessMode, build_knowledge_prompt_context
from orchestrator.core.platform_secret_service import resolve_platform_secret_ref
from orchestrator.core.project_policy import resolve_effective_policy
from orchestrator.core.run_human_input_service import create_human_input_request
from orchestrator.core.tenant_secret_service import resolve_scoped_secret_ref
from orchestrator.core.trusted_install_executor import run_install as execute_project_install
from orchestrator.core.worker_workspace import resolve_worker_workspace_key
from orchestrator.core.workflow.execution_snapshot import load_parsed_trigger_context_from_plan
from orchestrator.core.workflow.trigger_context import GithubPrRemediationTriggerContext
from orchestrator.storage.models import (
    DecisionAnswer,
    DecisionCase,
    DecisionCycle,
    DecisionEvidence,
    JiraOAuthConnection,
    Project,
    Run,
    Tenant,
)
from orchestrator.tools.git_ops import build_branch_name
from orchestrator.tools.github_app import GitHubApiError, github_client_from_tenant_config
from orchestrator.tools.project_repo_checkout import project_repo_dir, project_run_repo_dir
from orchestrator.tools.repo_allowlist import normalize_repo_identifier


TOOL_ALLOWLIST: dict[str, set[str]] = {
    "pm": {
        "jira.get_issue",
        "jira.comment",
        "jira.transition",
        "decision.read_state",
        "knowledge.exact_read",
        "knowledge.read",
        "project.list_installs",
        "project.check_runtime_bindings",
        "project.request_install",
        "run.request_human_input",
        "repo.read",
    },
    "dev": {
        "knowledge.exact_read",
        "jira.comment",
        "jira.transition",
        "github.create_branch",
        "github.commit_all",
        "github.push_branch",
        "github.open_pr",
        "repo.read",
        "project.list_installs",
        "project.check_runtime_bindings",
        "project.request_install",
        "exec.run_install",
        "run.request_human_input",
    },
    "test": {
        "knowledge.exact_read",
        "jira.comment",
        "repo.read",
        "project.list_installs",
        "project.check_runtime_bindings",
        "project.request_install",
        "exec.run_install",
        "run.request_human_input",
    },
    "review": {
        "knowledge.exact_read",
        "jira.comment",
        "jira.transition",
        "github.push_branch",
        "github.open_pr",
        "repo.read",
        "project.list_installs",
        "project.check_runtime_bindings",
        "project.request_install",
        "exec.run_install",
        "run.request_human_input",
    },
    "orchestrator": {
        "knowledge.exact_read",
        "jira.get_issue",
        "jira.comment",
        "jira.transition",
        "github.create_branch",
        "github.commit_all",
        "github.push_branch",
        "github.open_pr",
        "github.get_pr_details",
        "github.list_pr_files",
        "github.list_pr_reviews",
        "github.list_pr_review_comments",
        "github.list_pr_issue_comments",
        "github.list_check_suites",
        "repo.read",
        "project.list_installs",
        "project.check_runtime_bindings",
        "project.request_install",
        "exec.run_install",
        "run.request_human_input",
    },
    "decision_planner": {
        "jira.get_issue",
        "repo.read",
        "decision.read_state",
        "knowledge.exact_read",
        "knowledge.read",
        "project.list_installs",
        "project.check_runtime_bindings",
        "project.request_install",
        "run.request_human_input",
    },
    "voice_entry_router": {
        "knowledge.exact_read",
        "knowledge.read",
        "jira.get_issue",
    },
    # Persona-specific framing lives in ask_answer_*.j2 (persona_id); tool allowlist is shared across personas.
    "discord_ask_answer": {
        "knowledge.exact_read",
        "knowledge.read",
        "jira.get_issue",
        "repo.read",
    },
    "discord_voice_room_persona": {
        "knowledge.exact_read",
        "knowledge.read",
        "jira.get_issue",
        "repo.read",
    },
    "discord_pm_interview": {
        "jira.get_issue",
        "jira.comment",
        "jira.transition",
        "decision.read_state",
        "knowledge.exact_read",
        "knowledge.read",
        "run.request_human_input",
        "repo.read",
    },
}

TOOL_DESCRIPTIONS: dict[str, str] = {
    "decision.read_state": "Check whether the active issue has an open, answered, or cleared Decision Gate. Use this before planning or execution when prerequisite or clarification state may block the run.",
    "github.commit_all": "Create a git commit from already-staged changes on the active branch. Use this after local edits are complete and verified.",
    "github.create_branch": "Create or reset the working branch for the active issue from the repository base branch. Use this before making issue-scoped changes.",
    "github.get_pr_details": "Fetch the active pull request's title, body, branches, status, and URLs. Use this to confirm current PR state before review, follow-up work, or status reporting.",
    "github.list_check_suites": "Fetch CI and check-suite results for a pull request head commit. Use this to see whether automation is passing, failing, or still running.",
    "github.list_pr_files": "List the files changed in the active pull request. Use this to scope review, testing, or targeted follow-up fixes.",
    "github.list_pr_issue_comments": "Read top-level conversation comments on the pull request. Use this to see reviewer or stakeholder discussion that is not attached to specific lines.",
    "github.list_pr_review_comments": "Read inline review comments attached to changed lines in the pull request. Use this to address actionable code review feedback.",
    "github.list_pr_reviews": "Read submitted pull request reviews and states such as APPROVED or CHANGES_REQUESTED. Use this to understand overall review status.",
    "github.open_pr": "Create a new pull request or update the current branch's pull request against the target branch. Use this when implementation is ready for review or needs a PR refresh.",
    "github.push_branch": "Push the active branch to the configured remote repository. Use this after committing so the remote branch, PR, and CI see the latest changes.",
    "jira.comment": "Add a comment to the active Jira issue. Use this to report progress, blockers, validation steps, or handoff notes.",
    "jira.get_issue": "Fetch the active Jira issue's live summary, description, status, and related metadata. Use this when you need authoritative ticket scope or status.",
    "jira.transition": "Move the active Jira issue to another workflow state. Use this only when the stage outcome is clear, for example Testing, Done, or Blocked.",
    "knowledge.exact_read": "Read a specific knowledge asset or exact knowledge match by identifier. Use this when you already know the document you need and want authoritative contents.",
    "knowledge.read": "Search the knowledge base and summarize the most relevant results for the active issue. Use this when you need supporting context but do not know the exact document.",
    "project.list_installs": "List the integrations installed for the active project. Use this to confirm whether a required Fastlane, Supabase, Railway, Slack, or similar install already exists before planning or execution.",
    "project.check_runtime_bindings": "Check whether explicitly named project bindings are configured. Use this only to verify presence of required env or secret-backed bindings; it never returns the underlying values.",
    "project.request_install": "Create a structured install request for the active run and pause the workflow. Use this when execution depends on an integration that is not yet installed for the project.",
    "repo.read": "Run guarded read-only repository commands and return file or git metadata. Use this to inspect code, files, branches, or diffs without making changes.",
    "run.request_human_input": "Create a structured human-input request for the active run and pause the workflow until a reply arrives. Use this when one-time operator clarification or data is required to continue.",
    "exec.run_install": "Execute a registered project install with its pre-approved bindings injected server-side. Use this when a configured integration must run and the model must not see the binding values.",
}

_READ_ONLY_SHELL_OPERATOR_PATTERN = re.compile(r"[|;&><`]|(?:\$\()")
_READ_ONLY_REPO_COMMANDS = {
    "ls",
    "cat",
    "head",
    "tail",
    "wc",
    "pwd",
    "find",
    "rg",
    "stat",
    "du",
    "tree",
}
_READ_ONLY_GIT_SUBCOMMANDS = {
    "status",
    "diff",
    "log",
    "show",
    "rev-parse",
    "remote",
    "ls-files",
    "ls-tree",
    "describe",
    "tag",
}
_DEV_STAGE_BLOCKED_GIT_SUBCOMMANDS = {
    "push",
}


@dataclass(frozen=True)
class AgentToolContext:
    tenant: Tenant
    project: Project
    stage: str
    issue_key: str
    run_id: str | None
    repo_dir: Path
    run: Run | None = None


def _knowledge_embedding_access_mode_for_context(context: AgentToolContext) -> KnowledgeEmbeddingAccessMode:
    if context.run_id:
        return KnowledgeEmbeddingAccessMode.LOCAL_ONLY
    return KnowledgeEmbeddingAccessMode.BEST_EFFORT


def _ensure_repo_checkout_exists(repo_dir: Path) -> None:
    if not (repo_dir / ".git").exists():
        raise ValueError(f"Repository checkout missing at {repo_dir}")


def allowed_tools_for_stage(stage: str) -> set[str]:
    return set(TOOL_ALLOWLIST.get(str(stage or "").strip().lower(), set()))


def list_implemented_tools() -> list[dict[str, object]]:
    stage_order = list(TOOL_ALLOWLIST.keys())
    tool_stage_membership: dict[str, list[str]] = {}
    for stage in stage_order:
        for tool_name in sorted(TOOL_ALLOWLIST[stage]):
            tool_stage_membership.setdefault(tool_name, []).append(stage)
    tools: list[dict[str, object]] = []
    for tool_name in sorted(tool_stage_membership.keys()):
        category, _, _ = tool_name.partition(".")
        tools.append(
            {
                "tool_name": tool_name,
                "category": category or "other",
                "description": TOOL_DESCRIPTIONS.get(tool_name, "Implemented governed tool."),
                "stages": tool_stage_membership[tool_name],
            }
        )
    return tools


def tool_catalog_for_stage(stage: str) -> list[dict[str, object]]:
    normalized_stage = str(stage or "").strip().lower()
    if not normalized_stage:
        return []
    return [
        tool
        for tool in list_implemented_tools()
        if normalized_stage in tool.get("stages", [])
    ]


def build_agent_tool_command(
    *,
    tenant_id: str,
    project_id: str | None,
    run_id: str | None,
    issue_key: str,
    stage: str,
) -> str:
    command = [
        sys.executable,
        "-m",
        "orchestrator",
        "agent-tool",
        "--tenant",
        str(tenant_id or "").strip(),
    ]
    normalized_project_id = str(project_id or "").strip()
    if normalized_project_id:
        command.extend(["--project", normalized_project_id])
    normalized_run_id = str(run_id or "").strip()
    if normalized_run_id:
        command.extend(["--run", normalized_run_id])
    command.extend(
        [
            "--issue",
            str(issue_key or "").strip(),
            "--stage",
            str(stage or "").strip(),
            "--tool",
            "<tool_name>",
            "--args",
            "<json-object>",
        ]
    )
    return shlex.join(command)


def execute_agent_tool(
    *,
    session: Session,
    settings,
    tenant_id: str,
    project_id: str | None,
    run_id: str | None,
    issue_key: str,
    stage: str,
    tool_name: str,
    tool_args: dict[str, Any] | None,
) -> dict[str, Any]:  # noqa: ANN401
    context = _resolve_context(
        session=session,
        settings=settings,
        tenant_id=tenant_id,
        project_id=project_id,
        run_id=run_id,
        issue_key=issue_key,
        stage=stage,
    )
    allowed = allowed_tools_for_stage(context.stage)
    if tool_name not in allowed:
        raise PermissionError(f"Tool '{tool_name}' is not allowed in stage '{context.stage}'")
    args = tool_args or {}

    if tool_name == "repo.read":
        return _tool_repo_read(context=context, args=args)
    if tool_name.startswith("project."):
        return _execute_project_tool(
            session=session,
            settings=settings,
            context=context,
            tool_name=tool_name,
            args=args,
        )
    if tool_name.startswith("exec."):
        return _execute_exec_tool(
            session=session,
            settings=settings,
            context=context,
            tool_name=tool_name,
            args=args,
        )
    if tool_name.startswith("run."):
        return _execute_run_tool(
            session=session,
            settings=settings,
            context=context,
            tool_name=tool_name,
            args=args,
        )
    if tool_name.startswith("decision."):
        return _execute_decision_tool(session=session, context=context, tool_name=tool_name, args=args)
    if tool_name.startswith("knowledge."):
        return _execute_knowledge_tool(
            session=session,
            settings=settings,
            context=context,
            tool_name=tool_name,
            args=args,
        )
    if tool_name.startswith("jira."):
        return _execute_jira_tool(session=session, settings=settings, context=context, tool_name=tool_name, args=args)
    if tool_name.startswith("github."):
        return _execute_github_tool(session=session, settings=settings, context=context, tool_name=tool_name, args=args)

    raise ValueError(f"Unknown tool '{tool_name}'")


def _resolve_context(
    *,
    session: Session,
    settings,
    tenant_id: str,
    project_id: str | None,
    run_id: str | None,
    issue_key: str,
    stage: str,
) -> AgentToolContext:  # noqa: ANN001
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise ValueError(f"Unknown tenant '{tenant_id}'")

    run: Run | None = None
    run = session.get(Run, run_id) if run_id else None
    project: Project | None = None
    if project_id:
        project = session.get(Project, project_id)
    if project is None and run is not None:
        if run is not None and run.project_id:
            project = session.get(Project, run.project_id)
    if project is None:
        project = (
            session.query(Project)
            .filter(Project.tenant_id == tenant.tenant_id, Project.project_id == f"{tenant.tenant_id}-default")
            .first()
        )
    if project is None:
        raise ValueError(f"No project mapping available for tenant '{tenant.tenant_id}'")

    if run_id:
        workspace_key = resolve_worker_workspace_key(settings=settings)
        repo_dir = project_run_repo_dir(
            base_dir=settings.project_repo_checkout_base_dir,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
            run_id=run_id,
            workspace_key=workspace_key,
        )
    else:
        repo_dir = project_repo_dir(
            base_dir=settings.project_repo_checkout_base_dir,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
        )

    return AgentToolContext(
        tenant=tenant,
        project=project,
        stage=str(stage or "").strip().lower(),
        issue_key=str(issue_key or "").strip(),
        run_id=str(run_id).strip() if run_id else None,
        repo_dir=repo_dir,
        run=run,
    )


def _tool_repo_read(*, context: AgentToolContext, args: dict[str, Any]) -> dict[str, Any]:  # noqa: ANN401
    command = str(args.get("command") or "").strip()
    if not command:
        raise ValueError("repo.read requires 'command'")
    _ensure_repo_checkout_exists(context.repo_dir)
    _enforce_repo_command_for_stage(stage=context.stage, command=command)
    process = subprocess.run(  # noqa: S603
        ["/bin/zsh", "-lc", command],
        cwd=str(context.repo_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "ok": process.returncode == 0,
        "exit_code": process.returncode,
        "stdout": process.stdout,
        "stderr": process.stderr,
    }


def _enforce_repo_command_for_stage(*, stage: str, command: str) -> None:
    if _READ_ONLY_SHELL_OPERATOR_PATTERN.search(command):
        raise PermissionError("repo.read only allows a single command (no shell operators)")
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        raise PermissionError("repo.read command could not be parsed safely") from exc
    if not tokens:
        raise PermissionError("repo.read command must not be empty")

    normalized_stage = str(stage).strip().lower()
    executable = tokens[0]
    if normalized_stage == "dev":
        if executable == "git" and len(tokens) >= 2:
            subcommand = tokens[1]
            if subcommand in _DEV_STAGE_BLOCKED_GIT_SUBCOMMANDS:
                raise PermissionError(
                    "repo.read does not allow 'git push' in dev; use github.push_branch"
                )
        return

    if executable == "git":
        if len(tokens) < 2:
            raise PermissionError("repo.read git command must include a read-only subcommand")
        subcommand = tokens[1]
        if subcommand not in _READ_ONLY_GIT_SUBCOMMANDS:
            raise PermissionError(
                f"repo.read does not allow mutating git subcommand '{subcommand}'"
            )
        return

    if executable not in _READ_ONLY_REPO_COMMANDS:
        raise PermissionError(
            f"repo.read does not allow command '{executable}'; use a read-only command"
        )


def _execute_jira_tool(
    *,
    session: Session,
    settings,
    context: AgentToolContext,
    tool_name: str,
    args: dict[str, Any],  # noqa: ANN401
) -> dict[str, Any]:
    connection_id = tenant_jira_config_text(tenant=context.tenant, key=JiraConfigKey.CONNECTION_ID)
    if not connection_id:
        raise ValueError("Tenant Jira connection is not configured")
    connection = session.get(JiraOAuthConnection, connection_id)
    if connection is None:
        raise ValueError(f"Jira connection '{connection_id}' not found")

    client = jira_oauth_client(session=session, settings=settings, tenant_id=context.tenant.tenant_id)
    access_token = refresh_jira_connection_tokens(
        session,
        connection=connection,
        settings=settings,
        tenant_id=context.tenant.tenant_id,
    )
    issue_id_or_key = str(args.get("issue_key") or context.issue_key).strip()
    if not issue_id_or_key:
        raise ValueError("Missing issue_key")

    if tool_name == "jira.get_issue":
        detail = client.get_issue_detail(
            access_token=access_token,
            cloud_id=connection.cloud_id,
            issue_id_or_key=issue_id_or_key,
        )
        return {
            "issue_key": detail.key,
            "summary": detail.summary,
            "status": detail.status,
            "description": detail.description,
        }

    if tool_name == "jira.comment":
        comment = str(args.get("comment") or "").strip()
        if not comment:
            raise ValueError("jira.comment requires 'comment'")
        payload = client.add_issue_comment(
            access_token=access_token,
            cloud_id=connection.cloud_id,
            issue_id_or_key=issue_id_or_key,
            comment=comment,
        )
        return {"comment_id": payload.get("id")}

    if tool_name == "jira.transition":
        effective_policy = resolve_effective_policy(
            tenant_policy=context.tenant.policy_config,
            project_overrides=context.project.policy_overrides,
        )
        if not bool(effective_policy.get("allow_jira_transitions")):
            raise PermissionError("Jira transitions are disabled by policy")
        target_status = str(args.get("target_status") or "").strip()
        if not target_status:
            raise ValueError("jira.transition requires 'target_status'")
        payload = client.transition_issue(
            access_token=access_token,
            cloud_id=connection.cloud_id,
            issue_id_or_key=issue_id_or_key,
            target_status=target_status,
        )
        return payload

    raise ValueError(f"Unsupported Jira tool '{tool_name}'")


def _execute_decision_tool(
    *,
    session: Session,
    context: AgentToolContext,
    tool_name: str,
    args: dict[str, Any],  # noqa: ANN401
) -> dict[str, Any]:
    if tool_name != "decision.read_state":
        raise ValueError(f"Unsupported Decision tool '{tool_name}'")
    issue_key = str(args.get("issue_key") or context.issue_key).strip()
    if not issue_key:
        raise ValueError("decision.read_state requires 'issue_key'")
    case = session.execute(
        select(DecisionCase).where(
            DecisionCase.tenant_id == context.tenant.tenant_id,
            DecisionCase.issue_key == issue_key,
        )
    ).scalar_one_or_none()
    if case is None:
        return {"issue_key": issue_key, "case": None, "active_cycle": None, "answers": [], "recent_evidence": []}
    cycle = session.get(DecisionCycle, case.active_cycle_id) if case.active_cycle_id else None
    answers = session.execute(
        select(DecisionAnswer)
        .where(
            DecisionAnswer.tenant_id == context.tenant.tenant_id,
            DecisionAnswer.issue_key == issue_key,
        )
        .order_by(DecisionAnswer.updated_at.desc())
    ).scalars().all()
    recent_evidence = session.execute(
        select(DecisionEvidence)
        .where(
            DecisionEvidence.tenant_id == context.tenant.tenant_id,
            DecisionEvidence.issue_key == issue_key,
        )
        .order_by(DecisionEvidence.created_at.desc())
        .limit(8)
    ).scalars().all()
    return {
        "issue_key": issue_key,
        "case": {
            "case_id": case.case_id,
            "state": case.state,
            "classification": case.classification,
            "blocked_reason": case.blocked_reason,
            "active_cycle_id": case.active_cycle_id,
            "metadata": case.metadata_json if isinstance(case.metadata_json, dict) else {},
        },
        "active_cycle": (
            {
                "cycle_id": cycle.cycle_id,
                "status": cycle.status,
                "classification": cycle.classification,
                "reason": cycle.reason,
                "questions": cycle.question_set_json,
                "unresolved_question_ids": cycle.unresolved_question_ids_json,
                "metadata": cycle.metadata_json if isinstance(cycle.metadata_json, dict) else {},
            }
            if cycle is not None
            else None
        ),
        "answers": [
            {
                "question_id": answer.question_id,
                "question_text": answer.question_text,
                "status": answer.status,
                "answer": answer.normalized_answer,
                "notes": (
                    str((answer.metadata_json or {}).get("notes") or "").strip()
                    if isinstance(answer.metadata_json, dict)
                    else ""
                ),
                "updated_at": answer.updated_at.isoformat() if answer.updated_at else None,
            }
            for answer in answers
        ],
        "recent_evidence": [
            {
                "evidence_id": evidence.evidence_id,
                "source_transport": evidence.source_transport,
                "source_ref": evidence.source_ref,
                "raw_text": evidence.raw_text,
                "question_ids": evidence.question_ids_json,
                "normalized_answers": evidence.normalized_answers_json,
                "created_at": evidence.created_at.isoformat() if evidence.created_at else None,
            }
            for evidence in recent_evidence
        ],
    }


def _execute_knowledge_tool(
    *,
    session: Session,
    settings,
    context: AgentToolContext,
    tool_name: str,
    args: dict[str, Any],  # noqa: ANN401
) -> dict[str, Any]:
    if tool_name == "knowledge.exact_read":
        payload = exact_read_knowledge_source(
            session=session,
            settings=settings,
            request=ExactReadRequest(
                tenant=context.tenant,
                project=context.project,
                issue_key=context.issue_key,
                source_type=str(args.get("source_type") or "").strip() or None,
                source_ref=str(args.get("source_ref") or "").strip() or None,
                asset_id=str(args.get("asset_id") or "").strip() or None,
            ),
        )
        return payload
    if tool_name != "knowledge.read":
        raise ValueError(f"Unsupported Knowledge tool '{tool_name}'")
    query = str(args.get("query") or context.issue_key).strip()
    if not query:
        raise ValueError("knowledge.read requires 'query'")
    max_items = int(args.get("max_items") or 5)
    max_chars = int(args.get("max_chars") or 2400)
    payload = build_knowledge_prompt_context(
        session=session,
        tenant_id=context.tenant.tenant_id,
        project_id=context.project.project_id,
        query=query,
        max_items=max(1, min(max_items, 10)),
        max_chars=max(500, min(max_chars, 6000)),
        embedding_access_mode=_knowledge_embedding_access_mode_for_context(context),
    )
    return {
        "query": query,
        "text": payload.text,
        "citations": payload.citations,
    }


def _execute_project_tool(
    *,
    session: Session,
    settings,
    context: AgentToolContext,
    tool_name: str,
    args: dict[str, Any],  # noqa: ANN401
) -> dict[str, Any]:
    if tool_name == "project.list_installs":
        installs = list_project_installs(
            session=session,
            tenant_id=context.tenant.tenant_id,
            project_id=context.project.project_id,
        )
        return {
            "installs": [
                {
                    "install_id": install.install_id,
                    "kind": install.kind,
                    "label": install.label,
                    "enabled": install.enabled,
                }
                for install in installs
            ]
        }

    if tool_name == "project.check_runtime_bindings":
        raw_keys = args.get("keys")
        if isinstance(raw_keys, str):
            requested_keys = [raw_keys]
        elif isinstance(raw_keys, list):
            requested_keys = [str(item or "").strip() for item in raw_keys]
        else:
            requested_keys = []
        normalized_keys = [key for key in requested_keys if key]
        if not normalized_keys:
            raise ValueError("project.check_runtime_bindings requires non-empty 'keys'")
        statuses = check_project_bindings(
            session=session,
            project=context.project,
            tenant_id=context.tenant.tenant_id,
            encryption_key=str(getattr(settings, "secrets_encryption_key", "") or "").strip(),
            keys=normalized_keys,
        )
        return {
            "bindings": [
                {
                    "key": item.key,
                    "present": item.present,
                    "source": item.source,
                }
                for item in statuses
            ]
        }

    if tool_name == "project.request_install":
        if context.run_id is None or context.run is None:
            raise ValueError("project.request_install requires an active run context")
        kind = str(args.get("kind") or "").strip().lower()
        label = str(args.get("label") or "").strip()
        reason = str(args.get("reason") or "").strip()
        suggested_config = args.get("suggested_config")
        raw_required_bindings = args.get("required_bindings")
        if isinstance(raw_required_bindings, str):
            required_bindings = [raw_required_bindings]
        elif isinstance(raw_required_bindings, list):
            required_bindings = [str(item or "").strip() for item in raw_required_bindings]
        else:
            required_bindings = []
        if not kind:
            raise ValueError("project.request_install requires non-empty 'kind'")
        if not label:
            raise ValueError("project.request_install requires non-empty 'label'")
        if not reason:
            raise ValueError("project.request_install requires non-empty 'reason'")
        request = create_install_request(
            session=session,
            settings=settings,
            tenant=context.tenant,
            project=context.project,
            run=context.run,
            source_stage=context.stage,
            payload=ProjectInstallRequestWrite(
                kind=kind,
                label=label,
                reason=reason,
                suggested_config=suggested_config if isinstance(suggested_config, dict) else {},
                required_bindings=tuple(required_bindings),
            ),
        )
        return {
            "request_id": request.request_id,
            "request_kind": request.request_kind,
            "status": request.status,
            "waiting_for_input": True,
            "kind_supported": request_kind_for_install(kind) != "unsupported_kind",
        }

    raise ValueError(f"Unsupported Project tool '{tool_name}'")


def _execute_exec_tool(
    *,
    session: Session,
    settings,
    context: AgentToolContext,
    tool_name: str,
    args: dict[str, Any],  # noqa: ANN401
) -> dict[str, Any]:
    if tool_name != "exec.run_install":
        raise ValueError(f"Unsupported Exec tool '{tool_name}'")
    install_id = str(args.get("install_id") or "").strip()
    if not install_id:
        raise ValueError("exec.run_install requires non-empty 'install_id'")
    runtime_input = args.get("runtime_input")
    if runtime_input is not None and not isinstance(runtime_input, dict):
        raise ValueError("exec.run_install runtime_input must be a JSON object when provided")
    install = get_project_install(session=session, install_id=install_id)
    if install is None:
        raise ValueError(f"Install '{install_id}' was not found")
    return execute_project_install(
        session=session,
        settings=settings,
        project=context.project,
        install=install,
        repo_dir=context.repo_dir,
        runtime_input=runtime_input if isinstance(runtime_input, dict) else None,
    )


def _execute_run_tool(
    *,
    session: Session,
    settings,
    context: AgentToolContext,
    tool_name: str,
    args: dict[str, Any],  # noqa: ANN401
) -> dict[str, Any]:
    if tool_name != "run.request_human_input":
        raise ValueError(f"Unsupported Run tool '{tool_name}'")
    if not context.run_id:
        raise ValueError("run.request_human_input requires an active run context")
    run = session.get(Run, context.run_id)
    if run is None:
        raise ValueError(f"Run '{context.run_id}' was not found")
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("run.request_human_input requires non-empty 'prompt'")
    request_type = str(args.get("request_type") or "").strip().lower()
    if not request_type:
        raise ValueError("run.request_human_input requires non-empty 'request_type'")
    expected_reply_format = str(args.get("expected_reply_format") or "").strip() or None
    instructions = str(args.get("instructions") or "").strip() or None
    expires_in_minutes_raw = args.get("expires_in_minutes")
    expires_in_minutes = int(expires_in_minutes_raw) if expires_in_minutes_raw is not None else None
    request_context = args.get("request_context")
    if request_context is not None and not isinstance(request_context, dict):
        raise ValueError("run.request_human_input request_context must be a JSON object when provided")
    request = create_human_input_request(
        session=session,
        settings=settings,
        tenant=context.tenant,
        project=context.project,
        run=run,
        issue_key=context.issue_key,
        source_stage=context.stage,
        request_type=request_type,
        prompt=prompt,
        instructions=instructions,
        expected_reply_format=expected_reply_format,
        request_context=request_context if isinstance(request_context, dict) else None,
        expires_in_minutes=expires_in_minutes,
    )
    return {
        "request_id": request.request_id,
        "request_type": request.request_type,
        "source_stage": request.source_stage,
        "workflow_id": request.workflow_id,
        "checkpoint_id": request.checkpoint_id,
        "thread_channel_id": request.thread_channel_id,
        "expires_at": request.expires_at.isoformat() if request.expires_at else None,
    }


def _execute_github_tool(
    *,
    session: Session,
    settings,
    context: AgentToolContext,
    tool_name: str,
    args: dict[str, Any],  # noqa: ANN401
) -> dict[str, Any]:
    _ensure_repo_checkout_exists(context.repo_dir)
    github_config = context.tenant.github_config or {}
    github_repository = str(context.project.github_repository or "").strip()
    if not github_repository:
        raise ValueError("Project GitHub repository is not configured")
    github_client = github_client_from_tenant_config(
        github_config,
        tenant_secret_lookup=lambda secret_ref: resolve_scoped_secret_ref(
            session,
            secret_ref=secret_ref,
            tenant_id=context.tenant.tenant_id,
            project_id=context.project.project_id,
            encryption_key=settings.secrets_encryption_key,
        ),
        platform_secret_lookup=lambda secret_ref: resolve_platform_secret_ref(
            session,
            secret_ref=secret_ref,
            encryption_key=settings.secrets_encryption_key,
        ),
    )
    repo_full_name = _repo_full_name(github_repository)
    canonical_run_branch = _resolve_canonical_run_branch(session=session, context=context)
    default_base_branch = _default_project_base_branch(project=context.project)

    if tool_name == "github.create_branch":
        summary = str(args.get("summary") or context.issue_key).strip()
        requested_branch_name = str(args.get("branch_name") or "").strip()
        branch_name = (
            canonical_run_branch
            or requested_branch_name
            or build_branch_name(context.issue_key, summary)
        ).strip()
        base_branch = str(args.get("base_branch") or "").strip()
        installation_token = github_client.get_installation_token()
        resolved_base_branch = base_branch or _resolve_remote_default_branch(
            context.repo_dir,
            token=installation_token,
        )
        _sync_local_base_branch_to_origin(
            context.repo_dir,
            base_branch=resolved_base_branch,
            token=installation_token,
        )
        _run_git(context.repo_dir, ["checkout", "-B", branch_name])
        return {"branch_name": branch_name}

    if tool_name == "github.commit_all":
        message = str(args.get("message") or f"{context.issue_key}: automated changes").strip()
        _run_git(context.repo_dir, ["add", "-A"])
        _run_git(context.repo_dir, ["commit", "-m", message])
        sha = _run_git(context.repo_dir, ["rev-parse", "HEAD"]).strip()
        return {"sha": sha}

    if tool_name == "github.push_branch":
        branch_name = str(args.get("branch_name") or "").strip()
        push_ref = branch_name
        if canonical_run_branch:
            branch_name = canonical_run_branch
            push_ref = f"HEAD:{branch_name}"
        if not branch_name:
            branch_name = _run_git(context.repo_dir, ["rev-parse", "--abbrev-ref", "HEAD"]).strip()
        if not push_ref:
            push_ref = branch_name
        _run_git(
            context.repo_dir,
            ["push", "-u", "origin", push_ref],
            token=github_client.get_installation_token(),
        )
        return {"branch_name": branch_name}

    if tool_name == "github.open_pr":
        title = str(args.get("title") or f"{context.issue_key}: update").strip()
        head_branch = str(args.get("head_branch") or "").strip()
        base_branch = str(args.get("base_branch") or default_base_branch).strip()
        body = str(args.get("body") or "").strip()
        remediation_pr_number = _extract_remediation_pr_number_from_run(getattr(context, "run", None))
        if remediation_pr_number is not None:
            try:
                detail = github_client.get_pull_request_details(
                    repo_full_name=repo_full_name,
                    pr_number=remediation_pr_number,
                )
            except (GitHubApiError, ValueError):
                detail = None
            if detail is not None and str(detail.state or "").strip().lower() == "open":
                return {"pr_number": detail.number, "pr_url": detail.html_url}
        if canonical_run_branch:
            head_branch = canonical_run_branch
        elif not head_branch:
            head_branch = _run_git(context.repo_dir, ["rev-parse", "--abbrev-ref", "HEAD"]).strip()
        existing_pr = github_client.find_open_pull_request(
            repo_full_name=repo_full_name,
            head_branch=head_branch,
            base_branch=base_branch,
            limit=100,
        )
        if existing_pr is not None:
            return {"pr_number": existing_pr.number, "pr_url": existing_pr.html_url}
        result = github_client.create_pull_request(
            repo_full_name=repo_full_name,
            github_repository=github_repository,
            title=title,
            head_branch=head_branch,
            base_branch=base_branch,
            body=body,
        )
        return {"pr_number": result.number, "pr_url": result.html_url}

    if tool_name == "github.get_pr_details":
        pr_number = int(args.get("pr_number") or 0)
        if pr_number <= 0:
            raise ValueError("github.get_pr_details requires numeric 'pr_number'")
        detail = github_client.get_pull_request_details(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
        )
        return {
            "number": detail.number,
            "html_url": detail.html_url,
            "head_sha": detail.head_sha,
            "body": detail.body,
            "title": detail.title,
            "state": detail.state,
            "head_ref": detail.head_ref,
            "base_ref": detail.base_ref,
        }

    if tool_name == "github.list_pr_files":
        pr_number = int(args.get("pr_number") or 0)
        if pr_number <= 0:
            raise ValueError("github.list_pr_files requires numeric 'pr_number'")
        items = github_client.list_pull_request_files(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
        )
        return {
            "files": [{"filename": item.filename, "patch": item.patch} for item in items],
        }

    if tool_name == "github.list_pr_reviews":
        pr_number = int(args.get("pr_number") or 0)
        if pr_number <= 0:
            raise ValueError("github.list_pr_reviews requires numeric 'pr_number'")
        items = github_client.list_pull_request_reviews(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
        )
        return {
            "reviews": [
                {
                    "id": item.review_id,
                    "state": item.state,
                    "body": item.body,
                    "submitted_at": item.submitted_at,
                    "user_login": item.user_login,
                }
                for item in items
            ]
        }

    if tool_name == "github.list_pr_review_comments":
        pr_number = int(args.get("pr_number") or 0)
        if pr_number <= 0:
            raise ValueError("github.list_pr_review_comments requires numeric 'pr_number'")
        items = github_client.list_pull_request_review_comments(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
        )
        return {
            "comments": [
                {
                    "id": item.comment_id,
                    "body": item.body,
                    "path": item.path,
                    "line": item.line,
                    "state": item.state,
                    "user_login": item.user_login,
                }
                for item in items
            ]
        }

    if tool_name == "github.list_pr_issue_comments":
        pr_number = int(args.get("pr_number") or 0)
        if pr_number <= 0:
            raise ValueError("github.list_pr_issue_comments requires numeric 'pr_number'")
        items = github_client.list_pull_request_issue_comments(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
        )
        return {
            "comments": [
                {
                    "id": item.comment_id,
                    "body": item.body,
                    "created_at": item.created_at,
                    "user_login": item.user_login,
                }
                for item in items
            ]
        }

    if tool_name == "github.list_check_suites":
        ref = str(args.get("ref") or "").strip()
        if not ref:
            raise ValueError("github.list_check_suites requires non-empty 'ref'")
        items = github_client.list_check_suites(repo_full_name=repo_full_name, ref=ref)
        return {
            "checks": [
                {"name": item.name, "status": item.status, "conclusion": item.conclusion}
                for item in items
            ]
        }

    raise ValueError(f"Unsupported GitHub tool '{tool_name}'")


def _repo_full_name(repository_url: str) -> str:
    normalized = normalize_repo_identifier(repository_url)
    prefix = "github.com/"
    if not normalized.startswith(prefix):
        raise ValueError("Only GitHub repositories are supported")
    full_name = normalized[len(prefix) :].strip("/")
    if full_name.count("/") != 1:
        raise ValueError("Repository URL must be owner/repo")
    return full_name


def _git_auth_env(token: str) -> dict[str, str]:
    credential = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
    return {
        **os.environ,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {credential}",
    }


def _run_git(repo_dir: Path, args: list[str], *, token: str | None = None) -> str:
    env = _git_auth_env(token) if token else None
    process = subprocess.run(
        ["git", *args],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or process.stdout.strip() or f"git {' '.join(args)} failed")
    return process.stdout


def _resolve_remote_default_branch(repo_dir: Path, *, token: str) -> str:
    _run_git(repo_dir, ["fetch", "origin", "--prune"], token=token)
    symbolic_ref = _run_git(
        repo_dir,
        ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
    ).strip()
    if symbolic_ref.startswith("origin/"):
        branch_name = symbolic_ref[len("origin/") :].strip()
        if branch_name:
            return branch_name
    raise RuntimeError("Unable to resolve remote default branch from origin/HEAD")


def _normalize_branch_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _default_project_base_branch(*, project: Project) -> str:
    environment_raw = getattr(project, "environment", {})
    environment = environment_raw if isinstance(environment_raw, dict) else {}
    configured = environment.get("default_branch") if isinstance(environment, dict) else None
    return _normalize_branch_name(configured) or "main"


def _resolve_canonical_run_branch(*, session: Session, context: AgentToolContext) -> str | None:
    run = getattr(context, "run", None)
    if run is None:
        return None
    remediation_branch = _extract_remediation_head_ref_from_run(run)
    if remediation_branch:
        run.branch = remediation_branch
        flush_fn = getattr(session, "flush", None) if session is not None else None
        if callable(flush_fn):
            flush_fn()
        return remediation_branch
    existing = _normalize_branch_name(getattr(run, "branch", None))
    if existing:
        return existing
    issue_key = str(context.issue_key or "").strip()
    if not issue_key:
        return None
    branch_name = f"feature/{issue_key}"
    run.branch = branch_name
    flush_fn = getattr(session, "flush", None) if session is not None else None
    if callable(flush_fn):
        flush_fn()
    return branch_name


def _sync_local_base_branch_to_origin(repo_dir: Path, *, base_branch: str, token: str) -> None:
    normalized_base_branch = str(base_branch).strip()
    if not normalized_base_branch:
        raise ValueError("Base branch is required")
    _run_git(repo_dir, ["fetch", "origin", normalized_base_branch], token=token)
    _run_git(repo_dir, ["checkout", "-B", normalized_base_branch, f"origin/{normalized_base_branch}"])


def _extract_trigger_context_from_run(run: object):
    plan = getattr(run, "plan", None)
    return load_parsed_trigger_context_from_plan(plan)


def _is_pr_remediation_trigger_context(trigger_context: object) -> bool:
    return isinstance(trigger_context, GithubPrRemediationTriggerContext)


def _extract_remediation_head_ref_from_run(run: object) -> str | None:
    trigger_context = _extract_trigger_context_from_run(run)
    if not _is_pr_remediation_trigger_context(trigger_context):
        return None
    head_ref = str(trigger_context.head_ref or "").strip()
    return head_ref or None


def _extract_remediation_pr_number_from_run(run: object) -> int | None:
    trigger_context = _extract_trigger_context_from_run(run)
    if not _is_pr_remediation_trigger_context(trigger_context):
        return None
    return trigger_context.pr_number


def print_tool_event(*, stage: str, tool_name: str, args: dict[str, Any], outcome: str) -> None:  # noqa: ANN401
    safe_args = {key: ("[REDACTED]" if "token" in key.lower() or "secret" in key.lower() else value) for key, value in args.items()}
    print(
        json.dumps(
            {
                "event_kind": "agent_tool_call",
                "stage": stage,
                "tool": tool_name,
                "args": safe_args,
                "outcome": outcome,
            }
        )
    )
