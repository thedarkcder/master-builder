from __future__ import annotations


def normalize_browse_base_url(raw_base_url: str | None) -> str:
    return str(raw_base_url or "").strip().rstrip("/")


def build_jira_issue_url(*, issue_key: str, browse_base_url: str | None) -> str | None:
    normalized_base = normalize_browse_base_url(browse_base_url)
    if not normalized_base:
        return None
    return f"{normalized_base}/browse/{issue_key}"


def format_issue_markdown_link(*, issue_key: str, browse_base_url: str | None) -> str:
    issue_url = build_jira_issue_url(issue_key=issue_key, browse_base_url=browse_base_url)
    if not issue_url:
        return issue_key
    return f"[{issue_key}]({issue_url})"


def format_issue_markdown_list(*, issue_keys: list[str], browse_base_url: str | None) -> str:
    if not issue_keys:
        return "none"
    return ", ".join(format_issue_markdown_link(issue_key=issue_key, browse_base_url=browse_base_url) for issue_key in issue_keys)


def build_issue_url_list(*, issue_keys: list[str], browse_base_url: str | None) -> list[str]:
    issue_urls: list[str] = []
    for issue_key in issue_keys:
        issue_url = build_jira_issue_url(issue_key=issue_key, browse_base_url=browse_base_url)
        if issue_url:
            issue_urls.append(issue_url)
    return issue_urls
