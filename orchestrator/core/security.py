from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBasic, HTTPBasicCredentials, HTTPBearer

from orchestrator.core.admin_tokens import parse_admin_access_token
from orchestrator.core.config import get_settings

basic_auth = HTTPBasic(auto_error=False)
bearer_auth = HTTPBearer(auto_error=False)


def validate_admin_credentials(*, username: str, password: str) -> bool:
    settings = get_settings()
    valid_user = secrets.compare_digest(username, settings.admin_username)
    valid_password = secrets.compare_digest(password, settings.admin_password)
    return valid_user and valid_password


def _admin_unauthorized(detail: str = "Invalid admin credentials") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer, Basic"},
    )


def require_admin(
    bearer_credentials: HTTPAuthorizationCredentials | None = Depends(bearer_auth),
    basic_credentials: HTTPBasicCredentials | None = Depends(basic_auth),
) -> str:
    settings = get_settings()

    if bearer_credentials is not None and bearer_credentials.scheme.lower() == "bearer":
        try:
            return parse_admin_access_token(token=bearer_credentials.credentials, secret=settings.admin_token_secret)
        except ValueError as exc:
            raise _admin_unauthorized(str(exc)) from exc

    if basic_credentials is not None:
        if validate_admin_credentials(username=basic_credentials.username, password=basic_credentials.password):
            return basic_credentials.username
        raise _admin_unauthorized()

    raise _admin_unauthorized("Admin authentication required")
