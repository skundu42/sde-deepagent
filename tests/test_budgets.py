"""Budget enforcement: per-task abort via the runner's check, daily gate in the
worker, schema migration for pre-budget databases."""

import asyncio
import time

import aiosqlite
import pytest

from sde_deepagent.db import Database
from sde_deepagent.pricing import (
    BudgetExceeded,
    CostTracker,
    DailyBudget,
    DailyBudgetExceeded,
)
from sde_deepagent.worker import Worker, _utc_midnight_ts

# ---- runner-level per-task budget check ----

class _Emits:
    def __init__(self):
        self.events = []

    async def emit(self, task_id, kind, content, agent="orchestrator"):
        self.events.append((kind, content))


async def test_enforce_budget_warns_then_raises(temp_env):
    from sde_deepagent.runner import TaskRunner

    runner = TaskRunner.__new__(TaskRunner)  # no db/bus needed for this check
    sink = _Emits()
    runner.emit = sink.emit  # type: ignore[method-assign]

    class FakeTask:
        id = "t1"

    tracker = CostTracker(default_model="claude-sonnet-4-6")
    tracker.cost_usd = 0.85
    await runner._enforce_budget(FakeTask, tracker, budget_usd=1.0)
    assert tracker.budget_warned
    assert any("budget warning" in c.get("text", "") for _, c in sink.events)

    tracker.cost_usd = 1.01
    with pytest.raises(BudgetExceeded):
        await runner._enforce_budget(FakeTask, tracker, budget_usd=1.0)

    # 0 = unlimited: never warns or raises
    t2 = CostTracker(default_model="claude-sonnet-4-6")
    t2.cost_usd = 10_000
    await runner._enforce_budget(FakeTask, t2, budget_usd=0)
    assert not t2.budget_warned


# ---- daily budget gate in the worker ----

@pytest.fixture
async def db(tmp_path):
    db = Database(tmp_path / "b.db")
    await db.connect()
    yield db
    await db.close()


class NeverRunner:
    def __init__(self):
        self.calls = []

    async def run(self, task):
        self.calls.append(task.id)
        task.status = "completed"
        return task


class _BudgetSettings:
    daily_budget_usd = 5.0


async def test_daily_budget_pauses_pickup(db):
    # burn today's budget with an already-finished task
    done = await db.create_task("burned", "d")
    await db.update_task(done.id, status="completed", started_at=time.time(),
                         cost_usd=6.0)
    queued = await db.create_task("waiting", "d")

    runner = NeverRunner()
    worker = Worker(db, runner, max_concurrent=2, settings=_BudgetSettings())
    worker.start()
    try:
        await asyncio.sleep(1.5)
        assert runner.calls == []  # gate held the queued task back
        assert worker.budget_paused is True
    finally:
        await worker.stop()
    assert (await db.get_task(queued.id)).status == "queued"


# ---- daily budget is a HARD cap: in-flight spend counts immediately ----

async def test_daily_budget_counts_inflight_spend(db):
    # nothing persisted yet, but a running task is already over the cap
    budget = DailyBudget(db, 5.0)
    assert await budget.spent_usd() == pytest.approx(0.0)

    tracker = CostTracker(default_model="claude-sonnet-4-6")
    tracker.cost_usd = 6.0
    budget.track("running", tracker)
    # spend reflects live in-flight cost before it is ever persisted
    assert budget.live_usd() == pytest.approx(6.0)
    assert await budget.spent_usd() == pytest.approx(6.0)

    budget.untrack("running")
    assert await budget.spent_usd() == pytest.approx(0.0)


async def test_daily_budget_gate_counts_inflight(db):
    # nothing finished today (no persisted cost), but one running task has
    # already blown the cap — the gate must still hold queued work back
    queued = await db.create_task("waiting", "d")
    budget = DailyBudget(db, 5.0)
    inflight = CostTracker(default_model="claude-sonnet-4-6")
    inflight.cost_usd = 6.0
    budget.track("inflight", inflight)

    runner = NeverRunner()
    worker = Worker(db, runner, max_concurrent=2, settings=_BudgetSettings(),
                    daily_budget=budget)
    worker.start()
    try:
        await asyncio.sleep(1.5)
        assert runner.calls == []  # held back on in-flight spend alone
        assert worker.budget_paused is True
    finally:
        await worker.stop()
    assert (await db.get_task(queued.id)).status == "queued"


async def test_runner_aborts_midstream_on_daily_cap(db):
    # the runner's per-response check trips DailyBudgetExceeded so an in-flight
    # task cannot keep spending past the account-wide ceiling
    from sde_deepagent.runner import TaskRunner

    runner = TaskRunner.__new__(TaskRunner)  # only daily_budget is needed here
    runner.daily_budget = DailyBudget(db, 5.0)
    tracker = CostTracker(default_model="claude-sonnet-4-6")
    tracker.cost_usd = 6.0
    runner.daily_budget.track("t1", tracker)

    with pytest.raises(DailyBudgetExceeded):
        await runner._enforce_daily_budget()

    # 0 / unset == unlimited: never aborts
    runner.daily_budget.limit_usd = 0.0
    await runner._enforce_daily_budget()


async def test_spend_since_only_counts_window(db):
    old = await db.create_task("yesterday", "d")
    await db.update_task(old.id, status="completed",
                         started_at=_utc_midnight_ts() - 3600, cost_usd=100.0)
    new = await db.create_task("today", "d")
    await db.update_task(new.id, status="completed",
                         started_at=time.time(), cost_usd=1.25)
    assert await db.spend_since(_utc_midnight_ts()) == pytest.approx(1.25)


# ---- schema migration for databases created before budgets existed ----

OLD_SCHEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL,
    repo TEXT, source TEXT NOT NULL DEFAULT 'ui',
    source_ref TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'queued',
    branch TEXT, pr_url TEXT, error TEXT, model TEXT,
    created_at REAL NOT NULL, started_at REAL, finished_at REAL
);
"""


async def test_migration_adds_budget_columns(tmp_path):
    path = tmp_path / "old.db"
    async with aiosqlite.connect(path) as raw:
        await raw.executescript(OLD_SCHEMA)
        await raw.execute(
            "INSERT INTO tasks (id, title, description, created_at)"
            " VALUES ('abc', 'legacy', 'd', ?)", (time.time(),))
        await raw.commit()

    db = Database(path)
    await db.connect()
    task = await db.get_task("abc")
    assert task.cost_usd is None and task.budget_usd is None
    await db.update_task("abc", cost_usd=0.5, input_tokens=100, output_tokens=20)
    task = await db.get_task("abc")
    assert task.cost_usd == 0.5 and task.input_tokens == 100
    await db.close()
