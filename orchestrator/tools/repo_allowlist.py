from __future__ import annotations

from urllib.parse import urlparse


def normalize_repo_identifier(repo_url: str) -> str:
    trimmed = repo_url.strip()
    if trimmed.startswith("git@"):
        host_and_path = trimmed.split("@", maxsplit=1)[1]
        host, path = host_and_path.split(":", maxsplit=1)
        clean_path = path.removesuffix(".git").strip("/")
        return f"{host.lower()}/{clean_path.lower()}"

    parsed = urlparse(trimmed)
    host = parsed.netloc.lower()
    path = parsed.path.removesuffix(".git").strip("/")
    if host and path:
        return f"{host}/{path.lower()}"
    return trimmed.lower().removesuffix(".git").rstrip("/")


def enforce_repo_match(repo_url: str, github_repository: str) -> None:
    normalized_repo = normalize_repo_identifier(repo_url)
    normalized_target = normalize_repo_identifier(github_repository)
    if normalized_repo != normalized_target:
        raise PermissionError(f"Repo '{repo_url}' does not match tenant github_repository")
