from __future__ import annotations

import re

from logguard import load_sensitive_keys

_REDACTED = "[REDACTED]"
_SENSITIVE_VALUE_NAMES = r"token|secret|password|api[_-]?key|private[_-]?key|client[_-]?secret|access[_-]?key"
_SENSITIVE_KEY_NAME = rf"[A-Za-z0-9._-]*?(?:{_SENSITIVE_VALUE_NAMES})[A-Za-z0-9._-]*"
_LOGGUARD_KEY_PATTERNS = tuple(load_sensitive_keys())
_JSON_SENSITIVE_VALUE_PATTERN = re.compile(
    rf'(?i)("?(?:{_SENSITIVE_KEY_NAME})"?\s*:\s*")([^"]+)(")'
)
_HEADER_AUTH_PATTERN = re.compile(
    r"(?i)\b(authorization\s*:\s*(?:bearer|token)\s+)[^\s,;]+"
)
_TOKEN_HEADER_PATTERN = re.compile(r"(?i)\b(x-[a-z0-9-]*token\s*:\s*)[^\s,;]+")
_ASSIGNMENT_SENSITIVE_VALUE_PATTERN = re.compile(
    rf"(?i)\b((?:{_SENSITIVE_KEY_NAME})\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_TOKENIZED_URL_PATTERN = re.compile(r"(https://x-access-token:)[^@]+(@)", re.IGNORECASE)
_COOLIFY_WEBHOOK_TOKEN_PATTERN = re.compile(
    r"(/deployments/coolify/webhook/[^/\s]+/[^/\s]+/)[^/?\s\"']+"
)
_PEM_PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.IGNORECASE,
)
_EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _replace_with_redaction(pattern: re.Pattern[str], value: str) -> str:
    return pattern.sub(_REDACTED, value)


def _replace_preserving_prefix(
    pattern: re.Pattern[str],
    value: str,
    prefix_group: int,
    suffix_group: int | None = None,
) -> str:
    def _replacer(match: re.Match[str]) -> str:
        prefix = match.group(prefix_group)
        suffix = match.group(suffix_group) if suffix_group is not None else ""
        return f"{prefix}{_REDACTED}{suffix}"

    return pattern.sub(_replacer, value)


def redact_log_text(value: str) -> str:
    if not value:
        return value
    redacted = str(value)
    redacted = _replace_preserving_prefix(_COOLIFY_WEBHOOK_TOKEN_PATTERN, redacted, 1)
    redacted = _replace_with_redaction(_PEM_PRIVATE_KEY_PATTERN, redacted)
    redacted = _replace_preserving_prefix(_JSON_SENSITIVE_VALUE_PATTERN, redacted, 1, 3)
    redacted = _replace_preserving_prefix(_HEADER_AUTH_PATTERN, redacted, 1)
    redacted = _replace_preserving_prefix(_TOKEN_HEADER_PATTERN, redacted, 1)
    redacted = _replace_preserving_prefix(
        _ASSIGNMENT_SENSITIVE_VALUE_PATTERN, redacted, 1
    )
    redacted = _TOKENIZED_URL_PATTERN.sub(r"\1[REDACTED]\2", redacted)
    for pattern in _LOGGUARD_KEY_PATTERNS:
        redacted = pattern.sub(lambda match: f"{match.group(1)}={_REDACTED}", redacted)
    return _EMAIL_PATTERN.sub(_REDACTED, redacted)
