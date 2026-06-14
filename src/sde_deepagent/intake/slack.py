"""Slack intake via Socket Mode — no public URL needed. Mention the bot or DM
it to create a task; it replies in-thread when the task finishes.

Requires a Slack app with Socket Mode enabled, an app-level token (xapp-...),
a bot token (xoxb-...) with chat:write + app_mentions:read + im:history scopes,
and the app_mention + message.im event subscriptions. Task creation is denied
until SLACK_ALLOWED_USERS explicitly lists users (or is set to "*")."""

from __future__ import annotations

import logging
import re

from ..db import Database, Task
from ..settings import Settings
from .base import parse_task_text, task_summary

logger = logging.getLogger(__name__)

MENTION_RE = re.compile(r"<@[\w]+>\s*")


class SlackIntake:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.allowed = settings.slack_allowed_user_ids()
        self.allow_all = settings.slack_allow_all
        self._client = None
        self._web = None

    def _allowed_user(self, user_id: str | None) -> bool:
        return bool(user_id) and (self.allow_all or user_id in self.allowed)

    def start(self) -> None:
        import asyncio

        if not self.allow_all and not self.allowed:
            logger.warning("slack intake has no SLACK_ALLOWED_USERS; "
                           "all task-creation messages will be ignored")
        asyncio.create_task(self._run(), name="slack-intake")

    async def _run(self) -> None:
        try:
            from slack_sdk.socket_mode.aiohttp import SocketModeClient
            from slack_sdk.socket_mode.request import SocketModeRequest
            from slack_sdk.socket_mode.response import SocketModeResponse
            from slack_sdk.web.async_client import AsyncWebClient
        except ImportError:
            logger.error("slack-sdk (with aiohttp) not installed; slack intake disabled")
            return

        self._web = AsyncWebClient(token=self.settings.slack_bot_token)
        self._client = SocketModeClient(
            app_token=self.settings.slack_app_token, web_client=self._web
        )

        async def handle(client: SocketModeClient, req: SocketModeRequest) -> None:
            if req.type != "events_api":
                return
            await client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
            event = req.payload.get("event", {})
            if event.get("type") not in ("app_mention", "message"):
                return
            if event.get("bot_id") or event.get("subtype"):
                return  # ignore bot echo and edits
            if event["type"] == "message" and event.get("channel_type") != "im":
                return  # plain channel chatter; require a mention there
            channel = event.get("channel")
            if not channel:
                return  # malformed event; nowhere to reply
            user = event.get("user")
            if not self._allowed_user(user):
                logger.warning("ignoring message from non-allowed Slack user %s", user)
                return
            text = MENTION_RE.sub("", event.get("text", "")).strip()
            if not text:
                return
            repo, title, description = parse_task_text(text)
            ts = event.get("ts")  # the message's own timestamp, unique per channel
            # dedup on (channel, message ts) so a re-delivered socket-mode event
            # doesn't create a second task
            task = await self.db.create_task_if_new(
                title=title, description=description, repo=repo, source="slack",
                source_ref={"channel": channel, "thread_ts": event.get("thread_ts") or ts},
                dedup_key=f"slack:{channel}:{ts}",
            )
            if task is None:
                return  # already ingested
            await self._web.chat_postMessage(
                channel=channel,
                thread_ts=event.get("thread_ts") or ts,
                text=f"🤖 Task `{task.id}` queued: {task.title}",
            )

        self._client.socket_mode_request_listeners.append(handle)
        await self._client.connect()
        logger.info("slack intake connected (socket mode)")

    async def stop(self) -> None:
        if self._client:
            await self._client.disconnect()

    async def notify(self, task: Task) -> None:
        if task.source != "slack" or not self._web:
            return
        ref = task.source_ref
        if not ref.get("channel"):
            return
        await self._web.chat_postMessage(
            channel=ref["channel"], thread_ts=ref.get("thread_ts"), text=task_summary(task)
        )
