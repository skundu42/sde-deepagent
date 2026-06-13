"""Worker pool: pulls queued tasks from the DB and runs them concurrently up to
MAX_CONCURRENT_TASKS. Enforces the daily LLM budget (queue pauses when today's
spend reaches it) and notifies intake channels when tasks finish."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Awaitable, Callable

from .db import Database, Task
from .runner import TaskRunner
from .settings import Settings

logger = logging.getLogger(__name__)

Notifier = Callable[[Task], Awaitable[None]]


def _utc_midnight_ts() -> float:
    now = dt.datetime.now(dt.timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


class Worker:
    def __init__(self, db: Database, runner: TaskRunner, max_concurrent: int = 2,
                 settings: Settings | None = None):
        self.db = db
        self.runner = runner
        self.max_concurrent = max_concurrent
        self.settings = settings
        self.budget_paused = False
        self.running: dict[str, asyncio.Task] = {}
        self.notifiers: list[Notifier] = []
        self._stop = asyncio.Event()
        self._dispatcher: asyncio.Task | None = None

    def add_notifier(self, notifier: Notifier) -> None:
        self.notifiers.append(notifier)

    def start(self) -> None:
        self._dispatcher = asyncio.create_task(self._dispatch_loop(), name="worker-dispatcher")

    async def stop(self) -> None:
        self._stop.set()
        for t in list(self.running.values()):
            t.cancel()
        to_await = list(self.running.values())
        if self._dispatcher:
            self._dispatcher.cancel()
            to_await.append(self._dispatcher)  # await it too, or stop() returns early
        await asyncio.gather(*to_await, return_exceptions=True)

    def cancel_task(self, task_id: str) -> bool:
        t = self.running.get(task_id)
        if t:
            t.cancel()
            return True
        return False

    async def _daily_budget_ok(self) -> bool:
        limit = self.settings.daily_budget_usd if self.settings else 0.0
        if limit <= 0:
            return True
        spend = await self.db.spend_since(_utc_midnight_ts())
        if spend >= limit:
            if not self.budget_paused:
                logger.warning(
                    "daily budget reached ($%.2f of $%.2f) — pausing task pickup "
                    "until UTC midnight", spend, limit)
            self.budget_paused = True
            return False
        if self.budget_paused:
            logger.info("daily budget headroom restored — resuming task pickup")
        self.budget_paused = False
        return True

    async def _dispatch_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if len(self.running) < self.max_concurrent:
                    task = await self.db.next_queued_task()
                    # the runner flips the DB status to 'running' asynchronously,
                    # so guard against re-launching a task we just started
                    if task and task.id not in self.running:
                        if not await self._daily_budget_ok():
                            await asyncio.sleep(5.0)
                            continue
                        self._launch(task)
                        continue  # check immediately for more capacity
            except Exception:
                logger.exception("dispatcher error")
            await asyncio.sleep(1.0)

    def _launch(self, task: Task) -> None:
        async def _run() -> None:
            try:
                finished = await self.runner.run(task)
                for notify in self.notifiers:
                    try:
                        await notify(finished)
                    except Exception:
                        logger.exception("notifier failed for task %s", task.id)
            except asyncio.CancelledError:
                pass
            finally:
                self.running.pop(task.id, None)

        logger.info("starting task %s: %s", task.id, task.title)
        self.running[task.id] = asyncio.create_task(_run(), name=f"task-{task.id}")
