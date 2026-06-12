import asyncio

import httpx
import pytest

from devagent.server import create_app


@pytest.fixture
async def client(temp_env):
    app = create_app()
    async with httpx.ASGITransport(app=app) as transport:
        # run lifespan manually since ASGITransport doesn't
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://test") as client:
                yield client


async def test_health(client):
    r = await client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "providers" in body


async def test_repo_crud(client):
    r = await client.post("/api/repos", json={
        "name": "backend", "url": "https://github.com/acme/backend.git",
        "description": "the api", "test": "pytest -x"})
    assert r.status_code == 201
    r = await client.get("/api/repos")
    assert "backend" in r.json()
    r = await client.delete("/api/repos/backend")
    assert r.status_code == 200
    r = await client.delete("/api/repos/backend")
    assert r.status_code == 404


async def test_task_create_and_cancel(client):
    r = await client.post("/api/tasks", json={"title": "Do the thing",
                                              "description": "details"})
    assert r.status_code == 201
    task = r.json()
    assert task["status"] == "queued"

    r = await client.get(f"/api/tasks/{task['id']}")
    assert r.json()["title"] == "Do the thing"

    r = await client.get("/api/tasks")
    assert any(t["id"] == task["id"] for t in r.json())

    r = await client.post(f"/api/tasks/{task['id']}/cancel")
    assert r.status_code == 200
    r = await client.get(f"/api/tasks/{task['id']}")
    assert r.json()["status"] == "cancelled"

    # cancelling again conflicts
    r = await client.post(f"/api/tasks/{task['id']}/cancel")
    assert r.status_code == 409


async def test_task_unknown_repo_rejected(client):
    r = await client.post("/api/tasks", json={"title": "x", "repo": "ghost"})
    assert r.status_code == 400


async def test_events_endpoint(client):
    r = await client.post("/api/tasks", json={"title": "evt task"})
    task_id = r.json()["id"]
    r = await client.get(f"/api/tasks/{task_id}/events")
    assert r.status_code == 200
    assert r.json() == []


async def test_agents_config_validation(client):
    r = await client.get("/api/config/agents")
    cfg = r.json()
    assert cfg["orchestrator"]["model"].startswith("anthropic:")

    # invalid provider is rejected and rolled back
    bad = {**cfg, "orchestrator": {"model": "mistral:large"}}
    r = await client.put("/api/config/agents", json=bad)
    assert r.status_code == 400
    r = await client.get("/api/config/agents")
    assert r.json()["orchestrator"]["model"].startswith("anthropic:")

    # valid gemini and openai models are accepted
    for model in ("google_genai:gemini-2.5-pro", "openai:gpt-5.4"):
        good = {**cfg, "orchestrator": {"model": model}}
        r = await client.put("/api/config/agents", json=good)
        assert r.status_code == 200
        assert r.json()["orchestrator"]["model"] == model


async def test_stats(client):
    await client.post("/api/tasks", json={"title": "a"})
    r = await client.get("/api/stats")
    body = r.json()
    assert body["total"] >= 1
    assert body["spend_today_usd"] == 0
    assert "daily_budget_usd" in body and "budget_paused" in body


async def test_models_catalog(client):
    r = await client.get("/api/models")
    assert r.status_code == 200
    catalog = r.json()
    assert set(catalog) == {"anthropic", "google_genai", "openai"}
    assert "openai:gpt-5.4" in catalog["openai"]["models"]
    assert all(m.startswith("anthropic:") for m in catalog["anthropic"]["models"])
    assert catalog["anthropic"]["configured"] is False  # no keys in test env


async def test_task_budget_field(client):
    r = await client.post("/api/tasks", json={"title": "capped", "budget_usd": 2.5})
    assert r.status_code == 201
    assert r.json()["budget_usd"] == 2.5
    r = await client.post("/api/tasks", json={"title": "bad", "budget_usd": -1})
    assert r.status_code == 422
