from __future__ import annotations

from datetime import datetime, timezone


def normalize_discord_attachments(raw_attachments: object) -> list[dict[str, str]]:
    if not isinstance(raw_attachments, list):
        return []
    normalized: list[dict[str, str]] = []
    for item in raw_attachments:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        proxy_url = str(item.get("proxy_url") or "").strip()
        attachment_url = url or proxy_url
        if not attachment_url:
            continue
        filename = str(item.get("filename") or "").strip() or "attachment"
        content_type = str(item.get("content_type") or "").strip()
        attachment: dict[str, str] = {
            "url": url,
            "filename": filename,
            "content_type": content_type,
        }
        if proxy_url:
            attachment["proxy_url"] = proxy_url
        if not attachment["url"] and attachment.get("proxy_url"):
            attachment["url"] = attachment["proxy_url"]
        normalized.append(attachment)
    return normalized[:5]


def build_discord_bug_description(
    *,
    summary: str,
    details: str,
    reporter_user_id: str,
    channel_id: str | None,
    related_issue_key: str | None,
    attachments: list[dict[str, str]],
) -> str:
    lines = [
        "Summary",
        f"- {summary.strip() or 'No summary provided'}",
        "",
        "Details",
        f"- {details.strip() or 'No additional context provided.'}",
        "",
        "Notes",
        "- Reported via Discord",
        f"- Reporter: {reporter_user_id}",
        f"- Channel: {channel_id or 'unknown'}",
        f"- Reported at: {datetime.now(timezone.utc).isoformat()}",
    ]
    if related_issue_key:
        lines.append(f"- Related issue: {related_issue_key}")
    if attachments:
        lines.extend(["", "**Attachments**"])
        for attachment in attachments:
            filename = attachment.get("filename") or "attachment"
            content_type = attachment.get("content_type") or ""
            if content_type:
                lines.append(f"- {filename} ({content_type})")
            else:
                lines.append(f"- {filename}")
    return "\n".join(lines)
