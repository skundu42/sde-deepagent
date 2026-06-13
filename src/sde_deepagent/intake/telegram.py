"""Telegram intake via long-polling — no public URL or webhook needed, which
keeps self-hosting trivial. Messages from explicitly allowed chats (optionally
prefixed with `[repo]`) become tasks; the bot replies when the task finishes."""

from __future__ import annotations

import asyncio
import logging

import httpx

from ..db import Database, Task
from ..settings import Settings
from .base import parse_task_text, task_summary

logger = logging.getLogger(__name__)


class TelegramIntake:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.token = settings.telegram_bot_token
        self.api = f"https://api.telegram.org/bot{self.token}"
        self.allowed = settings.telegram_allowed_chat_ids()
        self.allow_all = settings.telegram_allow_all
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if not self.allow_all and not self.allowed:
            logger.warning("telegram intake has no TELEGRAM_ALLOWED_CHATS; "
                           "all task-creation messages will be ignored")
        self._task = asyncio.create_task(self._poll_loop(), name="telegram-intake")
        logger.info("telegram intake started (long polling)")

    def _allowed_chat(self, chat_id: int) -> bool:
        return self.allow_all or chat_id in self.allowed

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _poll_loop(self) -> None:
        offset = 0
        async with httpx.AsyncClient(timeout=70) as client:
            while True:
                try:
                    resp = await client.get(f"{self.api}/getUpdates",
                                            params={"timeout": 50, "offset": offset})
                    if resp.status_code != 200:
                        logger.warning("telegram getUpdates HTTP %s: %s",
                                       resp.status_code, resp.text[:200])
                        await asyncio.sleep(5)
                        continue
                    body = resp.json()
                    if not body.get("ok", True):
                        logger.warning("telegram getUpdates not ok: %s", str(body)[:200])
                        await asyncio.sleep(5)
                        continue
                    for update in body.get("result", []):
                        update_id = update.get("update_id")
                        if update_id is None:
                            continue  # malformed update; can't advance the offset on it
                        offset = update_id + 1
                        await self._handle_update(client, update)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("telegram poll error; retrying in 5s")
                    await asyncio.sleep(5)

    async def _handle_update(self, client: httpx.AsyncClient, update: dict) -> None:
        msg = update.get("message") or update.get("channel_post")
        if not msg or not msg.get("text"):
            return
        chat_id = msg["chat"]["id"]
        if not self._allowed_chat(chat_id):
            logger.warning("ignoring message from non-allowed chat %s", chat_id)
            return
        text = msg["text"].strip()
        if text in ("/start", "/help"):
            await self._send(client, chat_id,
                             "Send me a dev task. Optionally target a repo with "
                             "`[repo-name] your task...`. I'll reply with a PR when done.")
            return
        if text.startswith("/task"):
            text = text[len("/task"):].strip()
        if not text:
            return
        repo, title, description = parse_task_text(text)
        task = await self.db.create_task(
            title=title, description=description, repo=repo, source="telegram",
            source_ref={"chat_id": chat_id, "message_id": msg.get("message_id")},
        )
        await self._send(client, chat_id,
                         f"🤖 Task `{task.id}` queued: {task.title}",
                         reply_to=msg.get("message_id"))

    async def _send(self, client: httpx.AsyncClient, chat_id: int, text: str,
                    reply_to: int | None = None) -> None:
        payload: dict = {"chat_id": chat_id, "text": text}
        if reply_to:
            payload["reply_to_message_id"] = reply_to
        await client.post(f"{self.api}/sendMessage", json=payload)

    async def notify(self, task: Task) -> None:
        """Worker callback: report the finished task back to its chat."""
        if task.source != "telegram":
            return
        chat_id = task.source_ref.get("chat_id")
        if not chat_id:
            return
        async with httpx.AsyncClient(timeout=30) as client:
            await self._send(client, chat_id, task_summary(task),
                             reply_to=task.source_ref.get("message_id"))
