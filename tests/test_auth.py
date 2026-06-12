"""Bearer-token auth middleware: protects /api/* (except health), accepts the
token via header or ?token= query (for SSE), exempts webhooks and the UI shell."""

import httpx
import pytest

import sde_deepagent.settings as settings_mod
from sde_deepagent.server import create_app


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
