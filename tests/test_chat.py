"""Chat-over-task-history: the tools the chat agent uses, and the endpoint."""

import httpx
import pytest

from sde_deepagent.chat import ChatService, _safe_trim, make_chat_tools
from sde_deepagent.config import ConfigStore
from sde_deepagent.db import Database
from sde_deepagent.settings import get_settings


def test_safe_trim_does_not_orphan_tool_results():
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    msgs = [
        AIMessage(content="", tool_calls=[
            {"name": "x", "args": {}, "id": "c0", "type": "tool_call"}]),
        ToolMessage(content="r", tool_call_id="c0"),  # orphaned if it heads the window
        HumanMessage(content="q1"),
        AIMessage(content="a1"),
    ]
    # naive msgs[-3:] begins on the orphaned ToolMessage (which providers reject)
    out = _safe_trim(msgs, 3)
    assert getattr(out[0], "type", "") == "human"
    assert out == msgs[2:]


def test_safe_trim_keeps_recent_within_limit():
    from langchain_core.messages import HumanMessage
    msgs = [HumanMessage(content=str(i)) for i in range(100)]
    out = _safe_trim(msgs, 60)
    assert len(out) == 60 and out[-1].content == "99"


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
    assert {t.name for t in tools} == {"list_tasks", "get_task", "get_task_trace",
                                       "list_repos", "get_repo", "list_repo_files",
                                       "grep_repo", "read_repo_file"}

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


async def test_memory_tools_included_when_configured(stack, monkeypatch):
    db, settings, cfg = stack
    monkeypatch.setattr(settings, "supermemory_base_url", "http://localhost:6767")
    monkeypatch.setattr(settings, "supermemory_api_key", "sm_x")
    tools = make_chat_tools(db, settings, cfg)
    assert {"search_knowledge", "list_resources"} <= {t.name for t in tools}


async def test_repo_tools(stack):
    from sde_deepagent.config import RepoConfig
    db, settings, cfg = stack
    cfg.upsert_repo(RepoConfig(
        name="backend", url="git@github.com:acme/backend.git",
        description="FastAPI monolith serving the public API", default_branch="main",
        test="uv run pytest -x -q", context=["docs/arch.md"]))
    tools = make_chat_tools(db, settings, cfg)

    out = await _tool(tools, "list_repos").ainvoke({})
    assert "backend" in out and "FastAPI monolith" in out

    out = await _tool(tools, "get_repo").ainvoke({"name": "backend"})
    assert "uv run pytest" in out and "main" in out and "docs/arch.md" in out
    out = await _tool(tools, "get_repo").ainvoke({"name": "ghost"})
    assert "No repo" in out


async def _make_origin(path):
    from sde_deepagent.gitops import run_cmd
    path.mkdir(parents=True)
    for args in (["git", "init", "-b", "main"],
                 ["git", "config", "user.email", "t@t"],
                 ["git", "config", "user.name", "t"]):
        await run_cmd(args, cwd=path)
    (path / "main.py").write_text("def hello():\n    return 'hi'\n")
    await run_cmd(["git", "add", "-A"], cwd=path)
    await run_cmd(["git", "commit", "-m", "init"], cwd=path)


async def test_code_reading_tools_on_registered_repo(stack, tmp_path):
    from sde_deepagent.config import RepoConfig
    db, settings, cfg = stack
    origin = tmp_path / "origin"
    await _make_origin(origin)
    cfg.upsert_repo(RepoConfig(name="backend", url=str(origin), default_branch="main"))
    tools = make_chat_tools(db, settings, cfg)

    out = await _tool(tools, "list_repo_files").ainvoke({"repo": "backend"})
    assert "main.py" in out

    out = await _tool(tools, "read_repo_file").ainvoke({"repo": "backend", "path": "main.py"})
    assert "def hello" in out and "main.py" in out

    out = await _tool(tools, "grep_repo").ainvoke({"repo": "backend", "pattern": "def hello"})
    assert "main.py" in out

    # security gate: a repo neither registered nor ingested cannot be cloned
    out = await _tool(tools, "read_repo_file").ainvoke(
        {"repo": "https://github.com/some/unlisted", "path": "README.md"})
    assert "isn't available to read" in out


def _fake_memory():
    from sde_deepagent.memory import Memory

    def handler(request):
        path = request.url.path
        if path == "/v3/documents/list":
            return httpx.Response(200, json={"memories": [
                {"id": "d1", "title": "Welcome to Circles", "status": "done",
                 "summary": "Circles is a currency framework",
                 "metadata": {"source": "resource", "kind": "url", "scope": "global",
                              "url": "https://docs.aboutcircles.com/"}},
                {"id": "d2", "title": "still indexing", "status": "queued",
                 "metadata": {"source": "resource", "kind": "text", "scope": "global"}},
                {"id": "d3", "title": "an agent learning", "status": "done",
                 "metadata": {"source": "learning"}},  # not a resource → filtered out
            ]})
        if path == "/v4/search":
            return httpx.Response(200, json={"results": [
                {"memory": "Circles uses an invitation system (96 CRC).", "similarity": 0.9}]})
        return httpx.Response(404)

    return Memory("http://sm:6767", "k", transport=httpx.MockTransport(handler))


async def test_knowledge_and_resource_tools(stack):
    db, settings, cfg = stack
    tools = make_chat_tools(db, settings, cfg, memory=_fake_memory())
    assert {"search_knowledge", "list_resources"} <= {t.name for t in tools}

    out = await _tool(tools, "list_resources").ainvoke({})
    assert "Welcome to Circles" in out
    assert "https://docs.aboutcircles.com/" in out
    assert "an agent learning" not in out          # non-resource docs excluded
    assert "queued" in out                          # freshness: indexing status surfaced

    out = await _tool(tools, "search_knowledge").ainvoke({"query": "invitation"})
    assert "invitation system" in out


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


async def test_same_session_requests_are_serialized(stack, monkeypatch):
    """Overlapping requests on one session_id must not run concurrently (else the
    read-modify-write of session history races and drops messages)."""
    import asyncio

    from langchain_core.messages import AIMessage

    gauge = {"in_flight": 0, "max": 0}

    class FakeAgent:
        async def ainvoke(self, payload, config=None):
            gauge["in_flight"] += 1
            gauge["max"] = max(gauge["max"], gauge["in_flight"])
            await asyncio.sleep(0.02)  # hold the "turn" open to expose overlap
            gauge["in_flight"] -= 1
            return {"messages": list(payload["messages"]) + [AIMessage(content="r")]}

    monkeypatch.setattr("sde_deepagent.chat.create_agent", lambda **kw: FakeAgent())
    db, settings, cfg = stack
    svc = ChatService(db, cfg, settings)

    await asyncio.gather(
        svc.ask("first", session_id="s1"),
        svc.ask("second", session_id="s1"),
    )
    assert gauge["max"] == 1  # serialized: never two turns in flight at once
    contents = [getattr(m, "content", None) for m in svc.sessions["s1"]]
    assert "first" in contents and "second" in contents  # neither message lost
