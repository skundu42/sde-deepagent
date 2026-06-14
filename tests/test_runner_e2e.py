"""End-to-end exercise of TaskRunner.run(): a fake agent stream is driven
through the *real* orchestration loop against a *real* local git origin, so the
streaming → event persistence → usage tracking → finalize → push → completion
path runs for real. This is the one place the loop itself (not its parts in
isolation) is covered.

The agent is faked — but build_agent is the only seam mocked; everything after
it (event DB writes, CostTracker, commit/push to a real remote, status
transitions) is the production code path.
"""

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage

import sde_deepagent.runner as runner_module
from sde_deepagent.agent_factory import BuiltAgent
from sde_deepagent.config import ConfigStore, RepoConfig
from sde_deepagent.db import Database
from sde_deepagent.gitops import run_cmd
from sde_deepagent.pricing import lookup_price
from sde_deepagent.runner import TaskRunner
from sde_deepagent.settings import get_settings


@pytest.fixture
async def runner(temp_env):
    settings = get_settings()
    db = Database(settings.db_path)
    await db.connect()
    r = TaskRunner(db, SimpleNamespace(publish=lambda *a: None),
                   ConfigStore(settings.config_dir), settings)
    yield r
    await db.close()


async def _make_origin(tmp_path):
    """A real local git repo with one commit, usable as a clone/push remote."""
    origin = tmp_path / "origin"
    origin.mkdir()
    for args in (["git", "init", "-b", "main"],
                 ["git", "config", "user.email", "t@t"],
                 ["git", "config", "user.name", "t"]):
        await run_cmd(args, cwd=origin)
    (origin / "README.md").write_text("demo\n")
    await run_cmd(["git", "add", "-A"], cwd=origin)
    await run_cmd(["git", "commit", "-m", "init"], cwd=origin)
    return origin


def _ai(msg_id, text, *, tool_calls=None, usage):
    return AIMessage(
        id=msg_id, content=text, tool_calls=tool_calls or [],
        usage_metadata={**usage, "total_tokens": usage["input_tokens"]
                        + usage["output_tokens"]},
        response_metadata={"model_name": "claude-sonnet-4-6"})


class _FakeAgent:
    """Mimics the deepagents graph: astream() yields (namespace, update) chunks
    the way langgraph does with stream_mode='updates', subgraphs=True."""

    def __init__(self, ws):
        self.ws = ws
        self.calls: list = []  # (input, config) for each astream invocation

    def astream(self, _input, **kwargs):
        self.calls.append((_input, kwargs.get("config")))
        return self._run()

    async def _run(self):
        root, sub = (), ("task:call_1",)  # orchestrator ns vs. delegated-coder ns
        # orchestrator plans, then delegates the edit to the coder subagent
        yield (root, {"orchestrator": {"todos": [
            {"content": "implement feature", "status": "in_progress"}]}})
        yield (root, {"orchestrator": {"messages": [_ai(
            "m1", "I'll delegate the edit to the coder.",
            tool_calls=[{"name": "task", "id": "call_1", "type": "tool_call",
                         "args": {"subagent_type": "coder", "description": "add feature.py"}}],
            usage={"input_tokens": 1000, "output_tokens": 200})]}})
        # the coder makes a real change in the workspace, then reports a tool call
        (self.ws.path / "feature.py").write_text("x = 1\n")
        yield (sub, {"coder": {"messages": [_ai(
            "m2", "Adding feature.py",
            tool_calls=[{"name": "write_file", "id": "call_2", "type": "tool_call",
                         "args": {"path": "feature.py", "content": "x = 1\n"}}],
            usage={"input_tokens": 500, "output_tokens": 150})]}})
        yield (sub, {"tools": {"messages": [ToolMessage(
            id="m3", content="wrote feature.py", name="write_file",
            tool_call_id="call_2")]}})
        # delegation returns to the orchestrator, which gives the final summary
        yield (root, {"orchestrator": {"messages": [ToolMessage(
            id="m4", content="coder finished", name="task", tool_call_id="call_1")]}})
        yield (root, {"orchestrator": {"messages": [_ai(
            "m5", "Done. Added feature.py with the new behavior.",
            usage={"input_tokens": 800, "output_tokens": 150})]}})


async def test_run_end_to_end_completes_tracks_cost_and_pushes(runner, tmp_path, monkeypatch):
    origin = await _make_origin(tmp_path)
    runner.settings.sandbox_default = False  # no Docker in this path
    runner.settings.auto_finalize = True
    runner.settings.require_approval = False
    runner.cfg.upsert_repo(RepoConfig("demo", str(origin)))
    task = await runner.db.create_task("Add feature", "implement the new feature",
                                       repo="demo")

    async def fake_build_agent(ws, task_description, agents_cfg, settings, **kwargs):
        return BuiltAgent(agent=_FakeAgent(ws), workspace=ws, result={})

    monkeypatch.setattr(runner_module, "build_agent", fake_build_agent)

    out = await runner.run(task)

    # --- task reached completion ---
    assert out.status == "completed"
    refreshed = await runner.db.get_task(task.id)
    assert refreshed.status == "completed"

    # --- usage was tracked across orchestrator + subagent and persisted ---
    assert refreshed.input_tokens == 2300   # 1000 + 500 + 800
    assert refreshed.output_tokens == 500   # 200 + 150 + 150
    p_in, p_out = lookup_price("claude-sonnet-4-6")
    expected_cost = round((2300 * p_in + 500 * p_out) / 1_000_000, 6)
    assert refreshed.cost_usd == pytest.approx(expected_cost)
    assert refreshed.cost_usd > 0

    # --- every step was streamed to the event log ---
    events = await runner.db.list_events(task.id)
    kinds = [e["kind"] for e in events]
    assert "todos" in kinds
    assert "tool_call" in kinds
    assert "tool_result" in kinds
    messages = [e["content"].get("text", "") for e in events if e["kind"] == "message"]
    assert any("delegate the edit" in m for m in messages)

    # --- subagent attribution: the coder's work is logged under "coder" ---
    coder_events = [e for e in events if e["agent"] == "coder"]
    assert coder_events
    assert any(e["kind"] == "tool_result" and e["content"].get("name") == "write_file"
               for e in coder_events)

    # --- completion status carries the orchestrator's final summary ---
    completed = [e for e in events
                 if e["kind"] == "status" and e["content"].get("status") == "completed"]
    assert completed and "Added feature.py" in completed[0]["content"]["summary"]

    # --- finalize really committed + pushed the change to the origin remote ---
    branch = refreshed.branch
    assert branch and branch.startswith("agent/")
    code, _ = await run_cmd(["git", "rev-parse", "--verify", f"refs/heads/{branch}"],
                            cwd=origin)
    assert code == 0  # the origin received the pushed branch
    code, listing = await run_cmd(["git", "ls-tree", "--name-only", branch], cwd=origin)
    assert code == 0 and "feature.py" in listing  # the agent's change shipped


# ---- checkpointer / resume-after-restart ----

@pytest.fixture
async def ck_runner(temp_env):
    from langgraph.checkpoint.memory import InMemorySaver
    settings = get_settings()
    db = Database(settings.db_path)
    await db.connect()
    r = TaskRunner(db, SimpleNamespace(publish=lambda *a: None),
                   ConfigStore(settings.config_dir), settings,
                   checkpointer=InMemorySaver())
    yield r
    await db.close()


def _spy_build_agent(monkeypatch, sink):
    async def fake_build_agent(ws, *a, **k):
        ag = _FakeAgent(ws)
        sink.append(ag)
        return BuiltAgent(agent=ag, workspace=ws, result={})
    monkeypatch.setattr(runner_module, "build_agent", fake_build_agent)


async def test_run_passes_thread_id_when_checkpointer_present(ck_runner, tmp_path, monkeypatch):
    origin = await _make_origin(tmp_path)
    ck_runner.settings.sandbox_default = False
    ck_runner.cfg.upsert_repo(RepoConfig("demo", str(origin)))
    task = await ck_runner.db.create_task("Add feature", "do it", repo="demo")
    agents: list = []
    _spy_build_agent(monkeypatch, agents)

    await ck_runner.run(task)

    stream_input, config = agents[0].calls[0]
    assert config["configurable"]["thread_id"] == task.id  # checkpoints keyed per task
    assert stream_input is not None  # a fresh run seeds the conversation


async def test_run_resumes_from_existing_checkpoint(ck_runner, tmp_path, monkeypatch):
    from langgraph.checkpoint.base import empty_checkpoint

    origin = await _make_origin(tmp_path)
    ck_runner.settings.sandbox_default = False
    ck_runner.cfg.upsert_repo(RepoConfig("demo", str(origin)))
    task = await ck_runner.db.create_task("Add feature", "do it", repo="demo")
    agents: list = []
    _spy_build_agent(monkeypatch, agents)

    # first run leaves the workspace clone on disk (where the agent's edits live)
    await ck_runner.run(task)
    assert agents[0].calls[0][0] is not None  # fresh start

    # simulate an interruption that left a durable checkpoint for this task's thread
    await ck_runner.checkpointer.aput(
        {"configurable": {"thread_id": task.id, "checkpoint_ns": ""}},
        empty_checkpoint(), {"source": "test"}, {})

    # re-run as the worker would after re-queueing on restart: resume, don't restart
    await ck_runner.db.update_task(task.id, status="queued")
    await ck_runner.run(task)

    resumed_input, resumed_cfg = agents[1].calls[0]
    assert resumed_input is None  # continues the persisted thread instead of reseeding
    assert resumed_cfg["configurable"]["thread_id"] == task.id
    events = await ck_runner.db.list_events(task.id)
    assert any(e["kind"] == "log" and "resuming from checkpoint" in e["content"].get("text", "")
               for e in events)
    # once the resumed run finishes, its checkpoint is dropped (bounded growth)
    assert await ck_runner.checkpointer.aget_tuple(
        {"configurable": {"thread_id": task.id}}) is None


async def test_finalize_returns_persisted_pr_url_on_resume(runner):
    # on resume built.result is a fresh empty dict; _finalize must still see the
    # PR the interrupted run already opened (via task.pr_url) and not re-finalize
    task = await runner.db.create_task("t", "d", repo="demo")
    url = "https://github.com/acme/x/pull/7"
    task.pr_url = url
    built = SimpleNamespace(workspace=None, result={"pr_url": None})
    assert await runner._finalize(task, built) == url  # returns before touching git


class _UnpricedFakeAgent:
    """One AI message reporting a model that isn't in the price table."""

    def __init__(self, ws):
        self.ws = ws

    def astream(self, _input, **_kwargs):
        return self._run()

    async def _run(self):
        yield ((), {"orchestrator": {"messages": [AIMessage(
            id="u1", content="done",
            usage_metadata={"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
            response_metadata={"model_name": "totally-unknown-model-x"})]}})


async def test_unpriced_model_emits_warning_and_is_billed(runner, tmp_path, monkeypatch):
    origin = await _make_origin(tmp_path)
    runner.settings.sandbox_default = False
    runner.cfg.upsert_repo(RepoConfig("demo", str(origin)))
    task = await runner.db.create_task("t", "d", repo="demo")

    async def fake_build_agent(ws, *a, **k):
        return BuiltAgent(agent=_UnpricedFakeAgent(ws), workspace=ws, result={})
    monkeypatch.setattr(runner_module, "build_agent", fake_build_agent)

    await runner.run(task)

    events = await runner.db.list_events(task.id)
    assert any(e["kind"] == "log" and "not in the price table" in e["content"].get("text", "")
               for e in events)  # operator is told the model is unpriced
    refreshed = await runner.db.get_task(task.id)
    assert refreshed.cost_usd > 0  # billed at the fail-safe rate, not $0


async def test_run_clears_stale_checkpoint_when_workspace_gone(ck_runner, tmp_path, monkeypatch):
    from langgraph.checkpoint.base import empty_checkpoint

    origin = await _make_origin(tmp_path)
    ck_runner.settings.sandbox_default = False
    ck_runner.cfg.upsert_repo(RepoConfig("demo", str(origin)))
    task = await ck_runner.db.create_task("Add feature", "do it", repo="demo")
    agents: list = []
    _spy_build_agent(monkeypatch, agents)

    # a checkpoint exists but the workspace never did -> cannot resume coherently;
    # the runner must clear the stale thread and start fresh
    cfg = {"configurable": {"thread_id": task.id, "checkpoint_ns": ""}}
    await ck_runner.checkpointer.aput(cfg, empty_checkpoint(), {"source": "test"}, {})
    assert await ck_runner.checkpointer.aget_tuple(cfg) is not None

    await ck_runner.run(task)

    stream_input, _ = agents[0].calls[0]
    assert stream_input is not None  # fresh start, not a resume
    assert await ck_runner.checkpointer.aget_tuple(cfg) is None  # stale state cleared
