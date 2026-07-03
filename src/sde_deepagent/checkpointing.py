"""Durable agent-state checkpointing for resume-after-restart.

`create_deep_agent` returns a LangGraph graph; given a checkpointer and a stable
`thread_id`, it persists the agent's state (messages, todos) at every step. We
back that with SQLite so a task interrupted by a crash/restart can resume from
its last checkpoint instead of starting over. If the optional SQLite saver
package is missing we degrade to an in-memory saver (no cross-restart resume).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .settings import Settings

logger = logging.getLogger(__name__)


@dataclass
class Checkpointing:
    saver: Any            # langgraph BaseCheckpointSaver
    durable: bool         # True only when state survives a process restart
    _conn: Any = None     # aiosqlite connection to close on shutdown, if any

    async def aclose(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None


async def open_checkpointing(settings: Settings) -> Checkpointing:
    """Open a durable SQLite checkpointer, or fall back to in-memory."""
    try:
        import aiosqlite
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    except ImportError:
        from langgraph.checkpoint.memory import InMemorySaver
        logger.warning(
            "langgraph-checkpoint-sqlite not installed; agent state is in-memory "
            "only and interrupted tasks cannot resume after a restart")
        return Checkpointing(saver=InMemorySaver(), durable=False)

    path = settings.data_dir / "checkpoints.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(path))
    saver = AsyncSqliteSaver(conn)
    await saver.setup()
    return Checkpointing(saver=saver, durable=True, _conn=conn)
