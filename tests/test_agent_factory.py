"""Builds a real deep agent (no model calls) against a real git workspace to
verify the deepagents wiring: backend, subagents, prompt formatting, PR tool."""

import pytest

from sde_deepagent.agent_factory import build_agent
from sde_deepagent.config import ConfigStore, RepoConfig
from sde_deepagent.gitops import prepare_workspace, run_cmd
from sde_deepagent.settings import get_settings


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
