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
    enforce_repo_match,
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
from orchestrator.tools.atlassian_oauth import (
    AtlassianOAuthClient,
    AtlassianOAuthClientConfig,
    AtlassianOAuthError,
    AtlassianOAuthResource,
    AtlassianOAuthTokenSet,
    JiraProject,
)
from orchestrator.tools.discord_api import DiscordApiClient, DiscordApiError, DiscordTextChannel

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
    "enforce_repo_match",
    "normalize_repo_identifier",
    "GitHubApiError",
    "GitHubAppClient",
    "GitHubAppConfig",
    "InstallationRepository",
    "PullRequestDetails",
    "PullRequestResult",
    "WorkflowCheckSuite",
    "github_client_from_tenant_config",
    "AtlassianOAuthClient",
    "AtlassianOAuthClientConfig",
    "AtlassianOAuthError",
    "AtlassianOAuthResource",
    "AtlassianOAuthTokenSet",
    "JiraProject",
    "DiscordApiClient",
    "DiscordApiError",
    "DiscordTextChannel",
]
