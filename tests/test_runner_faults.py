"""Fault injection for TaskRunner.run(): transient-error retry, mid-run usage
persistence, daily-cap requeue, and the teardown handlers (cancel / budget /
timeout / crash). Complements test_runner_e2e.py, which covers the happy path.
"""

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

import sde_deepagent.runner as runner_module
from sde_deepagent.agent_factory import BuiltAgent
from sde_deepagent.config import ConfigStore, RepoConfig
from sde_deepagent.db import Database
from sde_deepagent.gitops import run_cmd
from sde_deepagent.runner import TaskRunner, is_transient_llm_error
from sde_deepagent.settings import get_settings


class FakeOverloaded(Exception):
    """Shape of a provider 529/overloaded error (duck-typed status_code)."""

    status_code = 529


class FakeRateLimited(Exception):
    status_code = 429


async def _make_origin(tmp_path):
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


def _usage_msg(msg_id: str, tokens: int = 1000) -> AIMessage:
    return AIMessage(
        id=msg_id, content=f"step {msg_id}",
        usage_metadata={"input_tokens": tokens, "output_tokens": 100,
                        "total_tokens": tokens + 100},
        response_metadata={"model_name": "claude-sonnet-4-6"})


class ScriptedAgent:
    """astream() runs a scripted behavior per attempt: a list whose entries are
    either an exception type to raise after one usage message, or None to
    complete cleanly."""

    def __init__(self, ws, script, checkpointer=None, thread_id=None):
        self.ws = ws
        self.script = list(script)
        self.calls = []  # (input, config) per astream() attempt
        self.checkpointer = checkpointer
        self.thread_id = thread_id

    def astream(self, _input, **kwargs):
        self.calls.append((_input, kwargs.get("config")))
        return self._run(len(self.calls) - 1)

    async def _run(self, attempt):
        if self.checkpointer is not None:
            # simulate langgraph having persisted progress for this thread
            # (before the yield: budget stops raise while the generator is
            # suspended there, and the checkpoint must already exist by then)
            from langgraph.checkpoint.base import empty_checkpoint
            await self.checkpointer.aput(
                {"configurable": {"thread_id": self.thread_id, "checkpoint_ns": ""}},
                empty_checkpoint(), {"source": "test"}, {})
        yield ((), {"orchestrator": {"messages": [_usage_msg(f"m{attempt}")]}})
        action = self.script[min(attempt, len(self.script) - 1)]
        if action is not None:
            raise action("provider fell over")
        yield ((), {"orchestrator": {"messages": [AIMessage(
            id=f"final{attempt}", content="Done.",
            usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            response_metadata={"model_name": "claude-sonnet-4-6"})]}})


@pytest.fixture
async def ck_runner(temp_env, monkeypatch):
    from langgraph.checkpoint.memory import InMemorySaver
    monkeypatch.setattr(runner_module, "TRANSIENT_BACKOFF_SECONDS", (0, 0, 0))
    settings = get_settings()
    db = Database(settings.db_path)
    await db.connect()
    r = TaskRunner(db, SimpleNamespace(publish=lambda *a: None),
                   ConfigStore(settings.config_dir), settings,
                   checkpointer=InMemorySaver())
    yield r
    await db.close()


@pytest.fixture
async def plain_runner(temp_env, monkeypatch):
    monkeypatch.setattr(runner_module, "TRANSIENT_BACKOFF_SECONDS", (0, 0, 0))
    settings = get_settings()
    db = Database(settings.db_path)
    await db.connect()
    r = TaskRunner(db, SimpleNamespace(publish=lambda *a: None),
                   ConfigStore(settings.config_dir), settings)
    yield r
    await db.close()


def _wire_agent(monkeypatch, factory):
    agents = []

    async def fake_build_agent(ws, *a, **k):
        ag = factory(ws)
        agents.append(ag)
        return BuiltAgent(agent=ag, workspace=ws, result={})

    monkeypatch.setattr(runner_module, "build_agent", fake_build_agent)
    return agents


async def _demo_task(r, tmp_path):
    origin = await _make_origin(tmp_path)
    r.settings.sandbox_default = False
    r.cfg.upsert_repo(RepoConfig("demo", str(origin)))
    return await r.db.create_task("t", "d", repo="demo")


# ---- transient-error classification ----

def test_transient_predicate():
    assert is_transient_llm_error(FakeOverloaded())
    assert is_transient_llm_error(FakeRateLimited())
    assert is_transient_llm_error(ConnectionResetError("peer"))
    # wrapped one level down (SDKs chain the transport error)
    outer = RuntimeError("call failed")
    outer.__cause__ = ConnectionResetError("peer")
    assert is_transient_llm_error(outer)
    assert not is_transient_llm_error(ValueError("bad json"))
    forbidden = FakeOverloaded()
    forbidden.status_code = 403
    assert not is_transient_llm_error(forbidden)


# ---- retry behavior ----

async def test_transient_error_retried_to_completion(ck_runner, tmp_path, monkeypatch):
    task = await _demo_task(ck_runner, tmp_path)
    agents = _wire_agent(
        monkeypatch,
        lambda ws: ScriptedAgent(ws, [FakeOverloaded, None],
                                 checkpointer=ck_runner.checkpointer,
                                 thread_id=task.id))

    out = await ck_runner.run(task)

    assert out.status == "completed"
    assert len(agents[0].calls) == 2  # first attempt died, retry finished
    # the retry resumed the checkpointed thread instead of reseeding from zero
    assert agents[0].calls[1][0] is None
    events = await ck_runner.db.list_events(task.id)
    assert any(e["kind"] == "log" and "retrying" in e["content"].get("text", "")
               for e in events)


async def test_retry_reseeds_when_nothing_checkpointed(ck_runner, tmp_path, monkeypatch):
    task = await _demo_task(ck_runner, tmp_path)
    # agent never writes a checkpoint -> the retry has no thread to resume
    agents = _wire_agent(monkeypatch,
                         lambda ws: ScriptedAgent(ws, [FakeOverloaded, None]))

    out = await ck_runner.run(task)

    assert out.status == "completed"
    assert len(agents[0].calls) == 2
    assert agents[0].calls[1][0] is not None  # reseeded, not resumed


async def test_nontransient_error_fails_immediately(ck_runner, tmp_path, monkeypatch):
    task = await _demo_task(ck_runner, tmp_path)
    agents = _wire_agent(monkeypatch, lambda ws: ScriptedAgent(ws, [ValueError]))

    out = await ck_runner.run(task)

    assert out.status == "failed"
    assert len(agents[0].calls) == 1  # no retry for a non-transient error
    assert "ValueError" in (out.error or "")


async def test_no_retry_without_checkpointer(plain_runner, tmp_path, monkeypatch):
    task = await _demo_task(plain_runner, tmp_path)
    agents = _wire_agent(monkeypatch, lambda ws: ScriptedAgent(ws, [FakeOverloaded]))

    out = await plain_runner.run(task)

    assert out.status == "failed"
    assert len(agents[0].calls) == 1  # replaying without checkpoints re-bills; don't


async def test_retries_exhausted_fails(ck_runner, tmp_path, monkeypatch):
    task = await _demo_task(ck_runner, tmp_path)
    agents = _wire_agent(
        monkeypatch,
        lambda ws: ScriptedAgent(ws, [FakeOverloaded],
                                 checkpointer=ck_runner.checkpointer,
                                 thread_id=task.id))

    out = await ck_runner.run(task)

    assert out.status == "failed"
    assert len(agents[0].calls) == 4  # initial + 3 retries
    assert "FakeOverloaded" in (out.error or "")


# ---- mid-run usage persistence ----

async def test_usage_persisted_mid_run(plain_runner, tmp_path, monkeypatch):
    plain_runner.settings.usage_flush_seconds = 0  # flush on every usage report
    task = await _demo_task(plain_runner, tmp_path)
    db = plain_runner.db
    observed: list[int] = []

    class MidRunProbe:
        def __init__(self, ws):
            self.ws = ws

        def astream(self, _input, **_kwargs):
            return self._run()

        async def _run(self):
            yield ((), {"orchestrator": {"messages": [_usage_msg("m1", tokens=1000)]}})
            # by now the first response's usage must already be in the DB —
            # a hard crash here must not lose it from the daily total
            refreshed = await db.get_task(task.id)
            observed.append(refreshed.input_tokens or 0)
            raise SystemExit  # simulated hard crash (BaseException)

    _wire_agent(monkeypatch, MidRunProbe)
    with pytest.raises(SystemExit):
        await plain_runner.run(task)
    assert observed == [1000]


# ---- daily-cap requeue ----

async def test_daily_cap_requeues_and_keeps_checkpoint(ck_runner, tmp_path, monkeypatch):
    task = await _demo_task(ck_runner, tmp_path)
    ck_runner.daily_budget.limit_usd = 0.000001  # first usage report trips it
    agents = _wire_agent(
        monkeypatch,
        lambda ws: ScriptedAgent(ws, [None],
                                 checkpointer=ck_runner.checkpointer,
                                 thread_id=task.id))

    out = await ck_runner.run(task)

    assert out.status == "queued"  # parked, not failed
    refreshed = await ck_runner.db.get_task(task.id)
    assert refreshed.status == "queued"
    assert refreshed.cost_usd and refreshed.cost_usd > 0  # spend persisted
    # checkpoint survives so tomorrow's run resumes rather than restarts
    assert await ck_runner.checkpointer.aget_tuple(
        {"configurable": {"thread_id": task.id}}) is not None
    events = await ck_runner.db.list_events(task.id)
    assert not any(e["kind"] == "status" and e["content"].get("status") == "failed"
                   for e in events)
    assert any(e["kind"] == "log" and "daily budget" in e["content"].get("text", "")
               for e in events)
    assert len(agents[0].calls) == 1  # a budget stop is never retried


async def test_task_budget_still_fails(ck_runner, tmp_path, monkeypatch):
    # the per-task cap keeps its fail semantics — only the DAILY cap requeues
    task = await _demo_task(ck_runner, tmp_path)
    ck_runner.settings.task_budget_usd = 0.000001
    _wire_agent(monkeypatch,
                lambda ws: ScriptedAgent(ws, [None],
                                         checkpointer=ck_runner.checkpointer,
                                         thread_id=task.id))

    out = await ck_runner.run(task)

    assert out.status == "failed"
    assert "budget" in (out.error or "")


async def test_worker_skips_notify_for_requeued_task(temp_env):
    from sde_deepagent.worker import Worker

    settings = get_settings()
    db = Database(settings.db_path)
    await db.connect()
    try:
        task = await db.create_task("t", "d")
        requeued = await db.get_task(task.id)
        requeued.status = "queued"

        async def fake_run(t):
            return requeued

        notified = []

        async def notifier(t):
            notified.append(t)

        w = Worker(db, SimpleNamespace(run=fake_run), settings=settings)
        w.add_notifier(notifier)
        w._launch(task)
        await asyncio.gather(*w.running.values())
        assert notified == []  # a parked task is not a finished task
    finally:
        await db.close()
