"""Mid-task steering (mailbox + check_messages tool + endpoint), per-repo
approval/sandbox resolution, and GitHub PR-review → auto-revision polling."""

from types import SimpleNamespace

import httpx
import pytest

import sde_deepagent.runner as runner_module
from sde_deepagent.agent_factory import make_check_messages_tool
from sde_deepagent.config import ConfigStore, RepoConfig
from sde_deepagent.db import Database
from sde_deepagent.gitops import run_cmd
from sde_deepagent.intake.github_reviews import GithubReviewIntake, parse_pr_url
from sde_deepagent.runner import TaskRunner
from sde_deepagent.settings import get_settings

# ---- mid-task steering ----

@pytest.fixture
async def runner(temp_env):
    settings = get_settings()
    db = Database(settings.db_path)
    await db.connect()
    r = TaskRunner(db, SimpleNamespace(publish=lambda *a: None),
                   ConfigStore(settings.config_dir), settings)
    yield r
    await db.close()


def test_steer_only_when_running(runner):
    assert runner.steer("ghost", "hi") is False  # no mailbox => not running
    runner.mailboxes["t1"] = []
    assert runner.steer("t1", "use tabs") is True
    assert runner.mailboxes["t1"] == ["use tabs"]


def test_drain_mailbox_clears(runner):
    runner.mailboxes["t1"] = ["a", "b"]
    assert runner._drain_mailbox("t1") == ["a", "b"]
    assert runner._drain_mailbox("t1") == []


async def test_check_messages_tool():
    box = {"m": ["fix the naming", "also add a test"]}
    tool = make_check_messages_tool(lambda: box.pop("m", []))
    out = await tool.ainvoke({})
    assert "fix the naming" in out and "add a test" in out
    out2 = await tool.ainvoke({})
    assert "No new operator messages" in out2


# ---- per-repo approval / sandbox resolution ----

def test_resolve_approval(runner):
    runner.settings.require_approval = False
    assert runner._resolve_approval(RepoConfig("a", "u", approval="required")) is True
    assert runner._resolve_approval(RepoConfig("a", "u", approval="auto")) is False
    assert runner._resolve_approval(RepoConfig("a", "u")) is False  # inherit default
    runner.settings.require_approval = True
    assert runner._resolve_approval(RepoConfig("a", "u")) is True
    assert runner._resolve_approval(RepoConfig("a", "u", approval="auto")) is False


def test_resolve_sandbox(runner):
    runner.settings.sandbox_default = False
    assert runner._resolve_sandbox(RepoConfig("a", "u", sandbox=True)) is True
    assert runner._resolve_sandbox(RepoConfig("a", "u")) is False
    runner.settings.sandbox_default = True
    assert runner._resolve_sandbox(RepoConfig("a", "u")) is True
    assert runner._resolve_sandbox(RepoConfig("a", "u", sandbox=False)) is False


async def test_runner_passes_effective_repo_approval_to_agent(runner, tmp_path, monkeypatch):
    origin = tmp_path / "origin"
    origin.mkdir()
    for args in (["git", "init", "-b", "main"],
                 ["git", "config", "user.email", "t@t"],
                 ["git", "config", "user.name", "t"]):
        await run_cmd(args, cwd=origin)
    (origin / "README.md").write_text("demo\n")
    await run_cmd(["git", "add", "-A"], cwd=origin)
    await run_cmd(["git", "commit", "-m", "init"], cwd=origin)

    runner.settings.require_approval = False
    runner.settings.sandbox_default = False
    runner.cfg.upsert_repo(RepoConfig("guarded", str(origin), approval="required"))
    task = await runner.db.create_task("guard this", "d", repo="guarded")
    captured = {}

    async def fake_build_agent(*args, **kwargs):
        captured["require_approval"] = kwargs["require_approval"]
        raise RuntimeError("stop after inspecting effective policy")

    monkeypatch.setattr(runner_module, "build_agent", fake_build_agent)
    await runner.run(task)
    assert captured["require_approval"] is True


# ---- steer endpoint ----

async def test_steer_endpoint(temp_env):
    from sde_deepagent.server import create_app

    app = create_app()
    async with httpx.ASGITransport(app=app) as transport:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://test") as client:
                t = (await client.post("/api/tasks", json={"title": "x"})).json()
                # task is queued, not running -> 409
                r = await client.post(f"/api/tasks/{t['id']}/steer",
                                      json={"message": "hello"})
                assert r.status_code == 409
                # simulate it running
                app.state.worker.runner.mailboxes[t["id"]] = []
                r = await client.post(f"/api/tasks/{t['id']}/steer",
                                      json={"message": "use tabs"})
                assert r.status_code == 200 and r.json()["queued"] is True
                assert app.state.worker.runner.mailboxes[t["id"]] == ["use tabs"]


# ---- github review polling ----

def test_parse_pr_url():
    assert parse_pr_url("https://github.com/acme/backend/pull/42") == \
        ("github.com", "acme", "backend", "42")
    assert parse_pr_url("https://ghe.corp.io/t/r/pull/7")[0] == "ghe.corp.io"
    assert parse_pr_url("not a pr") is None


@pytest.fixture
async def review_db(temp_env):
    db = Database(get_settings().db_path)
    await db.connect()
    yield db
    await db.close()


_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _patch_gh(monkeypatch, reviews):
    def handler(req):
        if req.url.path.endswith("/reviews"):
            return httpx.Response(200, json=reviews)
        return httpx.Response(200, json=[])  # comments endpoints empty
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler)))


async def test_review_polling_queues_revision(review_db, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "github_token", "ghp_x")
    parent = await review_db.create_task("Add feature", "do it", repo="backend")
    await review_db.update_task(parent.id, status="completed",
                                pr_url="https://github.com/acme/backend/pull/5",
                                finished_at=1.0)

    intake = GithubReviewIntake(settings, review_db)
    reviews = [{"submitted_at": "2026-06-12T10:00:00Z", "user": {"login": "alice"},
                "author_association": "MEMBER",
                "body": "Please rename the helper and add a test."}]
    _patch_gh(monkeypatch, reviews)
    await intake._poll_once()

    tasks = await review_db.list_tasks()
    revs = [t for t in tasks if t.parent_id == parent.id]
    assert len(revs) == 1
    assert "rename the helper" in revs[0].description
    assert revs[0].source == "github-review"

    # second poll with same feedback must not queue a duplicate (still open)
    await intake._poll_once()
    revs2 = [t for t in await review_db.list_tasks() if t.parent_id == parent.id]
    assert len(revs2) == 1


async def test_review_polling_ignores_bot_and_old(review_db, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "github_token", "ghp_x")
    parent = await review_db.create_task("t", "d", repo="backend")
    await review_db.update_task(parent.id, status="completed",
                                pr_url="https://github.com/acme/backend/pull/9",
                                finished_at=1000.0)
    intake = GithubReviewIntake(settings, review_db)
    reviews = [
        {"submitted_at": "2026-06-12T10:00:00Z", "user": {"login": "ci[bot]"},
         "author_association": "MEMBER", "body": "automated"},  # bot -> ignored
        {"submitted_at": "1970-01-01T00:00:00Z", "user": {"login": "bob"},
         "author_association": "MEMBER", "body": "old"},  # before floor -> ignored
        {"submitted_at": "2026-06-12T11:00:00Z", "user": {"login": "outsider"},
         "author_association": "NONE", "body": "run my request"},
    ]
    _patch_gh(monkeypatch, reviews)
    await intake._poll_once()
    assert not [t for t in await review_db.list_tasks() if t.parent_id == parent.id]
