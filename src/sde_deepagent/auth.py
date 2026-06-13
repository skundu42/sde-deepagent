"""Control-plane access middleware.

With AUTH_TOKEN, API and SSE requests require the bearer token. Without one,
the entire app is limited to loopback clients, regardless of the ASGI server's
bind address. This prevents an alternate Uvicorn/Gunicorn invocation from
accidentally exposing the unauthenticated control plane."""

from __future__ import annotations

import hmac

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .settings import is_loopback_host

# Paths reachable without a token even when auth is on.
PUBLIC_PATHS = {"/api/health"}
PUBLIC_PREFIXES = ("/webhooks/",)


def _present_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer "):
        return header[len("Bearer "):].strip()
    token = request.query_params.get("token")
    # strip the query-param token too, so header and ?token= behave identically
    return token.strip() if token else None


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


class LocalOnlyMiddleware(BaseHTTPMiddleware):
    """Reject every non-loopback request when application auth is disabled."""

    async def dispatch(self, request: Request, call_next):
        client_host = request.client.host if request.client else ""
        if not is_loopback_host(client_host):
            return JSONResponse(
                {"detail": "AUTH_TOKEN is required for non-loopback access"},
                status_code=403,
            )
        return await call_next(request)
