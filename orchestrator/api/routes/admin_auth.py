from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.api.auth_admission import enforce_auth_admission
from orchestrator.api.schemas import (
    AdminIdentityResponse,
    AdminLoginRequest,
    AdminLoginResponse,
)
from orchestrator.core.platform.admin_tokens import create_admin_access_token
from orchestrator.core.config import get_settings
from orchestrator.core.security import require_admin, validate_admin_credentials

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.post("/auth/login", response_model=AdminLoginResponse)
def admin_login(
    payload: AdminLoginRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> AdminLoginResponse:
    enforce_auth_admission(request, action="admin-login", account=payload.username)
    if not validate_admin_credentials(
        session=session, username=payload.username, password=payload.password
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid admin credentials"
        )

    settings = get_settings()
    token, expires_in = create_admin_access_token(
        username=settings.admin_username,
        secret=settings.admin_token_secret,
        ttl_seconds=settings.admin_token_ttl_seconds,
    )
    return AdminLoginResponse(access_token=token, expires_in=expires_in)


@router.get("/auth/me", response_model=AdminIdentityResponse)
def admin_me(username: str = Depends(require_admin)) -> AdminIdentityResponse:
    return AdminIdentityResponse(username=username)
