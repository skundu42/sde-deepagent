"""The five capability upgrades: junk-file guard, PR revision, approval gate,
reasoning effort, repo map."""

from types import SimpleNamespace

import httpx
import pytest

from sde_deepagent.bus import EventBus
from sde_deepagent.config import AgentsConfig, ConfigStore, RepoConfig, SubagentConfig
from sde_deepagent.context import build_repo_map
from sde_deepagent.db import Database
from sde_deepagent.gitops import git, prepare_workspace, run_cmd
from sde_deepagent.llm import build_model
from sde_deepagent.runner import TaskRunner
from sde_deepagent.settings import get_settings


async def make_origin(path, files=None):
    path.mkdir(parents=True, exist_ok=True)
    for args in (["git", "init", "-b", "main"], ["git", "config", "user.email", "t@t"],
                 ["git", "config", "user.name", "t"]):
        await run_cmd(args, cwd=path)
    for name, content in (files or {"README.md": "# demo\n"}).items():
        p = path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    await run_cmd(["git", "add", "-A"], cwd=path)
    await run_cmd(["git", "commit", "-m", "init"], cwd=path)


# ---- 1. junk-file guard ----

async def test_pycache_never_staged(temp_env, tmp_path):
    origin = tmp_path / "o"
    await make_origin(origin)
    repo = RepoConfig(name="demo", url=str(origin), default_branch="main")
    ws = await prepare_workspace("j1", "junk test", repo, get_settings())

    cache = ws.path / "__pycache__"
    cache.mkdir()
    (cache / "mod.cpython-312.pyc").write_bytes(b"\x00")
    (ws.path / ".DS_Store").write_bytes(b"\x00")
    (ws.path / "real_change.py").write_text("x = 1\n")

    status = await git(["status", "--porcelain"], cwd=ws.path)
    assert "real_change.py" in status
    assert "__pycache__" not in status and ".DS_Store" not in status


# ---- 2. revision tasks reuse the branch ----

async def test_prepare_workspace_existing_branch(temp_env, tmp_path):
    origin = tmp_path / "o2"
    await make_origin(origin)
    # simulate prior agent work pushed to origin
    await git(["checkout", "-b", "agent/old1-prior-work"], cwd=origin)
    (origin / "feature.py").write_text("done = True\n")
    await run_cmd(["git", "add", "-A"], cwd=origin)
    await run_cmd(["git", "commit", "-m", "prior work"], cwd=origin)
    await git(["checkout", "main"], cwd=origin)

    repo = RepoConfig(name="demo", url=str(origin), default_branch="main")
    ws = await prepare_workspace("rev1", "Revise: prior work", repo, get_settings(),
                                 existing_branch="agent/old1-prior-work")
    assert ws.branch == "agent/old1-prior-work"
    assert (ws.path / "feature.py").exists()  # prior commits present
    head = await git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=ws.path)
    assert head.strip() == "agent/old1-prior-work"


async def test_revision_missing_branch_fails(temp_env, tmp_path):
    from sde_deepagent.gitops import GitError

    origin = tmp_path / "o3"
    await make_origin(origin)
    repo = RepoConfig(name="demo", url=str(origin), default_branch="main")
    with pytest.raises(GitError, match="not found"):
        await prepare_workspace("rev2", "x", repo, get_settings(),
                                existing_branch="agent/ghost-branch")


# ---- 3. approval gate ----

@pytest.fixture
async def runner_stack(temp_env):
    settings = get_settings()
    db = Database(settings.db_path)
    await db.connect()
    cfg = ConfigStore(settings.config_dir)
    runner = TaskRunner(db, EventBus(), cfg, settings)
    yield runner, db, settings
    await db.close()


async def test_request_approval_holds_work(runner_stack, tmp_path):
    runner, db, settings = runner_stack
    settings.require_approval = True
    origin = tmp_path / "o4"
    await make_origin(origin)
    repo = RepoConfig(name="demo", url=str(origin), default_branch="main")
    task = await db.create_task("guarded change", "d", repo="demo")
    ws = await prepare_workspace(task.id, task.title, repo, settings)
    (ws.path / "new.py").write_text("a = 1\n")

    built = SimpleNamespace(workspace=ws, result={"pr_title": "My PR",
                                                  "pr_body": "body text"})
    held = await runner._request_approval(task, built, "did the thing")
    assert held is True
    fetched = await db.get_task(task.id)
    assert fetched.status == "awaiting_approval"
    events = await db.list_events(task.id)
    prop = next(e for e in events if e["kind"] == "approval_request")
    assert prop["content"]["title"] == "My PR"
    assert "new.py" in prop["content"]["diff_stat"]
    # nothing reached origin
    out = await git(["branch", "-a"], cwd=origin)
    assert "agent/" not in out
    # protected from pruning
    assert task.id in await runner._protected_workspaces()


async def test_request_approval_no_changes_completes(runner_stack, tmp_path):
    runner, db, settings = runner_stack
    settings.require_approval = True
    origin = tmp_path / "o5"
    await make_origin(origin)
    repo = RepoConfig(name="demo", url=str(origin), default_branch="main")
    task = await db.create_task("no-op", "d", repo="demo")
    ws = await prepare_workspace(task.id, task.title, repo, settings)
    built = SimpleNamespace(workspace=ws, result={})
    assert await runner._request_approval(task, built, "nothing to do") is False


async def test_approve_endpoint_ships(temp_env, tmp_path, monkeypatch):
    from sde_deepagent.server import create_app

    origin = tmp_path / "o6"
    await make_origin(origin)
    app = create_app()
    async with httpx.ASGITransport(app=app) as transport:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://test") as client:
                await client.post("/api/repos", json={"name": "demo",
                                                      "url": str(origin)})
                db = app.state.db
                settings = app.state.settings
                repo = RepoConfig(name="demo", url=str(origin),
                                  default_branch="main")
                task = await db.create_task("held work", "d", repo="demo")
                ws = await prepare_workspace(task.id, task.title, repo, settings)
                (ws.path / "shipped.py").write_text("ok = True\n")
                from sde_deepagent.gitops import commit_all
                await commit_all(ws, "the work")
                await db.update_task(task.id, status="awaiting_approval",
                                     branch=ws.branch)
                await db.add_event(task.id, "approval_request",
                                   {"title": "Ship it", "body": "b"})

                # reject path guards status
                r = await client.post(f"/api/tasks/{task.id}/approve")
                assert r.status_code == 200
                body = r.json()
                assert body["status"] == "completed"
                # local remote: branch pushed, PR not possible
                assert body["pr_url"] is None
                out = await git(["branch", "-a"], cwd=origin)
                assert ws.branch in out

                r = await client.post(f"/api/tasks/{task.id}/approve")
                assert r.status_code == 409  # already completed

                r = await client.post(f"/api/tasks/{task.id}/reject")
                assert r.status_code == 409


# ---- 4. reasoning effort ----

def test_build_model_effort_per_provider(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setenv("GOOGLE_API_KEY", "g-x")

    m = build_model("openai:gpt-5.4", effort="high")
    assert m.reasoning_effort == "high"
    assert m.use_responses_api is True  # chat/completions 400s on effort+tools
    m = build_model("anthropic:claude-sonnet-4-6", effort="medium")
    assert m.effort == "medium"
    m = build_model("google_genai:gemini-2.5-pro", effort="medium")
    assert m.thinking_level == "high"  # gemini has low/high only; medium rounds up
    m = build_model("google_genai:gemini-2.5-pro", effort="low")
    assert m.thinking_level == "low"
    with pytest.raises(ValueError, match="effort"):
        build_model("openai:gpt-5.4", effort="maximum")


def test_validate_models_checks_effort():
    from sde_deepagent.agent_factory import validate_models

    cfg = AgentsConfig(
        orchestrator_model="openai:gpt-5.4", orchestrator_effort="ultra",
        orchestrator_prompt=None,
        subagents=[SubagentConfig(name="coder", model="openai:gpt-5.4",
                                  effort="low")],
        mcp_servers={})
    errors = validate_models(cfg)
    assert len(errors) == 1 and "orchestrator" in errors[0] and "ultra" in errors[0]


# ---- 5. repo map ----

def test_repo_map_lists_files_and_symbols(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "core.py").write_text(
        "class Engine:\n    pass\n\ndef start():\n    pass\n\nasync def stop():\n    pass\n")
    (tmp_path / "app.js").write_text("export function main() {}\nclass Widget {}\n")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "junk.pyc").write_bytes(b"\x00")
    (tmp_path / "README.md").write_text("hi\n")

    out = build_repo_map(tmp_path)
    assert "pkg/core.py" in out and "Engine, start, stop" in out
    assert "main, Widget" in out
    assert "README.md" in out
    assert "__pycache__" not in out


def test_repo_map_caps_size(tmp_path):
    for i in range(600):
        (tmp_path / f"f{i:03}.txt").write_text("x")
    out = build_repo_map(tmp_path)
    assert len(out) < 8000
    assert "first 400 files" in out or "truncated" in out
