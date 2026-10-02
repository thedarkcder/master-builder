from __future__ import annotations

import logging
import re
import traceback
from collections.abc import Iterable

from orchestrator.core.observability.log_redaction import redact_log_text

_DANGEROUS_COMMAND_PATTERNS = (
    re.compile(r"(^|\s)rm\s+-rf(\s|$)", re.IGNORECASE),
    re.compile(r"(^|\s)(curl|wget)\b[^|]*\|\s*(sh|bash)\b", re.IGNORECASE),
    re.compile(r"(^|\s)sudo(\s|$)", re.IGNORECASE),
    re.compile(r"(^|\s)chmod\s+777(\s|$)", re.IGNORECASE),
)


def contains_dangerous_command_pattern(command: str) -> bool:
    normalized = " ".join(command.strip().split())
    if not normalized:
        return False
    return any(pattern.search(normalized) for pattern in _DANGEROUS_COMMAND_PATTERNS)


def enforce_safe_command(command: str) -> None:
    normalized = " ".join(command.strip().split())
    if not normalized:
        raise ValueError("Command must not be empty")
    if contains_dangerous_command_pattern(normalized):
        raise PermissionError(f"Command '{command}' is blocked by guardrail policy")


def enforce_command_allowlist(command: str, allowlist: Iterable[str]) -> None:
    enforce_safe_command(command)
    normalized_command = " ".join(command.strip().split()).lower()
    normalized_allowlist = [
        " ".join(allowed.strip().split()).lower()
        for allowed in allowlist
        if isinstance(allowed, str) and allowed.strip()
    ]
    if not normalized_allowlist:
        return

    for allowed in normalized_allowlist:
        if normalized_command == allowed or normalized_command.startswith(
            f"{allowed} "
        ):
            return

    raise PermissionError(f"Command '{command}' is not in the tenant allowlist")


def redact_sensitive_text(value: str) -> str:
    return redact_log_text(value)


def _redact_log_arg(arg: object) -> object:
    if isinstance(arg, str):
        return redact_sensitive_text(arg)
    return arg


class SensitiveDataRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            if isinstance(record.args, tuple):
                redacted_args = tuple(_redact_log_arg(arg) for arg in record.args)
            elif isinstance(record.args, dict):
                redacted_args = {
                    key: _redact_log_arg(value) for key, value in record.args.items()
                }
            else:
                redacted_args = record.args

            try:
                rendered = str(record.msg) % redacted_args
            except Exception:
                traceback.print_exc()
                rendered = f"{record.msg} {redacted_args}"
            record.msg = rendered
            record.args = ()
        else:
            record.msg = redact_sensitive_text(str(record.msg))
            return True

        record.msg = redact_sensitive_text(str(record.msg))
        return True
