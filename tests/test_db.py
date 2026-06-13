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


async def test_add_event_survives_unstringifiable_content(db):
    """A value with a broken __str__ must not crash event logging (and the task)."""

    class Broken:
        def __str__(self):
            raise RuntimeError("nope")

    task = await db.create_task("t", "d")
    ev = await db.add_event(task.id, "tool_call", {"obj": Broken(), "ok": 1})
    assert ev["id"]
    events = await db.list_events(task.id)
    assert len(events) == 1 and events[0]["kind"] == "tool_call"


async def test_list_events_tolerates_corrupt_row(db):
    """One row with invalid JSON content must not fail the whole event stream."""
    task = await db.create_task("t", "d")
    await db.add_event(task.id, "ok", {"a": 1})
    # simulate corruption / manual tampering of the content column
    await db.db.execute(
        "INSERT INTO events (task_id, ts, agent, kind, content) VALUES (?,?,?,?,?)",
        (task.id, 1.0, "orchestrator", "broken", "{not valid json"))
    await db.db.commit()

    events = await db.list_events(task.id)
    assert {e["kind"] for e in events} == {"ok", "broken"}
    broken = next(e for e in events if e["kind"] == "broken")
    assert "_parse_error" in broken["content"]


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
