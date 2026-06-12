"""Optional bearer-token auth for the API. When AUTH_TOKEN is set, every
/api/* request (except health) and the SSE streams must present the token —
via `Authorization: Bearer <token>` or, for EventSource which cannot set
headers, a `?token=<token>` query param. The static UI shell and the
HMAC-verified Linear webhook are exempt."""

from __future__ import annotations

import hmac

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

# Paths reachable without a token even when auth is on.
PUBLIC_PATHS = {"/api/health"}
PUBLIC_PREFIXES = ("/webhooks/",)


def _present_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer "):
        return header[len("Bearer "):].strip()
    return request.query_params.get("token")


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, token: str) -> None:
        super().__init__(app)
        self.token = token

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        protected = path.startswith("/api/") and path not in PUBLIC_PATHS
        if protected and not any(path.startswith(p) for p in PUBLIC_PREFIXES):
            given = _present_token(request)
            if not given or not hmac.compare_digest(given, self.token):
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)
