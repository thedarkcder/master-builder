from __future__ import annotations

from html import escape
from urllib.parse import quote, urlencode, urljoin

from orchestrator.tools.atlassian_oauth_models import ConfluencePage, ConfluenceSpace, AtlassianOAuthError


class AtlassianOAuthConfluenceService:
    def __init__(
        self,
        *,
        get_json,
        request_json,
    ) -> None:
        self._get_json = get_json
        self._request_json = request_json

    def get_space_by_key(self, *, access_token: str, cloud_id: str, space_key: str) -> ConfluenceSpace:
        normalized_space_key = str(space_key or "").strip()
        if not normalized_space_key:
            raise AtlassianOAuthError("Confluence space key is required")
        query = urlencode({"keys": normalized_space_key})
        payload = self._get_json(
            url=f"https://api.atlassian.com/ex/confluence/{cloud_id}/wiki/api/v2/spaces?{query}",
            access_token=access_token,
        )
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            raise AtlassianOAuthError("Confluence space response missing results list")
        for item in results:
            if not isinstance(item, dict):
                continue
            space = _parse_confluence_space(item)
            if space is not None and space.key == normalized_space_key:
                return space
        raise AtlassianOAuthError(f"Confluence space '{normalized_space_key}' was not found")

    def list_spaces(self, *, access_token: str, cloud_id: str, limit: int = 250) -> list[ConfluenceSpace]:
        payload = self._get_json(
            url=f"https://api.atlassian.com/ex/confluence/{cloud_id}/wiki/api/v2/spaces?{urlencode({'limit': max(1, min(limit, 250))})}",
            access_token=access_token,
        )
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            raise AtlassianOAuthError("Confluence space response missing results list")
        spaces: list[ConfluenceSpace] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            space = _parse_confluence_space(item)
            if space is not None:
                spaces.append(space)
        return spaces

    def list_pages(
        self,
        *,
        access_token: str,
        cloud_id: str,
        site_url: str,
        space_id: str,
        limit: int = 250,
    ) -> list[ConfluencePage]:
        normalized_space_id = str(space_id or "").strip()
        if not normalized_space_id:
            raise AtlassianOAuthError("Confluence page listing requires a space id")
        query = urlencode({"space-id": normalized_space_id, "limit": max(1, min(limit, 250))})
        payload = self._get_json(
            url=f"https://api.atlassian.com/ex/confluence/{cloud_id}/wiki/api/v2/pages?{query}",
            access_token=access_token,
        )
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            raise AtlassianOAuthError("Confluence page response missing results list")
        pages: list[ConfluencePage] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            pages.append(_parse_confluence_page(response=item, site_url=site_url))
        return pages

    def get_page(
        self,
        *,
        access_token: str,
        cloud_id: str,
        site_url: str,
        page_id: str,
    ) -> ConfluencePage:
        normalized_page_id = str(page_id or "").strip()
        if not normalized_page_id:
            raise AtlassianOAuthError("Confluence page lookup requires a page id")
        response = self._get_json(
            url=f"https://api.atlassian.com/ex/confluence/{cloud_id}/wiki/api/v2/pages/{quote(normalized_page_id, safe='')}",
            access_token=access_token,
        )
        return _parse_confluence_page(response=response, site_url=site_url)

    def create_page(
        self,
        *,
        access_token: str,
        cloud_id: str,
        site_url: str,
        space_id: str,
        title: str,
        body_storage_value: str,
        parent_page_id: str | None,
    ) -> ConfluencePage:
        normalized_space_id = str(space_id or "").strip()
        normalized_title = str(title or "").strip()
        normalized_body = str(body_storage_value or "").strip()
        normalized_parent_page_id = str(parent_page_id or "").strip() or None
        if not normalized_space_id:
            raise AtlassianOAuthError("Confluence page creation requires a space id")
        if not normalized_title:
            raise AtlassianOAuthError("Confluence page creation requires a title")
        if not normalized_body:
            raise AtlassianOAuthError("Confluence page creation requires body content")
        payload = {
            "spaceId": normalized_space_id,
            "status": "current",
            "title": normalized_title,
            "body": {
                "representation": "storage",
                "value": normalized_body,
            },
        }
        if normalized_parent_page_id is not None:
            payload["parentId"] = normalized_parent_page_id
        response = self._request_json(
            method="POST",
            url=f"https://api.atlassian.com/ex/confluence/{cloud_id}/wiki/api/v2/pages",
            access_token=access_token,
            payload=payload,
        )
        return _parse_confluence_page(response=response, site_url=site_url)

    def update_page_title(
        self,
        *,
        access_token: str,
        cloud_id: str,
        site_url: str,
        page_id: str,
        title: str,
    ) -> ConfluencePage:
        normalized_page_id = str(page_id or "").strip()
        normalized_title = str(title or "").strip()
        if not normalized_page_id:
            raise AtlassianOAuthError("Confluence page title update requires a page id")
        if not normalized_title:
            raise AtlassianOAuthError("Confluence page title update requires a title")
        response = self._request_json(
            method="PUT",
            url=(
                f"https://api.atlassian.com/ex/confluence/{cloud_id}/wiki/api/v2/pages/"
                f"{quote(normalized_page_id, safe='')}/title"
            ),
            access_token=access_token,
            payload={
                "status": "current",
                "title": normalized_title,
            },
        )
        return _parse_confluence_page(response=response, site_url=site_url)


def confluence_storage_template(*, title: str, parent_issue_key: str, issue_summary: str) -> str:
    normalized_title = escape(str(title or "").strip())
    normalized_parent_issue_key = escape(str(parent_issue_key or "").strip())
    normalized_issue_summary = escape(str(issue_summary or "").strip() or "Add the epic summary here.")
    sections = (
        "Architecture Overview",
        "System Diagrams",
        "Data Models",
        "Key Decisions (ADRs)",
        "API Contracts",
        "Constraints",
    )
    parts = [
        f"<h1>{normalized_title}</h1>",
        "<ul>",
        f"<li><p>Parent issue: {normalized_parent_issue_key}</p></li>",
        f"<li><p>Epic summary: {normalized_issue_summary}</p></li>",
        "</ul>",
    ]
    for section in sections:
        parts.extend(
            [
                f"<h2>{escape(section)}</h2>",
                "<ul>",
                "<li><p>Capture the canonical architecture knowledge for this epic here.</p></li>",
                "</ul>",
            ]
        )
    return "".join(parts)


def _parse_confluence_page(*, response, site_url: str) -> ConfluencePage:  # noqa: ANN001
    if not isinstance(response, dict):
        raise AtlassianOAuthError("Confluence page response was not an object")
    page_id = str(response.get("id") or "").strip()
    title = str(response.get("title") or "").strip()
    if not page_id or not title:
        raise AtlassianOAuthError("Confluence page response is missing id or title")
    links = response.get("_links")
    if not isinstance(links, dict):
        raise AtlassianOAuthError("Confluence page response is missing link metadata")
    webui = str(links.get("webui") or "").strip()
    if not webui:
        raise AtlassianOAuthError("Confluence page response is missing the web UI path")
    canonical_url = webui if webui.startswith("http://") or webui.startswith("https://") else urljoin(
        str(links.get("base") or "").strip() or str(site_url or "").strip().rstrip("/") + "/",
        webui,
    )
    if not canonical_url:
        raise AtlassianOAuthError("Confluence page response did not yield a canonical URL")
    return ConfluencePage(page_id=page_id, title=title, webui_url=canonical_url)


def _parse_confluence_space(response: dict) -> ConfluenceSpace | None:
    key = str(response.get("key") or "").strip()
    space_id = str(response.get("id") or "").strip()
    name = str(response.get("name") or "").strip() or key
    if not key or not space_id:
        return None
    return ConfluenceSpace(space_id=space_id, key=key, name=name)
