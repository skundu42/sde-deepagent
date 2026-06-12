"""Chat-over-task-history: the tools the chat agent uses, and the endpoint."""

import httpx
import pytest

from sde_deepagent.chat import ChatService, make_chat_tools
from sde_deepagent.config import ConfigStore
from sde_deepagent.db import Database
from sde_deepagent.settings import get_settings


@pytest.fixture
async def stack(temp_env):
    settings = get_settings()
    db = Database(settings.db_path)
    await db.connect()
    cfg = ConfigStore(settings.config_dir)
    yield db, settings, cfg
    await db.close()


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


async def test_list_and_get_task_tools(stack):
    db, settings, cfg = stack
    t1 = await db.create_task("Fix login", "the login 500s", repo="backend")
    await db.update_task(t1.id, status="completed", cost_usd=0.42,
                         pr_url="https://github.com/x/y/pull/7")
    await db.create_task("Add dark mode", "css work", repo="web")

    tools = make_chat_tools(db, settings, cfg)
    assert {t.name for t in tools} == {"list_tasks", "get_task", "get_task_trace"}

    out = await _tool(tools, "list_tasks").ainvoke({})
    assert t1.id in out and "Fix login" in out and "$0.4200" in out
    out = await _tool(tools, "list_tasks").ainvoke({"status": "completed"})
    assert "Fix login" in out and "dark mode" not in out

    out = await _tool(tools, "get_task").ainvoke({"task_id": t1.id})
    assert "the login 500s" in out and "pull/7" in out
    out = await _tool(tools, "get_task").ainvoke({"task_id": "nope"})
    assert "No task" in out


async def test_trace_tool(stack):
    db, settings, cfg = stack
    t = await db.create_task("traced", "d")
    await db.add_event(t.id, "tool_call", {"name": "execute", "args": {"command": "pytest"}},
                       agent="tester")
    await db.add_event(t.id, "status", {"status": "completed"})

    tools = make_chat_tools(db, settings, cfg)
    out = await _tool(tools, "get_task_trace").ainvoke({"task_id": t.id})
    assert "[tester] tool_call: $ execute" in out
    assert "STATUS completed" in out


async def test_memory_tool_included_when_configured(stack, monkeypatch):
    db, settings, cfg = stack
    monkeypatch.setattr(settings, "supermemory_base_url", "http://localhost:6767")
    monkeypatch.setattr(settings, "supermemory_api_key", "sm_x")
    tools = make_chat_tools(db, settings, cfg)
    assert "search_memory" in {t.name for t in tools}


async def test_chat_endpoint(temp_env, monkeypatch):
    async def fake_ask(self, message, session_id=None):
        return {"session_id": session_id or "sess1", "reply": f"echo: {message}"}

    monkeypatch.setattr(ChatService, "ask", fake_ask)
    from sde_deepagent.server import create_app

    app = create_app()
    async with httpx.ASGITransport(app=app) as transport:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://test") as client:
                r = await client.post("/api/chat", json={"message": "hi"})
                assert r.status_code == 200
                body = r.json()
                assert body["reply"] == "echo: hi"
                assert body["session_id"] == "sess1"

                r = await client.post("/api/chat", json={"message": ""})
                assert r.status_code == 422

                r = await client.delete("/api/chat/sess1")
                assert r.status_code == 200


async def test_session_reset(stack):
    db, settings, cfg = stack
    svc = ChatService(db, cfg, settings)
    svc.sessions["abc"] = ["something"]
    assert svc.reset("abc") is True
    assert svc.reset("abc") is False
