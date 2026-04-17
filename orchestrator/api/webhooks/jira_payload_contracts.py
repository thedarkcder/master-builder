from __future__ import annotations

from fastapi import HTTPException, status

SUPPORTED_JIRA_COMMENT_COMMANDS = {"run", "retry", "ask", "clarify"}


def normalize_jira_webhook_event(raw_value: object) -> str | None:
    if not isinstance(raw_value, str):
        return None
    normalized = raw_value.strip().lower()
    if not normalized:
        return None
    if normalized.startswith("jira:"):
        normalized = normalized[len("jira:") :]
    return normalized


def adf_to_text(node: object) -> str:
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return " ".join(part for part in (adf_to_text(item) for item in node) if part).strip()
    if not isinstance(node, dict):
        return ""

    text = node.get("text")
    if isinstance(text, str):
        return text

    content = node.get("content")
    if isinstance(content, list):
        return " ".join(part for part in (adf_to_text(item) for item in content) if part).strip()
    return ""


def extract_issue_payload(
    payload: dict,
) -> tuple[str, list[str], str | None, str | None, str | None, str | None]:
    issue = payload.get("issue")
    if not isinstance(issue, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing issue object")

    issue_key = issue.get("key")
    if not isinstance(issue_key, str) or not issue_key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing issue key")

    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    labels = fields.get("labels") if isinstance(fields, dict) else []
    if not isinstance(labels, list):
        labels = []

    summary_raw = fields.get("summary") if isinstance(fields, dict) else None
    summary = str(summary_raw).strip() if isinstance(summary_raw, str) and summary_raw.strip() else None

    description_raw = fields.get("description") if isinstance(fields, dict) else None
    description_text = adf_to_text(description_raw).strip()
    description = description_text or None

    status_name: str | None = None
    status_category_key: str | None = None
    status_field = fields.get("status")
    if isinstance(status_field, dict):
        raw_status_name = status_field.get("name")
        if isinstance(raw_status_name, str):
            normalized_status_name = raw_status_name.strip()
            if normalized_status_name:
                status_name = normalized_status_name

        status_category = status_field.get("statusCategory")
        if isinstance(status_category, dict):
            raw_status_category_key = status_category.get("key")
            if isinstance(raw_status_category_key, str):
                normalized_status_category_key = raw_status_category_key.strip().lower()
                if normalized_status_category_key:
                    status_category_key = normalized_status_category_key

    normalized_labels = [str(label) for label in labels]
    return issue_key, normalized_labels, status_name, status_category_key, summary, description


def extract_jira_comment_text(payload: dict) -> str | None:
    comment = payload.get("comment")
    if not isinstance(comment, dict):
        return None
    body = comment.get("body")
    if isinstance(body, str):
        text = body.strip()
        return text or None
    if isinstance(body, dict):
        text = adf_to_text(body).strip()
        return text or None
    return None


def parse_jira_comment_command(payload: dict) -> tuple[str | None, str | None, str | None]:
    comment_text = extract_jira_comment_text(payload)
    if not comment_text:
        return None, None, None

    first_non_empty_line = ""
    remaining_lines: list[str] = []
    line_index = -1
    for raw_line in comment_text.splitlines():
        line_index += 1
        candidate = raw_line.strip()
        if candidate:
            first_non_empty_line = candidate
            remaining_lines = [line.strip() for line in comment_text.splitlines()[line_index + 1 :] if line.strip()]
            break
    if not first_non_empty_line:
        return None, None, None
    if not first_non_empty_line.lower().startswith("/mb"):
        return None, None, None

    command_payload = first_non_empty_line[3:].strip()
    if not command_payload:
        return None, None, "invalid_comment_command"

    parts = [part for part in command_payload.split(" ") if part]
    if not parts:
        return None, None, "invalid_comment_command"

    command_name = parts[0].strip().lower()
    if command_name not in SUPPORTED_JIRA_COMMENT_COMMANDS:
        return None, None, "invalid_comment_command"

    if command_name in {"run", "retry"}:
        if len(parts) != 1:
            return None, None, "invalid_comment_command"
        return command_name, None, None

    if command_name in {"ask", "clarify"}:
        inline_question = " ".join(parts[1:]).strip()
        full_question = "\n".join(part for part in [inline_question, *remaining_lines] if part).strip()
        if not full_question:
            return None, None, "invalid_comment_command"
        return command_name, full_question, None

    return None, None, "invalid_comment_command"


def extract_jira_comment_author_account_id(payload: dict) -> str | None:
    comment = payload.get("comment")
    if not isinstance(comment, dict):
        return None
    author = comment.get("author")
    if not isinstance(author, dict):
        return None
    account_id = author.get("accountId")
    if isinstance(account_id, str) and account_id.strip():
        return account_id.strip()
    return None


def extract_jira_comment_id(payload: dict) -> str | None:
    comment = payload.get("comment")
    if not isinstance(comment, dict):
        return None
    comment_id = comment.get("id")
    if isinstance(comment_id, str) and comment_id.strip():
        return comment_id.strip()
    if isinstance(comment_id, int):
        return str(comment_id)
    return None


def extract_status_transition(payload: dict) -> tuple[str | None, str | None]:
    changelog = payload.get("changelog")
    if not isinstance(changelog, dict):
        return None, None

    items = changelog.get("items")
    if not isinstance(items, list):
        return None, None

    for item in items:
        if not isinstance(item, dict):
            continue
        field = item.get("field")
        if not isinstance(field, str) or field.strip().lower() != "status":
            continue

        from_status = item.get("fromString")
        to_status = item.get("toString")
        normalized_from_status = from_status.strip() if isinstance(from_status, str) and from_status.strip() else None
        normalized_to_status = to_status.strip() if isinstance(to_status, str) and to_status.strip() else None
        return normalized_from_status, normalized_to_status

    return None, None


def extract_changed_fields(payload: dict) -> list[str]:
    changelog = payload.get("changelog")
    if not isinstance(changelog, dict):
        return []
    items = changelog.get("items")
    if not isinstance(items, list):
        return []
    changed_fields: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        field = item.get("field")
        normalized = field.strip().casefold() if isinstance(field, str) and field.strip() else ""
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        changed_fields.append(normalized)
    return changed_fields
