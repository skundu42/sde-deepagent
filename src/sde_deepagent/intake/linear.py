"""Linear intake. Two paths:

1. Seatless agent (recommended): create a Linear OAuth app, authorize it with
   actor=app (scopes app:assignable + app:mentionable), and delegate issues to
   it. Linear opens an *agent session* and POSTs an AgentSessionEvent to
   /webhooks/linear; we create a task and reply with agent activities
   (thought/response/error) — no Linear seat consumed, no polling.

2. Legacy: with a personal LINEAR_API_KEY (a seat), poll the GraphQL API for
   issues by label (LINEAR_TRIGGER=label) or assignment (assignee), and post
   progress/PR links as issue comments.

Write-back auth prefers LINEAR_OAUTH_TOKEN (Bearer, actor=app); it falls back to
the personal key, or is skipped if neither is set."""

from __future__ import annotations

import asyncio
import logging

import httpx

from ..db import Database, Task
from ..settings import Settings
from .base import task_summary

logger = logging.getLogger(__name__)

LINEAR_API = "https://api.linear.app/graphql"

ISSUES_QUERY = """
query AgentLabelled($label: String!) {
  issues(
    filter: {
      labels: { name: { eq: $label } }
      state: { type: { in: ["triage", "backlog", "unstarted"] } }
    }
    first: 25
  ) {
    nodes { id identifier title description url }
  }
}
"""

ASSIGNED_QUERY = """
query AgentAssigned($uid: ID!) {
  issues(
    filter: {
      assignee: { id: { eq: $uid } }
      state: { type: { in: ["triage", "backlog", "unstarted"] } }
    }
    first: 25
  ) {
    nodes { id identifier title description url }
  }
}
"""

VIEWER_QUERY = "query { viewer { id name } }"

COMMENT_MUTATION = """
mutation CreateComment($issueId: String!, $body: String!) {
  commentCreate(input: { issueId: $issueId, body: $body }) { success }
}
"""

# Agent Sessions: when the OAuth app is delegated an issue, Linear opens an
# agent session and the app replies with activities (thought/response/error)
# rather than issue comments. See https://linear.app/developers/agent-interaction
AGENT_ACTIVITY_MUTATION = """
mutation AgentActivityCreate($input: AgentActivityCreateInput!) {
  agentActivityCreate(input: $input) { success }
}
"""


class LinearIntake:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self._task: asyncio.Task | None = None
        # Polling needs a personal api key (a seat); webhook-only deployments run
        # without one. (Assignee mode needs the key to resolve the viewer id, so
        # webhook-only assignee config simply never matches — use label mode.)
        self._polling = bool(settings.linear_api_key)
        self._by_assignee = (settings.linear_trigger or "label").strip().lower() == "assignee"
        self._my_user_id: str | None = None  # resolved lazily in assignee mode

    def start(self) -> None:
        if self._polling:
            self._task = asyncio.create_task(self._poll_loop(), name="linear-intake")
            mode = ("issues assigned to this key's user" if self._by_assignee
                    else f"label '{self.settings.linear_label}'")
            logger.info("linear intake started (polling, trigger: %s)", mode)
        else:
            logger.info("linear intake started (webhook-only, label '%s'%s)",
                        self.settings.linear_label,
                        "" if self.settings.linear_oauth_token
                        else "; write-back disabled: no LINEAR_OAUTH_TOKEN")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": self.settings.linear_api_key or "",
                "Content-Type": "application/json"}

    def _write_headers(self) -> dict[str, str] | None:
        """Auth for posting comments. Prefer the OAuth app token (actor=app, no
        seat); fall back to the personal api key; return None if neither is set
        (webhook-only with no write-back)."""
        if self.settings.linear_oauth_token:
            return {"Authorization": f"Bearer {self.settings.linear_oauth_token}",
                    "Content-Type": "application/json"}
        if self.settings.linear_api_key:
            return {"Authorization": self.settings.linear_api_key,
                    "Content-Type": "application/json"}
        return None

    async def _resolve_my_id(self, client: httpx.AsyncClient) -> str | None:
        """Resolve and cache the api key's own Linear user id (assignee mode)."""
        if self._my_user_id:
            return self._my_user_id
        try:
            resp = await client.post(LINEAR_API, headers=self._headers(),
                                     json={"query": VIEWER_QUERY})
            viewer = (resp.json().get("data") or {}).get("viewer") or {}
            self._my_user_id = viewer.get("id")
            if self._my_user_id:
                logger.info("linear: watching assignments for %s (%s)",
                            viewer.get("name", "?"), self._my_user_id)
        except Exception:
            logger.exception("linear: failed to resolve viewer id")
        return self._my_user_id

    async def _poll_query(self, client: httpx.AsyncClient) -> tuple[str | None, dict]:
        """The GraphQL query + variables for this poll, or (None, {}) to skip
        (assignee mode before the viewer id has been resolved)."""
        if self._by_assignee:
            uid = await self._resolve_my_id(client)
            if not uid:
                return None, {}
            return ASSIGNED_QUERY, {"uid": uid}
        return ISSUES_QUERY, {"label": self.settings.linear_label}

    async def _poll_loop(self) -> None:
        async with httpx.AsyncClient(timeout=30) as client:
            while True:
                try:
                    query, variables = await self._poll_query(client)
                    if query is None:
                        await asyncio.sleep(self.settings.linear_poll_seconds)
                        continue
                    resp = await client.post(
                        LINEAR_API, headers=self._headers(),
                        json={"query": query, "variables": variables},
                    )
                    if resp.status_code != 200:
                        logger.warning("linear API HTTP %s: %s",
                                       resp.status_code, resp.text[:200])
                    else:
                        data = resp.json()
                        if data.get("errors"):
                            logger.warning("linear API errors: %s",
                                           str(data["errors"])[:200])
                        else:
                            nodes = ((data.get("data") or {})
                                     .get("issues", {}).get("nodes", []))
                            for issue in nodes:
                                await self.ingest_issue(client, issue)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("linear poll error")
                await asyncio.sleep(self.settings.linear_poll_seconds)

    async def ingest_issue(self, client: httpx.AsyncClient, issue: dict) -> None:
        description = issue.get("description") or ""
        # dedup on the issue id, atomically — survives restarts (no 500-row scan
        # limit) and closes the poll-vs-webhook race that could double-ingest
        task = await self.db.create_task_if_new(
            title=f"{issue['identifier']}: {issue['title']}",
            description=f"{issue['title']}\n\n{description}\n\nLinear issue: {issue['url']}",
            source="linear",
            source_ref={"issue_id": issue["id"], "identifier": issue["identifier"],
                        "url": issue["url"]},
            dedup_key=f"linear:{issue['id']}",
        )
        if task is None:
            return  # already picked up
        await self._comment(client, issue["id"],
                            f"🤖 sde-deepagent picked this up as task `{task.id}`.")
        logger.info("linear issue %s -> task %s", issue["identifier"], task.id)

    async def _comment(self, client: httpx.AsyncClient, issue_id: str, body: str) -> None:
        headers = self._write_headers()
        if not headers:
            return  # webhook-only with no write-back credential — nothing to post as
        try:
            await client.post(LINEAR_API, headers=headers,
                              json={"query": COMMENT_MUTATION,
                                    "variables": {"issueId": issue_id, "body": body}})
        except Exception:
            logger.exception("failed to comment on linear issue %s", issue_id)

    # ---- agent sessions (delegation to the OAuth app; seatless) ----------

    def _agent_session_issue(self, payload: dict) -> tuple[str, dict] | None:
        """For a `created` AgentSessionEvent, return (session_id, issue) to start
        a task, or None (follow-up prompts, or mentions without an issue)."""
        if payload.get("type") != "AgentSessionEvent" or payload.get("action") != "created":
            return None
        session = payload.get("agentSession") or {}
        sid = session.get("id")
        issue = session.get("issue") or {}
        if not sid or not issue.get("id"):
            return None
        return sid, {
            "id": issue["id"], "identifier": issue.get("identifier", "?"),
            "title": issue.get("title", "task"), "description": issue.get("description"),
            "url": issue.get("url", ""),
        }

    async def _agent_activity(self, client: httpx.AsyncClient, session_id: str,
                             activity_type: str, body: str) -> None:
        """Post an agent activity (thought/response/error) to a session. Skips if
        there's no write credential (an OAuth app token is required)."""
        headers = self._write_headers()
        if not headers:
            return
        try:
            await client.post(LINEAR_API, headers=headers, json={
                "query": AGENT_ACTIVITY_MUTATION,
                "variables": {"input": {"agentSessionId": session_id,
                                        "content": {"type": activity_type, "body": body}}},
            })
        except Exception:
            logger.exception("failed to post agent activity to session %s", session_id)

    async def _handle_agent_session(self, client: httpx.AsyncClient, payload: dict) -> None:
        parsed = self._agent_session_issue(payload)
        if not parsed:
            return
        session_id, issue = parsed
        task = await self.db.create_task_if_new(
            title=f"{issue['identifier']}: {issue['title']}",
            description=f"{issue['title']}\n\n{issue.get('description') or ''}\n\n"
                        f"Linear issue: {issue['url']}",
            source="linear",
            source_ref={"issue_id": issue["id"], "identifier": issue["identifier"],
                        "url": issue["url"], "agent_session_id": session_id},
            dedup_key=f"linear:agent:{session_id}",
        )
        if task is None:
            return  # already picked up this session
        # ack within Linear's 10s window so the session isn't marked unresponsive
        await self._agent_activity(client, session_id, "thought",
                                   f"Picked this up as task `{task.id}` — working on it.")
        logger.info("linear agent session %s -> task %s", session_id, task.id)

    def _webhook_issue(self, payload: dict) -> dict | None:
        """Return the normalized issue dict if this `Issue` webhook should be
        ingested, else None. In assignee mode the match needs `_my_user_id` to
        already be resolved (handle_webhook resolves it first)."""
        if payload.get("type") != "Issue":
            return None
        data = payload.get("data") or {}
        if not data.get("id"):
            return None
        if self._by_assignee:
            if not self._my_user_id or data.get("assigneeId") != self._my_user_id:
                return None
        else:
            labels = [lbl.get("name") for lbl in data.get("labels", [])
                      if isinstance(lbl, dict)]
            if self.settings.linear_label not in labels:
                return None
        return {"id": data["id"], "identifier": data.get("identifier", "?"),
                "title": data.get("title", "task"),
                "description": data.get("description"),
                "url": data.get("url", "")}

    async def handle_webhook(self, payload: dict) -> None:
        """Instant pickup path for `Issue` webhooks: the configured label must be
        present (label mode), or the issue must be assigned to the agent's own
        user (assignee mode)."""
        async with httpx.AsyncClient(timeout=30) as client:
            # delegation to the OAuth app → agent session (seatless, primary path)
            if payload.get("type") == "AgentSessionEvent":
                await self._handle_agent_session(client, payload)
                return
            # legacy: plain Issue webhook matched by label/assignee
            if self._by_assignee and not self._my_user_id:
                await self._resolve_my_id(client)
            issue = self._webhook_issue(payload)
            if issue:
                await self.ingest_issue(client, issue)

    async def notify(self, task: Task, client: httpx.AsyncClient | None = None) -> None:
        if task.source != "linear" or not self._write_headers():
            return  # not a linear task, or no write-back credential
        sref = task.source_ref or {}
        session_id = sref.get("agent_session_id")
        issue_id = sref.get("issue_id")
        if not session_id and not issue_id:
            return
        owns = client is None
        if owns:
            client = httpx.AsyncClient(timeout=30)
        try:
            if session_id:
                # agent-session task → respond as an activity (error if it failed)
                kind = "error" if task.status == "failed" else "response"
                await self._agent_activity(client, session_id, kind, task_summary(task))
            else:
                await self._comment(client, issue_id, task_summary(task))
        finally:
            if owns:
                await client.aclose()
