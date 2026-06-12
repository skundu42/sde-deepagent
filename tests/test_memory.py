"""Supermemory client + agent memory tools, against a mocked HTTP transport."""

import json

import httpx
import pytest

from sde_deepagent.agent_factory import make_memory_tools
from sde_deepagent.memory import GLOBAL_TAG, Memory, memory_from_settings, repo_tag
from sde_deepagent.settings import Settings


def make_transport(state: dict) -> httpx.MockTransport:
    """Fake supermemory server: records adds, serves canned search results."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        state.setdefault("auth", request.headers.get("authorization"))
        if request.url.path == "/v3/documents":
            state.setdefault("added", []).append(body)
            return httpx.Response(200, json={"id": f"mem_{len(state['added'])}",
                                             "status": "queued"})
        if request.url.path == "/v4/search":
            tag = body["containerTag"]
            results = state.get("search_results", {}).get(tag, [])
            return httpx.Response(200, json={"results": results, "total": len(results)})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.fixture
def state() -> dict:
    return {
        "search_results": {
            repo_tag("backend"): [
                {"memory": "Tests must run with uv run pytest -x", "similarity": 0.9},
                {"memory": "Auth lives in src/auth/", "similarity": 0.7},
            ],
            GLOBAL_TAG: [
                {"memory": "Company uses conventional commits", "similarity": 0.8},
            ],
        }
    }


@pytest.fixture
def memory(state) -> Memory:
    return Memory("http://sm:6767", "sm_test", transport=make_transport(state))


def test_repo_tag_sanitized():
    assert repo_tag("backend") == "devagent_repo_backend"
    assert repo_tag("my repo/x!") == "devagent_repo_my-repo-x-"


async def test_add(memory, state):
    mem_id = await memory.add("a fact", repo_tag("backend"), metadata={"task_id": "t1"})
    assert mem_id == "mem_1"
    assert state["auth"] == "Bearer sm_test"
    added = state["added"][0]
    assert added["containerTag"] == "devagent_repo_backend"
    assert added["metadata"]["task_id"] == "t1"


async def test_search_merges_and_sorts(memory):
    results = await memory.search("anything", [repo_tag("backend"), GLOBAL_TAG], limit=2)
    assert len(results) == 2
    assert results[0]["memory"].startswith("Tests must run")   # 0.9 first
    assert results[1]["memory"].startswith("Company uses")     # 0.8 second
    assert results[1]["container"] == GLOBAL_TAG


async def test_failures_degrade_gracefully():
    def boom(request):
        raise httpx.ConnectError("down")

    mem = Memory("http://sm:6767", "k", transport=httpx.MockTransport(boom))
    assert await mem.add("x", "tag") is None
    assert await mem.search("x", ["tag"]) == []
    assert await mem.ping() is False


def test_memory_from_settings_gating(monkeypatch):
    s = Settings(supermemory_base_url=None, supermemory_api_key=None)
    assert memory_from_settings(s) is None
    s = Settings(supermemory_base_url="http://localhost:6767", supermemory_api_key="sm_x")
    assert memory_from_settings(s) is not None


async def test_memory_tools(memory, state):
    search_memory, save_memory = make_memory_tools(memory, "backend", "task42")

    out = await search_memory.ainvoke({"query": "how to run tests", "scope": "all"})
    assert "[repo] Tests must run" in out
    assert "[global] Company uses conventional commits" in out

    out = await search_memory.ainvoke({"query": "x", "scope": "global"})
    assert "[repo]" not in out and "[global]" in out

    out = await save_memory.ainvoke({"content": "CI needs FOO=1", "scope": "repo"})
    assert out == "Saved to repo memory."
    saved = state["added"][-1]
    assert saved["containerTag"] == repo_tag("backend")
    assert saved["metadata"] == {"task_id": "task42", "repo": "backend",
                                 "source": "agent"}

    await save_memory.ainvoke({"content": "org-wide fact", "scope": "global"})
    assert state["added"][-1]["containerTag"] == GLOBAL_TAG


async def test_search_tool_no_results():
    def empty(request):
        return httpx.Response(200, json={"results": []})

    mem = Memory("http://sm", "k", transport=httpx.MockTransport(empty))
    search_memory, _ = make_memory_tools(mem, "backend", "t")
    out = await search_memory.ainvoke({"query": "anything"})
    assert out == "No relevant memories found."
