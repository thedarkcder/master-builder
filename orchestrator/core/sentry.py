from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)
_initialized_fingerprint: tuple[str, str, str, float] | None = None


def initialize_sentry(
    *,
    settings,  # noqa: ANN001
    init_fn: Callable[..., Any] | None = None,
) -> bool:
    global _initialized_fingerprint
    dsn = (settings.sentry_dsn or "").strip()
    if not dsn:
        return False
    fingerprint = (
        dsn,
        str(settings.sentry_environment or ""),
        str(settings.sentry_release or ""),
        float(settings.sentry_traces_sample_rate or 0.0),
    )
    if _initialized_fingerprint is not None:
        if _initialized_fingerprint != fingerprint:
            raise RuntimeError("Sentry has already been initialized with different settings")
        logger.info(
            "sentry_already_initialized environment=%s release=%s traces_sample_rate=%s",
            settings.sentry_environment,
            settings.sentry_release or "unset",
            settings.sentry_traces_sample_rate,
        )
        return True

    if init_fn is None:
        try:
            import sentry_sdk
            from sentry_sdk.integrations.fastapi import FastApiIntegration
        except ImportError:
            logger.exception("sentry_sdk_import_failed")
            return False
        init_fn = sentry_sdk.init
        integrations: list[Any] = [FastApiIntegration()]
    else:
        integrations = []

    if integrations and settings.sentry_release:
        init_fn(
            dsn=dsn,
            environment=settings.sentry_environment,
            traces_sample_rate=settings.sentry_traces_sample_rate,
            integrations=integrations,
            release=settings.sentry_release,
        )
    elif integrations:
        init_fn(
            dsn=dsn,
            environment=settings.sentry_environment,
            traces_sample_rate=settings.sentry_traces_sample_rate,
            integrations=integrations,
        )
    elif settings.sentry_release:
        init_fn(
            dsn=dsn,
            environment=settings.sentry_environment,
            traces_sample_rate=settings.sentry_traces_sample_rate,
            release=settings.sentry_release,
        )
    else:
        init_fn(
            dsn=dsn,
            environment=settings.sentry_environment,
            traces_sample_rate=settings.sentry_traces_sample_rate,
        )
    _initialized_fingerprint = fingerprint
    logger.info(
        "sentry_initialized environment=%s release=%s traces_sample_rate=%s",
        settings.sentry_environment,
        settings.sentry_release or "unset",
        settings.sentry_traces_sample_rate,
    )
    return True


def _reset_sentry_initialization_for_tests() -> None:
    global _initialized_fingerprint
    _initialized_fingerprint = None
