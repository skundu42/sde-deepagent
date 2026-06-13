"""Closes the review loop: polls open PRs that the agent created for new human
review feedback (reviews, inline comments, conversation comments) from owners,
members, or collaborators and queues a revision task that continues on the same
branch. Requires GITHUB_TOKEN and GITHUB_REVIEW_POLLING=true."""

from __future__ import annotations

import asyncio
import logging
import re

import httpx

from ..db import Database
from ..gitops import GitError, github_api_for_host, trusted_github_hosts
from ..settings import Settings

logger = logging.getLogger(__name__)

PR_URL_RE = re.compile(r"https?://([\w.-]+)/([\w.-]+)/([\w.-]+)/pull/(\d+)")
OPEN_STATUSES = {"queued", "running", "awaiting_approval"}
TRUSTED_AUTHOR_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


def parse_pr_url(url: str) -> tuple[str, str, str, str] | None:
    m = PR_URL_RE.match(url or "")
    if not m:
        return None
    host, owner, repo, number = m.groups()
    return host, owner, repo, number


class GithubReviewIntake:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self._task: asyncio.Task | None = None
        self._seen_since: dict[str, str] = {}  # pr_url -> ISO floor for "new" comments

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="github-review-intake")
        logger.info("github review polling started (every %ss)",
                    self.settings.github_review_poll_seconds)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def _api_base(self, host: str) -> str:
        if host.lower() not in trusted_github_hosts(self.settings):
            raise GitError(f"refusing GitHub review token access to untrusted host '{host}'")
        return github_api_for_host(self.settings, host)

    async def _loop(self) -> None:
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("github review poll error")
            await asyncio.sleep(self.settings.github_review_poll_seconds)

    async def _open_revision_exists(self, parent_id: str) -> bool:
        for t in await self.db.list_tasks(limit=500):
            if t.parent_id == parent_id and t.status in OPEN_STATUSES:
                return True
        return False

    async def _poll_once(self) -> None:
        tasks = await self.db.list_tasks(status="completed", limit=200)
        candidates = [t for t in tasks if t.pr_url and parse_pr_url(t.pr_url)]
        if not candidates:
            return
        async with httpx.AsyncClient(timeout=30) as client:
            for task in candidates:
                try:
                    await self._check_task(client, task)
                except Exception:
                    logger.exception("review check failed for task %s", task.id)

    async def _check_task(self, client: httpx.AsyncClient, task) -> None:
        host, owner, repo, number = parse_pr_url(task.pr_url)
        base = self._api_base(host)
        headers = {"Authorization": f"Bearer {self.settings.github_token}",
                   "Accept": "application/vnd.github+json"}
        # first time we see this PR, only count feedback newer than task completion
        floor = self._seen_since.get(task.pr_url) or _iso(task.finished_at)

        comments: list[tuple[str, str, str]] = []  # (created_at, author, body)
        for path in (f"/repos/{owner}/{repo}/pulls/{number}/reviews",
                     f"/repos/{owner}/{repo}/pulls/{number}/comments",
                     f"/repos/{owner}/{repo}/issues/{number}/comments"):
            resp = await client.get(f"{base}{path}", headers=headers)
            if resp.status_code >= 300:
                continue
            for c in resp.json():
                ts = c.get("submitted_at") or c.get("created_at") or ""
                author = (c.get("user") or {}).get("login", "?")
                body = (c.get("body") or "").strip()
                association = c.get("author_association")
                if (not body or author.endswith("[bot]")
                        or association not in TRUSTED_AUTHOR_ASSOCIATIONS):
                    continue
                if ts and ts > floor:
                    loc = f" ({c['path']}:{c.get('line', '?')})" if c.get("path") else ""
                    comments.append((ts, f"{author}{loc}", body))

        newest = max((ts for ts, _, _ in comments), default=floor)
        self._seen_since[task.pr_url] = max(newest, floor)
        if not comments:
            return
        if await self._open_revision_exists(task.id):
            return  # already revising; wait for it to finish before queuing more

        body = "\n".join(f"- {author}: {text}" for _, author, text in sorted(comments))
        rev = await self.db.create_task(
            title=f"Address PR review: {task.title}"[:200],
            description=f"New review feedback on the pull request:\n\n{body}",
            repo=task.repo, source="github-review", parent_id=task.id,
            source_ref={"pr_url": task.pr_url},
        )
        logger.info("queued revision %s for PR review on task %s", rev.id, task.id)


def _iso(epoch: float | None) -> str:
    import datetime as dt
    if not epoch:
        return "1970-01-01T00:00:00Z"
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
