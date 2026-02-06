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


def enforce_repo_allowlist(repo_url: str, allowlist: list[str]) -> None:
    allowed = {normalize_repo_identifier(item) for item in allowlist}
    normalized_repo = normalize_repo_identifier(repo_url)
    if normalized_repo not in allowed:
        raise PermissionError(f"Repo '{repo_url}' is not in tenant allowlist")
