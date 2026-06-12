"""Resources ingestion endpoints + chat spend accounting in the daily budget."""

import json
import time

import httpx
import pytest

from devagent.chat import ChatService
from devagent.db import Database
from devagent.memory import GLOBAL_TAG, Memory, repo_tag
from devagent.server import create_app


def make_memory(state: dict) -> Memory:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v3/documents" and request.method == "POST":
            body = json.loads(request.content)
            doc_id = f"doc_{len(state.setdefault('docs', [])) + 1}"
            state["docs"].append({"id": doc_id, "title": body["content"][:40],
                                  "summary": "", "status": "queued",
                                  "containerTags": [body["containerTag"]],
                                  "metadata": body.get("metadata") or {},
                                  "createdAt": "2026-06-12T00:00:00Z"})
            return httpx.Response(200, json={"id": doc_id, "status": "queued"})
        if request.url.path == "/v3/documents/list":
            return httpx.Response(200, json={"memories": state.get("docs", [])})
        if request.url.path.startswith("/v3/documents/") and request.method == "DELETE":
            doc_id = request.url.path.rsplit("/", 1)[1]
            before = len(state.get("docs", []))
            state["docs"] = [d for d in state.get("docs", []) if d["id"] != doc_id]
            return httpx.Response(200 if len(state["docs"]) < before else 404)
        return httpx.Response(404)

    return Memory("http://sm:6767", "sm_test", transport=httpx.MockTransport(handler))


@pytest.fixture
async def client_with_memory(temp_env):
    state: dict = {}
    app = create_app()
    async with httpx.ASGITransport(app=app) as transport:
        async with app.router.lifespan_context(app):
            app.state.memory = make_memory(state)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://test") as client:
                yield client, app, state


async def test_resource_lifecycle(client_with_memory, monkeypatch):
    client, app, state = client_with_memory

    async def fake_fetch(url, timeout=30.0, **kwargs):
        return "Arch Docs", "The API gateway routes to services."

    monkeypatch.setattr("devagent.webfetch.fetch_page_text", fake_fetch)
    # url into global scope — fetched by devagent, stored as extracted text
    r = await client.post("/api/resources",
                          json={"content": "https://docs.example.com/arch"})
    assert r.status_code == 201
    assert r.json() == {"id": "doc_1", "scope": "global", "kind": "url",
                        "title": "Arch Docs"}
    assert state["docs"][0]["containerTags"] == [GLOBAL_TAG]
    assert state["docs"][0]["metadata"]["url"] == "https://docs.example.com/arch"

    # raw text into a repo scope
    await client.post("/api/repos", json={"name": "backend", "url": "/tmp/x"})
    r = await client.post("/api/resources",
                          json={"content": "Deploys go through ArgoCD.",
                                "scope": "backend"})
    assert r.json()["kind"] == "text"
    assert state["docs"][1]["containerTags"] == [repo_tag("backend")]

    r = await client.get("/api/resources")
    docs = r.json()
    assert len(docs) == 2 and {d["scope"] for d in docs} == {"global", "backend"}

    r = await client.delete("/api/resources/doc_1")
    assert r.status_code == 200
    assert len((await client.get("/api/resources")).json()) == 1


async def test_resource_unknown_scope_and_no_memory(client_with_memory):
    client, app, _ = client_with_memory
    r = await client.post("/api/resources", json={"content": "x", "scope": "ghost"})
    assert r.status_code == 400

    app.state.memory = None  # memory unconfigured
    for call in (client.post("/api/resources", json={"content": "x"}),
                 client.get("/api/resources")):
        assert (await call).status_code == 503


async def test_non_resource_memories_hidden(client_with_memory):
    client, app, state = client_with_memory
    state.setdefault("docs", []).append({
        "id": "doc_agent", "title": "agent learning", "status": "done",
        "containerTags": [GLOBAL_TAG], "metadata": {"source": "agent"},
        "createdAt": "2026-06-12T00:00:00Z"})
    r = await client.get("/api/resources")
    assert r.json() == []  # only source=resource entries are listed


# ---- chat spend in the daily budget ----

async def test_chat_spend_recorded_and_summed(tmp_path):
    db = Database(tmp_path / "c.db")
    await db.connect()
    await db.add_chat_spend("s1", "openai:gpt-5.4", 1000, 200, 0.05)
    await db.add_chat_spend("s1", "openai:gpt-5.4", 500, 100, 0.02)
    assert await db.chat_spend_since(0) == pytest.approx(0.07)
    # combined with task spend
    t = await db.create_task("t", "d")
    await db.update_task(t.id, status="completed", started_at=time.time(),
                         cost_usd=0.10)
    assert await db.spend_since(0) == pytest.approx(0.17)
    await db.close()


async def test_chat_blocked_when_daily_budget_spent(temp_env, monkeypatch):
    monkeypatch.setenv("DAILY_BUDGET_USD", "0.05")

    async def fake_ask(self, message, session_id=None):
        return {"session_id": "s", "reply": "ok", "cost_usd": 0.0}

    monkeypatch.setattr(ChatService, "ask", fake_ask)
    app = create_app()
    async with httpx.ASGITransport(app=app) as transport:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://test") as client:
                r = await client.post("/api/chat", json={"message": "hi"})
                assert r.status_code == 200  # under budget

                await app.state.db.add_chat_spend("s", "m", 10, 10, 0.06)
                r = await client.post("/api/chat", json={"message": "hi again"})
                assert r.status_code == 429
                assert "daily budget" in r.json()["detail"]
