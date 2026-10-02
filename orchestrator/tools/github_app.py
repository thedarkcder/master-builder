from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import jwt
from jwt.exceptions import InvalidKeyError

from orchestrator.tools.repo_allowlist import enforce_repo_match


class GitHubApiError(RuntimeError):
    pass


_PULL_REQUEST_STATUS_REACTIONS = frozenset({"+1", "confused", "eyes"})


@dataclass(frozen=True)
class GitHubAppConfig:
    app_id: str
    installation_id: str
    private_key_pem: str
    api_base_url: str = "https://api.github.com"


@dataclass(frozen=True)
class PullRequestResult:
    number: int
    html_url: str


@dataclass(frozen=True)
class PullRequestDetails:
    number: int
    html_url: str
    head_sha: str
    title: str
    state: str
    node_id: str | None = None
    head_ref: str | None = None
    base_ref: str | None = None
    base_sha: str | None = None
    body: str | None = None
    draft: bool = False
    mergeable: bool | None = None
    mergeable_state: str | None = None


@dataclass(frozen=True)
class WorkflowCheckSuite:
    name: str
    status: str
    conclusion: str | None


@dataclass(frozen=True)
class PullRequestFileChange:
    filename: str
    patch: str | None


@dataclass(frozen=True)
class PullRequestSummary:
    number: int
    title: str
    state: str
    html_url: str
    head_ref: str
    base_ref: str
    created_at: str | None = None
    updated_at: str | None = None
    closed_at: str | None = None
    merged_at: str | None = None


@dataclass(frozen=True)
class PullRequestReview:
    review_id: int
    state: str
    body: str | None
    submitted_at: str | None
    user_login: str | None


@dataclass(frozen=True)
class PullRequestReviewComment:
    comment_id: int
    body: str
    path: str | None
    line: int | None
    state: str | None
    created_at: str | None
    user_login: str | None


@dataclass(frozen=True)
class PullRequestIssueComment:
    comment_id: int
    body: str
    created_at: str | None
    user_login: str | None


@dataclass(frozen=True)
class CommentReactionResult:
    reaction_id: int | None
    content: str | None


@dataclass(frozen=True)
class ReactionSummary:
    reaction_id: int
    content: str | None
    user_login: str | None


@dataclass(frozen=True)
class PullRequestInlineCommentDraft:
    path: str
    line: int
    body: str


@dataclass(frozen=True)
class PullRequestReviewSubmissionResult:
    review_id: int | None
    state: str | None


@dataclass(frozen=True)
class PullRequestMergeResult:
    merged: bool
    message: str | None
    sha: str | None


@dataclass(frozen=True)
class CheckRunResult:
    check_run_id: int | None
    html_url: str | None


@dataclass(frozen=True)
class InstallationRepository:
    full_name: str
    html_url: str
    default_branch: str
    private: bool


@dataclass(frozen=True)
class GitHubBranch:
    name: str
    protected: bool = False
    head_sha: str | None = None


@dataclass
class _InstallationToken:
    token: str
    expires_at: datetime


def _parse_github_datetime(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_scoped_secret_ref(secret_ref: str) -> bool:
    return str(secret_ref).strip().startswith(("tenant/", "project/"))


def _resolve_github_secret_ref(
    secret_ref: str,
    *,
    tenant_secret_lookup: Callable[[str], str | None],
    platform_secret_lookup: Callable[[str], str | None],
) -> str | None:
    normalized_secret_ref = str(secret_ref).strip()
    if not normalized_secret_ref:
        return None
    if _is_scoped_secret_ref(normalized_secret_ref):
        return tenant_secret_lookup(normalized_secret_ref)
    return platform_secret_lookup(normalized_secret_ref)


def github_client_from_tenant_config(
    tenant_github_config: dict,
    *,
    tenant_secret_lookup: Callable[[str], str | None],
    platform_secret_lookup: Callable[[str], str | None],
) -> "GitHubAppClient":
    app_id_ref = str(tenant_github_config.get("app_id_ref") or "GITHUB_APP_ID")
    private_key_ref = str(
        tenant_github_config.get("private_key_ref") or "GITHUB_APP_PRIVATE_KEY"
    )
    installation_id = str(tenant_github_config.get("installation_id") or "")

    if not installation_id:
        raise ValueError("Missing github_app required config fields")

    app_id = _resolve_github_secret_ref(
        app_id_ref,
        tenant_secret_lookup=tenant_secret_lookup,
        platform_secret_lookup=platform_secret_lookup,
    )
    if not app_id:
        raise ValueError(f"Missing GitHub App ID secret for ref '{app_id_ref}'")

    private_key_pem = _resolve_github_secret_ref(
        private_key_ref,
        tenant_secret_lookup=tenant_secret_lookup,
        platform_secret_lookup=platform_secret_lookup,
    )
    if not private_key_pem:
        raise ValueError(
            f"Missing GitHub private key secret for ref '{private_key_ref}'"
        )

    return GitHubAppClient(
        GitHubAppConfig(
            app_id=app_id,
            installation_id=installation_id,
            private_key_pem=private_key_pem,
        )
    )


class GitHubAppClient:
    def __init__(self, config: GitHubAppConfig):
        self._config = config
        self._cached_installation_token: _InstallationToken | None = None
        self._cached_actor_login: str | None = None
        self._cached_app_bot_login: str | None = None

    def create_app_jwt(self) -> str:
        private_key_pem = self._normalize_private_key(self._config.private_key_pem)
        if private_key_pem.startswith(("gho_", "ghu_", "ghs_", "github_pat_")):
            raise ValueError(
                "Invalid GitHub App private key secret: received OAuth/PAT token, expected PEM private key"
            )
        if not private_key_pem.startswith(
            ("-----BEGIN PRIVATE KEY-----", "-----BEGIN RSA PRIVATE KEY-----")
        ):
            raise ValueError(
                "Invalid GitHub App private key secret: expected GitHub App PEM private key "
                "(-----BEGIN PRIVATE KEY----- or -----BEGIN RSA PRIVATE KEY-----)"
            )

        now = datetime.now(timezone.utc)
        payload = {
            "iat": int((now - timedelta(seconds=60)).timestamp()),
            "exp": int((now + timedelta(minutes=9)).timestamp()),
            "iss": self._config.app_id,
        }
        try:
            encoded = jwt.encode(payload, private_key_pem, algorithm="RS256")
        except InvalidKeyError as exc:
            raise ValueError(
                "Invalid GitHub App private key secret: expected PEM (-----BEGIN...-----) format"
            ) from exc
        return str(encoded)

    @staticmethod
    def _normalize_private_key(raw_value: str) -> str:
        value = raw_value.strip()
        # Accept env-style quoted strings.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1].strip()
        # Accept escaped newlines from forms/env files.
        value = value.replace("\\r\\n", "\n").replace("\\n", "\n")
        value = value.replace("\r\n", "\n")
        return value.strip()

    def _request_json(
        self,
        *,
        method: str,
        path: str,
        bearer_token: str,
        payload: dict | None = None,
    ) -> dict:
        url = f"{self._config.api_base_url.rstrip('/')}{path}"
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {bearer_token}",
            "User-Agent": "master-builder-orchestrator",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = Request(url=url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=30) as response:
                response_body = response.read().decode("utf-8")
        except HTTPError as exc:
            error_body = exc.read().decode("utf-8")
            raise GitHubApiError(
                f"GitHub API request failed ({exc.code}) for {method} {path}: {error_body}"
            ) from exc

        if not response_body:
            return {}
        return json.loads(response_body)

    def get_installation_token(self) -> str:
        now = datetime.now(timezone.utc)
        if self._cached_installation_token is not None:
            if self._cached_installation_token.expires_at - now > timedelta(seconds=60):
                return self._cached_installation_token.token

        app_jwt = self.create_app_jwt()
        response = self._request_json(
            method="POST",
            path=f"/app/installations/{self._config.installation_id}/access_tokens",
            bearer_token=app_jwt,
            payload={},
        )

        token = response.get("token")
        expires_at_raw = response.get("expires_at")
        if not isinstance(token, str) or not token:
            raise GitHubApiError(
                "GitHub installation token response did not include token"
            )
        if not isinstance(expires_at_raw, str) or not expires_at_raw:
            raise GitHubApiError(
                "GitHub installation token response did not include expires_at"
            )

        expires_at = _parse_github_datetime(expires_at_raw)
        self._cached_installation_token = _InstallationToken(
            token=token, expires_at=expires_at
        )
        return token

    def get_actor_login(self) -> str:
        if self._cached_actor_login is not None:
            return self._cached_actor_login
        installation_token = self.get_installation_token()
        response = self._request_json(
            method="GET",
            path="/user",
            bearer_token=installation_token,
        )
        login = response.get("login")
        if not isinstance(login, str) or not login.strip():
            raise GitHubApiError(
                "GitHub authenticated user response did not include login"
            )
        self._cached_actor_login = login.strip()
        return self._cached_actor_login

    def get_app_bot_login(self) -> str:
        if self._cached_app_bot_login is not None:
            return self._cached_app_bot_login
        app_jwt = self.create_app_jwt()
        response = self._request_json(
            method="GET",
            path="/app",
            bearer_token=app_jwt,
        )
        slug = response.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            raise GitHubApiError("GitHub app response did not include slug")
        self._cached_app_bot_login = f"{slug.strip()}[bot]"
        return self._cached_app_bot_login

    def create_pull_request(
        self,
        *,
        repo_full_name: str,
        github_repository: str,
        title: str,
        head_branch: str,
        base_branch: str,
        body: str,
        draft: bool = False,
    ) -> PullRequestResult:
        enforce_repo_match(f"https://github.com/{repo_full_name}", github_repository)
        installation_token = self.get_installation_token()
        payload = {
            "title": title,
            "head": head_branch,
            "base": base_branch,
            "body": body,
        }
        if draft:
            payload["draft"] = True
        response = self._request_json(
            method="POST",
            path=f"/repos/{repo_full_name}/pulls",
            bearer_token=installation_token,
            payload=payload,
        )
        number = response.get("number")
        html_url = response.get("html_url")
        if not isinstance(number, int):
            raise GitHubApiError("GitHub PR response did not include numeric PR number")
        if not isinstance(html_url, str) or not html_url:
            raise GitHubApiError("GitHub PR response did not include html_url")

        return PullRequestResult(number=number, html_url=html_url)

    def mark_pull_request_ready_for_review(
        self, *, repo_full_name: str, pr_number: int
    ) -> PullRequestDetails:
        details = self.get_pull_request_details(
            repo_full_name=repo_full_name, pr_number=pr_number
        )
        if not details.draft:
            return details
        if not details.node_id:
            raise GitHubApiError(
                "GitHub PR details response did not include node_id for ready-for-review mutation"
            )
        installation_token = self.get_installation_token()
        response = self._request_json(
            method="POST",
            path="/graphql",
            bearer_token=installation_token,
            payload={
                "query": (
                    "mutation($pullRequestId: ID!) { "
                    "markPullRequestReadyForReview(input: {pullRequestId: $pullRequestId}) { "
                    "pullRequest { number isDraft url } } }"
                ),
                "variables": {"pullRequestId": details.node_id},
            },
        )
        errors = response.get("errors")
        if errors:
            raise GitHubApiError(f"GitHub ready-for-review mutation failed: {errors}")
        refreshed = self.get_pull_request_details(
            repo_full_name=repo_full_name, pr_number=pr_number
        )
        if refreshed.draft:
            raise GitHubApiError(
                "GitHub ready-for-review mutation completed but PR remained draft"
            )
        return refreshed

    def update_pull_request(
        self,
        *,
        repo_full_name: str,
        github_repository: str,
        pr_number: int,
        title: str,
        base_branch: str,
        body: str,
    ) -> PullRequestResult:
        enforce_repo_match(f"https://github.com/{repo_full_name}", github_repository)
        installation_token = self.get_installation_token()
        response = self._request_json(
            method="PATCH",
            path=f"/repos/{repo_full_name}/pulls/{pr_number}",
            bearer_token=installation_token,
            payload={
                "title": title,
                "base": base_branch,
                "body": body,
            },
        )
        number = response.get("number")
        html_url = response.get("html_url")
        if not isinstance(number, int):
            raise GitHubApiError(
                "GitHub PR update response did not include numeric PR number"
            )
        if not isinstance(html_url, str) or not html_url:
            raise GitHubApiError("GitHub PR update response did not include html_url")
        return PullRequestResult(number=number, html_url=html_url)

    def get_pull_request_details(
        self, *, repo_full_name: str, pr_number: int
    ) -> PullRequestDetails:
        installation_token = self.get_installation_token()
        response = self._request_json(
            method="GET",
            path=f"/repos/{repo_full_name}/pulls/{pr_number}",
            bearer_token=installation_token,
        )

        number = response.get("number")
        html_url = response.get("html_url")
        head = response.get("head")
        base = response.get("base")
        head_sha = head.get("sha") if isinstance(head, dict) else None
        head_ref = head.get("ref") if isinstance(head, dict) else None
        base_ref = base.get("ref") if isinstance(base, dict) else None
        base_sha = base.get("sha") if isinstance(base, dict) else None
        title = response.get("title")
        state = response.get("state")
        node_id = response.get("node_id")
        draft = bool(response.get("draft"))
        mergeable = response.get("mergeable")
        if mergeable is not None and not isinstance(mergeable, bool):
            mergeable = None
        mergeable_state = response.get("mergeable_state")
        if mergeable_state is not None and not isinstance(mergeable_state, str):
            mergeable_state = None
        raw_body = response.get("body")
        body = (
            raw_body.strip() if isinstance(raw_body, str) and raw_body.strip() else None
        )

        if not isinstance(number, int):
            raise GitHubApiError(
                "GitHub PR details response did not include numeric PR number"
            )
        if not isinstance(html_url, str) or not html_url:
            raise GitHubApiError("GitHub PR details response did not include html_url")
        if not isinstance(head_sha, str) or not head_sha:
            raise GitHubApiError("GitHub PR details response did not include head SHA")
        if not isinstance(title, str) or not title.strip():
            raise GitHubApiError("GitHub PR details response did not include title")
        if not isinstance(state, str) or not state.strip():
            raise GitHubApiError("GitHub PR details response did not include state")

        return PullRequestDetails(
            number=number,
            html_url=html_url,
            head_sha=head_sha,
            title=title.strip(),
            state=state.strip(),
            node_id=node_id.strip()
            if isinstance(node_id, str) and node_id.strip()
            else None,
            head_ref=head_ref.strip()
            if isinstance(head_ref, str) and head_ref.strip()
            else None,
            base_ref=base_ref.strip()
            if isinstance(base_ref, str) and base_ref.strip()
            else None,
            base_sha=base_sha.strip()
            if isinstance(base_sha, str) and base_sha.strip()
            else None,
            body=body,
            draft=draft,
            mergeable=mergeable,
            mergeable_state=mergeable_state.strip()
            if isinstance(mergeable_state, str) and mergeable_state.strip()
            else None,
        )

    def get_branch_head_sha(self, *, repo_full_name: str, branch: str) -> str:
        installation_token = self.get_installation_token()
        normalized_branch = str(branch or "").strip()
        if not normalized_branch:
            raise GitHubApiError("GitHub branch head lookup requires a branch")
        response = self._request_json(
            method="GET",
            path=f"/repos/{repo_full_name}/branches/{quote(normalized_branch, safe='')}",
            bearer_token=installation_token,
        )
        if not isinstance(response, dict):
            raise GitHubApiError("GitHub branch response was not an object")
        commit = response.get("commit")
        sha = commit.get("sha") if isinstance(commit, dict) else None
        if not isinstance(sha, str) or not sha.strip():
            raise GitHubApiError("GitHub branch response did not include head SHA")
        return sha.strip()

    def create_check_run(
        self,
        *,
        repo_full_name: str,
        head_sha: str,
        name: str,
        status: str,
        conclusion: str | None = None,
        title: str | None = None,
        summary: str | None = None,
    ) -> CheckRunResult:
        installation_token = self.get_installation_token()
        normalized_name = str(name or "").strip()
        normalized_status = str(status or "").strip()
        if not normalized_name:
            raise GitHubApiError("GitHub check run requires a name")
        if not normalized_status:
            raise GitHubApiError("GitHub check run requires a status")
        payload: dict[str, object] = {
            "name": normalized_name,
            "head_sha": str(head_sha or "").strip(),
            "status": normalized_status,
        }
        normalized_conclusion = str(conclusion or "").strip() or None
        normalized_title = str(title or "").strip() or None
        normalized_summary = str(summary or "").strip() or None
        if normalized_conclusion is not None:
            payload["conclusion"] = normalized_conclusion
        if normalized_title or normalized_summary:
            payload["output"] = {
                "title": normalized_title or normalized_name,
                "summary": normalized_summary or normalized_name,
            }
        response = self._request_json(
            method="POST",
            path=f"/repos/{repo_full_name}/check-runs",
            bearer_token=installation_token,
            payload=payload,
        )
        check_run_id = response.get("id")
        html_url = response.get("html_url")
        return CheckRunResult(
            check_run_id=check_run_id if isinstance(check_run_id, int) else None,
            html_url=html_url.strip()
            if isinstance(html_url, str) and html_url.strip()
            else None,
        )

    def list_check_suites(
        self, *, repo_full_name: str, ref: str
    ) -> list[WorkflowCheckSuite]:
        installation_token = self.get_installation_token()
        response = self._request_json(
            method="GET",
            path=f"/repos/{repo_full_name}/commits/{ref}/check-suites?per_page=100",
            bearer_token=installation_token,
        )

        suites = response.get("check_suites")
        if not isinstance(suites, list):
            raise GitHubApiError(
                "GitHub check suite response did not include check_suites"
            )

        parsed: list[WorkflowCheckSuite] = []
        for suite in suites:
            if not isinstance(suite, dict):
                continue
            app = suite.get("app") if isinstance(suite.get("app"), dict) else {}
            app_slug = app.get("slug") if isinstance(app, dict) else None
            if app_slug != "github-actions":
                continue

            name = suite.get("name")
            status = suite.get("status")
            conclusion = suite.get("conclusion")
            if not isinstance(name, str) or not name:
                continue
            if not isinstance(status, str) or not status:
                continue
            if conclusion is not None and not isinstance(conclusion, str):
                conclusion = None

            parsed.append(
                WorkflowCheckSuite(name=name, status=status, conclusion=conclusion)
            )

        return parsed

    def list_pull_request_files(
        self, *, repo_full_name: str, pr_number: int
    ) -> list[PullRequestFileChange]:
        installation_token = self.get_installation_token()
        parsed: list[PullRequestFileChange] = []
        page = 1

        while True:
            response = self._request_json(
                method="GET",
                path=f"/repos/{repo_full_name}/pulls/{pr_number}/files?per_page=100&page={page}",
                bearer_token=installation_token,
            )

            if not isinstance(response, list):
                raise GitHubApiError(
                    "GitHub pull request files response was not a list"
                )

            for item in response:
                if not isinstance(item, dict):
                    continue
                filename = item.get("filename")
                if not isinstance(filename, str) or not filename:
                    continue
                patch = item.get("patch")
                if patch is not None and not isinstance(patch, str):
                    patch = None
                parsed.append(PullRequestFileChange(filename=filename, patch=patch))

            if len(response) < 100:
                break
            page += 1

        return parsed

    def get_file_text_at_ref(self, *, repo_full_name: str, path: str, ref: str) -> str:
        installation_token = self.get_installation_token()
        normalized_path = str(path or "").strip()
        normalized_ref = str(ref or "").strip()
        if not normalized_path:
            raise GitHubApiError("GitHub file lookup requires a path")
        if not normalized_ref:
            raise GitHubApiError("GitHub file lookup requires a ref")
        response = self._request_json(
            method="GET",
            path=(
                f"/repos/{repo_full_name}/contents/{quote(normalized_path, safe='/')}"
                f"?ref={quote(normalized_ref, safe='')}"
            ),
            bearer_token=installation_token,
        )
        if not isinstance(response, dict):
            raise GitHubApiError("GitHub file content response was not an object")
        encoding = str(response.get("encoding") or "").strip().lower()
        content = response.get("content")
        if encoding != "base64" or not isinstance(content, str):
            raise GitHubApiError(
                "GitHub file content response did not include base64 content"
            )
        try:
            decoded = base64.b64decode(content.encode("ascii"), validate=False)
        except Exception as exc:  # noqa: BLE001
            raise GitHubApiError(
                f"GitHub file content could not be decoded: {exc}"
            ) from exc
        return decoded.decode("utf-8")

    def list_pull_requests(
        self,
        *,
        repo_full_name: str,
        state: str = "open",
        limit: int = 20,
    ) -> list[PullRequestSummary]:
        installation_token = self.get_installation_token()
        normalized_state = str(state or "").strip().lower() or "open"
        if normalized_state not in {"open", "closed", "all"}:
            raise ValueError(f"Unsupported pull request state '{state}'")
        safe_limit = min(max(1, int(limit)), 100)
        response = self._request_json(
            method="GET",
            path=(
                f"/repos/{repo_full_name}/pulls"
                f"?state={quote(normalized_state, safe='')}&sort=updated&direction=desc&per_page={safe_limit}"
            ),
            bearer_token=installation_token,
        )
        if not isinstance(response, list):
            raise GitHubApiError("GitHub pull request list response was not a list")

        parsed: list[PullRequestSummary] = []
        for item in response:
            if not isinstance(item, dict):
                continue
            number = item.get("number")
            title = item.get("title")
            state = item.get("state")
            html_url = item.get("html_url")
            created_at = item.get("created_at")
            updated_at = item.get("updated_at")
            closed_at = item.get("closed_at")
            merged_at = item.get("merged_at")
            head = item.get("head")
            base = item.get("base")
            head_ref = head.get("ref") if isinstance(head, dict) else None
            base_ref = base.get("ref") if isinstance(base, dict) else None
            if not isinstance(number, int):
                continue
            if not isinstance(title, str) or not title.strip():
                continue
            if not isinstance(state, str) or not state.strip():
                continue
            if not isinstance(html_url, str) or not html_url.strip():
                continue
            if not isinstance(head_ref, str) or not head_ref.strip():
                continue
            if not isinstance(base_ref, str) or not base_ref.strip():
                continue
            parsed.append(
                PullRequestSummary(
                    number=number,
                    title=title.strip(),
                    state=state.strip(),
                    html_url=html_url.strip(),
                    head_ref=head_ref.strip(),
                    base_ref=base_ref.strip(),
                    created_at=created_at.strip()
                    if isinstance(created_at, str) and created_at.strip()
                    else None,
                    updated_at=updated_at.strip()
                    if isinstance(updated_at, str) and updated_at.strip()
                    else None,
                    closed_at=closed_at.strip()
                    if isinstance(closed_at, str) and closed_at.strip()
                    else None,
                    merged_at=merged_at.strip()
                    if isinstance(merged_at, str) and merged_at.strip()
                    else None,
                )
            )
        return parsed

    def list_open_pull_requests(
        self, *, repo_full_name: str, limit: int = 20
    ) -> list[PullRequestSummary]:
        return self.list_pull_requests(
            repo_full_name=repo_full_name, state="open", limit=limit
        )

    def find_open_pull_request(
        self,
        *,
        repo_full_name: str,
        head_branch: str,
        base_branch: str | None = None,
        limit: int = 100,
    ) -> PullRequestSummary | None:
        normalized_head = str(head_branch or "").strip()
        normalized_base = str(base_branch or "").strip()
        if not normalized_head:
            return None
        pull_requests = self.list_open_pull_requests(
            repo_full_name=repo_full_name, limit=limit
        )
        for pull_request in pull_requests:
            if pull_request.head_ref != normalized_head:
                continue
            if normalized_base and pull_request.base_ref != normalized_base:
                continue
            return pull_request
        return None

    def list_installation_repositories(self) -> list[InstallationRepository]:
        installation_token = self.get_installation_token()
        response = self._request_json(
            method="GET",
            path="/installation/repositories?per_page=100",
            bearer_token=installation_token,
        )

        repositories = response.get("repositories")
        if not isinstance(repositories, list):
            raise GitHubApiError(
                "GitHub installation repositories response did not include repositories"
            )

        parsed: list[InstallationRepository] = []
        for item in repositories:
            if not isinstance(item, dict):
                continue
            full_name = item.get("full_name")
            html_url = item.get("html_url")
            default_branch = item.get("default_branch")
            private = item.get("private")
            if not isinstance(full_name, str) or not full_name:
                continue
            if not isinstance(html_url, str) or not html_url:
                continue
            if not isinstance(default_branch, str) or not default_branch:
                default_branch = "main"
            if not isinstance(private, bool):
                private = False

            parsed.append(
                InstallationRepository(
                    full_name=full_name,
                    html_url=html_url,
                    default_branch=default_branch,
                    private=private,
                )
            )

        parsed.sort(key=lambda repo: repo.full_name.lower())
        return parsed

    def get_repository_default_branch(
        self, *, repo_full_name: str, github_repository: str
    ) -> str:
        enforce_repo_match(f"https://github.com/{repo_full_name}", github_repository)
        installation_token = self.get_installation_token()
        response = self._request_json(
            method="GET",
            path=f"/repos/{repo_full_name}",
            bearer_token=installation_token,
        )
        default_branch = (
            response.get("default_branch") if isinstance(response, dict) else None
        )
        if not isinstance(default_branch, str) or not default_branch.strip():
            raise GitHubApiError(
                "GitHub repository response did not include default_branch"
            )
        return default_branch.strip()

    def list_repository_branches(
        self,
        *,
        repo_full_name: str,
        github_repository: str,
        limit: int = 100,
    ) -> list[GitHubBranch]:
        enforce_repo_match(f"https://github.com/{repo_full_name}", github_repository)
        installation_token = self.get_installation_token()
        safe_limit = min(max(1, int(limit)), 100)
        response = self._request_json(
            method="GET",
            path=f"/repos/{repo_full_name}/branches?per_page={safe_limit}",
            bearer_token=installation_token,
        )
        if not isinstance(response, list):
            raise GitHubApiError("GitHub branch list response was not a list")

        parsed: list[GitHubBranch] = []
        for item in response:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            protected = item.get("protected")
            if not isinstance(name, str) or not name.strip():
                continue
            commit = item.get("commit")
            head_sha = commit.get("sha") if isinstance(commit, dict) else None
            parsed.append(
                GitHubBranch(
                    name=name.strip(),
                    protected=protected if isinstance(protected, bool) else False,
                    head_sha=head_sha.strip()
                    if isinstance(head_sha, str) and head_sha.strip()
                    else None,
                )
            )
        parsed.sort(key=lambda branch: branch.name.lower())
        return parsed

    def get_repository_branch_head_sha(
        self,
        *,
        repo_full_name: str,
        github_repository: str,
        branch: str,
    ) -> str:
        enforce_repo_match(f"https://github.com/{repo_full_name}", github_repository)
        normalized_branch = str(branch or "").strip()
        if not normalized_branch:
            raise GitHubApiError("GitHub branch is required")
        installation_token = self.get_installation_token()
        response = self._request_json(
            method="GET",
            path=f"/repos/{repo_full_name}/branches/{quote(normalized_branch, safe='')}",
            bearer_token=installation_token,
        )
        commit = response.get("commit") if isinstance(response, dict) else None
        head_sha = commit.get("sha") if isinstance(commit, dict) else None
        if not isinstance(head_sha, str) or not head_sha.strip():
            raise GitHubApiError("GitHub branch response did not include commit sha")
        return head_sha.strip()

    def list_pull_request_reviews(
        self, *, repo_full_name: str, pr_number: int
    ) -> list[PullRequestReview]:
        installation_token = self.get_installation_token()
        response = self._request_json(
            method="GET",
            path=f"/repos/{repo_full_name}/pulls/{pr_number}/reviews?per_page=100",
            bearer_token=installation_token,
        )
        if not isinstance(response, list):
            raise GitHubApiError("GitHub pull request reviews response was not a list")

        parsed: list[PullRequestReview] = []
        for item in response:
            if not isinstance(item, dict):
                continue
            review_id = item.get("id")
            state = item.get("state")
            if (
                not isinstance(review_id, int)
                or not isinstance(state, str)
                or not state.strip()
            ):
                continue
            body = item.get("body")
            submitted_at = item.get("submitted_at")
            user = item.get("user")
            user_login = user.get("login") if isinstance(user, dict) else None
            parsed.append(
                PullRequestReview(
                    review_id=review_id,
                    state=state.strip(),
                    body=body.strip()
                    if isinstance(body, str) and body.strip()
                    else None,
                    submitted_at=submitted_at.strip()
                    if isinstance(submitted_at, str) and submitted_at.strip()
                    else None,
                    user_login=user_login.strip()
                    if isinstance(user_login, str) and user_login.strip()
                    else None,
                )
            )
        return parsed

    def list_pull_request_review_comments(
        self,
        *,
        repo_full_name: str,
        pr_number: int,
    ) -> list[PullRequestReviewComment]:
        installation_token = self.get_installation_token()
        response = self._request_json(
            method="GET",
            path=f"/repos/{repo_full_name}/pulls/{pr_number}/comments?per_page=100",
            bearer_token=installation_token,
        )
        if not isinstance(response, list):
            raise GitHubApiError(
                "GitHub pull request review comments response was not a list"
            )

        parsed: list[PullRequestReviewComment] = []
        for item in response:
            comment = self._parse_pull_request_review_comment(item)
            if comment is not None:
                parsed.append(comment)
        return parsed

    def create_pull_request_review_comment_reply(
        self,
        *,
        repo_full_name: str,
        pr_number: int,
        in_reply_to: int,
        body: str,
    ) -> PullRequestReviewComment:
        installation_token = self.get_installation_token()
        response = self._request_json(
            method="POST",
            path=f"/repos/{repo_full_name}/pulls/{pr_number}/comments",
            bearer_token=installation_token,
            payload={"body": body, "in_reply_to": in_reply_to},
        )
        comment = self._parse_pull_request_review_comment(response)
        if comment is None:
            raise GitHubApiError(
                "GitHub create pull request review comment reply response was not valid"
            )
        return comment

    def list_pull_request_issue_comments(
        self,
        *,
        repo_full_name: str,
        pr_number: int,
    ) -> list[PullRequestIssueComment]:
        installation_token = self.get_installation_token()
        response = self._request_json(
            method="GET",
            path=f"/repos/{repo_full_name}/issues/{pr_number}/comments?per_page=100",
            bearer_token=installation_token,
        )
        if not isinstance(response, list):
            raise GitHubApiError(
                "GitHub pull request issue comments response was not a list"
            )

        parsed: list[PullRequestIssueComment] = []
        for item in response:
            if not isinstance(item, dict):
                continue
            comment = self._parse_issue_comment(item)
            if comment is not None:
                parsed.append(comment)
        return parsed

    def create_pull_request_issue_comment(
        self,
        *,
        repo_full_name: str,
        pr_number: int,
        body: str,
    ) -> PullRequestIssueComment:
        installation_token = self.get_installation_token()
        response = self._request_json(
            method="POST",
            path=f"/repos/{repo_full_name}/issues/{pr_number}/comments",
            bearer_token=installation_token,
            payload={"body": body},
        )
        comment = self._parse_issue_comment(response)
        if comment is None:
            raise GitHubApiError("GitHub create issue comment response was not valid")
        return comment

    def update_issue_comment(
        self,
        *,
        repo_full_name: str,
        comment_id: int,
        body: str,
    ) -> PullRequestIssueComment:
        installation_token = self.get_installation_token()
        response = self._request_json(
            method="PATCH",
            path=f"/repos/{repo_full_name}/issues/comments/{comment_id}",
            bearer_token=installation_token,
            payload={"body": body},
        )
        comment = self._parse_issue_comment(response)
        if comment is None:
            raise GitHubApiError("GitHub update issue comment response was not valid")
        return comment

    def delete_issue_comment(
        self,
        *,
        repo_full_name: str,
        comment_id: int,
    ) -> None:
        installation_token = self.get_installation_token()
        self._request_json(
            method="DELETE",
            path=f"/repos/{repo_full_name}/issues/comments/{comment_id}",
            bearer_token=installation_token,
            accepted_statuses=(204,),
        )

    def update_pull_request_review_comment(
        self,
        *,
        repo_full_name: str,
        comment_id: int,
        body: str,
    ) -> PullRequestReviewComment:
        installation_token = self.get_installation_token()
        response = self._request_json(
            method="PATCH",
            path=f"/repos/{repo_full_name}/pulls/comments/{comment_id}",
            bearer_token=installation_token,
            payload={"body": body},
        )
        comment = self._parse_pull_request_review_comment(response)
        if comment is None:
            raise GitHubApiError(
                "GitHub update pull request review comment response was not valid"
            )
        return comment

    def add_issue_comment_reaction(
        self,
        *,
        repo_full_name: str,
        comment_id: int,
        content: str = "eyes",
    ) -> CommentReactionResult:
        installation_token = self.get_installation_token()
        response = self._request_json(
            method="POST",
            path=f"/repos/{repo_full_name}/issues/comments/{comment_id}/reactions",
            bearer_token=installation_token,
            payload={"content": content},
        )
        reaction_id = response.get("id")
        reaction_content = response.get("content")
        return CommentReactionResult(
            reaction_id=reaction_id if isinstance(reaction_id, int) else None,
            content=reaction_content if isinstance(reaction_content, str) else None,
        )

    def add_pull_request_review_comment_reaction(
        self,
        *,
        repo_full_name: str,
        comment_id: int,
        content: str = "eyes",
    ) -> CommentReactionResult:
        installation_token = self.get_installation_token()
        response = self._request_json(
            method="POST",
            path=f"/repos/{repo_full_name}/pulls/comments/{comment_id}/reactions",
            bearer_token=installation_token,
            payload={"content": content},
        )
        reaction_id = response.get("id")
        reaction_content = response.get("content")
        return CommentReactionResult(
            reaction_id=reaction_id if isinstance(reaction_id, int) else None,
            content=reaction_content if isinstance(reaction_content, str) else None,
        )

    def add_pull_request_reaction(
        self,
        *,
        repo_full_name: str,
        pr_number: int,
        content: str = "eyes",
    ) -> CommentReactionResult:
        installation_token = self.get_installation_token()
        response = self._request_json(
            method="POST",
            path=f"/repos/{repo_full_name}/issues/{pr_number}/reactions",
            bearer_token=installation_token,
            payload={"content": content},
        )
        reaction_id = response.get("id")
        reaction_content = response.get("content")
        return CommentReactionResult(
            reaction_id=reaction_id if isinstance(reaction_id, int) else None,
            content=reaction_content if isinstance(reaction_content, str) else None,
        )

    def list_pull_request_reactions(
        self,
        *,
        repo_full_name: str,
        pr_number: int,
    ) -> list[ReactionSummary]:
        installation_token = self.get_installation_token()
        response = self._request_json(
            method="GET",
            path=f"/repos/{repo_full_name}/issues/{pr_number}/reactions?per_page=100",
            bearer_token=installation_token,
        )
        if not isinstance(response, list):
            raise GitHubApiError(
                "GitHub pull request reactions response was not a list"
            )
        reactions: list[ReactionSummary] = []
        for item in response:
            if not isinstance(item, dict):
                continue
            reaction_id = item.get("id")
            if not isinstance(reaction_id, int) or reaction_id <= 0:
                continue
            content = item.get("content")
            if content is not None and not isinstance(content, str):
                content = None
            user = item.get("user")
            login = user.get("login") if isinstance(user, dict) else None
            reactions.append(
                ReactionSummary(
                    reaction_id=reaction_id,
                    content=content.strip()
                    if isinstance(content, str) and content.strip()
                    else None,
                    user_login=login.strip()
                    if isinstance(login, str) and login.strip()
                    else None,
                )
            )
        return reactions

    def delete_issue_reaction(
        self,
        *,
        repo_full_name: str,
        reaction_id: int,
    ) -> None:
        installation_token = self.get_installation_token()
        self._request_json(
            method="DELETE",
            path=f"/repos/{repo_full_name}/issues/reactions/{reaction_id}",
            bearer_token=installation_token,
        )

    def sync_pull_request_reaction(
        self,
        *,
        repo_full_name: str,
        pr_number: int,
        content: str,
    ) -> CommentReactionResult:
        app_bot_login = self.get_app_bot_login()
        for reaction in self.list_pull_request_reactions(
            repo_full_name=repo_full_name, pr_number=pr_number
        ):
            if (
                reaction.user_login == app_bot_login
                and reaction.content in _PULL_REQUEST_STATUS_REACTIONS
            ):
                self.delete_issue_reaction(
                    repo_full_name=repo_full_name, reaction_id=reaction.reaction_id
                )
        return self.add_pull_request_reaction(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            content=content,
        )

    def submit_pull_request_review(
        self,
        *,
        repo_full_name: str,
        pr_number: int,
        commit_id: str,
        body: str,
        comments: list[PullRequestInlineCommentDraft],
        event: str = "COMMENT",
    ) -> PullRequestReviewSubmissionResult:
        installation_token = self.get_installation_token()
        payload = {
            "commit_id": commit_id,
            "body": body,
            "event": event,
            "comments": [
                {
                    "path": comment.path,
                    "line": comment.line,
                    "body": comment.body,
                }
                for comment in comments
            ],
        }
        response = self._request_json(
            method="POST",
            path=f"/repos/{repo_full_name}/pulls/{pr_number}/reviews",
            bearer_token=installation_token,
            payload=payload,
        )
        review_id = response.get("id")
        state = response.get("state")
        return PullRequestReviewSubmissionResult(
            review_id=review_id if isinstance(review_id, int) else None,
            state=state.strip() if isinstance(state, str) and state.strip() else None,
        )

    def merge_pull_request(
        self,
        *,
        repo_full_name: str,
        pr_number: int,
        head_sha: str,
        merge_method: str = "squash",
    ) -> PullRequestMergeResult:
        installation_token = self.get_installation_token()
        response = self._request_json(
            method="PUT",
            path=f"/repos/{repo_full_name}/pulls/{pr_number}/merge",
            bearer_token=installation_token,
            payload={"sha": head_sha, "merge_method": merge_method},
        )
        merged = bool(response.get("merged"))
        message = response.get("message")
        sha = response.get("sha")
        return PullRequestMergeResult(
            merged=merged,
            message=message.strip()
            if isinstance(message, str) and message.strip()
            else None,
            sha=sha.strip() if isinstance(sha, str) and sha.strip() else None,
        )

    def _parse_issue_comment(self, item: object) -> PullRequestIssueComment | None:
        if not isinstance(item, dict):
            return None
        comment_id = item.get("id")
        body = item.get("body")
        if (
            not isinstance(comment_id, int)
            or not isinstance(body, str)
            or not body.strip()
        ):
            return None
        user = item.get("user")
        user_login = user.get("login") if isinstance(user, dict) else None
        created_at = item.get("created_at")
        return PullRequestIssueComment(
            comment_id=comment_id,
            body=body.strip(),
            created_at=created_at.strip()
            if isinstance(created_at, str) and created_at.strip()
            else None,
            user_login=user_login.strip()
            if isinstance(user_login, str) and user_login.strip()
            else None,
        )

    def _parse_pull_request_review_comment(
        self, item: object
    ) -> PullRequestReviewComment | None:
        if not isinstance(item, dict):
            return None
        comment_id = item.get("id")
        body = item.get("body")
        if (
            not isinstance(comment_id, int)
            or not isinstance(body, str)
            or not body.strip()
        ):
            return None
        user = item.get("user")
        user_login = user.get("login") if isinstance(user, dict) else None
        line = item.get("line")
        return PullRequestReviewComment(
            comment_id=comment_id,
            body=body.strip(),
            path=item.get("path") if isinstance(item.get("path"), str) else None,
            line=line if isinstance(line, int) else None,
            state=item.get("state") if isinstance(item.get("state"), str) else None,
            created_at=created_at.strip()
            if isinstance((created_at := item.get("created_at")), str)
            and created_at.strip()
            else None,
            user_login=user_login.strip()
            if isinstance(user_login, str) and user_login.strip()
            else None,
        )
