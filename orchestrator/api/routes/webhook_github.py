from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.api.webhooks.github_ingress import ingest_github_webhook_event
from orchestrator.core.config import get_settings

router = APIRouter(tags=["github-webhook"])


@router.post("/github/webhook")
async def ingest_github_webhook(
    request: Request,
    session: Session = Depends(get_session),
) -> object:
    return await ingest_github_webhook_event(
        request=request,
        session=session,
        settings=get_settings(),
    )
