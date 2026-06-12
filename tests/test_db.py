import pytest

from sde_deepagent.db import Database


@pytest.fixture
async def db(tmp_path):
    db = Database(tmp_path / "test.db")
    await db.connect()
    yield db
    await db.close()


async def test_task_lifecycle(db):
    task = await db.create_task("Fix bug", "Fix the login bug", repo="backend")
    assert task.status == "queued"
    assert len(task.id) == 10

    fetched = await db.get_task(task.id)
    assert fetched.title == "Fix bug"
    assert fetched.repo == "backend"

    await db.update_task(task.id, status="running", branch="agent/x")
    fetched = await db.get_task(task.id)
    assert fetched.status == "running"
    assert fetched.branch == "agent/x"

    await db.update_task(task.id, status="completed", pr_url="https://github.com/x/y/pull/1")
    fetched = await db.get_task(task.id)
    assert fetched.pr_url.endswith("/pull/1")


async def test_invalid_updates_rejected(db):
    task = await db.create_task("t", "d")
    with pytest.raises(ValueError):
        await db.update_task(task.id, status="bogus")
    with pytest.raises(ValueError):
        await db.update_task(task.id, title="nope")


async def test_queue_order_and_stats(db):
    t1 = await db.create_task("first", "d")
    t2 = await db.create_task("second", "d")
    nxt = await db.next_queued_task()
    assert nxt.id == t1.id
    await db.update_task(t1.id, status="running")
    nxt = await db.next_queued_task()
    assert nxt.id == t2.id

    stats = await db.stats()
    assert stats["queued"] == 1 and stats["running"] == 1 and stats["total"] == 2


async def test_events(db):
    task = await db.create_task("t", "d")
    e1 = await db.add_event(task.id, "log", {"text": "hello"})
    await db.add_event(task.id, "tool_call", {"name": "execute"}, agent="coder")
    events = await db.list_events(task.id)
    assert len(events) == 2
    assert events[0]["content"]["text"] == "hello"
    assert events[1]["agent"] == "coder"
    # incremental fetch
    newer = await db.list_events(task.id, after_id=e1["id"])
    assert len(newer) == 1


async def test_running_tasks_failed_on_restart(tmp_path):
    db = Database(tmp_path / "restart.db")
    await db.connect()
    t = await db.create_task("t", "d")
    await db.update_task(t.id, status="running")
    await db.close()

    db2 = Database(tmp_path / "restart.db")
    await db2.connect()
    fetched = await db2.get_task(t.id)
    assert fetched.status == "failed"
    assert "restart" in fetched.error
    await db2.close()
