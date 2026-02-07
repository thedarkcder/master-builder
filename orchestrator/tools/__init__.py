"""Tool adapters package."""

from orchestrator.tools.bootstrap import (
    CodexBootstrapResult,
    RepoBootstrapStateResult,
    WorkflowBootstrapResult,
    bootstrap_ci_workflows,
    bootstrap_codex_assets,
    ensure_codex_bootstrap_state,
    list_repo_bootstrap_states,
    load_codex_preflight_context,
)
from orchestrator.tools.git_ops import (
    GitOperationError,
    GitWorkspaceManager,
    build_branch_name,
    enforce_repo_allowlist,
)
from orchestrator.tools.repo_allowlist import normalize_repo_identifier
from orchestrator.tools.github_app import (
    GitHubApiError,
    GitHubAppClient,
    GitHubAppConfig,
    InstallationRepository,
    PullRequestDetails,
    PullRequestResult,
    WorkflowCheckSuite,
    github_client_from_tenant_config,
)
from orchestrator.tools.jira_oauth import (
    JiraOAuthClient,
    JiraOAuthClientConfig,
    JiraOAuthError,
    JiraOAuthResource,
    JiraOAuthTokenSet,
    JiraProject,
)

__all__ = [
    "WorkflowBootstrapResult",
    "CodexBootstrapResult",
    "RepoBootstrapStateResult",
    "bootstrap_ci_workflows",
    "bootstrap_codex_assets",
    "ensure_codex_bootstrap_state",
    "list_repo_bootstrap_states",
    "load_codex_preflight_context",
    "GitOperationError",
    "GitWorkspaceManager",
    "build_branch_name",
    "enforce_repo_allowlist",
    "normalize_repo_identifier",
    "GitHubApiError",
    "GitHubAppClient",
    "GitHubAppConfig",
    "InstallationRepository",
    "PullRequestDetails",
    "PullRequestResult",
    "WorkflowCheckSuite",
    "github_client_from_tenant_config",
    "JiraOAuthClient",
    "JiraOAuthClientConfig",
    "JiraOAuthError",
    "JiraOAuthResource",
    "JiraOAuthTokenSet",
    "JiraProject",
]
