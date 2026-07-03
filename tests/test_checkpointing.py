"""Durable agent-state checkpointing used for resume-after-restart."""

import sys

from sde_deepagent.checkpointing import open_checkpointing
from sde_deepagent.settings import get_settings


async def test_open_checkpointing_is_durable_and_roundtrips(temp_env):
    # with langgraph-checkpoint-sqlite installed, we get a durable SQLite saver
    ck = await open_checkpointing(get_settings())
    try:
        assert ck.durable is True
        cfg = {"configurable": {"thread_id": "task-xyz"}}
        assert await ck.saver.aget_tuple(cfg) is None  # nothing stored yet

        from langgraph.checkpoint.base import empty_checkpoint
        chk = empty_checkpoint()
        put_cfg = {"configurable": {"thread_id": "task-xyz", "checkpoint_ns": ""}}
        await ck.saver.aput(put_cfg, chk, {"source": "test"}, {})
        got = await ck.saver.aget_tuple(cfg)
        assert got is not None and got.checkpoint["id"] == chk["id"]  # persisted
    finally:
        await ck.aclose()

    # the checkpoint DB lives under the data dir
    assert (get_settings().data_dir / "checkpoints.sqlite").exists()


async def test_open_checkpointing_falls_back_to_memory(temp_env, monkeypatch):
    # if the sqlite saver package is unavailable, degrade to in-memory (no resume)
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.sqlite.aio", None)
    ck = await open_checkpointing(get_settings())
    try:
        assert ck.durable is False
        from langgraph.checkpoint.memory import InMemorySaver
        assert isinstance(ck.saver, InMemorySaver)
    finally:
        await ck.aclose()  # must be a no-op, not raise
