"""In-process event bus: the runner publishes task events, SSE subscribers
consume them. Single-process by design (self-hostable, no broker)."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._global: set[asyncio.Queue] = set()

    def publish(self, task_id: str, event: dict[str, Any]) -> None:
        for q in self._subscribers.get(task_id, set()):
            q.put_nowait(event)
        for q in self._global:
            q.put_nowait(event)

    def attach(self, task_id: str | None = None) -> asyncio.Queue:
        """Register a subscriber queue immediately (no lazy-generator gap)."""
        q: asyncio.Queue = asyncio.Queue()
        (self._global if task_id is None else self._subscribers[task_id]).add(q)
        return q

    def detach(self, task_id: str | None, q: asyncio.Queue) -> None:
        if task_id is None:
            self._global.discard(q)
        else:
            self._subscribers[task_id].discard(q)
            if not self._subscribers[task_id]:
                del self._subscribers[task_id]
