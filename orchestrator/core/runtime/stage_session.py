from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from orchestrator.core.runtime.tools import (
    execute_agent_tool,
    governed_allowed_tools_for_stage,
    governed_tool_catalog_for_stage,
    native_tool_catalog_for_stage,
)
from orchestrator.core.runtime.invocation import (
    AgentInvocationContext,
    invoke_runtime_json,
    invoke_runtime_json_with_tools,
)


AgentToolExecutor = Callable[[str, dict[str, object]], dict[str, object]]


@dataclass(frozen=True)
class RuntimeStageTooling:
    policy_stage: str
    runtime_command: str | None
    worker_platform: str | None
    governed_tools: frozenset[str]
    governed_catalog: tuple[dict[str, object], ...]
    native_catalog: tuple[dict[str, object], ...]

    def governed_native_prompt_context(
        self,
        *,
        governed_key: str = "governed_tools_json",
        native_key: str = "native_tools_json",
    ) -> dict[str, str]:
        return {
            governed_key: json.dumps(list(self.governed_catalog), ensure_ascii=False, sort_keys=True),
            native_key: json.dumps(list(self.native_catalog), ensure_ascii=False, sort_keys=True),
        }

    def governed_prompt_context(
        self,
        *,
        allowed_key: str = "allowed_tools_json",
        catalog_key: str = "tool_catalog_json",
    ) -> dict[str, str]:
        return {
            allowed_key: json.dumps(sorted(self.governed_tools), ensure_ascii=False),
            catalog_key: json.dumps(list(self.governed_catalog), ensure_ascii=False, sort_keys=True),
        }


def build_runtime_stage_tooling(
    *,
    policy_stage: str,
    runtime_command: str | None,
    worker_platform: str | None = None,
) -> RuntimeStageTooling:
    return RuntimeStageTooling(
        policy_stage=policy_stage,
        runtime_command=runtime_command,
        worker_platform=worker_platform,
        governed_tools=frozenset(
            governed_allowed_tools_for_stage(
                policy_stage,
                runtime_command=runtime_command,
                worker_platform=worker_platform,
            )
        ),
        governed_catalog=tuple(
            governed_tool_catalog_for_stage(
                policy_stage,
                runtime_command=runtime_command,
                worker_platform=worker_platform,
            )
        ),
        native_catalog=tuple(
            native_tool_catalog_for_stage(
                policy_stage,
                runtime_command=runtime_command,
                worker_platform=worker_platform,
            )
        ),
    )


def build_governed_tool_executor(
    *,
    session: Session,
    settings: Any,
    context: AgentInvocationContext,
    policy_stage: str,
    issue_key: str | None = None,
) -> AgentToolExecutor:
    tenant_id = str(context.tenant_id or "").strip()
    if not tenant_id:
        raise RuntimeError("tenant_id is required for governed tool execution")

    resolved_issue_key = str(issue_key or context.issue_key or "").strip()

    def _run(tool_name: str, tool_args: dict[str, object]) -> dict[str, object]:
        raw = execute_agent_tool(
            session=session,
            settings=settings,
            tenant_id=tenant_id,
            project_id=str(context.project_id or "").strip() or None,
            run_id=str(context.run_id or "").strip() or None,
            issue_key=resolved_issue_key,
            stage=policy_stage,
            tool_name=tool_name,
            tool_args=dict(tool_args),
            worker_platform=context.worker_platform,
        )
        return dict(raw)

    return _run


@dataclass(frozen=True)
class RuntimeStageSession:
    runtime: object
    context: AgentInvocationContext
    tooling: RuntimeStageTooling
    execute_tool: AgentToolExecutor | None = None

    @classmethod
    def create(
        cls,
        *,
        runtime: object,
        context: AgentInvocationContext,
        policy_stage: str,
        session: Session | None = None,
        settings: Any | None = None,
        issue_key: str | None = None,
        execute_tool: AgentToolExecutor | None = None,
    ) -> RuntimeStageSession:
        runtime_command = str(getattr(runtime, "command", "") or "")
        tool_executor = execute_tool
        if tool_executor is None and session is not None and settings is not None:
            tool_executor = build_governed_tool_executor(
                session=session,
                settings=settings,
                context=context,
                policy_stage=policy_stage,
                issue_key=issue_key,
            )
        return cls(
            runtime=runtime,
            context=context,
            tooling=build_runtime_stage_tooling(
                policy_stage=policy_stage,
                runtime_command=runtime_command,
                worker_platform=context.worker_platform,
            ),
            execute_tool=tool_executor,
        )

    def invoke_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tool_hops: int = 8,
        extra_on_log_line: Callable[[str, str], None] | None = None,
    ) -> dict[str, Any]:
        if self.execute_tool is None or not self.tooling.governed_tools:
            return invoke_runtime_json(
                runtime=self.runtime,
                context=self.context,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                allowed_native_tools={
                    str(item.get("tool_name") or "").strip()
                    for item in self.tooling.native_catalog
                    if str(item.get("tool_name") or "").strip()
                },
            )
        return invoke_runtime_json_with_tools(
            runtime=self.runtime,
            context=self.context,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            allowed_tools=set(self.tooling.governed_tools),
            execute_tool=self.execute_tool,
            max_tool_hops=max_tool_hops,
            extra_on_log_line=extra_on_log_line,
            allowed_native_tools={
                str(item.get("tool_name") or "").strip()
                for item in self.tooling.native_catalog
                if str(item.get("tool_name") or "").strip()
            },
        )
