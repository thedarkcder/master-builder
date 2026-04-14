from __future__ import annotations

from dataclasses import dataclass


PR_REMEDIATION_TRIGGER_SOURCE = "github_pr_review_feedback"
_MANUAL_FIX_COMMENT_TYPES = {"review_comment", "issue_comment"}


class TriggerContextDecodeError(ValueError):
    """Raised when a trigger context payload is present but malformed."""


@dataclass(frozen=True)
class TriggerContext:
    source: str


@dataclass(frozen=True)
class RequestedComment:
    comment_type: str
    comment_id: int
    url: str | None = None
    body: str | None = None
    user_login: str | None = None
    path: str | None = None
    line: int | None = None


@dataclass(frozen=True)
class CodeContext:
    path: str
    line: int
    head_sha: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    snippet: str | None = None


@dataclass(frozen=True)
class ManualFixRequestContext:
    requested_by: str | None = None
    instruction_text: str | None = None
    command: str | None = None
    requested_comment: RequestedComment | None = None
    triggering_comment_id: int | None = None
    triggering_comment_url: str | None = None
    code_context: CodeContext | None = None
    code_context_resolution: str | None = None


@dataclass(frozen=True)
class GithubPrRemediationTriggerContext(TriggerContext):
    pr_number: int | None = None
    head_sha: str | None = None
    event: str | None = None
    action: str | None = None
    pr_url: str | None = None
    head_ref: str | None = None
    base_ref: str | None = None
    issue_key: str | None = None
    issue_created: bool | None = None
    requested_comment: RequestedComment | None = None
    manual_fix_request: ManualFixRequestContext | None = None

    def matches_pr_head(self, *, pr_number: int, head_sha: str) -> bool:
        normalized_head_sha = str(head_sha or "").strip()
        if self.pr_number is None or self.head_sha is None or not normalized_head_sha:
            return False
        return self.pr_number == pr_number and self.head_sha == normalized_head_sha


def decode_trigger_context(raw: object) -> TriggerContext | None:
    if not isinstance(raw, dict):
        return None
    source = _normalized_text(raw.get("source"))
    if source is None and _looks_like_pr_remediation(raw):
        source = PR_REMEDIATION_TRIGGER_SOURCE
    if source is None:
        return None
    if source != PR_REMEDIATION_TRIGGER_SOURCE:
        return TriggerContext(source=source)
    return _decode_pr_remediation_context(raw)


def require_github_pr_remediation_context(raw: object) -> GithubPrRemediationTriggerContext:
    parsed = decode_trigger_context(raw)
    if not isinstance(parsed, GithubPrRemediationTriggerContext):
        raise TriggerContextDecodeError("Trigger context is not a GitHub PR remediation payload")
    return parsed


def _decode_pr_remediation_context(raw: dict[str, object]) -> GithubPrRemediationTriggerContext:
    pr_number = _positive_int(raw.get("pr_number"))
    head_sha = _normalized_text(raw.get("head_sha"))

    requested_comment = _decode_requested_comment(raw.get("requested_comment"))
    manual_fix = _decode_manual_fix_request(
        raw.get("manual_fix_request"),
        fallback_requested_comment=requested_comment,
    )
    if manual_fix is not None and requested_comment is None:
        requested_comment = manual_fix.requested_comment

    issue_created_raw = raw.get("issue_created")
    issue_created = issue_created_raw if isinstance(issue_created_raw, bool) else None

    return GithubPrRemediationTriggerContext(
        source=PR_REMEDIATION_TRIGGER_SOURCE,
        event=_normalized_text(raw.get("event")),
        action=_normalized_text(raw.get("action")),
        pr_number=pr_number,
        pr_url=_normalized_text(raw.get("pr_url")),
        head_sha=head_sha,
        head_ref=_normalized_text(raw.get("head_ref")),
        base_ref=_normalized_text(raw.get("base_ref")),
        issue_key=_normalized_text(raw.get("issue_key")),
        issue_created=issue_created,
        requested_comment=requested_comment,
        manual_fix_request=manual_fix,
    )


def _looks_like_pr_remediation(raw: dict[str, object]) -> bool:
    remediation_markers = {
        "pr_number",
        "head_sha",
        "head_ref",
        "base_ref",
        "manual_fix_request",
        "requested_comment",
        "pr_url",
    }
    return any(key in raw for key in remediation_markers)


def _decode_requested_comment(raw: object) -> RequestedComment | None:
    if not isinstance(raw, dict):
        return None
    comment_id = _positive_int(raw.get("id"))
    comment_type = _normalized_text(raw.get("type"))
    if comment_id is None or comment_type is None or comment_type not in _MANUAL_FIX_COMMENT_TYPES:
        return None
    line_value = _positive_int(raw.get("line"))
    return RequestedComment(
        comment_type=comment_type,
        comment_id=comment_id,
        url=_normalized_text(raw.get("url")),
        body=_normalized_text(raw.get("body")),
        user_login=_normalized_text(raw.get("user_login")),
        path=_normalized_text(raw.get("path")),
        line=line_value,
    )


def _decode_manual_fix_request(
    raw: object,
    *,
    fallback_requested_comment: RequestedComment | None,
) -> ManualFixRequestContext | None:
    if not isinstance(raw, dict):
        return None
    requested_comment = _decode_requested_comment(raw.get("requested_comment")) or fallback_requested_comment
    code_context = _decode_code_context(raw.get("code_context"))
    return ManualFixRequestContext(
        requested_by=_normalized_text(raw.get("requested_by")),
        instruction_text=_normalized_text(raw.get("instruction_text")),
        command=_normalized_text(raw.get("command")),
        requested_comment=requested_comment,
        triggering_comment_id=_positive_int(raw.get("triggering_comment_id")),
        triggering_comment_url=_normalized_text(raw.get("triggering_comment_url")),
        code_context=code_context,
        code_context_resolution=_normalized_text(raw.get("code_context_resolution")),
    )


def _decode_code_context(raw: object) -> CodeContext | None:
    if not isinstance(raw, dict):
        return None
    path = _normalized_text(raw.get("path"))
    line = _positive_int(raw.get("line"))
    if path is None or line is None:
        return None
    return CodeContext(
        path=path,
        line=line,
        head_sha=_normalized_text(raw.get("head_sha")),
        start_line=_positive_int(raw.get("start_line")),
        end_line=_positive_int(raw.get("end_line")),
        snippet=_normalized_text(raw.get("snippet")),
    )


def _normalized_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _positive_int(value: object) -> int | None:
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            return None
        return parsed if parsed > 0 else None
    return None
