import aiosqlite
import pytest

from sde_deepagent.db import Database


async def test_create_task_if_new_dedups_by_key(db):
    t1 = await db.create_task_if_new(title="x", description="d", source="telegram",
                                     source_ref={"m": 1}, dedup_key="telegram:1:2")
    assert t1 is not None
    # a re-delivered/duplicate source event with the same key is ignored
    t2 = await db.create_task_if_new(title="x", description="d", source="telegram",
                                     source_ref={"m": 1}, dedup_key="telegram:1:2")
    assert t2 is None
    # a different key still creates
    t3 = await db.create_task_if_new(title="y", description="d", source="telegram",
                                     source_ref={"m": 2}, dedup_key="telegram:1:3")
    assert t3 is not None
    assert len([t for t in await db.list_tasks() if t.source == "telegram"]) == 1 + 1


async def test_dedup_survives_migration_from_pre_dedup_db(tmp_path):
    # a DB created before the dedup column exists must migrate (add column + unique
    # index) and then dedup correctly — proves the index is created post-migration
    path = tmp_path / "old.db"
    async with aiosqlite.connect(path) as raw:
        await raw.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL,"
            " description TEXT NOT NULL, repo TEXT, source TEXT NOT NULL DEFAULT 'ui',"
            " source_ref TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'queued',"
            " branch TEXT, pr_url TEXT, error TEXT, model TEXT, created_at REAL NOT NULL,"
            " started_at REAL, finished_at REAL)")
        await raw.commit()
    db = Database(path)
    await db.connect()
    assert await db.create_task_if_new(title="x", description="d", dedup_key="k1") is not None
    assert await db.create_task_if_new(title="x", description="d", dedup_key="k1") is None
    await db.close()


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


async def test_running_tasks_requeued_on_restart_when_resumable(tmp_path):
    # with a durable checkpointer, an interrupted task is re-queued (so the
    # worker picks it up and resumes from its checkpoint) rather than failed
    db = Database(tmp_path / "resume.db")
    await db.connect()
    t = await db.create_task("t", "d")
    await db.update_task(t.id, status="running")
    await db.close()

    db2 = Database(tmp_path / "resume.db")
    await db2.connect(resume_interrupted=True)
    fetched = await db2.get_task(t.id)
    assert fetched.status == "queued"
    assert fetched.error is None


async def test_requeue_preserves_started_at_so_cost_stays_counted(tmp_path):
    # a resumed task's already-persisted cost must remain in the daily spend total;
    # nulling started_at would drop it from the spend_since window
    db = Database(tmp_path / "resume2.db")
    await db.connect()
    t = await db.create_task("t", "d")
    await db.update_task(t.id, status="running", started_at=1000.0, cost_usd=0.5)
    await db.close()

    db2 = Database(tmp_path / "resume2.db")
    await db2.connect(resume_interrupted=True)
    fetched = await db2.get_task(t.id)
    assert fetched.status == "queued"
    assert fetched.started_at == 1000.0  # preserved, not nulled
    assert await db2.spend_since(0.0) == pytest.approx(0.5)  # cost still counted
    await db2.close()
    await db2.close()
