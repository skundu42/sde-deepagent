"""Builds a real deep agent (no model calls) against a real git workspace to
verify the deepagents wiring: backend, subagents, prompt formatting, PR tool."""

import pytest

import sde_deepagent.agent_factory as agent_factory
from sde_deepagent.agent_factory import build_agent
from sde_deepagent.config import ConfigStore, RepoConfig
from sde_deepagent.gitops import prepare_workspace, run_cmd
from sde_deepagent.settings import get_settings


async def _make_ws(tmp_path, settings):
    origin = tmp_path / "origin"
    origin.mkdir()
    for args in (["git", "init", "-b", "main"],
                 ["git", "config", "user.email", "t@t"],
                 ["git", "config", "user.name", "t"]):
        await run_cmd(args, cwd=origin)
    (origin / "README.md").write_text("# demo\n")
    await run_cmd(["git", "add", "-A"], cwd=origin)
    await run_cmd(["git", "commit", "-m", "init"], cwd=origin)
    repo = RepoConfig(name="demo", url=str(origin), default_branch="main")
    return await prepare_workspace("t9", "Do it", repo, settings)


async def test_build_agent_passes_checkpointer_through(
        temp_env, dummy_keys, tmp_path, monkeypatch):
    # the checkpointer is handed straight to the graph so agent state can be
    # persisted for resume-after-restart (summarization is already on by default)
    settings = get_settings()
    ws = await _make_ws(tmp_path, settings)
    cfg = ConfigStore(settings.config_dir)

    captured: dict = {}
    monkeypatch.setattr(agent_factory, "create_deep_agent",
                        lambda **kw: captured.update(kw) or object())
    sentinel_ckpt = object()

    await build_agent(ws, "Do it", cfg.agents(), settings, checkpointer=sentinel_ckpt)

    assert captured.get("checkpointer") is sentinel_ckpt


async def test_real_graph_persists_and_resumes_checkpoint(
        temp_env, dummy_keys, tmp_path, monkeypatch):
    # build a REAL deepagents graph (fake tool-capable model, no network) and prove
    # the checkpointer wiring actually persists state and resumes cleanly — the
    # fake-agent unit tests cannot exercise the real langgraph checkpoint cycle
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.memory import InMemorySaver

    class FakeTooling(GenericFakeChatModel):
        def bind_tools(self, *a, **k):  # the deep agent binds its tools
            return self

    settings = get_settings()
    ws = await _make_ws(tmp_path, settings)
    cfg = ConfigStore(settings.config_dir)
    fake = FakeTooling(messages=iter([AIMessage(content="all done")] * 20))
    monkeypatch.setattr(agent_factory, "build_model", lambda *a, **k: fake)
    saver = InMemorySaver()
    built = await build_agent(ws, "do it", cfg.agents(), settings, checkpointer=saver)

    thread = {"configurable": {"thread_id": "tA"}, "recursion_limit": 6}
    async for _ in built.agent.astream(
            {"messages": [{"role": "user", "content": "go"}]},
            stream_mode="updates", subgraphs=True, config=thread):
        pass
    # state was actually checkpointed under the thread id
    assert await saver.aget_tuple({"configurable": {"thread_id": "tA"}}) is not None
    # resuming the completed thread with input=None runs cleanly (no re-seed/crash)
    resumed = [c async for c in built.agent.astream(
        None, stream_mode="updates", subgraphs=True, config=thread)]
    assert resumed == []


@pytest.fixture
def dummy_keys(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-dummy")
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy")


async def test_build_agent(temp_env, dummy_keys, tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    for args in (["git", "init", "-b", "main"],
                 ["git", "config", "user.email", "t@t"],
                 ["git", "config", "user.name", "t"]):
        await run_cmd(args, cwd=origin)
    (origin / "README.md").write_text("# demo\n")
    (origin / "AGENTS.md").write_text("Always use tabs.\n")
    await run_cmd(["git", "add", "-A"], cwd=origin)
    await run_cmd(["git", "commit", "-m", "init"], cwd=origin)

    settings = get_settings()
    # add a company context doc
    settings.context_dir.mkdir(parents=True, exist_ok=True)
    (settings.context_dir / "style.md").write_text("Company style guide.")

    cfg = ConfigStore(settings.config_dir)
    repo = RepoConfig(name="demo", url=str(origin), default_branch="main",
                      description="demo repo", test="echo ok")
    ws = await prepare_workspace("t1", "Add thing", repo, settings)

    built = await build_agent(ws, "Add the thing", cfg.agents(), settings)

    assert built.agent is not None
    assert built.result == {"pr_url": None, "pr_title": None, "pr_body": None}
    # company context was mounted into the workspace and git-excluded
    assert (ws.path / "_context" / "style.md").exists()
    exclude = (ws.path / ".git" / "info" / "exclude").read_text()
    assert "_context/" in exclude
    # deepagents' summarization offloads history into the repo root — keep it
    # out of the agent's commits/PR
    assert "conversation_history/" in exclude

    # the orchestrator graph exposes the expected toolset
    graph = built.agent.get_graph()
    assert "tools" in graph.nodes


async def test_prompt_contains_task_and_context(temp_env, dummy_keys, tmp_path):
    from sde_deepagent.context import build_context_block
    from sde_deepagent.prompts import ORCHESTRATOR_PROMPT

    origin = tmp_path / "o2"
    origin.mkdir()
    (origin / "AGENTS.md").write_text("Use 4-space indents.")
    repo = RepoConfig(name="demo", url=str(origin), default_branch="main")
    block = build_context_block(origin, repo, get_settings())
    assert "Use 4-space indents." in block

    from sde_deepagent.prompts import SANDBOX_ENV_PROMPT, SHIP_NORMAL

    rendered = ORCHESTRATOR_PROMPT.format(
        branch="agent/x", repo_name="demo", repo_description="d",
        default_branch="main", setup_cmd="-", test_cmd="pytest",
        task_description="Fix it", context_block=block,
        ship_instructions=SHIP_NORMAL, repo_map="(map)",
        exec_environment=SANDBOX_ENV_PROMPT,
    )
    assert "Fix it" in rendered and "agent/x" in rendered
    assert "open_pull_request" in rendered and "(map)" in rendered
    # the agent is told to bootstrap its own environment in the container
    assert "install" in rendered and "REUSED" in rendered
