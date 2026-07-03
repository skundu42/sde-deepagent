"""Worker pool: pulls queued tasks from the DB and runs them concurrently up to
MAX_CONCURRENT_TASKS. Enforces the daily LLM budget (queue pauses when today's
spend reaches it) and notifies intake channels when tasks finish."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from .db import Database, Task
from .pricing import DailyBudget
from .pricing import utc_midnight_ts as _utc_midnight_ts  # noqa: F401 — re-export for callers/tests
from .repo_reader import prune_ref_clones
from .runner import TaskRunner
from .settings import Settings

RETENTION_SWEEP_SECONDS = 86400  # once a day is plenty for 90-day retention

logger = logging.getLogger(__name__)

Notifier = Callable[[Task], Awaitable[None]]


class Worker:
    def __init__(self, db: Database, runner: TaskRunner, max_concurrent: int = 2,
                 settings: Settings | None = None,
                 daily_budget: DailyBudget | None = None):
        self.db = db
        self.runner = runner
        self.max_concurrent = max_concurrent
        self.settings = settings
        # Share one accountant with the runner so the launch gate and the
        # runner's mid-stream check agree on the true (persisted + in-flight)
        # daily spend. Falls back to a self-owned one for standalone use/tests.
        self.daily_budget = daily_budget or DailyBudget(
            db, settings.daily_budget_usd if settings else 0.0)
        self.budget_paused = False
        self.running: dict[str, asyncio.Task] = {}
        self.notifiers: list[Notifier] = []
        self._stop = asyncio.Event()
        self._dispatcher: asyncio.Task | None = None
        self._sweeper: asyncio.Task | None = None

    def add_notifier(self, notifier: Notifier) -> None:
        self.notifiers.append(notifier)

    def start(self) -> None:
        self._dispatcher = asyncio.create_task(self._dispatch_loop(), name="worker-dispatcher")
        self._sweeper = asyncio.create_task(self._retention_loop(), name="retention-sweeper")

    async def stop(self) -> None:
        self._stop.set()
        for t in list(self.running.values()):
            t.cancel()
        to_await = list(self.running.values())
        for background in (self._dispatcher, self._sweeper):
            if background:
                background.cancel()
                to_await.append(background)  # await them too, or stop() returns early
        await asyncio.gather(*to_await, return_exceptions=True)

    # ---- retention ----

    async def _retention_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._run_retention_sweep()
            except Exception:
                logger.exception("retention sweep failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=RETENTION_SWEEP_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def _run_retention_sweep(self) -> None:
        """Daily bounded-growth sweep: old finished-task events and chat-spend
        rows, plus reference clones the chat hasn't read in weeks."""
        if self.settings is None:
            return
        if self.settings.retention_days > 0:
            cutoff = time.time() - self.settings.retention_days * 86400
            events, chats = await self.db.prune_history(cutoff)
            if events or chats:
                logger.info("retention: pruned %d old event(s), %d chat-spend row(s)",
                            events, chats)
        await asyncio.to_thread(prune_ref_clones, self.settings)

    def cancel_task(self, task_id: str) -> bool:
        t = self.running.get(task_id)
        if t:
            t.cancel()
            return True
        return False

    async def _daily_budget_ok(self) -> bool:
        limit = self.daily_budget.limit_usd
        if limit <= 0:
            return True
        # includes in-flight tasks' unpersisted spend — a hard cap, not a gate
        # that several concurrent launches can all slip past
        spend = await self.daily_budget.spent_usd()
        if spend >= limit:
            if not self.budget_paused:
                logger.warning(
                    "daily budget reached ($%.2f of $%.2f): pausing task pickup "
                    "until UTC midnight", spend, limit)
            self.budget_paused = True
            return False
        if self.budget_paused:
            logger.info("daily budget headroom restored: resuming task pickup")
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
                if finished.status == "queued":
                    # parked by the daily cap — it will resume, so don't report
                    # it to the source channel as if it had finished
                    logger.info("task %s re-queued (daily budget reached)", task.id)
                    return
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
