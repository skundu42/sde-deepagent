"""Regression test: the dispatcher must not launch the same queued task twice
while the runner is still flipping its DB status to 'running'."""

import asyncio

import pytest

from sde_deepagent.db import Database
from sde_deepagent.worker import Worker


class SlowRunner:
    """Stands in for TaskRunner: waits past several dispatcher polls before
    updating the DB, exactly the window where double-launch used to happen."""

    def __init__(self, db: Database):
        self.db = db
        self.calls: list[str] = []

    async def run(self, task):
        self.calls.append(task.id)
        await asyncio.sleep(2.2)
        await self.db.update_task(task.id, status="completed")
        task.status = "completed"
        return task


@pytest.fixture
async def db(tmp_path):
    db = Database(tmp_path / "w.db")
    await db.connect()
    yield db
    await db.close()


async def test_no_double_launch(db):
    runner = SlowRunner(db)
    worker = Worker(db, runner, max_concurrent=4)
    task = await db.create_task("only once", "d")
    worker.start()
    try:
        await asyncio.sleep(3.0)  # several 1s dispatcher polls
    finally:
        await worker.stop()
    assert runner.calls == [task.id]


async def test_stop_awaits_dispatcher(db):
    """stop() must fully wind down the dispatcher, not return with it still pending."""
    worker = Worker(db, SlowRunner(db), max_concurrent=1)
    worker.start()
    await asyncio.sleep(0.1)
    await worker.stop()
    assert worker._dispatcher.done()


async def test_concurrency_cap(db):
    runner = SlowRunner(db)
    worker = Worker(db, runner, max_concurrent=1)
    await db.create_task("a", "d")
    await db.create_task("b", "d")
    worker.start()
    try:
        await asyncio.sleep(1.5)
        assert len(runner.calls) == 1  # second task waits for a slot
    finally:
        await worker.stop()
