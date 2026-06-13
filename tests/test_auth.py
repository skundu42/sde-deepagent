"""Bearer-token auth middleware: protects /api/* (except health), accepts the
token via header or ?token= query (for SSE), exempts webhooks and the UI shell."""

import httpx
import pytest

import sde_deepagent.settings as settings_mod
from sde_deepagent.server import create_app
from sde_deepagent.settings import Settings, validate_control_plane_security


@pytest.fixture
async def auth_client(temp_env, monkeypatch):
    monkeypatch.setenv("AUTH_TOKEN", "s3cret")
    settings_mod._settings = None
    app = create_app()
    async with httpx.ASGITransport(app=app) as transport:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://test") as client:
                yield client
    settings_mod._settings = None


async def test_health_is_public(auth_client):
    assert (await auth_client.get("/api/health")).status_code == 200


async def test_api_requires_token(auth_client):
    assert (await auth_client.get("/api/tasks")).status_code == 401
    assert (await auth_client.get("/api/stats")).status_code == 401


async def test_token_via_header(auth_client):
    r = await auth_client.get("/api/tasks", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200


async def test_token_via_query_for_sse(auth_client):
    # EventSource can't set headers — query param must work
    r = await auth_client.get("/api/tasks?token=s3cret")
    assert r.status_code == 200
    r = await auth_client.get("/api/tasks?token=wrong")
    assert r.status_code == 401


async def test_wrong_token_rejected(auth_client):
    r = await auth_client.get("/api/tasks", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


async def test_query_token_is_stripped_like_header(auth_client):
    # surrounding whitespace must be tolerated the same way for ?token= and Bearer
    r = await auth_client.get("/api/tasks", params={"token": "  s3cret  "})
    assert r.status_code == 200


async def test_webhook_exempt(auth_client):
    # linear webhook has its own HMAC; not gated by the token (404 = not configured,
    # not 401 = blocked by auth)
    r = await auth_client.post("/webhooks/linear", json={})
    assert r.status_code != 401


async def test_no_auth_when_unset(temp_env):
    # default temp_env has AUTH_TOKEN="" -> middleware not installed
    app = create_app()
    async with httpx.ASGITransport(app=app) as transport:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://test") as client:
                assert (await client.get("/api/tasks")).status_code == 200


async def test_no_auth_rejects_non_loopback_client(temp_env):
    app = create_app()
    transport = httpx.ASGITransport(app=app, client=("203.0.113.10", 12345))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/")).status_code == 403
            assert (await client.get("/api/health")).status_code == 403


def test_non_loopback_bind_requires_auth():
    with pytest.raises(RuntimeError, match="refusing unauthenticated network bind"):
        validate_control_plane_security(Settings(_env_file=None, host="0.0.0.0", auth_token=None))
    validate_control_plane_security(
        Settings(_env_file=None, host="0.0.0.0", auth_token="strong-token"))


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_bind_can_run_without_auth(host):
    validate_control_plane_security(Settings(_env_file=None, host=host, auth_token=None))
