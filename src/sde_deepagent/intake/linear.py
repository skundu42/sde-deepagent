"""Linear intake. Polls the Linear GraphQL API for unstarted issues carrying the
configured label (default: "agent") — no public URL required. A webhook endpoint
is also exposed at /webhooks/linear for instant pickup if you can receive
webhooks. Progress and the final PR link are posted as issue comments."""

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
query AgentIssues($label: String!) {
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

COMMENT_MUTATION = """
mutation CreateComment($issueId: String!, $body: String!) {
  commentCreate(input: { issueId: $issueId, body: $body }) { success }
}
"""


class LinearIntake:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._poll_loop(), name="linear-intake")
        logger.info("linear intake started (polling label '%s')", self.settings.linear_label)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": self.settings.linear_api_key or "",
                "Content-Type": "application/json"}

    async def _poll_loop(self) -> None:
        async with httpx.AsyncClient(timeout=30) as client:
            while True:
                try:
                    resp = await client.post(
                        LINEAR_API, headers=self._headers(),
                        json={"query": ISSUES_QUERY,
                              "variables": {"label": self.settings.linear_label}},
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
        try:
            await client.post(LINEAR_API, headers=self._headers(),
                              json={"query": COMMENT_MUTATION,
                                    "variables": {"issueId": issue_id, "body": body}})
        except Exception:
            logger.exception("failed to comment on linear issue %s", issue_id)

    async def handle_webhook(self, payload: dict) -> None:
        """Instant pickup path for `Issue` webhooks (label must match)."""
        data = payload.get("data") or {}
        labels = [lbl.get("name") for lbl in data.get("labels", []) if isinstance(lbl, dict)]
        if payload.get("type") == "Issue" and self.settings.linear_label in labels:
            issue = {"id": data.get("id"), "identifier": data.get("identifier", "?"),
                     "title": data.get("title", "task"),
                     "description": data.get("description"),
                     "url": data.get("url", "")}
            if issue["id"]:
                async with httpx.AsyncClient(timeout=30) as client:
                    await self.ingest_issue(client, issue)

    async def notify(self, task: Task) -> None:
        if task.source != "linear":
            return
        issue_id = task.source_ref.get("issue_id")
        if not issue_id or not self.settings.linear_api_key:
            return
        async with httpx.AsyncClient(timeout=30) as client:
            await self._comment(client, issue_id, task_summary(task))
