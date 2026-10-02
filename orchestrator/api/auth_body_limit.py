"""Bound anonymous identity JSON input before request parsing allocates its body."""

from fastapi import HTTPException

_AUTH_PATHS = frozenset(
    {
        "/api/public/register",
        "/api/public/password-reset/request",
        "/api/public/password-reset/confirm",
        "/api/app/auth/login",
        "/api/admin/auth/login",
        "/api/public/invites/accept",
    }
)


class AuthBodyLimitMiddleware:
    def __init__(self, app, *, max_bytes: int = 16384):  # noqa: ANN001
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):  # noqa: ANN001
        if scope["type"] != "http" or scope.get("path") not in _AUTH_PATHS:
            await self.app(scope, receive, send)
            return
        consumed = 0

        async def bounded_receive():
            nonlocal consumed
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > self.max_bytes:
                    raise HTTPException(413, "Authentication request exceeds 16 KiB")
            return message

        await self.app(scope, bounded_receive, send)
