from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from orchestrator.api.schemas import (
    AdminIdentityResponse,
    AdminLoginRequest,
    AdminLoginResponse,
)
from orchestrator.core.admin_tokens import create_admin_access_token
from orchestrator.core.config import get_settings
from orchestrator.core.security import require_admin, validate_admin_credentials

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.post("/auth/login", response_model=AdminLoginResponse)
def admin_login(payload: AdminLoginRequest) -> AdminLoginResponse:
    if not validate_admin_credentials(username=payload.username, password=payload.password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid admin credentials")

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
