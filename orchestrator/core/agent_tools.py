from __future__ import annotations

import base64
import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session
from sqlalchemy import select

from orchestrator.api.jira_oauth.service import jira_oauth_client, refresh_jira_connection_tokens
from orchestrator.core.discord.notifications import send_tenant_discord_message
from orchestrator.core.knowledge_exact_read import ExactReadRequest, exact_read_knowledge_source
from orchestrator.core.knowledge_base import build_knowledge_prompt_context
from orchestrator.core.platform_secret_service import resolve_platform_secret_ref
from orchestrator.core.project_policy import resolve_effective_policy
from orchestrator.core.tenant_secret_service import resolve_scoped_secret_ref
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
from orchestrator.tools.github_app import github_client_from_tenant_config
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
        "project.get_runtime_values",
        "project.list_runtime_keys",
        "project.request_runtime_values",
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
        "project.get_runtime_values",
        "project.list_runtime_keys",
        "project.request_runtime_values",
    },
    "test": {
        "knowledge.exact_read",
        "jira.comment",
        "repo.read",
        "project.get_runtime_values",
        "project.list_runtime_keys",
        "project.request_runtime_values",
    },
    "review": {
        "knowledge.exact_read",
        "jira.comment",
        "jira.transition",
        "github.open_pr",
        "repo.read",
        "project.get_runtime_values",
        "project.list_runtime_keys",
        "project.request_runtime_values",
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
        "project.get_runtime_values",
        "project.list_runtime_keys",
        "project.request_runtime_values",
    },
    "decision_planner": {
        "jira.get_issue",
        "repo.read",
        "decision.read_state",
        "knowledge.exact_read",
        "knowledge.read",
        "project.get_runtime_values",
        "project.list_runtime_keys",
        "project.request_runtime_values",
    },
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


def _ensure_repo_checkout_exists(repo_dir: Path) -> None:
    if not (repo_dir / ".git").exists():
        raise ValueError(f"Repository checkout missing at {repo_dir}")


def allowed_tools_for_stage(stage: str) -> set[str]:
    return set(TOOL_ALLOWLIST.get(str(stage or "").strip().lower(), set()))


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

    project: Project | None = None
    if project_id:
        project = session.get(Project, project_id)
    if project is None and run_id:
        run = session.get(Run, run_id)
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
        repo_dir = project_run_repo_dir(
            base_dir=settings.project_repo_checkout_base_dir,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
            run_id=run_id,
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
    jira_config = context.tenant.jira_config or {}
    connection_id = str(jira_config.get("connection_id") or "").strip()
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
    if tool_name == "project.list_runtime_keys":
        environment_map = context.project.environment if isinstance(context.project.environment, dict) else {}
        secret_ref_map = context.project.secret_refs if isinstance(context.project.secret_refs, dict) else {}
        environment_keys = sorted({str(key).strip() for key in environment_map.keys() if str(key).strip()})
        secret_ref_keys = sorted({str(key).strip() for key in secret_ref_map.keys() if str(key).strip()})
        all_keys = sorted(set(environment_keys) | set(secret_ref_keys))
        return {
            "keys": all_keys,
            "environment_keys": environment_keys,
            "secret_ref_keys": secret_ref_keys,
        }

    if tool_name == "project.request_runtime_values":
        raw_keys = args.get("keys")
        if isinstance(raw_keys, str):
            requested_keys = [raw_keys]
        elif isinstance(raw_keys, list):
            requested_keys = [str(item or "").strip() for item in raw_keys]
        else:
            requested_keys = []
        normalized_keys = [key for key in requested_keys if key]
        if not normalized_keys:
            raise ValueError("project.request_runtime_values requires non-empty 'keys'")
        reason = str(args.get("reason") or "").strip()
        message_lines = [
            f"Runtime value request for {context.issue_key or 'unknown issue'}",
            f"- Tenant: {context.tenant.tenant_id}",
            f"- Project: {context.project.project_id}",
            f"- Stage: {context.stage}",
            f"- Run: {context.run_id or 'n/a'}",
            f"- Keys: {', '.join(normalized_keys)}",
        ]
        if reason:
            message_lines.append(f"- Reason: {reason}")
        send_result = send_tenant_discord_message(
            session=session,
            tenant=context.tenant,
            project=context.project,
            message="\n".join(message_lines),
            settings=settings,
            event=None,
        )
        return {
            "requested_keys": normalized_keys,
            "reason": reason or None,
            "notification_sent": bool(send_result.sent),
            "notification_reason": send_result.reason,
            "channel_id": send_result.channel_id,
        }

    if tool_name != "project.get_runtime_values":
        raise ValueError(f"Unsupported Project tool '{tool_name}'")

    raw_keys = args.get("keys")
    if isinstance(raw_keys, str):
        requested_keys = [raw_keys]
    elif isinstance(raw_keys, list):
        requested_keys = [str(item or "").strip() for item in raw_keys]
    else:
        requested_keys = []
    normalized_keys = [key for key in requested_keys if key]
    if not normalized_keys:
        raise ValueError("project.get_runtime_values requires non-empty 'keys'")

    environment_map = context.project.environment if isinstance(context.project.environment, dict) else {}
    secret_ref_map = context.project.secret_refs if isinstance(context.project.secret_refs, dict) else {}
    encryption_key = str(getattr(settings, "secrets_encryption_key", "") or "").strip()

    values: dict[str, dict[str, Any]] = {}
    for key in normalized_keys:
        if key in environment_map:
            values[key] = {
                "source": "environment",
                "value": str(environment_map.get(key) or ""),
                "secret_ref": None,
            }
            continue
        secret_ref = str(secret_ref_map.get(key) or "").strip()
        if not secret_ref:
            values[key] = {"source": "missing", "value": None, "secret_ref": None}
            continue
        try:
            if secret_ref.startswith("platform/"):
                resolved_value = resolve_platform_secret_ref(
                    session,
                    secret_ref=secret_ref,
                    encryption_key=encryption_key,
                )
            else:
                resolved_value = resolve_scoped_secret_ref(
                    session,
                    secret_ref=secret_ref,
                    tenant_id=context.tenant.tenant_id,
                    project_id=context.project.project_id,
                    encryption_key=encryption_key,
                )
        except ValueError as exc:
            raise ValueError(f"Invalid secret ref for {key}: {exc}") from exc
        values[key] = {
            "source": "secret_ref" if resolved_value else "missing",
            "value": resolved_value,
            "secret_ref": secret_ref,
        }

    return {"values": values}


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

    if tool_name == "github.create_branch":
        summary = str(args.get("summary") or context.issue_key).strip()
        branch_name = str(args.get("branch_name") or build_branch_name(context.issue_key, summary)).strip()
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
        if not branch_name:
            branch_name = _run_git(context.repo_dir, ["rev-parse", "--abbrev-ref", "HEAD"]).strip()
        _run_git(
            context.repo_dir,
            ["push", "-u", "origin", branch_name],
            token=github_client.get_installation_token(),
        )
        return {"branch_name": branch_name}

    if tool_name == "github.open_pr":
        title = str(args.get("title") or f"{context.issue_key}: update").strip()
        head_branch = str(args.get("head_branch") or "").strip()
        base_branch = str(args.get("base_branch") or "main").strip()
        body = str(args.get("body") or "").strip()
        if not head_branch:
            head_branch = _run_git(context.repo_dir, ["rev-parse", "--abbrev-ref", "HEAD"]).strip()
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


def _sync_local_base_branch_to_origin(repo_dir: Path, *, base_branch: str, token: str) -> None:
    normalized_base_branch = str(base_branch).strip()
    if not normalized_base_branch:
        raise ValueError("Base branch is required")
    _run_git(repo_dir, ["fetch", "origin", normalized_base_branch], token=token)
    _run_git(repo_dir, ["checkout", "-B", normalized_base_branch, f"origin/{normalized_base_branch}"])


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
