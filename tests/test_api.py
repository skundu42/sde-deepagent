
import httpx
import pytest

from sde_deepagent.server import create_app


@pytest.fixture
async def client(temp_env):
    app = create_app()
    async with httpx.ASGITransport(app=app) as transport:
        # run lifespan manually since ASGITransport doesn't
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://test") as client:
                yield client


async def test_health_is_slim(client):
    # /api/health is PUBLIC (no auth): it must not fingerprint the deployment
    # (which providers/intakes are configured, whether auth is on, ...)
    r = await client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and "version" in body
    assert set(body) == {"ok", "version"}


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


async def test_oversized_request_fields_rejected(client):
    # unbounded string fields would otherwise let a client store multi-MB blobs
    r = await client.post("/api/tasks",
                          json={"title": "t", "description": "x" * 60_000})
    assert r.status_code == 422
    r = await client.post("/api/repos", json={
        "name": "r", "url": "https://github.com/x/y.git", "setup": "y" * 5000})
    assert r.status_code == 422


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


async def test_task_bad_model_rejected_at_creation(client):
    # a per-task model override is validated up front, not deep inside run()
    r = await client.post("/api/tasks", json={"title": "x", "model": "not-a-real-model"})
    assert r.status_code == 400
    r = await client.post("/api/tasks", json={"title": "x", "model": "anthropic:claude-x"})
    assert r.status_code == 201  # known provider prefix is accepted


async def test_cancel_queued_emits_status_event_and_finished_at(client):
    task = (await client.post("/api/tasks", json={"title": "cancel me"})).json()
    r = await client.post(f"/api/tasks/{task['id']}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"

    # the cancellation is broadcast (so the live UI updates) and finished_at is set
    events = (await client.get(f"/api/tasks/{task['id']}/events")).json()
    assert any(e["kind"] == "status" and e["content"].get("status") == "cancelled"
               for e in events)
    assert (await client.get(f"/api/tasks/{task['id']}")).json()["finished_at"] is not None


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


async def test_prompt_defaults_endpoint(client):
    r = await client.get("/api/config/prompt-defaults")
    assert r.status_code == 200
    defaults = r.json()
    assert set(defaults) >= {"orchestrator", "explorer", "coder", "tester", "reviewer"}
    assert all(isinstance(v, str) and v.strip() for v in defaults.values())
    # the orchestrator default carries the format placeholders the UI hints at
    assert "{repo_name}" in defaults["orchestrator"]


async def test_orchestrator_prompt_override_validation(client):
    cfg = (await client.get("/api/config/agents")).json()

    # unknown placeholder -> rejected, nothing written
    bad = {**cfg, "orchestrator": {**cfg["orchestrator"], "system_prompt": "Do {nonsense}"}}
    r = await client.put("/api/config/agents", json=bad)
    assert r.status_code == 400
    after = (await client.get("/api/config/agents")).json()
    assert after.get("orchestrator", {}).get("system_prompt") is None

    # unbalanced brace (literal, unescaped) -> rejected
    bad2 = {**cfg, "orchestrator": {**cfg["orchestrator"], "system_prompt": "weird { brace"}}
    assert (await client.put("/api/config/agents", json=bad2)).status_code == 400

    # valid override using a known placeholder + plain text -> accepted and persisted
    text = "Work on {repo_name}. Plain text only."
    good = {**cfg, "orchestrator": {**cfg["orchestrator"], "system_prompt": text}}
    r = await client.put("/api/config/agents", json=good)
    assert r.status_code == 200
    assert (await client.get("/api/config/agents")).json()["orchestrator"]["system_prompt"] == text


async def test_subagent_prompt_accepts_literal_braces(client):
    # subagent prompts are used verbatim (no str.format), so braces are fine
    cfg = (await client.get("/api/config/agents")).json()
    text = 'Return JSON like {"ok": true} when the review passes.'
    cfg["subagents"]["reviewer"]["system_prompt"] = text
    r = await client.put("/api/config/agents", json=cfg)
    assert r.status_code == 200
    got = (await client.get("/api/config/agents")).json()
    assert got["subagents"]["reviewer"]["system_prompt"] == text


async def test_mcp_servers_round_trip_and_validation(client):
    cfg = (await client.get("/api/config/agents")).json()
    cfg["mcp_servers"] = {
        "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
                   "env": {"TOKEN": "x"}, "transport": "stdio"},
        "docs": {"url": "https://example.com/mcp", "transport": "streamable_http"},
    }
    r = await client.put("/api/config/agents", json=cfg)
    assert r.status_code == 200
    got = (await client.get("/api/config/agents")).json()
    assert got["mcp_servers"]["github"]["command"] == "npx"
    assert got["mcp_servers"]["docs"]["url"] == "https://example.com/mcp"

    # malformed mcp_servers (server config must be an object) -> rejected
    bad = {**cfg, "mcp_servers": {"oops": "not-a-dict"}}
    assert (await client.put("/api/config/agents", json=bad)).status_code == 400
    # mcp_servers itself must be a mapping
    bad2 = {**cfg, "mcp_servers": ["not", "a", "mapping"]}
    assert (await client.put("/api/config/agents", json=bad2)).status_code == 400


async def test_status_endpoint(client, monkeypatch):
    import sde_deepagent.sandbox as sandbox_mod
    monkeypatch.setattr(sandbox_mod, "docker_available", lambda: False)

    r = await client.get("/api/status")
    assert r.status_code == 200
    comps = {c["key"]: c for c in r.json()["components"]}
    assert {"providers", "memory", "sandbox", "github", "firecrawl",
            "intakes", "auth", "worker"} <= set(comps)

    # test env has no keys configured
    assert comps["memory"]["state"] == "unconfigured"
    assert comps["github"]["state"] == "unconfigured"
    assert comps["providers"]["state"] == "down"      # no model key → can't run
    assert comps["sandbox"]["state"] == "down"        # docker_available monkeypatched False
    assert comps["auth"]["state"] == "warn"           # AUTH_TOKEN empty in tests

    for c in comps.values():
        assert set(c) >= {"key", "label", "state", "detail"}
        assert c["state"] in {"ok", "warn", "down", "off", "unconfigured"}

    # the config fingerprint that used to be on public /api/health lives here,
    # behind auth
    config = r.json()["config"]
    assert set(config) >= {"providers", "github", "memory", "firecrawl",
                           "require_approval", "auth", "sandbox_default",
                           "review_polling", "intakes", "running"}
    assert config["providers"] == {"anthropic": False, "google": False,
                                   "openai": False}


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
